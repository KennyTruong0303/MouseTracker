from __future__ import annotations

import io
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from rfid_tracking.recording import encoder
from rfid_tracking.recording.camera import (
    CameraDevice,
    choose_dshow_input_format,
    discover_dshow_camera,
    parse_dshow_video_devices,
    resolve_backend,
)
from rfid_tracking.recording.ffmpeg_recorder import (
    RecorderState,
    RecorderStats,
    SENTINEL,
    SegmentState,
    SegmentWriter,
    capture_command,
    capture_thread,
    put_packet,
    synthetic_capture_thread,
    writer_thread,
)
from rfid_tracking.recording.timestamps import (
    EXPECTED_INTERVAL_MS,
    FramePacket,
    count_csv_rows,
    iter_csv_rows,
    matching_timestamp_name,
    segment_paths,
    timestamp_row,
    TimestampCsvWriter,
)
from rfid_tracking.recording.validation import recover_partial_segment


class FakeStdin(io.BytesIO):
    def close(self) -> None:
        self.flush()


class FakePopen:
    instances: list["FakePopen"] = []

    def __init__(self, command, stdin=None, stderr=None, creationflags=0):
        self.command = command
        self.stdin = FakeStdin()
        self.stderr = io.BytesIO()
        self.returncode = 0
        self.output_path = Path(command[-1])
        FakePopen.instances.append(self)

    def wait(self, timeout=None):
        self.output_path.write_bytes(b"fake hevc payload")
        return self.returncode


class FailingRecoverablePopen(FakePopen):
    def __init__(self, command, stdin=None, stderr=None, creationflags=0):
        super().__init__(command, stdin=stdin, stderr=stderr, creationflags=creationflags)
        self.returncode = 1
        self.stderr = io.BytesIO(b"encoder exited after Ctrl+C but file is readable")


class FakeFullQueue:
    def put(self, _item, timeout=None):
        raise queue.Full

    def qsize(self):
        return 0


class ShutdownDuringCaptureRead:
    def __init__(self, state: RecorderState):
        self.state = state

    def read(self, _size: int) -> bytes:
        self.state.request_shutdown()
        return b""


class CaptureExitPopen:
    def __init__(self, command, stdout=None, creationflags=0):
        self.command = command
        self.stdout = None
        self.creationflags = creationflags
        self.returncode = 0
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class RecordingTimestampTests(unittest.TestCase):
    def test_filename_formatting_and_matching_names(self):
        paths = segment_paths(Path("/tmp/out"), 1_784_087_000_000_000_000)
        self.assertRegex(paths.final_video.name, r"record_\d{8}_\d{6}\.mp4")
        self.assertEqual(
            matching_timestamp_name(paths.final_video.name),
            paths.final_csv.name,
        )

    def test_timestamp_row_interval_and_gap_detection(self):
        packet = FramePacket(b"x", 3, 1_700_000_000_000_000_000, 1_100_000_000)
        row = timestamp_row(
            packet,
            segment_filename="record_20260715_144320.mp4",
            segment_frame_index=3,
            recording_start_monotonic_ns=1_000_000_000,
            segment_start_monotonic_ns=1_000_000_000,
            previous_frame_monotonic_ns=1_000_000_000,
            expected_interval_ms=EXPECTED_INTERVAL_MS,
            gap_threshold_ms=50.0,
        )
        self.assertEqual(row["global_frame_index"], 3)
        self.assertEqual(row["segment_frame_index"], 3)
        self.assertAlmostEqual(row["inter_frame_interval_ms"], 100.0)
        self.assertGreater(row["frame_gap_ms"], 60.0)
        self.assertTrue(row["gap_flag"])


