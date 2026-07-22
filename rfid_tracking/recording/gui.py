"""Native Windows GUI for the MouseTracker recorder."""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from pathlib import Path

from .camera import DECXIN_CAMERA_NAME
from .ffmpeg_recorder import DEFAULT_OUTPUT_DIR
from rfid_tracking.analysis.movement import MovementSummary
from .service import (
    PreflightResult,
    PreviewFrame,
    RecorderConfig,
    RecorderService,
    RecorderStatus,
    RecoveryReport,
    is_onedrive_path,
    settings_path,
    write_session_metadata,
)


try:  # Optional GUI dependency. The CLI must keep working without PySide6.
    from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
    from PySide6.QtGui import QCloseEvent, QImage, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QDoubleSpinBox,
        QSplitter,
        QTextEdit,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )

    PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in environments without Qt.
    PYSIDE6_AVAILABLE = False


DEFAULT_CAMERA_NAME = DECXIN_CAMERA_NAME
LOG_LEVELS = {"all": logging.INFO, "warnings and errors": logging.WARNING, "errors only": logging.ERROR}


def select_default_camera(cameras: list[str], saved_camera: str | None = None) -> str | None:
    if saved_camera and saved_camera in cameras:
        return saved_camera
    if DEFAULT_CAMERA_NAME in cameras:
        return DEFAULT_CAMERA_NAME
    return None


def can_start_recording(preflight_ok: bool, recording: bool) -> bool:
    return preflight_ok and not recording


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class QtLogHandler(logging.Handler):
    def __init__(self, emit_log) -> None:
        super().__init__(logging.INFO)
        self.emit_log = emit_log

    def emit(self, record: logging.LogRecord) -> None:
        self.emit_log(record.levelno, self.format(record))


