"""V4L2 camera discovery and control helpers for WSL2."""

from __future__ import annotations

import glob
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


LOGGER = logging.getLogger(__name__)
Runner = Callable[..., subprocess.CompletedProcess[str]]


WSL_CAMERA_DIAGNOSTIC = """No V4L2 camera was found.

For WSL2 USB webcams, verify the device is attached to the Linux distribution:
  1. Install usbipd-win on Windows if needed.
  2. In an elevated Windows terminal, list devices with: usbipd list
  3. Attach the webcam to WSL with the appropriate usbipd bind/attach flow.
  4. Inside WSL, verify with: lsusb
  5. Inside WSL, verify video devices with: ls /dev/video*

This recorder will not run privileged Windows or USB attachment commands automatically.
"""


@dataclass(frozen=True)
class CameraDevice:
    path: str
    name: str
    formats_text: str
    preferred_input_format: str


def require_v4l2_ctl() -> None:
    if not shutil.which("v4l2-ctl"):
        raise RuntimeError("Required tool 'v4l2-ctl' was not found on PATH")


def run_text(command: list[str], runner: Runner = subprocess.run, timeout: float = 10.0) -> str:
    completed = runner(command, check=False, text=True, capture_output=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{completed.stderr.strip()}")
    return completed.stdout


def discover_video_paths() -> list[str]:
    return sorted(glob.glob("/dev/video*"))


def device_name(device: str, runner: Runner = subprocess.run) -> str:
    try:
        output = run_text(["v4l2-ctl", "-d", device, "--info"], runner=runner)
    except RuntimeError:
        return Path(device).name
    for line in output.splitlines():
        if "Card type" in line:
            return line.split(":", 1)[1].strip()
    return Path(device).name


def list_formats(device: str, runner: Runner = subprocess.run) -> str:
    return run_text(["v4l2-ctl", "-d", device, "--list-formats-ext"], runner=runner)


def _format_blocks(formats_text: str) -> list[tuple[str, str]]:
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


def supports_exact_format(formats_text: str, pixel_format: str, width: int, height: int, fps: float) -> bool:
    size_re = re.compile(rf"Size:\s+Discrete\s+{width}x{height}\b")
    fps_re = re.compile(r"\((\d+(?:\.\d+)?)\s+fps\)")
    for fmt, block in _format_blocks(formats_text):
        if fmt != pixel_format or not size_re.search(block):
            continue
        for line in block.splitlines():
            match = fps_re.search(line)
            if match and abs(float(match.group(1)) - fps) < 0.01:
                return True
    return False


def choose_input_format(formats_text: str, width: int, height: int, fps: float) -> str | None:
    for pixel_format in ("MJPG", "MJPEG", "YUYV", "YUYV422"):
        if supports_exact_format(formats_text, pixel_format, width, height, fps):
            return "mjpeg" if pixel_format in {"MJPG", "MJPEG"} else "yuyv422"
    return None


def discover_camera(
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
            formats = list_formats(path, runner=runner)
        except RuntimeError as exc:
            rejected.append(f"{path}: {exc}")
            continue
        input_format = choose_input_format(formats, width, height, fps)
        if input_format:
            return CameraDevice(
                path=path,
                name=device_name(path, runner=runner),
                formats_text=formats,
                preferred_input_format=input_format,
            )
        rejected.append(f"{path}: does not expose exactly {width}x{height} at {fps:g} fps")

    details = "\n".join(f"  - {item}" for item in rejected)
    raise RuntimeError(f"No camera supports exactly {width}x{height} at {fps:g} fps.\n{details}")


def list_camera_devices(width: int, height: int, fps: float, runner: Runner = subprocess.run) -> str:
    require_v4l2_ctl()
    paths = discover_video_paths()
    if not paths:
        return WSL_CAMERA_DIAGNOSTIC
    lines: list[str] = []
    for path in paths:
        try:
            formats = list_formats(path, runner=runner)
            chosen = choose_input_format(formats, width, height, fps)
            status = f"usable ({chosen})" if chosen else f"no exact {width}x{height}@{fps:g}"
            lines.append(f"{path}: {device_name(path, runner=runner)} - {status}")
        except RuntimeError as exc:
            lines.append(f"{path}: unavailable - {exc}")
    return "\n".join(lines)


def list_controls(device: str, runner: Runner = subprocess.run) -> str:
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
    runner: Runner = subprocess.run,
) -> None:
    if not requested:
        return
    controls = list_controls(device, runner=runner)
    supported = parse_control_names(controls)
    unsupported = sorted(set(requested) - supported)
    if unsupported:
        raise RuntimeError(f"Requested unsupported camera controls: {', '.join(unsupported)}")
    for name, value in requested.items():
        command = ["v4l2-ctl", "-d", device, f"--set-ctrl={name}={value}"]
        run_text(command, runner=runner)
        LOGGER.info("Applied camera control %s=%s", name, value)

