"""FFprobe validation for completed recording segments."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .timestamps import count_csv_rows


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SegmentValidation:
    ok: bool
    video_path: Path
    csv_path: Path
    codec: str | None
    width: int | None
    height: int | None
    frame_rate: str | None
    video_frames: int | None
    csv_rows: int
    errors: list[str]


def _ffprobe_json(video_path: Path, runner: Runner = subprocess.run) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,nb_read_frames,nb_frames",
        "-of",
        "json",
        str(video_path),
    ]
    completed = runner(command, check=False, text=True, capture_output=True, timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffprobe failed")
    return json.loads(completed.stdout)


def _parse_frame_count(stream: dict) -> int | None:
    for key in ("nb_read_frames", "nb_frames"):
        value = stream.get(key)
        if value not in (None, "N/A"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def validate_segment(
    video_path: Path,
    csv_path: Path,
    *,
    width: int,
    height: int,
    fps: float,
    runner: Runner = subprocess.run,
) -> SegmentValidation:
    errors: list[str] = []
    codec = None
    actual_width = None
    actual_height = None
    frame_rate = None
    video_frames = None
    csv_rows = count_csv_rows(csv_path) if csv_path.exists() else 0
    try:
        data = _ffprobe_json(video_path, runner=runner)
        streams = data.get("streams", [])
        if not streams:
            errors.append("ffprobe found no video stream")
        else:
            stream = streams[0]
            codec = stream.get("codec_name")
            actual_width = stream.get("width")
            actual_height = stream.get("height")
            frame_rate = stream.get("avg_frame_rate")
            video_frames = _parse_frame_count(stream)
    except Exception as exc:  # noqa: BLE001 - preserve diagnostic text in logs.
        errors.append(f"ffprobe failed: {exc}")

    if codec not in {"hevc", "h265"}:
        errors.append(f"codec is {codec!r}, expected HEVC")
    if actual_width != width:
        errors.append(f"width is {actual_width}, expected {width}")
    if actual_height != height:
        errors.append(f"height is {actual_height}, expected {height}")
    if frame_rate and "/" in frame_rate:
        num, den = frame_rate.split("/", 1)
        try:
            actual_fps = float(num) / float(den)
            if abs(actual_fps - fps) > 0.1:
                errors.append(f"frame rate is {actual_fps:g}, expected {fps:g}")
        except (ValueError, ZeroDivisionError):
            errors.append(f"could not parse frame rate {frame_rate!r}")
    if video_frames is not None and video_frames != csv_rows:
        errors.append(f"video frame count {video_frames} != CSV row count {csv_rows}")

    result = SegmentValidation(
        ok=not errors,
        video_path=video_path,
        csv_path=csv_path,
        codec=codec,
        width=actual_width,
        height=actual_height,
        frame_rate=frame_rate,
        video_frames=video_frames,
        csv_rows=csv_rows,
        errors=errors,
    )
    if not result.ok:
        marker = video_path.with_suffix(video_path.suffix + ".invalid")
        marker.write_text("\n".join(errors) + "\n", encoding="utf-8")
    return result