if PYSIDE6_AVAILABLE:

    class MetadataDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Session Metadata")
            self.fields: dict[str, QLineEdit | QTextEdit] = {
                "experiment_id": QLineEdit(),
                "cohort": QLineEdit(),
                "mouse_ids": QLineEdit(),
                "condition": QLineEdit(),
                "operator": QLineEdit(),
                "notes": QTextEdit(),
            }
            layout = QVBoxLayout(self)
            form = QFormLayout()
            form.addRow("Experiment ID", self.fields["experiment_id"])
            form.addRow("Cohort", self.fields["cohort"])
            form.addRow("Mouse IDs", self.fields["mouse_ids"])
            form.addRow("Condition", self.fields["condition"])
            form.addRow("Operator", self.fields["operator"])
            form.addRow("Notes", self.fields["notes"])
            layout.addLayout(form)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def metadata(self) -> dict[str, str]:
            values: dict[str, str] = {}
            for key, widget in self.fields.items():
                values[key] = widget.toPlainText().strip() if isinstance(widget, QTextEdit) else widget.text().strip()
            return values


    class RecorderWorker(QObject):
        preflightFinished = Signal(object)
        statusChanged = Signal(object)
        previewFrame = Signal(object)
        recoveryFinished = Signal(object)
        analysisFinished = Signal(object)
        errorOccurred = Signal(str)
        finished = Signal(int)

        def __init__(self):
            super().__init__()
            self.service = RecorderService()

        @Slot(object)
        def run_preflight(self, config: RecorderConfig) -> None:
            try:
                self.preflightFinished.emit(self.service.run_preflight(config))
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("rfid_tracking.recording").exception("GUI preflight failed")
                self.errorOccurred.emit(str(exc))

        @Slot(object, object)
        def start_recording(self, config: RecorderConfig, metadata: dict[str, str]) -> None:
            try:
                write_session_metadata(config.output_dir, metadata)
                rc = self.service.start(
                    config,
                    status_callback=self.statusChanged.emit,
                    preview_callback=self.previewFrame.emit,
                )
                self.finished.emit(rc)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("rfid_tracking.recording").error("GUI recording failed\n%s", traceback.format_exc())
                self.errorOccurred.emit(str(exc))
                self.finished.emit(1)

        @Slot(str)
        def stop_recording(self, reason: str = "GUI stop button") -> None:
            self.service.request_shutdown(reason)

        @Slot(object)
        def recover(self, config: RecorderConfig) -> None:
            try:
                self.recoveryFinished.emit(
                    self.service.recover_partials(
                        config.output_dir,
                        width=config.width,
                        height=config.height,
                        fps=config.fps,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("rfid_tracking.recording").exception("GUI recovery failed")
                self.errorOccurred.emit(str(exc))

        @Slot(object)
        def analyze_latest_movement(self, config: RecorderConfig) -> None:
            try:
                self.analysisFinished.emit(
                    self.service.analyze_latest_movement(
                        config.output_dir,
                        width=config.width,
                        height=config.height,
                        fps=config.fps,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("rfid_tracking.recording").exception("GUI movement analysis failed")
                self.errorOccurred.emit(str(exc))


    class MainWindow(QMainWindow):
        preflightRequested = Signal(object)
        startRequested = Signal(object, object)
        stopRequested = Signal(str)
        recoveryRequested = Signal(object)
        analysisRequested = Signal(object)

        def __init__(self):
            super().__init__()
            self.setWindowTitle("MouseTracker Recorder")
            self.settings = load_settings()
            self.preflight_ok = False
            self.recording = False
            self.pending_close = False
            self.last_status: RecorderStatus | None = None

            self.worker_thread = QThread(self)
            self.worker = RecorderWorker()
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.start()
            self.preflightRequested.connect(self.worker.run_preflight)
            self.startRequested.connect(self.worker.start_recording)
            self.stopRequested.connect(self.worker.stop_recording)
            self.recoveryRequested.connect(self.worker.recover)
            self.analysisRequested.connect(self.worker.analyze_latest_movement)
            self.worker.preflightFinished.connect(self.on_preflight_finished)
            self.worker.statusChanged.connect(self.on_status)
            self.worker.previewFrame.connect(self.on_preview_frame)
            self.worker.recoveryFinished.connect(self.on_recovery_finished)
            self.worker.analysisFinished.connect(self.on_analysis_finished)
            self.worker.errorOccurred.connect(self.on_error)
            self.worker.finished.connect(self.on_recording_finished)

            self._build_ui()
            self._restore_settings()
            self.refresh_cameras()

            self.log_handler = QtLogHandler(self.add_log_message)
            self.log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
            logging.getLogger("rfid_tracking.recording").addHandler(self.log_handler)

        def _build_ui(self) -> None:
            central = QWidget()
            root = QHBoxLayout(central)
            root.setContentsMargins(8, 8, 8, 8)
            self.main_splitter = QSplitter(Qt.Horizontal)
            self.main_splitter.addWidget(self._build_controls_panel())
            self.detail_splitter = QSplitter(Qt.Vertical)
            self.detail_splitter.addWidget(self._build_live_panel())
            self.detail_splitter.addWidget(self._build_log_panel())
            self.detail_splitter.setChildrenCollapsible(False)
            self.main_splitter.addWidget(self.detail_splitter)
            self.main_splitter.setChildrenCollapsible(False)
            self.main_splitter.setStretchFactor(0, 0)
            self.main_splitter.setStretchFactor(1, 1)
            self.detail_splitter.setStretchFactor(0, 2)
            self.detail_splitter.setStretchFactor(1, 1)
            self.main_splitter.setSizes([420, 780])
            self.detail_splitter.setSizes([560, 260])
            root.addWidget(self.main_splitter)
            self.setCentralWidget(central)

        def _build_controls_panel(self) -> QScrollArea:
            scroll = QScrollArea()
            self.controls_scroll = scroll
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            scroll.setMinimumWidth(430)
            scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
            page = QWidget()
            page.setMinimumWidth(420)
            layout = QVBoxLayout(page)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(10)
            for group in (self._camera_group(), self._settings_group(), self._preflight_group(), self._controls_group()):
                group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
                layout.addWidget(group)
            layout.addStretch(1)
            scroll.setWidget(page)
            return scroll

        def _build_live_panel(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            preview = self._preview_group()
            status = self._status_group()
            layout.addWidget(preview, 3)
            layout.addWidget(status, 2)
            return page

        def _camera_group(self) -> QGroupBox:
            group = QGroupBox("Camera")
            form = QFormLayout(group)
            self._configure_form_layout(form)
            self.backend_combo = QComboBox()
            self._set_field_policy(self.backend_combo)
            self.backend_combo.setMinimumWidth(180)
            self.backend_combo.addItems(["dshow", "auto", "v4l2"])
            self.camera_combo = QComboBox()
            self._set_field_policy(self.camera_combo)
            self.camera_combo.setMinimumWidth(180)
            self.refresh_button = QPushButton("Refresh Cameras")
            self.refresh_button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            self.refresh_button.clicked.connect(self.refresh_cameras)
            self.camera_details = QLabel("No camera selected")
            self.camera_details.setWordWrap(True)
            self.input_codec_label = QLabel("MJPEG")
            self.resolution_label = QLabel("1280x720")
            self.fps_label = QLabel("30 fps")
            form.addRow("Backend", self.backend_combo)
            form.addRow("Camera", self.camera_combo)
            form.addRow("", self.refresh_button)
            form.addRow("Details", self.camera_details)
            form.addRow("Input codec", self.input_codec_label)
            form.addRow("Resolution", self.resolution_label)
            form.addRow("Frame rate", self.fps_label)
            return group

        def _settings_group(self) -> QGroupBox:
            group = QGroupBox("Recording Settings")
            form = QFormLayout(group)
            self._configure_form_layout(form)
            self.output_dir_edit = QLineEdit(str(DEFAULT_OUTPUT_DIR))
            self.output_dir_edit.setMinimumWidth(220)
            self._set_field_policy(self.output_dir_edit)
            self.output_browse_button = QPushButton("Browse")
            self.output_browse_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.output_browse_button.clicked.connect(self.browse_output_dir)
            output_row = self._browse_row(self.output_dir_edit, self.output_browse_button)
            self.segment_spin = QDoubleSpinBox()
            self._set_field_policy(self.segment_spin)
            self.segment_spin.setRange(1, 3600)
            self.segment_spin.setValue(60)
            self.encoder_combo = QComboBox()
            self._set_field_policy(self.encoder_combo)
            self.encoder_combo.setMinimumWidth(180)
            self.encoder_combo.addItems(["auto", "hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"])
            self.ffmpeg_bin_edit = QLineEdit()
            self._set_field_policy(self.ffmpeg_bin_edit)
            self.ffmpeg_browse_button = QPushButton("Browse")
            self.ffmpeg_browse_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.ffmpeg_browse_button.clicked.connect(self.browse_ffmpeg_bin)
            ffmpeg_row = self._browse_row(self.ffmpeg_bin_edit, self.ffmpeg_browse_button)
            self.gap_spin = QDoubleSpinBox()
            self._set_field_policy(self.gap_spin)
            self.gap_spin.setRange(1, 1000)
            self.gap_spin.setValue(50)
            self.width_spin = QSpinBox()
            self._set_field_policy(self.width_spin)
            self.width_spin.setRange(1, 8192)
            self.width_spin.setValue(1280)
            self.height_spin = QSpinBox()
            self._set_field_policy(self.height_spin)
            self.height_spin.setRange(1, 8192)
            self.height_spin.setValue(720)
            self.fps_spin = QDoubleSpinBox()
            self._set_field_policy(self.fps_spin)
            self.fps_spin.setRange(1, 240)
            self.fps_spin.setValue(30)
            self.capture_mode_label = QLabel("MJPEG")
            self.capture_size_label = QLabel("1280 x 720")
            self.capture_fps_label = QLabel("30 fps")
            self.timezone_label = QLabel("Timezone: checking at preflight")
            self.timezone_label.setWordWrap(True)
            self.onedrive_warning = QLabel("")
            self.onedrive_warning.setWordWrap(True)
            self.onedrive_warning.setStyleSheet("color: #9a6700")
            form.addRow("Output directory", output_row)
            form.addRow("Segment seconds", self.segment_spin)
            form.addRow("Encoder", self.encoder_combo)
            form.addRow("Gap threshold ms", self.gap_spin)
            form.addRow("Input codec", self.capture_mode_label)
            form.addRow("Resolution", self.capture_size_label)
            form.addRow("FPS", self.capture_fps_label)
            form.addRow("Timezone", self.timezone_label)
            form.addRow("", self.onedrive_warning)
            self.advanced_button = QToolButton()
            self.advanced_button.setText("Advanced Settings")
            self.advanced_button.setCheckable(True)
            self.advanced_button.setChecked(False)
            self.advanced_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            self.advanced_button.setArrowType(Qt.RightArrow)
            self.advanced_button.toggled.connect(self._set_advanced_visible)
            self.advanced_container = QWidget()
            advanced_form = QFormLayout(self.advanced_container)
            self._configure_form_layout(advanced_form)
            advanced_form.addRow("FFmpeg bin", ffmpeg_row)
            advanced_form.addRow("Width", self.width_spin)
            advanced_form.addRow("Height", self.height_spin)
            advanced_form.addRow("FPS", self.fps_spin)
            self.advanced_container.setVisible(False)
            form.addRow("", self.advanced_button)
            form.addRow("", self.advanced_container)
            self.output_dir_edit.textChanged.connect(self.update_onedrive_warning)
            self.width_spin.valueChanged.connect(self._update_capture_summary)
            self.height_spin.valueChanged.connect(self._update_capture_summary)
            self.fps_spin.valueChanged.connect(self._update_capture_summary)
            return group

        def _preflight_group(self) -> QGroupBox:
            group = QGroupBox("Preflight")
            layout = QVBoxLayout(group)
            self.preflight_button = QPushButton("Run Preflight")
            self.preflight_button.clicked.connect(self.run_preflight)
            self.preflight_list = QListWidget()
            self.preflight_list.setMinimumHeight(140)
            self.preflight_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            layout.addWidget(self.preflight_button)
            layout.addWidget(self.preflight_list)
            return group

        def _controls_group(self) -> QGroupBox:
            group = QGroupBox("Recording Controls")
            grid = QGridLayout(group)
            self.start_button = QPushButton("Start Recording")
            self.stop_button = QPushButton("Stop Recording")
            self.open_button = QPushButton("Open Output Folder")
            self.recover_button = QPushButton("Recover Partial Files")
            self.analyze_button = QPushButton("Analyze Latest Movement")
            for button in (
                self.start_button,
                self.stop_button,
                self.open_button,
                self.recover_button,
                self.analyze_button,
            ):
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.start_button.clicked.connect(self.start_recording)
            self.stop_button.clicked.connect(self.stop_recording)
            self.open_button.clicked.connect(self.open_output_folder)
            self.recover_button.clicked.connect(lambda: self.recoveryRequested.emit(self.config_from_ui()))
            self.analyze_button.clicked.connect(lambda: self.analysisRequested.emit(self.config_from_ui()))
            grid.addWidget(self.start_button, 0, 0)
            grid.addWidget(self.stop_button, 0, 1)
            grid.addWidget(self.open_button, 1, 0)
            grid.addWidget(self.recover_button, 1, 1)
            grid.addWidget(self.analyze_button, 2, 0, 1, 2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            return group

        def _status_group(self) -> QGroupBox:
            group = QGroupBox("Live Status")
            group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            form = QFormLayout(group)
            self._configure_form_layout(form)
            self.status_labels: dict[str, QLabel] = {}
            for label in (
                "State",
                "Camera",
                "Encoder",
                "Start time",
                "Elapsed",
                "Segment",
                "Segment elapsed",
                "Frames",
                "Queue",
                "Queue high-water",
                "Frame gaps",
                "Reconnects",
                "Capture rate",
                "Disk free",
                "Last validation",
            ):
                widget = QLabel("-")
                widget.setWordWrap(True)
                widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                self.status_labels[label] = widget
                form.addRow(label, widget)
            return group

        def _preview_group(self) -> QGroupBox:
            group = QGroupBox("Live Preview")
            group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            layout = QVBoxLayout(group)
            self.preview_checkbox = QCheckBox("Show live preview")
            self.preview_label = QLabel("Preview disabled")
            self.preview_label.setMinimumSize(320, 180)
            self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.preview_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(self.preview_checkbox)
            layout.addWidget(self.preview_label, 1)
            return group

        def _build_log_panel(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            self.log_filter = QComboBox()
            self.log_filter.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            self.log_filter.addItems(list(LOG_LEVELS))
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            layout.addWidget(self.log_filter)
            layout.addWidget(self.log_view, 1)
            return page

        def _configure_form_layout(self, form: QFormLayout) -> None:
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            form.setLabelAlignment(Qt.AlignLeft)
            form.setFormAlignment(Qt.AlignTop)
            form.setHorizontalSpacing(12)
            form.setVerticalSpacing(8)

        def _set_field_policy(self, widget: QWidget) -> None:
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        def _browse_row(self, field: QLineEdit, button: QPushButton) -> QWidget:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            layout.addWidget(field, 1)
            layout.addWidget(button, 0)
            return row

        def _set_advanced_visible(self, visible: bool) -> None:
            self.advanced_button.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
            self.advanced_container.setVisible(visible)

        def _update_capture_summary(self) -> None:
            self.capture_size_label.setText(f"{self.width_spin.value()} x {self.height_spin.value()}")
            self.capture_fps_label.setText(f"{self.fps_spin.value():g} fps")

        def config_from_ui(self) -> RecorderConfig:
            return RecorderConfig(
                backend=self.backend_combo.currentText(),
                device=self.camera_combo.currentText() or DEFAULT_CAMERA_NAME,
                output_dir=Path(self.output_dir_edit.text()),
                width=self.width_spin.value(),
                height=self.height_spin.value(),
                fps=self.fps_spin.value(),
                segment_seconds=self.segment_spin.value(),
                encoder=self.encoder_combo.currentText(),
                gap_threshold_ms=self.gap_spin.value(),
                keyboard_stop=False,
                ffmpeg_bin_dir=Path(self.ffmpeg_bin_edit.text()) if self.ffmpeg_bin_edit.text().strip() else None,
            )

        def refresh_cameras(self) -> None:
            self.camera_combo.clear()
            try:
                cameras = RecorderService().list_cameras(self.backend_combo.currentText())
            except Exception as exc:  # noqa: BLE001
                self.camera_details.setText(f"Camera discovery failed: {exc}")
                cameras = []
            self.camera_combo.addItems(cameras)
            preferred = self.settings.get("camera_name") or DEFAULT_CAMERA_NAME
            selected = select_default_camera(cameras, preferred)
            if selected:
                self.camera_combo.setCurrentText(selected)
                self.camera_details.setText(f"Selected {selected}")
            elif preferred and preferred != DEFAULT_CAMERA_NAME:
                self.camera_details.setText(f"Saved camera unavailable: {preferred}")
            else:
                self.camera_details.setText("Select an external camera")

        def browse_output_dir(self) -> None:
            directory = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.output_dir_edit.text())
            if directory:
                self.output_dir_edit.setText(directory)

        def browse_ffmpeg_bin(self) -> None:
            directory = QFileDialog.getExistingDirectory(self, "Select FFmpeg Bin Directory", self.ffmpeg_bin_edit.text())
            if directory:
                self.ffmpeg_bin_edit.setText(directory)

        def update_onedrive_warning(self) -> None:
            self.onedrive_warning.setText(
                "Warning: OneDrive sync can interfere with active recording."
                if is_onedrive_path(Path(self.output_dir_edit.text()))
                else ""
            )

        def run_preflight(self) -> None:
            self.preflight_button.setEnabled(False)
            self.preflight_list.clear()
            self.preflight_list.addItem("Running preflight...")
            self.preflightRequested.emit(self.config_from_ui())

        def on_preflight_finished(self, result: PreflightResult) -> None:
            self.preflight_button.setEnabled(True)
            self.preflight_list.clear()
            for check in result.checks:
                icon = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}[check.status]
                item = QListWidgetItem(f"{icon} {check.name}: {check.detail}")
                self.preflight_list.addItem(item)
            self.preflight_ok = result.ok
            self.start_button.setEnabled(result.ok and not self.recording)
            if result.selected_camera:
                self.input_codec_label.setText(result.selected_camera.preferred_input_format)
                self.resolution_label.setText(f"{result.selected_camera.width}x{result.selected_camera.height}")
                self.fps_label.setText(f"{result.selected_camera.fps:g} fps")
            if result.selected_encoder:
                self.status_labels["Encoder"].setText(result.selected_encoder)

        def start_recording(self) -> None:
            metadata_dialog = MetadataDialog(self)
            if metadata_dialog.exec() != QDialog.Accepted:
                return
            metadata = metadata_dialog.metadata()
            if not metadata.get("experiment_id") and not metadata.get("mouse_ids"):
                QMessageBox.warning(self, "Metadata warning", "Experiment ID and mouse IDs are blank.")
            self.recording = True
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.startRequested.emit(self.config_from_ui(), metadata)

        def stop_recording(self) -> None:
            self.stop_button.setEnabled(False)
            self.stopRequested.emit("GUI stop button")

        def open_output_folder(self) -> None:
            path = Path(self.output_dir_edit.text())
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                QMessageBox.information(self, "Output Folder", str(path))

        def on_status(self, status: RecorderStatus) -> None:
            self.last_status = status
            values = {
                "State": status.state,
                "Camera": status.selected_camera or "-",
                "Encoder": status.selected_encoder or "-",
                "Start time": status.recording_start_time or "-",
                "Elapsed": f"{status.elapsed_seconds:.1f} s",
                "Segment": status.current_segment_filename or "-",
                "Segment elapsed": f"{status.current_segment_elapsed_seconds:.1f} s",
                "Frames": str(status.total_frames),
                "Queue": str(status.current_queue_size),
                "Queue high-water": str(status.queue_high_water_mark),
                "Frame gaps": str(status.frame_gap_count),
                "Reconnects": str(status.reconnect_count),
                "Capture rate": f"{status.effective_capture_rate_fps:.1f} fps",
                "Disk free": "-" if status.disk_free_bytes is None else f"{status.disk_free_bytes / 1_000_000_000:.1f} GB",
                "Last validation": status.last_validation_result or "-",
            }
            for key, value in values.items():
                self.status_labels[key].setText(value)

        def on_preview_frame(self, frame: PreviewFrame) -> None:
            if not self.preview_checkbox.isChecked():
                return
            if frame.pixel_format == "gray":
                image = QImage(frame.data, frame.width, frame.height, frame.width, QImage.Format_Grayscale8).copy()
            else:
                image = QImage(frame.data, frame.width, frame.height, frame.width * 3, QImage.Format_BGR888).copy()
            pixmap = QPixmap.fromImage(image).scaled(
                self.preview_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self.preview_label.setPixmap(pixmap)

        def on_recovery_finished(self, report: RecoveryReport) -> None:
            QMessageBox.information(self, "Partial Recovery", report.message)

        def on_analysis_finished(self, summary: MovementSummary) -> None:
            QMessageBox.information(
                self,
                "Movement Analysis",
                "Movement analysis complete.\n"
                f"CSV: {summary.movement_csv}\n"
                f"Summary: {summary.summary_json}\n"
                f"Frames matched: {summary.frame_count_match}",
            )

        def on_error(self, message: str) -> None:
            QMessageBox.critical(self, "Recorder Error", message)

        def on_recording_finished(self, rc: int) -> None:
            self.recording = False
            self.stop_button.setEnabled(False)
            self.start_button.setEnabled(self.preflight_ok)
            if rc != 0:
                QMessageBox.critical(self, "Recording failed", f"Recorder exited with code {rc}. See logs.")
            if self.pending_close:
                self.pending_close = False
                self.close()

        def add_log_message(self, level: int, message: str) -> None:
            if level < LOG_LEVELS[self.log_filter.currentText()]:
                return
            if "Frame gap" in message:
                message = message.split("Frame gap", 1)[0] + "Frame gap warning recorded. See run log for details."
            self.log_view.appendPlainText(message)

        def _restore_settings(self) -> None:
            self.output_dir_edit.setText(self.settings.get("output_dir", str(DEFAULT_OUTPUT_DIR)))
            self.encoder_combo.setCurrentText(self.settings.get("encoder", "auto"))
            self.ffmpeg_bin_edit.setText(self.settings.get("ffmpeg_bin_dir", ""))
            self.segment_spin.setValue(float(self.settings.get("segment_seconds", 60)))
            self.gap_spin.setValue(float(self.settings.get("gap_threshold_ms", 50)))
            self.preview_checkbox.setChecked(bool(self.settings.get("preview_enabled", False)))
            if "window_geometry" in self.settings:
                geo = self.settings["window_geometry"]
                self.resize(geo.get("width", 1000), geo.get("height", 900))
                if "x" in geo and "y" in geo:
                    self.move(geo["x"], geo["y"])
            else:
                self.resize(1000, 900)
            if "main_splitter_sizes" in self.settings:
                self.main_splitter.setSizes(self.settings["main_splitter_sizes"])
            if "detail_splitter_sizes" in self.settings:
                self.detail_splitter.setSizes(self.settings["detail_splitter_sizes"])
            self.update_onedrive_warning()
            self._update_capture_summary()

        def _save_settings(self) -> None:
            position = self.pos()
            save_settings(
                {
                    "camera_name": self.camera_combo.currentText(),
                    "output_dir": self.output_dir_edit.text(),
                    "encoder": self.encoder_combo.currentText(),
                    "ffmpeg_bin_dir": self.ffmpeg_bin_edit.text(),
                    "segment_seconds": self.segment_spin.value(),
                    "gap_threshold_ms": self.gap_spin.value(),
                    "preview_enabled": self.preview_checkbox.isChecked(),
                    "window_geometry": {
                        "x": position.x(),
                        "y": position.y(),
                        "width": self.width(),
                        "height": self.height(),
                    },
                    "main_splitter_sizes": self.main_splitter.sizes(),
                    "detail_splitter_sizes": self.detail_splitter.sizes(),
                }
            )

        def closeEvent(self, event: QCloseEvent) -> None:
            if self.recording:
                answer = QMessageBox.question(
                    self,
                    "Stop recording?",
                    "Recording is active. Stop safely and close?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
                self.pending_close = True
                self.stop_button.setEnabled(False)
                self.stopRequested.emit("GUI window close")
                event.ignore()
                return
            self._save_settings()
            logging.getLogger("rfid_tracking.recording").removeHandler(self.log_handler)
            self.worker_thread.quit()
            self.worker_thread.wait(3000)
            event.accept()


def main(argv: list[str] | None = None) -> int:
    if not PYSIDE6_AVAILABLE:
        print("PySide6 is not installed. Install GUI dependencies with: pip install -r requirements-gui.txt", file=sys.stderr)
        return 1
    app = QApplication(argv if argv is not None else sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