class RecordingWriterTests(unittest.TestCase):
    def setUp(self):
        FakePopen.instances = []

    def packet(self, index: int, mono_ns: int, wall_ns: int | None = None) -> FramePacket:
        return FramePacket(
            data=b"\0\0\0",
            global_frame_index=index,
            wall_time_unix_ns=wall_ns if wall_ns is not None else 1_784_087_000_000_000_000 + mono_ns,
            monotonic_ns=mono_ns,
        )

    def test_segment_rollover_has_no_duplicate_boundary_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = RecorderStats()
            writer = SegmentWriter(
                output_dir=Path(tmp),
                width=1,
                height=1,
                fps=1.0,
                segment_seconds=2.0,
                encoder="libx265",
                gap_threshold_ms=1500.0,
                validate=False,
            )
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", FakePopen):
                writer.write_packet(self.packet(0, 0), stats)
                writer.write_packet(self.packet(1, 1_000_000_000), stats)
                writer.write_packet(self.packet(2, 2_000_000_000), stats)
                writer.finalize_current(stats)

            csvs = sorted(Path(tmp).glob("*_timestamps.csv"))
            self.assertEqual(len(csvs), 2)
            rows = [row for csv in csvs for row in iter_csv_rows(csv)]
            self.assertEqual([int(row["global_frame_index"]) for row in rows], [0, 1, 2])
            self.assertEqual([int(row["segment_frame_index"]) for row in rows], [0, 1, 0])

    def test_csv_row_count_matches_synthetic_written_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = RecorderStats()
            writer = SegmentWriter(
                output_dir=Path(tmp),
                width=1,
                height=1,
                fps=2.0,
                segment_seconds=5.0,
                encoder="libx265",
                gap_threshold_ms=800.0,
                validate=False,
            )
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", FakePopen):
                for index in range(6):
                    writer.write_packet(self.packet(index, index * 500_000_000), stats)
                writer.finalize_current(stats)
            csv_path = next(Path(tmp).glob("*_timestamps.csv"))
            self.assertEqual(count_csv_rows(csv_path), 6)
            self.assertEqual(len(FakePopen.instances[0].stdin.getvalue()), 18)

    def test_graceful_synthetic_shutdown(self):
        packets: queue.Queue = queue.Queue(maxsize=20)
        state = RecorderState(
            stop_event=mock.Mock(),
            failure_event=mock.Mock(),
            stats=RecorderStats(),
        )
        real_stop = mock.Mock()
        real_stop.is_set.side_effect = [False, False, False, False]
        state.stop_event = real_stop
        synthetic_capture_thread(
            packets=packets,
            state=state,
            width=1,
            height=1,
            fps=2.0,
            duration_seconds=1.5,
        )
        self.assertGreaterEqual(packets.qsize(), 4)

    def test_queue_full_handling_sets_failure(self):
        state = RecorderState(
            stop_event=mock.Mock(),
            failure_event=mock.Mock(),
            stats=RecorderStats(),
        )
        packet = self.packet(9, 0)
        self.assertFalse(put_packet(FakeFullQueue(), packet, state))
        self.assertEqual(state.stats.queue_full_events, 1)
        state.failure_event.set.assert_called_once()

    def test_writer_thread_finalizes_on_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            packets: queue.Queue = queue.Queue()
            state = RecorderState(
                stop_event=mock.Mock(),
                failure_event=mock.Mock(),
                stats=RecorderStats(),
            )
            writer = SegmentWriter(
                output_dir=Path(tmp),
                width=1,
                height=1,
                fps=1.0,
                segment_seconds=10.0,
                encoder="libx265",
                gap_threshold_ms=1500.0,
                validate=False,
            )
            packets.put(self.packet(0, 0))
            packets.put(SENTINEL)
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", FakePopen):
                writer_thread(packets=packets, state=state, writer=writer)
            self.assertEqual(len(list(Path(tmp).glob("*.mp4"))), 1)
            self.assertEqual(len(list(Path(tmp).glob("*_timestamps.csv"))), 1)
            state.failure_event.set.assert_not_called()

    def test_repeated_finalize_current_calls_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = RecorderStats()
            writer = SegmentWriter(
                output_dir=Path(tmp),
                width=1,
                height=1,
                fps=1.0,
                segment_seconds=10.0,
                encoder="libx265",
                gap_threshold_ms=1500.0,
                validate=False,
            )
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", FakePopen):
                writer.write_packet(self.packet(0, 0), stats)
                writer.finalize_current(stats)
                writer.finalize_current(stats)
            self.assertEqual(writer.state, SegmentState.FINALIZED)
            self.assertEqual(len(stats.segments), 1)

    def test_ctrl_c_style_sentinel_finalizes_partial_segment_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            packets: queue.Queue = queue.Queue()
            state = RecorderState(
                stop_event=mock.Mock(),
                failure_event=mock.Mock(),
                stats=RecorderStats(),
            )
            writer = SegmentWriter(
                output_dir=Path(tmp),
                width=1,
                height=1,
                fps=30.0,
                segment_seconds=60.0,
                encoder="libx265",
                gap_threshold_ms=50.0,
                validate=False,
            )
            packets.put(self.packet(0, 0))
            packets.put(self.packet(1, 33_333_333))
            packets.put(SENTINEL)
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", FakePopen):
                writer_thread(packets=packets, state=state, writer=writer)
            self.assertEqual(writer.state, SegmentState.FINALIZED)
            self.assertEqual(len(list(Path(tmp).glob("*.part.*"))), 0)
            state.failure_event.set.assert_not_called()

    def test_shutdown_request_disables_reconnect_after_capture_failure(self):
        packets: queue.Queue = queue.Queue()
        state = RecorderState(
            stop_event=threading.Event(),
            failure_event=mock.Mock(),
            stats=RecorderStats(),
        )
        camera = CameraDevice(
            backend="dshow",
            path="DECXIN CAMERA",
            name="DECXIN CAMERA",
            formats_text="",
            preferred_input_format="mjpeg",
        )

        class ShutdownCapturePopen:
            def __init__(self, command, stdout=None, creationflags=0):
                self.command = command
                self.stdout = ShutdownDuringCaptureRead(state)
                self.creationflags = creationflags

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", ShutdownCapturePopen):
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.discover_camera") as discover:
                with self.assertLogs("rfid_tracking.recording", level="INFO") as logs:
                    capture_thread(
                        packets=packets,
                        state=state,
                        requested_device="DECXIN CAMERA",
                        requested_backend="dshow",
                        camera=camera,
                        width=1,
                        height=1,
                        fps=30.0,
                    )
        discover.assert_not_called()
        self.assertIn("Shutdown requested, disabling capture recovery", "\n".join(logs.output))
        queued = []
        while not packets.empty():
            queued.append(packets.get_nowait())
        self.assertGreaterEqual(len(queued), 2)

    def test_successful_eof_based_ffmpeg_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = RecorderStats()
            writer = SegmentWriter(
                output_dir=Path(tmp),
                width=1,
                height=1,
                fps=30.0,
                segment_seconds=60.0,
                encoder="libx265",
                gap_threshold_ms=50.0,
                validate=False,
            )
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", FakePopen):
                writer.write_packet(self.packet(0, 0), stats)
                writer.finalize_current(stats)
            self.assertEqual(writer.state, SegmentState.FINALIZED)
            self.assertEqual(len(stats.segments), 1)

    def test_nonzero_ffmpeg_return_code_recovers_valid_partial_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = RecorderStats()
            writer = SegmentWriter(
                output_dir=Path(tmp),
                width=1,
                height=1,
                fps=30.0,
                segment_seconds=60.0,
                encoder="libx265",
                gap_threshold_ms=50.0,
                validate=True,
            )
            final_video = Path(tmp) / "record_20260715_144320.mp4"
            final_csv = Path(tmp) / "record_20260715_144320_timestamps.csv"
            recovery = mock.Mock(ok=True, final_video=final_video, final_csv=final_csv)
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", FailingRecoverablePopen):
                with mock.patch("rfid_tracking.recording.ffmpeg_recorder.recover_partial_segment", return_value=recovery):
                    writer.write_packet(self.packet(0, 0), stats)
                    writer.finalize_current(stats)
            self.assertEqual(writer.state, SegmentState.FINALIZED)
            self.assertEqual(stats.segments, [final_video])

    def test_finalize_failure_is_not_retried_by_writer_exception_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            packets: queue.Queue = queue.Queue()
            state = RecorderState(
                stop_event=mock.Mock(),
                failure_event=mock.Mock(),
                stats=RecorderStats(),
            )
            writer = SegmentWriter(
                output_dir=Path(tmp),
                width=1,
                height=1,
                fps=30.0,
                segment_seconds=60.0,
                encoder="libx265",
                gap_threshold_ms=50.0,
                validate=True,
            )
            recovery = mock.Mock(ok=False, validation=mock.Mock(errors=["mismatched counts"]))
            packets.put(self.packet(0, 0))
            packets.put(SENTINEL)
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", FailingRecoverablePopen):
                with mock.patch("rfid_tracking.recording.ffmpeg_recorder.recover_partial_segment", return_value=recovery) as recover:
                    writer_thread(packets=packets, state=state, writer=writer)
            self.assertEqual(writer.state, SegmentState.FAILED)
            self.assertEqual(recover.call_count, 1)
            state.failure_event.set.assert_called_once()

    def test_repeated_csv_close_and_flush_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_writer = TimestampCsvWriter(Path(tmp) / "timestamps.csv")
            csv_writer.close()
            csv_writer.close()
            csv_writer.flush()


