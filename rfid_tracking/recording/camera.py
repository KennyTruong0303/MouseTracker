"""Camera discovery helpers for Windows DirectShow and Linux V4L2."""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal


LOGGER = logging.getLogger(__name__)
Runner = Callable[..., subprocess.CompletedProcess[str]]
Backend = Literal["dshow", "v4l2"]
DECXIN_CAMERA_NAME = "DECXIN CAMERA"
DECXIN_USB_VID = "1bcf"
DECXIN_USB_PID = "2cd1"
DECXIN_USB_REV = "9281"
DECXIN_USB_INTERFACE = "00"
DECXIN_HARDWARE_IDS = (
    r"USB\VID_1BCF&PID_2CD1&REV_9281&MI_00",
    r"USB\VID_1BCF&PID_2CD1&MI_00",
)
DECXIN_PARENT_DEVICE = r"USB\VID_1BCF&PID_2CD1\01.00.00"
DECXIN_BUS_REPORTED_DESCRIPTION = DECXIN_CAMERA_NAME
DECXIN_SENSOR_MODEL_HINT = "9281 monochrome global-shutter UVC sensor family"
DECXIN_DEFAULT_INPUT_FORMAT = "mjpeg"
DECXIN_DEFAULT_WIDTH = 1280
DECXIN_DEFAULT_HEIGHT = 720
DECXIN_DEFAULT_FPS = 30.0
DECXIN_SENSOR_COLOR = "monochrome"
DECXIN_SHUTTER_TYPE = "global"
DECXIN_RAW_PIXEL_FORMAT = "gray"


WSL_CAMERA_DIAGNOSTIC = """No V4L2 camera was found.

For WSL2 USB webcams, verify the device is attached to the Linux distribution:
  1. Install usbipd-win on Windows if needed.
  2. In an elevated Windows terminal, list devices with: usbipd list
  3. Attach the webcam to WSL with the appropriate usbipd bind/attach flow.
  4. Inside WSL, verify with: lsusb
  5. Inside WSL, verify video devices with: ls /dev/video*

This recorder will not run privileged Windows or USB attachment commands automatically.
"""


DSHOW_CAMERA_DIAGNOSTIC = """No DirectShow video camera was found.

On native Windows, verify the webcam is connected and visible to FFmpeg:
  ffmpeg -hide_banner -list_devices true -f dshow -i dummy

If the device is visible in Windows Camera but not FFmpeg, check camera privacy
permissions and close other applications that may be holding exclusive access.
"""


@dataclass(frozen=True)
class CameraDevice:
    backend: Backend
    path: str
    name: str
    formats_text: str
    preferred_input_format: str
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    alternative_name: str | None = None
    usb_vid: str | None = None
    usb_pid: str | None = None
    usb_revision: str | None = None
    usb_interface: str | None = None
    hardware_ids: tuple[str, ...] = ()
    device_instance_path: str | None = None
    parent_device: str | None = None
    bus_reported_description: str | None = None
    sensor_model_hint: str | None = None
    profile: str | None = None
    sensor_color: str | None = None
    shutter_type: str | None = None
    raw_pixel_format: str = "bgr24"


@dataclass(frozen=True)
class DirectShowDeviceInfo:
    name: str
    alternative_name: str | None = None
    usb_vid: str | None = None
    usb_pid: str | None = None
    usb_interface: str | None = None
    device_instance_path: str | None = None

    @property
    def is_decxin(self) -> bool:
        return (
            self.name == DECXIN_CAMERA_NAME
            and (self.usb_vid in (None, DECXIN_USB_VID))
            and (self.usb_pid in (None, DECXIN_USB_PID))
        )


@dataclass(frozen=True)
class DirectShowMode:
    input_format: str
    width: int
    height: int
    min_fps: float
    max_fps: float
    raw_line: str

    def supports(self, width: int, height: int, fps: float) -> bool:
        return self.width == width and self.height == height and self.min_fps <= fps <= self.max_fps


def resolve_backend(requested: str) -> Backend:
    if requested == "auto":
        return "dshow" if os.name == "nt" else "v4l2"
    if requested in {"dshow", "v4l2"}:
        return requested  # type: ignore[return-value]
    raise ValueError("--backend must be one of: auto, dshow, v4l2")


def require_v4l2_ctl() -> None:
    if not shutil.which("v4l2-ctl"):
        raise RuntimeError("Required tool 'v4l2-ctl' was not found on PATH")


