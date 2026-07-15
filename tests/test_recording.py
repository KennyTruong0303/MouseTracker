from __future__ import annotations

import io
import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rfid_tracking.recording import encoder
from rfid_tracking.recording.camera import (
    CameraDevice,
    choose_dshow_input_format,
    parse_dshow_video_devices,
    resolve_backend,
)
from rfid_tracking.recording.ffmpeg_recorder import (
    RecorderState,
    RecorderStats,
    SENTINEL,
    SegmentWriter,
    capture_command,
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
)


class FakeStdin(io.BytesIO):
    def close(self) -> None:
        self.flush()


class FakePopen:
    instances: list["FakePopen"] = []

    def __init__(self, command, stdin=None):
        self.command = command
        self.stdin = FakeStdin()
        self.returncode = 0
        self.output_path = Path(command[-1])
        FakePopen.instances.append(self)

    def wait(self, timeout=None):
        self.output_path.write_bytes(b"fake hevc payload")
        return self.returncode


class FakeFullQueue:
    def put(self, _item, timeout=None):
        raise queue.Full

    def qsize(self):
        return 0


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
        self.assertIn("-f", command)
        self.assertIn("dshow", command)
        self.assertIn("video=USB Camera", command)
        self.assertIn("-vcodec", command)
        self.assertIn("mjpeg", command)


if __name__ == "__main__":
    unittest.main()