class PartialRecoveryTests(unittest.TestCase):
    def _make_partial_pair(self, tmp: str, rows: int = 3) -> tuple[Path, Path]:
        root = Path(tmp)
        part_video = root / "record_20260715_144320.part.mp4"
        part_csv = root / "record_20260715_144320_timestamps.part.csv"
        part_video.write_bytes(b"fake mp4")
        with part_csv.open("w", encoding="utf-8", newline="") as handle:
            handle.write(
                "segment_filename,global_frame_index,segment_frame_index,wall_time_iso8601,"
                "wall_time_unix_ns,monotonic_ns,elapsed_from_recording_start_s,"
                "elapsed_from_segment_start_s,inter_frame_interval_ms,expected_interval_ms,"
                "frame_gap_ms,gap_flag\n"
            )
            for index in range(rows):
                handle.write(
                    f"record_20260715_144320.mp4,{index},{index},2026-07-15T14:43:20+08:00,"
                    f"{index},{index},0,0,,33.333333,,False\n"
                )
        return part_video, part_csv

    def _ffprobe_runner(self, frame_count: int, codec: str = "hevc", width: int = 1280, height: int = 720):
        def runner(command, check=False, text=True, capture_output=True, timeout=30):
            payload = {
                "streams": [
                    {
                        "codec_name": codec,
                        "width": width,
                        "height": height,
                        "avg_frame_rate": "30/1",
                        "nb_read_frames": str(frame_count),
                    }
                ]
            }
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        return runner

    def test_recover_partial_segment_promotes_valid_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            part_video, part_csv = self._make_partial_pair(tmp, rows=3)
            result = recover_partial_segment(
                part_video,
                width=1280,
                height=720,
                fps=30.0,
                runner=self._ffprobe_runner(frame_count=3),
            )
            self.assertTrue(result.ok)
            self.assertTrue(result.final_video.exists())
            self.assertTrue(result.final_csv.exists())
            self.assertFalse(part_video.exists())
            self.assertFalse(part_csv.exists())

    def test_recover_partial_segment_rejects_mismatched_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            part_video, part_csv = self._make_partial_pair(tmp, rows=3)
            result = recover_partial_segment(
                part_video,
                width=1280,
                height=720,
                fps=30.0,
                runner=self._ffprobe_runner(frame_count=2),
            )
            self.assertFalse(result.ok)
            self.assertTrue(part_video.exists())
            self.assertTrue(part_csv.exists())
            self.assertTrue(part_video.with_suffix(part_video.suffix + ".invalid").exists())


