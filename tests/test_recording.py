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
    DECXIN_CAMERA_NAME,
    DECXIN_HARDWARE_IDS,
    DECXIN_PARENT_DEVICE,
    DECXIN_USB_PID,
    DECXIN_USB_REV,
    DECXIN_USB_VID,
    choose_dshow_input_format,
    decxin_device_info,
    discover_dshow_camera,
    dshow_alternative_to_device_instance_path,
    parse_dshow_modes,
    parse_dshow_video_device_infos,
    parse_dshow_video_devices,
    resolve_backend,
)
from rfid_tracking.recording.ffmpeg_recorder import (
    DEFAULT_QUEUE_SIZE,
    RecorderState,
    RecorderStats,
    SENTINEL,
    SegmentState,
    build_parser,
    SegmentWriter,
    capture_command,
    capture_thread,
    create_clock_anchor,
    default_stop_file,
    frame_byte_count,
    high_resolution_monotonic_ns,
    keyboard_stop_thread,
    main,
    put_packet,
    request_stop_file,
    resolve_timestamp_source,
    stop_file_watcher_thread,
    synthetic_capture_thread,
    wall_time_from_anchor,
    writer_thread,
)
from rfid_tracking.recording.service import RecorderConfig
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


class RecorderDefaultTests(unittest.TestCase):
    def test_default_queue_size_has_csds_headroom(self):
        self.assertEqual(DEFAULT_QUEUE_SIZE, 900)
        self.assertEqual(build_parser().parse_args([]).queue_size, DEFAULT_QUEUE_SIZE)
        self.assertEqual(RecorderConfig().queue_size, DEFAULT_QUEUE_SIZE)


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
    def test_high_resolution_monotonic_uses_perf_counter(self):
        with mock.patch("rfid_tracking.recording.ffmpeg_recorder.time.perf_counter_ns", return_value=123456):
            self.assertEqual(high_resolution_monotonic_ns(), 123456)

    def test_wall_time_anchor_uses_high_resolution_monotonic_delta(self):
        with mock.patch("rfid_tracking.recording.ffmpeg_recorder.time.time_ns", return_value=1_000_000_000):
            with mock.patch(
                "rfid_tracking.recording.ffmpeg_recorder.high_resolution_monotonic_ns",
                return_value=5_000_000_000,
            ):
                anchor = create_clock_anchor()
        self.assertEqual(wall_time_from_anchor(anchor, 5_033_333_333), 1_033_333_333)

    def test_timestamp_row_includes_source_and_arrival_diagnostics(self):
        packet = FramePacket(
            b"x",
            1,
            1_700_000_000_033_333_333,
            2_000_033_333,
            capture_arrival_wall_time_unix_ns=1_700_000_000_040_000_000,
            capture_arrival_monotonic_ns=2_000_040_000,
            timestamp_source="cadence",
        )
        row = timestamp_row(
            packet,
            segment_filename="record_20260721_191201.mp4",
            segment_frame_index=1,
            recording_start_monotonic_ns=2_000_000_000,
            segment_start_monotonic_ns=2_000_000_000,
            previous_frame_monotonic_ns=2_000_000_000,
            expected_interval_ms=1000.0 / 30.0,
            gap_threshold_ms=50.0,
        )
        self.assertEqual(row["timestamp_source"], "cadence")
        self.assertEqual(row["capture_arrival_monotonic_ns"], 2_000_040_000)
        self.assertAlmostEqual(row["capture_arrival_offset_ms"], 0.006667)

    def test_timestamp_source_auto_uses_cadence_for_directshow(self):
        camera = CameraDevice("dshow", "DECXIN CAMERA", "DECXIN CAMERA", "", "mjpeg")
        self.assertEqual(resolve_timestamp_source("auto", camera=camera, synthetic=False), "cadence")
        self.assertEqual(resolve_timestamp_source("auto", camera=None, synthetic=True), "cadence")
        self.assertEqual(resolve_timestamp_source("arrival", camera=camera, synthetic=False), "arrival")

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

    def test_keyboard_q_requests_shutdown(self):
        state = RecorderState(threading.Event(), mock.Mock(), RecorderStats())
        keyboard_stop_thread(state, io.StringIO("q\n"))
        self.assertTrue(state.shutdown_requested.is_set())
        self.assertEqual(state.shutdown_source, "keyboard q command")

    def test_keyboard_stop_requests_shutdown(self):
        state = RecorderState(threading.Event(), mock.Mock(), RecorderStats())
        keyboard_stop_thread(state, io.StringIO("stop\n"))
        self.assertTrue(state.shutdown_requested.is_set())
        self.assertEqual(state.shutdown_source, "keyboard stop command")

    def test_stop_file_requests_shutdown_and_marks_file_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RecorderState(threading.Event(), mock.Mock(), RecorderStats())
            stop_file = Path(tmp) / "STOP_RECORDING"
            stop_file.write_text("stop\n", encoding="utf-8")
            stop_file_watcher_thread(state, stop_file, interval_s=0.001)
            self.assertTrue(state.shutdown_requested.is_set())
            self.assertEqual(state.shutdown_source, f"stop file {stop_file}")
            self.assertFalse(stop_file.exists())
            self.assertTrue((Path(tmp) / "STOP_RECORDING.handled").exists())

    def test_request_stop_creates_default_stop_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            path = request_stop_file(output_dir)
            self.assertEqual(path, default_stop_file(output_dir))
            self.assertEqual(path.read_text(encoding="utf-8"), "stop\n")

    def test_main_request_stop_uses_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = main(["--request-stop", "--output-dir", tmp])
            self.assertEqual(rc, 0)
            self.assertTrue((Path(tmp) / "STOP_RECORDING").exists())

    def test_multiple_stop_requests_are_idempotent(self):
        state = RecorderState(threading.Event(), mock.Mock(), RecorderStats())
        self.assertTrue(state.request_shutdown("first"))
        self.assertFalse(state.request_shutdown("second"))
        self.assertEqual(state.shutdown_source, "first")

    def test_no_reconnect_after_keyboard_shutdown(self):
        packets: queue.Queue = queue.Queue()
        state = RecorderState(threading.Event(), mock.Mock(), RecorderStats())
        camera = CameraDevice("dshow", "DECXIN CAMERA", "DECXIN CAMERA", "", "mjpeg")

        class KeyboardShutdownPopen:
            def __init__(self, command, stdout=None, creationflags=0):
                self.stdout = ShutdownDuringCaptureRead(state)

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        with mock.patch("rfid_tracking.recording.ffmpeg_recorder.subprocess.Popen", KeyboardShutdownPopen):
            with mock.patch("rfid_tracking.recording.ffmpeg_recorder.discover_camera") as discover:
                keyboard_stop_thread(state, io.StringIO("q\n"))
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

    def test_no_reconnect_after_stop_file_shutdown(self):
        packets: queue.Queue = queue.Queue()
        state = RecorderState(threading.Event(), mock.Mock(), RecorderStats())
        camera = CameraDevice("dshow", "DECXIN CAMERA", "DECXIN CAMERA", "", "mjpeg")
        with tempfile.TemporaryDirectory() as tmp:
            stop_file = Path(tmp) / "STOP_RECORDING"
            stop_file.write_text("stop\n", encoding="utf-8")
            stop_file_watcher_thread(state, stop_file, interval_s=0.001)
        with mock.patch("rfid_tracking.recording.ffmpeg_recorder.discover_camera") as discover:
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
    DECXIN_LISTING = r"""
[dshow @ 000001] DirectShow video devices (some may be both video and audio devices)
[dshow @ 000001]  "Smart Connect Camera"
[dshow @ 000001]     Alternative name "@device_pnp_\\?\root#camera#0000#{e5323777-f976-4f5b-9b55-b94699c46e44}\global"
[dshow @ 000001]  "DECXIN CAMERA"
[dshow @ 000001]     Alternative name "@device_pnp_\\?\usb#vid_1bcf&pid_2cd1&mi_00#6&1bd18552&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 000001]  "Integrated Camera"
[dshow @ 000001]     Alternative name "@device_pnp_\\?\usb#vid_13d3&pid_56ff&mi_00#7&2897bf3a&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 000001] DirectShow audio devices
[dshow @ 000001]  "Microphone"
"""

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

    def test_parse_dshow_video_device_infos_extracts_decxin_usb_identity(self):
        devices = parse_dshow_video_device_infos(self.DECXIN_LISTING)
        decxin = decxin_device_info(devices)
        self.assertIsNotNone(decxin)
        assert decxin is not None
        self.assertEqual(decxin.name, DECXIN_CAMERA_NAME)
        self.assertEqual(decxin.usb_vid, DECXIN_USB_VID)
        self.assertEqual(decxin.usb_pid, DECXIN_USB_PID)
        self.assertEqual(decxin.usb_interface, "00")
        self.assertEqual(
            decxin.device_instance_path,
            r"USB\VID_1BCF&PID_2CD1&MI_00\6&1BD18552&0&0000",
        )
        self.assertIn("vid_1bcf&pid_2cd1", decxin.alternative_name or "")
        self.assertTrue(decxin.is_decxin)

    def test_dshow_alternative_name_converts_to_device_instance_path(self):
        alternative = (
            r"@device_pnp_\\?\usb#vid_1bcf&pid_2cd1&mi_00#6&1bd18552&0&0000"
            r"#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
        )
        self.assertEqual(
            dshow_alternative_to_device_instance_path(alternative),
            r"USB\VID_1BCF&PID_2CD1&MI_00\6&1BD18552&0&0000",
        )

    def test_parse_dshow_video_device_infos_accepts_ffmpeg_8_inline_device_type(self):
        listing = r"""
[in#0 @ 000001] "Smart Connect Camera" (video)
[in#0 @ 000001]   Alternative name "@device_pnp_\\?\root#camera#0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[in#0 @ 000001] "DECXIN CAMERA" (video)
[in#0 @ 000001]   Alternative name "@device_pnp_\\?\usb#vid_1bcf&pid_2cd1&mi_00#6&1bd18552&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[in#0 @ 000001] "OBS Virtual Camera" (none)
[in#0 @ 000001] "Microphone Array (Realtek(R) Audio)" (audio)
"""
        devices = parse_dshow_video_device_infos(listing)
        self.assertEqual([device.name for device in devices], ["Smart Connect Camera", "DECXIN CAMERA"])
        decxin = decxin_device_info(devices)
        self.assertIsNotNone(decxin)
        assert decxin is not None
        self.assertEqual(decxin.usb_vid, DECXIN_USB_VID)
        self.assertEqual(decxin.usb_pid, DECXIN_USB_PID)

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

    def test_parse_dshow_modes_reports_decxin_mjpeg_range(self):
        options = """
[dshow @ 000001]   vcodec=mjpeg min s=1280x720 fps=10 max s=1280x720 fps=120
"""
        modes = parse_dshow_modes(options)
        self.assertEqual(len(modes), 1)
        mode = modes[0]
        self.assertEqual(mode.input_format, "mjpeg")
        self.assertEqual((mode.width, mode.height), (1280, 720))
        self.assertEqual((mode.min_fps, mode.max_fps), (10.0, 120.0))
        self.assertTrue(mode.supports(1280, 720, 30.0))

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

    def test_discover_dshow_auto_prefers_decxin_over_other_cameras(self):
        def runner(command, check=False, text=True, capture_output=True, timeout=10.0):
            if "-list_devices" in command:
                return mock.Mock(returncode=1, stdout="", stderr=self.DECXIN_LISTING)
            if "-list_options" in command:
                self.assertIn("video=DECXIN CAMERA", command)
                return mock.Mock(
                    returncode=1,
                    stdout="",
                    stderr="[dshow @ 000001]   vcodec=mjpeg min s=1280x720 fps=10 max s=1280x720 fps=120\n",
                )
            raise AssertionError(f"Unexpected command: {command}")

        camera = discover_dshow_camera(
            device="auto",
            width=1280,
            height=720,
            fps=30.0,
            runner=runner,
        )
        self.assertEqual(camera.name, DECXIN_CAMERA_NAME)
        self.assertEqual(camera.profile, "DECXIN")
        self.assertEqual(camera.usb_vid, DECXIN_USB_VID)
        self.assertEqual(camera.usb_pid, DECXIN_USB_PID)
        self.assertEqual(camera.usb_revision, DECXIN_USB_REV)
        self.assertEqual(camera.usb_interface, "00")
        self.assertEqual(camera.hardware_ids, DECXIN_HARDWARE_IDS)
        self.assertEqual(camera.parent_device, DECXIN_PARENT_DEVICE)
        self.assertEqual(
            camera.device_instance_path,
            r"USB\VID_1BCF&PID_2CD1&MI_00\6&1BD18552&0&0000",
        )
        self.assertIn("9281", camera.sensor_model_hint or "")
        self.assertEqual(camera.preferred_input_format, "mjpeg")
        self.assertEqual(camera.sensor_color, "monochrome")
        self.assertEqual(camera.shutter_type, "global")
        self.assertEqual(camera.raw_pixel_format, "gray")

    def test_discover_dshow_auto_rejects_non_decxin_cameras(self):
        listing = """
[dshow @ 000001] DirectShow video devices
[dshow @ 000001]  "Smart Connect Camera"
[dshow @ 000001]  "Integrated Camera"
[dshow @ 000001] DirectShow audio devices
"""

        def runner(command, check=False, text=True, capture_output=True, timeout=10.0):
            if "-list_devices" in command:
                return mock.Mock(returncode=1, stdout="", stderr=listing)
            raise AssertionError("Auto discovery should fail before reading camera options")

        with self.assertRaisesRegex(RuntimeError, "DECXIN CAMERA was not found"):
            discover_dshow_camera(
                device="auto",
                width=1280,
                height=720,
                fps=30.0,
                runner=runner,
            )

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

    def test_decxin_capture_command_decodes_to_gray_raw_frames(self):
        camera = CameraDevice(
            backend="dshow",
            path="DECXIN CAMERA",
            name="DECXIN CAMERA",
            formats_text="",
            preferred_input_format="mjpeg",
            profile="DECXIN",
            sensor_color="monochrome",
            shutter_type="global",
            raw_pixel_format="gray",
        )
        command = capture_command(camera, 1280, 720, 30.0)
        self.assertIn("-vcodec", command)
        self.assertIn("mjpeg", command)
        pix_fmt_index = command.index("-pix_fmt")
        self.assertEqual(command[pix_fmt_index + 1], "gray")
        self.assertEqual(frame_byte_count(1280, 720, "gray"), 921600)
        self.assertEqual(frame_byte_count(1280, 720, "bgr24"), 2764800)


if __name__ == "__main__":
    unittest.main()
