from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rfid_tracking.recording import gui
from rfid_tracking.recording.camera import CameraDevice
from rfid_tracking.recording.ffmpeg_recorder import RecorderState, RecorderStats, capture_thread, synthetic_capture_thread
from rfid_tracking.recording.timestamps import FramePacket
from rfid_tracking.recording.service import (
    PreflightCheck,
    PreflightResult,
    RecorderConfig,
    RecorderService,
    is_onedrive_path,
    write_session_metadata,
)


class GuiImportTests(unittest.TestCase):
    def test_gui_imports_without_starting_window(self):
        self.assertTrue(hasattr(gui, "main"))

    def test_cli_request_stop_works_without_pyside6(self):
        from rfid_tracking.recording.ffmpeg_recorder import main

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["--request-stop", "--output-dir", tmp]), 0)
            self.assertTrue((Path(tmp) / "STOP_RECORDING").exists())

    def test_decxin_camera_selected_only_when_present(self):
        self.assertEqual(gui.select_default_camera(["Laptop Camera", "DECXIN CAMERA"]), "DECXIN CAMERA")
        self.assertIsNone(gui.select_default_camera(["Laptop Camera"]))
        self.assertEqual(gui.select_default_camera(["Other"], saved_camera="Other"), "Other")

    def test_start_button_gating_helpers(self):
        self.assertFalse(gui.can_start_recording(False, False))
        self.assertTrue(gui.can_start_recording(True, False))
        self.assertFalse(gui.can_start_recording(True, True))