class EncoderProbeTests(unittest.TestCase):
    def test_encoder_probe_fallback_with_mocked_subprocess(self):
        calls: list[list[str]] = []

        def runner(command, check=False, text=True, capture_output=True, timeout=20.0):
            calls.append(command)
            if "hevc_nvenc" in command:
                return mock.Mock(returncode=1, stdout="", stderr="no gpu")
            Path(command[-1]).write_bytes(b"ok")
            return mock.Mock(returncode=0, stdout="", stderr="")

        selected = encoder.select_hevc_encoder("auto", runner=runner)
        self.assertEqual(selected.name, "hevc_qsv")
        self.assertEqual([result.encoder for result in selected.probe_results], ["hevc_nvenc", "hevc_qsv"])
        self.assertEqual(len(calls), 2)

    def test_windows_encoder_order_skips_vaapi(self):
        attempted: list[str] = []

        def runner(command, check=False, text=True, capture_output=True, timeout=20.0):
            for name in ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"):
                if name in command:
                    attempted.append(name)
                    if name == "libx265":
                        Path(command[-1]).write_bytes(b"ok")
                        return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="unavailable")

        selected = encoder.select_hevc_encoder("auto", backend="dshow", runner=runner)
        self.assertEqual(selected.name, "libx265")
        self.assertEqual(attempted, ["hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"])


