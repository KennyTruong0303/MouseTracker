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
    devices: list[str] = []
    in_video_section = False
    for line in listing_text.splitlines():
        lowered = line.lower()
        if "directshow video devices" in lowered:
            in_video_section = True
            continue
        if "directshow audio devices" in lowered:
            in_video_section = False
            continue
        if not in_video_section or "alternative name" in lowered:
            continue
        match = re.search(r'"([^"]+)"', line)
        if match:
            devices.append(match.group(1))
    return devices


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
    size = f"{width}x{height}"
    if size not in line:
        return False
    values = _fps_values_from_dshow_line(line)
    return any(abs(value - fps) < 0.01 for value in values)


def supports_exact_dshow_format(
    options_text: str,
    input_format: str,
    width: int,
    height: int,
    fps: float,
) -> bool:
    lowered_format = input_format.lower()
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
    candidates = parse_dshow_video_devices(list_dshow_devices_raw(runner=runner)) if device == "auto" else [device]
    if not candidates:
        raise RuntimeError(DSHOW_CAMERA_DIAGNOSTIC)

    rejected: list[str] = []
    for name in candidates:
        options = list_dshow_options(name, runner=runner)
        input_format = choose_dshow_input_format(options, width, height, fps)
        if input_format:
            return CameraDevice(
                backend="dshow",
                path=name,
                name=name,
                formats_text=options,
                preferred_input_format=input_format,
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