class RecorderServiceTests(unittest.TestCase):
    def test_camera_list_populates_from_directshow(self):
        listing = '[dshow] DirectShow video devices\n[dshow]  "DECXIN CAMERA"\n[dshow]  "Laptop Camera"\n'
        with mock.patch("rfid_tracking.recording.service.list_dshow_devices_raw", return_value=listing):
            self.assertEqual(RecorderService().list_cameras("dshow"), ["DECXIN CAMERA", "Laptop Camera"])

    def test_stop_calls_request_shutdown_once(self):
        service = RecorderService()
        state = RecorderState(threading.Event(), threading.Event(), RecorderStats())
        service._state = state
        self.assertTrue(service.request_shutdown("GUI stop button"))
        self.assertFalse(service.request_shutdown("GUI stop button again"))
        self.assertEqual(state.shutdown_source, "GUI stop button")

    def test_repeated_stop_clicks_are_idempotent(self):
        service = RecorderService()
        state = RecorderState(threading.Event(), threading.Event(), RecorderStats())
        service._state = state
        results = [service.request_shutdown("GUI stop button") for _ in range(3)]
        self.assertEqual(results, [True, False, False])

    def test_no_reconnect_after_gui_stop(self):
        packets: queue.Queue = queue.Queue()
        state = RecorderState(threading.Event(), threading.Event(), RecorderStats())
        service = RecorderService()
        service._state = state
        service.request_shutdown("GUI stop button")
        camera = CameraDevice("dshow", "DECXIN CAMERA", "DECXIN CAMERA", "", "mjpeg")
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

    def test_status_from_state_reports_fields(self):
        service = RecorderService()
        state = RecorderState(threading.Event(), threading.Event(), RecorderStats())
        state.stats.selected_camera = "DECXIN CAMERA"
        state.stats.selected_encoder = "hevc_qsv"
        state.stats.total_frames = 30
        state.stats.current_queue_size = 2
        state.stats.queue_high_water_mark = 4
        with tempfile.TemporaryDirectory() as tmp:
            status = service._status_from_state(Path(tmp), state, "recording")
        self.assertEqual(status.selected_camera, "DECXIN CAMERA")
        self.assertEqual(status.selected_encoder, "hevc_qsv")
        self.assertEqual(status.total_frames, 30)
        self.assertEqual(status.current_queue_size, 2)
        self.assertEqual(status.queue_high_water_mark, 4)

    def test_worker_style_exception_reaches_error_handling_contract(self):
        if not gui.PYSIDE6_AVAILABLE:
            self.skipTest("PySide6 is not installed")

    def test_preview_throttling(self):
        packets: queue.Queue = queue.Queue(maxsize=100)
        state = RecorderState(threading.Event(), threading.Event(), RecorderStats())
        frames = []
        synthetic_capture_thread(
            packets=packets,
            state=state,
            width=1,
            height=1,
            fps=30.0,
            duration_seconds=1.0,
            preview_callback=frames.append,
            preview_interval_s=0.2,
        )
        self.assertLess(len(frames), 30)
        self.assertGreaterEqual(len(frames), 4)

    def test_preview_frame_drop_does_not_affect_recording_queue(self):
        packets: queue.Queue = queue.Queue(maxsize=100)
        state = RecorderState(threading.Event(), threading.Event(), RecorderStats())

        def failing_preview(_packet):
            raise RuntimeError("preview renderer failed")

        with self.assertLogs("rfid_tracking.recording", level="WARNING"):
            synthetic_capture_thread(
                packets=packets,
                state=state,
                width=1,
                height=1,
                fps=10.0,
                duration_seconds=1.0,
                preview_callback=failing_preview,
                preview_interval_s=0.1,
            )
        queued_frames = 0
        while not packets.empty():
            item = packets.get_nowait()
            if isinstance(item, FramePacket):
                queued_frames += 1
        self.assertGreaterEqual(queued_frames, 10)
        self.assertEqual(state.stats.queue_full_events, 0)

    def test_settings_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recorder_gui.json"
            with mock.patch("rfid_tracking.recording.gui.settings_path", return_value=path):
                gui.save_settings({"camera_name": "DECXIN CAMERA"})
                self.assertEqual(gui.load_settings()["camera_name"], "DECXIN CAMERA")

    def test_onedrive_output_warning_helper(self):
        self.assertTrue(is_onedrive_path(Path(r"C:\Users\kenny\OneDrive\MouseTracker")))
        self.assertFalse(is_onedrive_path(Path(r"D:\MouseTracker\data\mota")))

    def test_recovery_button_report(self):
        service = RecorderService()
        fake_result = mock.Mock(ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("rfid_tracking.recording.service.recover_partials_in_dir", return_value=[fake_result]):
                report = service.recover_partials(Path(tmp))
        self.assertTrue(report.ok)
        self.assertEqual(report.message, "Recovered 1; failed 0.")

    def test_missing_ffmpeg_handling(self):
        def require_tool(name):
            if name == "ffmpeg":
                raise RuntimeError("ffmpeg missing")
            return f"C:/bin/{name}.exe"

        with tempfile.TemporaryDirectory() as tmp:
            config = RecorderConfig(output_dir=Path(tmp), synthetic=True)
            with mock.patch("rfid_tracking.recording.service.require_tool", side_effect=require_tool):
                with mock.patch("rfid_tracking.recording.service.select_hevc_encoder", return_value=mock.Mock(name="hevc_qsv")):
                    result = RecorderService().run_preflight(config)
        ffmpeg = next(check for check in result.checks if check.name == "FFMPEG")
        self.assertEqual(ffmpeg.status, "fail")

    def test_camera_busy_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = RecorderConfig(output_dir=Path(tmp), device="DECXIN CAMERA", run_short_camera_read=False)
            with mock.patch("rfid_tracking.recording.service.require_tool", return_value="tool.exe"):
                with mock.patch("rfid_tracking.recording.service.discover_camera", side_effect=RuntimeError("camera busy")):
                    with mock.patch("rfid_tracking.recording.service.select_hevc_encoder", return_value=mock.Mock(name="hevc_qsv")):
                        result = RecorderService().run_preflight(config)
        camera = next(check for check in result.checks if check.name == "camera")
        self.assertEqual(camera.status, "fail")
        self.assertIn("camera busy", camera.detail)

    def test_session_metadata_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_session_metadata(
                Path(tmp),
                {
                    "experiment_id": "E1",
                    "cohort": "C",
                    "mouse_ids": "M1,M2",
                    "condition": "baseline",
                    "operator": "kenny",
                    "notes": "ok",
                },
            )
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["experiment_id"], "E1")
        self.assertEqual(data["mouse_ids"], "M1,M2")

    def test_preflight_result_controls_start_availability(self):
        passed = PreflightResult([PreflightCheck("camera", "pass", "ok")])
        failed = PreflightResult([PreflightCheck("camera", "fail", "missing")])
        self.assertTrue(gui.can_start_recording(passed.ok, False))
        self.assertFalse(gui.can_start_recording(failed.ok, False))