class DirectShowBackendTests(unittest.TestCase):
    def test_parse_dshow_video_devices(self):
        listing = """
[dshow @ 000001] DirectShow video devices (some may be both video and audio devices)
[dshow @ 000001]  "USB Camera"
[dshow @ 000001]     Alternative name "@device_pnp_\\\\?\\usb#vid"
[dshow @ 000001]  "Integrated Webcam"
[dshow @ 000001] DirectShow audio devices
[dshow @ 000001]  "Microphone"
"""
        self.assertEqual(parse_dshow_video_devices(listing), ["USB Camera", "Integrated Webcam"])

    def test_choose_dshow_input_format_prefers_mjpeg_exact_mode(self):
        options = """
[dshow @ 000001]   pixel_format=yuyv422  min s=1280x720 fps=30 max s=1280x720 fps=30
[dshow @ 000001]   vcodec=mjpeg  min s=1280x720 fps=30 max s=1280x720 fps=30
"""
        self.assertEqual(choose_dshow_input_format(options, 1280, 720, 30.0), "mjpeg")
        self.assertIsNone(choose_dshow_input_format(options, 1920, 1080, 30.0))

    def test_dshow_mjpeg_range_supports_requested_fps_inside_bounds(self):
        options = """
[dshow @ 000001]   vcodec=mjpeg min s=1280x720 fps=10 max s=1280x720 fps=120
"""
        self.assertEqual(choose_dshow_input_format(options, 1280, 720, 30.0), "mjpeg")
        self.assertIsNone(choose_dshow_input_format(options, 1280, 720, 121.0))
        self.assertIsNone(choose_dshow_input_format(options, 640, 480, 30.0))

    def test_discover_dshow_camera_accepts_decxin_mjpeg_range(self):
        def runner(command, check=False, text=True, capture_output=True, timeout=10.0):
            if "-list_devices" in command:
                return mock.Mock(
                    returncode=1,
                    stdout="",
                    stderr='[dshow @ 000001] DirectShow video devices\n[dshow @ 000001]  "DECXIN CAMERA"\n',
                )
            if "-list_options" in command:
                self.assertIn("video=DECXIN CAMERA", command)
                return mock.Mock(
                    returncode=1,
                    stdout="",
                    stderr="[dshow @ 000001]   vcodec=mjpeg min s=1280x720 fps=10 max s=1280x720 fps=120\n",
                )
            raise AssertionError(f"Unexpected command: {command}")

        camera = discover_dshow_camera(
            device="DECXIN CAMERA",
            width=1280,
            height=720,
            fps=30.0,
            runner=runner,
        )
        self.assertEqual(camera.name, "DECXIN CAMERA")
        self.assertEqual(camera.preferred_input_format, "mjpeg")
        self.assertEqual(camera.width, 1280)
        self.assertEqual(camera.height, 720)
        self.assertEqual(camera.fps, 30.0)

    def test_resolve_backend_auto_uses_native_platform(self):
        expected = "dshow" if __import__("os").name == "nt" else "v4l2"
        self.assertEqual(resolve_backend("auto"), expected)

    def test_dshow_capture_command_uses_directshow_camera_name(self):
        camera = CameraDevice(
            backend="dshow",
            path="USB Camera",
            name="USB Camera",
            formats_text="",
            preferred_input_format="mjpeg",
        )
        command = capture_command(camera, 1280, 720, 30.0)
        self.assertEqual(
            command,
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-f",
                "dshow",
                "-vcodec",
                "mjpeg",
                "-video_size",
                "1280x720",
                "-framerate",
                "30.0",
                "-i",
                "video=USB Camera",
                "-an",
                "-pix_fmt",
                "bgr24",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
        )
        self.assertNotIn("-input_format", command)
        self.assertNotIn('"USB Camera"', command)


if __name__ == "__main__":
    unittest.main()
