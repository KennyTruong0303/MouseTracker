"""Programmatic service layer for the MouseTracker recorder."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from rfid_tracking.analysis.movement import MotionSettings, MovementSummary, analyze_video

from .camera import (
    CameraDevice,
    DECXIN_CAMERA_NAME,
    DECXIN_DEFAULT_INPUT_FORMAT,
    DECXIN_DEFAULT_FPS,
    DECXIN_DEFAULT_HEIGHT,
    DECXIN_DEFAULT_WIDTH,
    discover_camera,
    list_dshow_devices_raw,
    parse_dshow_video_devices,
    resolve_backend,
)
from .encoder import require_tool, select_hevc_encoder
from .ffmpeg_recorder import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_QUEUE_SIZE,
    RecorderState,
    RecorderStats,
    capture_command,
    check_output_dir,
    disk_free_bytes,
    frame_byte_count,
    high_resolution_monotonic_ns,
    run_recording,
    timezone_offset_string,
)
from .timestamps import FramePacket
from .validation import PartialRecovery, recover_partials_in_dir


CheckStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class RecorderConfig:
    backend: str = "auto"
    device: str = DECXIN_CAMERA_NAME
    output_dir: Path = DEFAULT_OUTPUT_DIR
    width: int = DECXIN_DEFAULT_WIDTH
    height: int = DECXIN_DEFAULT_HEIGHT
    fps: float = DECXIN_DEFAULT_FPS
    segment_seconds: float = 60.0
    encoder: str = "auto"
    gap_threshold_ms: float = 50.0
    queue_size: int = DEFAULT_QUEUE_SIZE
    synthetic: bool = False
    duration_seconds: float = 0.0
    skip_validation: bool = False
    keyboard_stop: bool | None = False
    stop_file: Path | None = None
    camera_controls_config: str | None = None
    expected_timezone_offset: str | None = "+08:00"
    timestamp_source: str = "auto"
    run_short_camera_read: bool = True
    ffmpeg_bin_dir: Path | None = None


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: CheckStatus
    detail: str
    required: bool = True


@dataclass(frozen=True)
class PreflightResult:
    checks: list[PreflightCheck]
    selected_camera: CameraDevice | None = None
    selected_encoder: str | None = None

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks if check.required)


@dataclass(frozen=True)
class PreviewFrame:
    data: bytes
    width: int
    height: int
    pixel_format: str
    global_frame_index: int
    wall_time_unix_ns: int
    monotonic_ns: int


@dataclass(frozen=True)
class RecorderStatus:
    state: str
    selected_camera: str | None
    selected_encoder: str | None
    recording_start_time: str | None
    elapsed_seconds: float
    current_segment_filename: str | None
    current_segment_elapsed_seconds: float
    total_frames: int
    current_queue_size: int
    queue_high_water_mark: int
    frame_gap_count: int
    reconnect_count: int
    effective_capture_rate_fps: float
    disk_free_bytes: int | None
    last_validation_result: str | None
    shutdown_source: str | None


@dataclass(frozen=True)
class RecoveryReport:
    results: list[PartialRecovery] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def message(self) -> str:
        if not self.results:
            return "No .part MP4 files found."
        recovered = sum(1 for result in self.results if result.ok)
        failed = len(self.results) - recovered
        return f"Recovered {recovered}; failed {failed}."


StatusCallback = Callable[[RecorderStatus], None]
PreviewCallback = Callable[[PreviewFrame], None]


def is_onedrive_path(path: Path) -> bool:
    lowered = str(path).lower()
    return "\\onedrive\\" in lowered or "/onedrive/" in lowered


def settings_path() -> Path:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return root / "MouseTracker" / "recorder_gui.json"


def write_session_metadata(output_dir: Path, metadata: dict[str, str]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    path = output_dir / f"recording_session_{stamp}.json"
    payload = {
        "created_at": datetime.now().astimezone().isoformat(),
        "experiment_id": metadata.get("experiment_id", ""),
        "cohort": metadata.get("cohort", ""),
        "mouse_ids": metadata.get("mouse_ids", ""),
        "condition": metadata.get("condition", ""),
        "operator": metadata.get("operator", ""),
        "notes": metadata.get("notes", ""),
    }
    import json

    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


@contextmanager
def ffmpeg_path_context(ffmpeg_bin_dir: Path | None):
    if not ffmpeg_bin_dir:
        yield
        return
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(ffmpeg_bin_dir) + os.pathsep + old_path
    try:
        yield
    finally:
        os.environ["PATH"] = old_path


class RecorderService:
    def __init__(self) -> None:
        self._state: RecorderState | None = None
        self._state_lock = threading.Lock()
        self._finished = threading.Event()

    @property
    def state(self) -> RecorderState | None:
        with self._state_lock:
            return self._state

    def list_cameras(self, backend: str = "auto") -> list[str]:
        resolved = resolve_backend(backend)
        if resolved != "dshow":
            return []
        return parse_dshow_video_devices(list_dshow_devices_raw())

    def run_preflight(self, config: RecorderConfig) -> PreflightResult:
        with ffmpeg_path_context(config.ffmpeg_bin_dir):
            return self._run_preflight(config)

    def _run_preflight(self, config: RecorderConfig) -> PreflightResult:
        checks: list[PreflightCheck] = []
        camera: CameraDevice | None = None
        selected_encoder: str | None = None
        backend = resolve_backend(config.backend)

        checks.append(PreflightCheck("Python", "pass", sys.version.split()[0]))
        for tool in ("ffmpeg", "ffprobe"):
            try:
                checks.append(PreflightCheck(tool.upper(), "pass", require_tool(tool)))
            except Exception as exc:  # noqa: BLE001
                checks.append(PreflightCheck(tool.upper(), "fail", str(exc)))

        out_ok, out_detail = check_output_dir(config.output_dir)
        checks.append(PreflightCheck("output directory", "pass" if out_ok else "fail", out_detail))
        if out_ok:
            free = disk_free_bytes(config.output_dir)
            if free < 1_000_000_000:
                checks.append(PreflightCheck("disk space", "fail", f"{free} bytes free"))
            elif free < 5_000_000_000:
                checks.append(PreflightCheck("disk space", "warn", f"{free} bytes free", required=False))
            else:
                checks.append(PreflightCheck("disk space", "pass", f"{free} bytes free"))
            if is_onedrive_path(config.output_dir):
                checks.append(
                    PreflightCheck(
                        "OneDrive output",
                        "warn",
                        "Output directory is inside OneDrive; sync can interfere with long recordings.",
                        required=False,
                    )
                )

        if config.synthetic:
            checks.append(PreflightCheck("camera", "pass", "synthetic source"))
            checks.append(PreflightCheck("exact camera mode", "pass", "synthetic source"))
            checks.append(PreflightCheck("short camera read", "pass", "synthetic source"))
        else:
            try:
                camera = discover_camera(
                    device=config.device,
                    width=config.width,
                    height=config.height,
                    fps=config.fps,
                    backend=backend,
                )
                checks.append(PreflightCheck("camera", "pass", camera.name))
                if backend == "dshow":
                    if camera.profile == "DECXIN":
                        checks.append(
                            PreflightCheck(
                                "DECXIN profile",
                                "pass",
                                (
                                    f"{camera.name}; USB VID:PID "
                                    f"{camera.usb_vid or 'unknown'}:{camera.usb_pid or 'unknown'}; "
                                    f"REV {camera.usb_revision or 'unknown'}; MI {camera.usb_interface or 'unknown'}; "
                                    f"{camera.sensor_color}; {camera.shutter_type} shutter; "
                                    f"{DECXIN_DEFAULT_INPUT_FORMAT} "
                                    f"{DECXIN_DEFAULT_WIDTH}x{DECXIN_DEFAULT_HEIGHT}@{DECXIN_DEFAULT_FPS:g}; "
                                    f"raw {camera.raw_pixel_format}; "
                                    f"instance {camera.device_instance_path or 'unknown'}"
                                ),
                                required=False,
                            )
                        )
                    else:
                        checks.append(
                            PreflightCheck(
                                "DECXIN profile",
                                "warn",
                                f"Selected DirectShow camera is {camera.name}, not {DECXIN_CAMERA_NAME}.",
                                required=False,
                            )
                        )
                checks.append(
                    PreflightCheck(
                        "exact camera mode",
                        "pass",
                        f"{camera.preferred_input_format} {config.width}x{config.height}@{config.fps:g}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                checks.append(PreflightCheck("camera", "fail", str(exc).replace("\n", " ")))

            if camera and config.run_short_camera_read:
                ok, detail = self._short_camera_read(camera, config)
                checks.append(PreflightCheck("short camera read", "pass" if ok else "fail", detail))

        try:
            selected = select_hevc_encoder(config.encoder, backend=backend)
            selected_encoder = selected.name
            checks.append(PreflightCheck("HEVC encoder", "pass", selected.name))
        except Exception as exc:  # noqa: BLE001
            checks.append(PreflightCheck("HEVC encoder", "fail", str(exc)))

        current_offset = timezone_offset_string()
        if config.expected_timezone_offset and current_offset != config.expected_timezone_offset:
            checks.append(
                PreflightCheck(
                    "timezone",
                    "warn",
                    f"{current_offset}; expected {config.expected_timezone_offset}",
                    required=False,
                )
            )
        else:
            checks.append(PreflightCheck("timezone", "pass", current_offset, required=False))

        return PreflightResult(checks, selected_camera=camera, selected_encoder=selected_encoder)

    def start(
        self,
        config: RecorderConfig,
        status_callback: StatusCallback | None = None,
        preview_callback: PreviewCallback | None = None,
    ) -> int:
        state = RecorderState(
            stop_event=threading.Event(),
            failure_event=threading.Event(),
            stats=RecorderStats(),
        )
        self._finished.clear()
        with self._state_lock:
            self._state = state

        status_thread: threading.Thread | None = None
        if status_callback:
            status_thread = threading.Thread(
                target=self._status_poller,
                args=(config.output_dir, state, status_callback),
                daemon=True,
            )
            status_thread.start()

        try:
            with ffmpeg_path_context(config.ffmpeg_bin_dir):
                return run_recording(
                    self._namespace(config),
                    state=state,
                    preview_callback=self._preview_adapter(config, preview_callback),
                )
        finally:
            self._finished.set()
            if status_callback:
                status_callback(self._status_from_state(config.output_dir, state, "stopped"))
            with self._state_lock:
                self._state = None
            if status_thread:
                status_thread.join(timeout=1.0)

    def request_shutdown(self, reason: str) -> bool:
        state = self.state
        if not state:
            return False
        return state.request_shutdown(reason)

    def recover_partials(self, output_dir: Path, *, width: int = 1280, height: int = 720, fps: float = 30.0) -> RecoveryReport:
        return RecoveryReport(recover_partials_in_dir(output_dir, width=width, height=height, fps=fps))

    def analyze_movement(self, video_path: Path, *, width: int = 1280, height: int = 720, fps: float = 30.0) -> MovementSummary:
        return analyze_video(video_path, settings=MotionSettings(width=width, height=height, fps=fps))

    def analyze_latest_movement(
        self,
        output_dir: Path,
        *,
        width: int = 1280,
        height: int = 720,
        fps: float = 30.0,
    ) -> MovementSummary:
        videos = sorted(
            output_dir.glob("record_*.mp4"),
            key=lambda path: path.name,
            reverse=True,
        )
        if not videos:
            raise FileNotFoundError(f"No finalized recording MP4 files found in {output_dir}")
        return self.analyze_movement(videos[0], width=width, height=height, fps=fps)

    def _status_poller(self, output_dir: Path, state: RecorderState, callback: StatusCallback) -> None:
        while not self._finished.is_set():
            callback(self._status_from_state(output_dir, state, "recording"))
            self._finished.wait(0.25)

    def _status_from_state(self, output_dir: Path, state: RecorderState, state_name: str) -> RecorderStatus:
        stats = state.stats
        now_ns = high_resolution_monotonic_ns()
        elapsed = 0.0
        if stats.recording_start_monotonic_ns is not None:
            elapsed = max(0.0, (now_ns - stats.recording_start_monotonic_ns) / 1_000_000_000)
        segment_elapsed = 0.0
        if stats.current_segment_start_monotonic_ns is not None:
            segment_elapsed = max(0.0, (now_ns - stats.current_segment_start_monotonic_ns) / 1_000_000_000)
        effective_fps = stats.total_frames / elapsed if elapsed > 0 else 0.0
        try:
            free = shutil.disk_usage(output_dir).free
        except OSError:
            free = None
        return RecorderStatus(
            state=state_name,
            selected_camera=stats.selected_camera,
            selected_encoder=stats.selected_encoder,
            recording_start_time=stats.recording_start_wall_time_iso8601,
            elapsed_seconds=elapsed,
            current_segment_filename=stats.current_segment_filename,
            current_segment_elapsed_seconds=segment_elapsed,
            total_frames=stats.total_frames,
            current_queue_size=stats.current_queue_size,
            queue_high_water_mark=stats.queue_high_water_mark,
            frame_gap_count=stats.frame_gaps,
            reconnect_count=stats.reconnect_count,
            effective_capture_rate_fps=effective_fps,
            disk_free_bytes=free,
            last_validation_result=stats.last_validation_result,
            shutdown_source=state.shutdown_source,
        )

    def _preview_adapter(
        self,
        config: RecorderConfig,
        callback: PreviewCallback | None,
    ) -> Callable[[FramePacket], None] | None:
        if callback is None:
            return None

        def publish(packet: FramePacket) -> None:
            callback(
                PreviewFrame(
                    data=packet.data,
                    width=config.width,
                    height=config.height,
                    pixel_format=packet.pixel_format,
                    global_frame_index=packet.global_frame_index,
                    wall_time_unix_ns=packet.wall_time_unix_ns,
                    monotonic_ns=packet.monotonic_ns,
                )
            )

        return publish

    def _namespace(self, config: RecorderConfig) -> argparse.Namespace:
        return argparse.Namespace(
            backend=config.backend,
            device=config.device,
            output_dir=config.output_dir,
            width=config.width,
            height=config.height,
            fps=config.fps,
            segment_seconds=config.segment_seconds,
            encoder=config.encoder,
            gap_threshold_ms=config.gap_threshold_ms,
            queue_size=config.queue_size,
            synthetic=config.synthetic,
            duration_seconds=config.duration_seconds,
            skip_validation=config.skip_validation,
            keyboard_stop=config.keyboard_stop,
            stop_file=config.stop_file,
            camera_controls_config=config.camera_controls_config,
            expected_timezone_offset=config.expected_timezone_offset,
            timestamp_source=config.timestamp_source,
        )

    def _short_camera_read(self, camera: CameraDevice, config: RecorderConfig) -> tuple[bool, str]:
        command = capture_command(camera, config.width, config.height, config.fps)
        frame_bytes = frame_byte_count(config.width, config.height, camera.raw_pixel_format)
        process: subprocess.Popen[bytes] | None = None
        try:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            assert process.stdout is not None
            data = process.stdout.read(frame_bytes)
            if len(data) != frame_bytes:
                return False, f"read {len(data)} of {frame_bytes} bytes"
            return True, f"read one {config.width}x{config.height} {camera.raw_pixel_format} frame"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            if process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        process.kill()
                    except Exception:  # noqa: BLE001
                        pass