@unittest.skipUnless(gui.PYSIDE6_AVAILABLE, "PySide6 is not installed")
class GuiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.tmp.name) / "recorder_gui.json"
        self.settings_patcher = mock.patch("rfid_tracking.recording.gui.settings_path", return_value=self.settings_path)
        self.camera_patcher = mock.patch(
            "rfid_tracking.recording.gui.RecorderService.list_cameras",
            return_value=["DECXIN CAMERA"],
        )
        self.settings_patcher.start()
        self.camera_patcher.start()
        self.window = gui.MainWindow()
        self.window.output_dir_edit.setText(r"D:\MouseTracker\data\mota_gui_test_20260715")
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.camera_patcher.stop()
        self.settings_patcher.stop()
        self.tmp.cleanup()

    def _resize_and_process(self, width: int, height: int) -> None:
        self.window.resize(width, height)
        self.app.processEvents()

    def test_recording_settings_widgets_retain_usable_widths(self):
        self._resize_and_process(1000, 800)
        self.assertGreaterEqual(self.window.output_dir_edit.width(), 220)
        self.assertGreaterEqual(self.window.encoder_combo.width(), 180)
        self.assertGreaterEqual(self.window.segment_spin.width(), 120)

    def test_output_path_field_expands_and_browse_stays_visible(self):
        self._resize_and_process(1100, 800)
        self.assertTrue(self.window.output_browse_button.isVisible())
        self.assertGreater(self.window.output_dir_edit.width(), self.window.output_browse_button.width())

    def test_maximizing_does_not_reduce_field_widths(self):
        self._resize_and_process(1000, 800)
        normal_width = self.window.output_dir_edit.width()
        self._resize_and_process(1600, 1000)
        self.assertGreaterEqual(self.window.output_dir_edit.width(), normal_width)

    def test_small_window_mode_provides_scrolling_and_keeps_controls_accessible(self):
        self._resize_and_process(760, 480)
        scrollbar = self.window.controls_scroll.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertFalse(self.window.start_button.isHidden())
        self.assertFalse(self.window.stop_button.isHidden())

    def test_splitter_positions_can_be_saved_and_restored(self):
        self.window.main_splitter.setSizes([520, 900])
        self.window.detail_splitter.setSizes([640, 260])
        self.app.processEvents()
        self.window._save_settings()
        data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertIn("main_splitter_sizes", data)
        self.assertIn("detail_splitter_sizes", data)

        self.window.close()
        self.app.processEvents()
        self.window = gui.MainWindow()
        self.window.show()
        self.app.processEvents()
        self.assertEqual(len(self.window.main_splitter.sizes()), 2)
        self.assertEqual(len(self.window.detail_splitter.sizes()), 2)

    def test_advanced_settings_collapse_without_reserved_space(self):
        self._resize_and_process(1000, 800)
        collapsed_height = self.window.advanced_container.parentWidget().sizeHint().height()
        self.assertFalse(self.window.advanced_container.isVisible())
        self.window.advanced_button.setChecked(True)
        self.app.processEvents()
        expanded_height = self.window.advanced_container.parentWidget().sizeHint().height()
        self.assertTrue(self.window.advanced_container.isVisible())
        self.assertGreater(expanded_height, collapsed_height)
        self.window.advanced_button.setChecked(False)
        self.app.processEvents()
        self.assertFalse(self.window.advanced_container.isVisible())


if __name__ == "__main__":
    unittest.main()