def run_text(command: list[str], runner: Runner = subprocess.run, timeout: float = 10.0) -> str:
    completed = runner(command, check=False, text=True, capture_output=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{completed.stderr.strip()}")
    return completed.stdout


def run_ffmpeg_listing(command: list[str], runner: Runner = subprocess.run, timeout: float = 10.0) -> str:
    completed = runner(command, check=False, text=True, capture_output=True, timeout=timeout)
    return "\n".join(part for part in (completed.stdout, completed.stderr) if part)


def discover_video_paths() -> list[str]:
    return sorted(glob.glob("/dev/video*"))


def v4l2_device_name(device: str, runner: Runner = subprocess.run) -> str:
    try:
        output = run_text(["v4l2-ctl", "-d", device, "--info"], runner=runner)
    except RuntimeError:
        return Path(device).name
    for line in output.splitlines():
        if "Card type" in line:
            return line.split(":", 1)[1].strip()
    return Path(device).name


def list_v4l2_formats(device: str, runner: Runner = subprocess.run) -> str:
    return run_text(["v4l2-ctl", "-d", device, "--list-formats-ext"], runner=runner)


def _v4l2_format_blocks(formats_text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current_format = ""
    current_lines: list[str] = []
    format_re = re.compile(r"\[\d+\]:\s+'([^']+)'")
    for line in formats_text.splitlines():
        match = format_re.search(line)
        if match:
            if current_format:
                blocks.append((current_format, "\n".join(current_lines)))
            current_format = match.group(1)
            current_lines = [line]
        elif current_format:
            current_lines.append(line)
    if current_format:
        blocks.append((current_format, "\n".join(current_lines)))
    return blocks


def supports_exact_v4l2_format(
    formats_text: str,
    pixel_format: str,
    width: int,
    height: int,
    fps: float,
) -> bool:
    size_re = re.compile(rf"Size:\s+Discrete\s+{width}x{height}\b")
    fps_re = re.compile(r"\((\d+(?:\.\d+)?)\s+fps\)")
    for fmt, block in _v4l2_format_blocks(formats_text):
        if fmt != pixel_format or not size_re.search(block):
            continue
        for line in block.splitlines():
            match = fps_re.search(line)
            if match and abs(float(match.group(1)) - fps) < 0.01:
                return True
    return False


def choose_v4l2_input_format(formats_text: str, width: int, height: int, fps: float) -> str | None:
    for pixel_format in ("MJPG", "MJPEG", "YUYV", "YUYV422"):
        if supports_exact_v4l2_format(formats_text, pixel_format, width, height, fps):
            return "mjpeg" if pixel_format in {"MJPG", "MJPEG"} else "yuyv422"
    return None


def discover_v4l2_camera(
    *,
    device: str,
    width: int,
    height: int,
    fps: float,
    runner: Runner = subprocess.run,
) -> CameraDevice:
    require_v4l2_ctl()
    candidates = discover_video_paths() if device == "auto" else [device]
    if not candidates:
        raise RuntimeError(WSL_CAMERA_DIAGNOSTIC)

    rejected: list[str] = []
    for path in candidates:
        try:
            formats = list_v4l2_formats(path, runner=runner)
        except RuntimeError as exc:
            rejected.append(f"{path}: {exc}")
            continue
        input_format = choose_v4l2_input_format(formats, width, height, fps)
        if input_format:
            return CameraDevice(
                backend="v4l2",
                path=path,
                name=v4l2_device_name(path, runner=runner),
                formats_text=formats,
                preferred_input_format=input_format,
            )
        rejected.append(f"{path}: does not expose exactly {width}x{height} at {fps:g} fps")

    details = "\n".join(f"  - {item}" for item in rejected)
    raise RuntimeError(f"No V4L2 camera supports exactly {width}x{height} at {fps:g} fps.\n{details}")


def list_dshow_devices_raw(runner: Runner = subprocess.run) -> str:
    return run_ffmpeg_listing(
        ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        runner=runner,
    )


def parse_dshow_video_devices(listing_text: str) -> list[str]:
    return [device.name for device in parse_dshow_video_device_infos(listing_text)]


def dshow_alternative_to_device_instance_path(alternative_name: str | None) -> str | None:
    if not alternative_name:
        return None
    match = re.search(r"\\\\\?\\([^{}]+)#\{", alternative_name, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).replace("#", "\\").upper()


def parse_dshow_video_device_infos(listing_text: str) -> list[DirectShowDeviceInfo]:
    devices: list[DirectShowDeviceInfo] = []
    in_video_section = False
    current_name: str | None = None

    def append_current_without_alternative() -> None:
        nonlocal current_name
        if current_name is not None:
            devices.append(DirectShowDeviceInfo(name=current_name))
            current_name = None

    for line in listing_text.splitlines():
        lowered = line.lower()
        if "directshow video devices" in lowered:
            in_video_section = True
            continue
        if "directshow audio devices" in lowered:
            append_current_without_alternative()
            in_video_section = False
            continue

        match = re.search(r'"([^"]+)"', line)
        if match and "(video)" in lowered:
            append_current_without_alternative()
            current_name = match.group(1)
            in_video_section = True
            continue
        if match and ("(audio)" in lowered or "(none)" in lowered):
            append_current_without_alternative()
            in_video_section = False
            continue

        if "alternative name" in lowered:
            if current_name is not None:
                alt_match = re.search(r'"([^"]+)"', line)
                alternative_name = alt_match.group(1) if alt_match else None
                vid_match = re.search(r"vid_([0-9a-f]{4})", lowered)
                pid_match = re.search(r"pid_([0-9a-f]{4})", lowered)
                mi_match = re.search(r"mi_([0-9a-f]{2})", lowered)
                devices.append(
                    DirectShowDeviceInfo(
                        name=current_name,
                        alternative_name=alternative_name,
                        usb_vid=vid_match.group(1) if vid_match else None,
                        usb_pid=pid_match.group(1) if pid_match else None,
                        usb_interface=mi_match.group(1) if mi_match else None,
                        device_instance_path=dshow_alternative_to_device_instance_path(alternative_name),
                    )
                )
                current_name = None
            continue

        if not in_video_section:
            continue
        if match:
            append_current_without_alternative()
            current_name = match.group(1)
    append_current_without_alternative()
    return devices


def decxin_device_info(devices: list[DirectShowDeviceInfo]) -> DirectShowDeviceInfo | None:
    for device in devices:
        if device.is_decxin:
            return device
    for device in devices:
        if device.name == DECXIN_CAMERA_NAME:
            return device
    return None


def list_dshow_options(camera_name: str, runner: Runner = subprocess.run) -> str:
    return run_ffmpeg_listing(
        [
            "ffmpeg",
            "-hide_banner",
            "-list_options",
            "true",
            "-f",
            "dshow",
            "-i",
            f"video={camera_name}",
        ],
        runner=runner,
    )


def _fps_values_from_dshow_line(line: str) -> list[float]:
    values: list[float] = []
    for match in re.finditer(r"(?:fps=|fps: )(\d+(?:\.\d+)?)", line):
        values.append(float(match.group(1)))
    return values


def _dshow_line_has_exact_mode(line: str, width: int, height: int, fps: float) -> bool:
    range_match = re.search(
        r"min\s+s=(\d+)x(\d+)\s+fps=(\d+(?:\.\d+)?)\s+max\s+s=(\d+)x(\d+)\s+fps=(\d+(?:\.\d+)?)",
        line,
    )
    if range_match:
        min_width = int(range_match.group(1))
        min_height = int(range_match.group(2))
        min_fps = float(range_match.group(3))
        max_width = int(range_match.group(4))
        max_height = int(range_match.group(5))
        max_fps = float(range_match.group(6))
        return (
            min_width == width
            and max_width == width
            and min_height == height
            and max_height == height
            and min_fps <= fps <= max_fps
        )

    size = f"{width}x{height}"
    if size not in line:
        return False
    values = _fps_values_from_dshow_line(line)
    return any(abs(value - fps) < 0.01 for value in values)


def parse_dshow_modes(options_text: str) -> list[DirectShowMode]:
    modes: list[DirectShowMode] = []
    range_re = re.compile(
        r"(?:vcodec=(mjpeg|mjpg)|pixel_format=([a-z0-9]+))\s+"
        r"min\s+s=(\d+)x(\d+)\s+fps=(\d+(?:\.\d+)?)\s+"
        r"max\s+s=(\d+)x(\d+)\s+fps=(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    for line in options_text.splitlines():
        match = range_re.search(line)
        if not match:
            continue
        codec = match.group(1)
        pixel_format = match.group(2)
        min_width = int(match.group(3))
        min_height = int(match.group(4))
        min_fps = float(match.group(5))
        max_width = int(match.group(6))
        max_height = int(match.group(7))
        max_fps = float(match.group(8))
        if min_width != max_width or min_height != max_height:
            continue
        input_format = "mjpeg" if codec else pixel_format.lower()
        modes.append(
            DirectShowMode(
                input_format=input_format,
                width=min_width,
                height=min_height,
                min_fps=min_fps,
                max_fps=max_fps,
                raw_line=line.strip(),
            )
        )
    return modes


def decxin_mode_report(options_text: str) -> str:
    modes = parse_dshow_modes(options_text)
    lines = [
        f"{mode.input_format} {mode.width}x{mode.height} {mode.min_fps:g}-{mode.max_fps:g} fps"
        for mode in modes
    ]
    return "\n".join(lines)


def supports_exact_dshow_format(
    options_text: str,
    input_format: str,
    width: int,
    height: int,
    fps: float,
) -> bool:
    lowered_format = input_format.lower()
    for mode in parse_dshow_modes(options_text):
        if mode.input_format == lowered_format and mode.supports(width, height, fps):
            return True
    for line in options_text.splitlines():
        lowered = line.lower()
        if not _dshow_line_has_exact_mode(lowered, width, height, fps):
            continue
        if lowered_format == "mjpeg" and any(token in lowered for token in ("vcodec=mjpeg", "vcodec=mjpg")):
            return True
        if lowered_format == "yuyv422" and any(
            token in lowered for token in ("pixel_format=yuyv422", "pixel_format=yuy2")
        ):
            return True
    return False


def choose_dshow_input_format(options_text: str, width: int, height: int, fps: float) -> str | None:
    for input_format in ("mjpeg", "yuyv422"):
        if supports_exact_dshow_format(options_text, input_format, width, height, fps):
            return input_format
    return None


def discover_dshow_camera(
    *,
    device: str,
    width: int,
    height: int,
    fps: float,
    runner: Runner = subprocess.run,
) -> CameraDevice:
    device_infos = parse_dshow_video_device_infos(list_dshow_devices_raw(runner=runner))
    device_info_by_name = {info.name: info for info in device_infos}
    if device == "auto":
        preferred = decxin_device_info(device_infos)
        candidates = [preferred.name] if preferred else []
        if not candidates:
            raise RuntimeError(
                f"{DECXIN_CAMERA_NAME} was not found. Connected DirectShow cameras: "
                + (", ".join(info.name for info in device_infos) or "none")
            )
    else:
        candidates = [device]
    if not candidates:
        raise RuntimeError(DSHOW_CAMERA_DIAGNOSTIC)

    rejected: list[str] = []
    for name in candidates:
        options = list_dshow_options(name, runner=runner)
        input_format = choose_dshow_input_format(options, width, height, fps)
        if input_format:
            info = device_info_by_name.get(name)
            return CameraDevice(
                backend="dshow",
                path=name,
                name=name,
                formats_text=options,
                preferred_input_format=input_format,
                width=width,
                height=height,
                fps=fps,
                alternative_name=info.alternative_name if info else None,
                usb_vid=info.usb_vid if info else None,
                usb_pid=info.usb_pid if info else None,
                usb_revision=DECXIN_USB_REV if info and info.is_decxin else None,
                usb_interface=(info.usb_interface or DECXIN_USB_INTERFACE) if info and info.is_decxin else None,
                hardware_ids=DECXIN_HARDWARE_IDS if info and info.is_decxin else (),
                device_instance_path=info.device_instance_path if info else None,
                parent_device=DECXIN_PARENT_DEVICE if info and info.is_decxin else None,
                bus_reported_description=DECXIN_BUS_REPORTED_DESCRIPTION if info and info.is_decxin else None,
                sensor_model_hint=DECXIN_SENSOR_MODEL_HINT if info and info.is_decxin else None,
                profile="DECXIN" if info and info.is_decxin else None,
                sensor_color=DECXIN_SENSOR_COLOR if info and info.is_decxin else None,
                shutter_type=DECXIN_SHUTTER_TYPE if info and info.is_decxin else None,
                raw_pixel_format=DECXIN_RAW_PIXEL_FORMAT if info and info.is_decxin else "bgr24",
            )
        rejected.append(f"{name}: does not expose exactly {width}x{height} at {fps:g} fps")

    details = "\n".join(f"  - {item}" for item in rejected)
    raise RuntimeError(f"No DirectShow camera supports exactly {width}x{height} at {fps:g} fps.\n{details}")


def discover_camera(
    *,
    device: str,
    width: int,
    height: int,
    fps: float,
    backend: str = "auto",
    runner: Runner = subprocess.run,
) -> CameraDevice:
    resolved = resolve_backend(backend)
    if resolved == "dshow":
        return discover_dshow_camera(device=device, width=width, height=height, fps=fps, runner=runner)
    return discover_v4l2_camera(device=device, width=width, height=height, fps=fps, runner=runner)


def list_camera_devices(
    width: int,
    height: int,
    fps: float,
    *,
    backend: str = "auto",
    runner: Runner = subprocess.run,
) -> str:
    resolved = resolve_backend(backend)
    if resolved == "dshow":
        devices = parse_dshow_video_devices(list_dshow_devices_raw(runner=runner))
        if not devices:
            return DSHOW_CAMERA_DIAGNOSTIC
        lines: list[str] = []
        for name in devices:
            options = list_dshow_options(name, runner=runner)
            chosen = choose_dshow_input_format(options, width, height, fps)
            status = f"usable ({chosen})" if chosen else f"no exact {width}x{height}@{fps:g}"
            lines.append(f"{name}: {status}")
        return "\n".join(lines)

    require_v4l2_ctl()
    paths = discover_video_paths()
    if not paths:
        return WSL_CAMERA_DIAGNOSTIC
    lines = []
    for path in paths:
        try:
            formats = list_v4l2_formats(path, runner=runner)
            chosen = choose_v4l2_input_format(formats, width, height, fps)
            status = f"usable ({chosen})" if chosen else f"no exact {width}x{height}@{fps:g}"
            lines.append(f"{path}: {v4l2_device_name(path, runner=runner)} - {status}")
        except RuntimeError as exc:
            lines.append(f"{path}: unavailable - {exc}")
    return "\n".join(lines)


def list_controls(device: str, *, backend: str = "auto", runner: Runner = subprocess.run) -> str:
    resolved = resolve_backend(backend)
    if resolved == "dshow":
        return list_dshow_options(device, runner=runner)
    require_v4l2_ctl()
    return run_text(["v4l2-ctl", "-d", device, "--list-ctrls"], runner=runner)


def parse_control_names(controls_text: str) -> set[str]:
    names: set[str] = set()
    for line in controls_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("User Controls"):
            continue
        match = re.match(r"([a-zA-Z0-9_]+)\s+", stripped)
        if match:
            names.add(match.group(1))
    return names


def warn_about_auto_controls(controls_text: str) -> list[str]:
    warnings: list[str] = []
    lowered = controls_text.lower()
    checks = {
        "auto exposure": ("exposure_auto", "auto"),
        "auto white balance": ("white_balance_temperature_auto", "1"),
        "autofocus": ("focus_auto", "1"),
    }
    for label, (name, active_hint) in checks.items():
        pattern = re.compile(rf"{re.escape(name)}.*value=([^\s]+)")
        match = pattern.search(lowered)
        if match and active_hint in match.group(1):
            warnings.append(f"{label} appears active ({name} value={match.group(1)})")
    return warnings


def load_control_config(path: str | None) -> dict[str, str | int | float | bool]:
    if not path:
        return {}
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError("Camera control config must be a JSON object")
    return loaded


def apply_controls(
    device: str,
    requested: dict[str, str | int | float | bool],
    *,
    backend: str = "auto",
    runner: Runner = subprocess.run,
) -> None:
    if not requested:
        return
    resolved = resolve_backend(backend)
    if resolved == "dshow":
        raise RuntimeError("Applying camera controls is only implemented for the V4L2 backend")
    controls = list_controls(device, backend=resolved, runner=runner)
    supported = parse_control_names(controls)
    unsupported = sorted(set(requested) - supported)
    if unsupported:
        raise RuntimeError(f"Requested unsupported camera controls: {', '.join(unsupported)}")
    for name, value in requested.items():
        command = ["v4l2-ctl", "-d", device, f"--set-ctrl={name}={value}"]
        run_text(command, runner=runner)
        LOGGER.info("Applied camera control %s=%s", name, value)
