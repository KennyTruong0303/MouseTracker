"""Frame-by-frame movement analysis for MouseTracker recordings.

This module intentionally starts with conservative image processing. It does
not try to infer social behavior by itself; it turns synchronized MP4/CSV
recordings into objective per-frame movement measurements that later CSDS
analysis can build on.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator

from rfid_tracking.recording.timestamps import iter_csv_rows, matching_timestamp_name


MOVEMENT_COLUMNS = [
    "segment_filename",
    "global_frame_index",
    "segment_frame_index",
    "wall_time_iso8601",
    "wall_time_unix_ns",
    "monotonic_ns",
    "elapsed_from_recording_start_s",
    "elapsed_from_segment_start_s",
    "inter_frame_interval_ms",
    "motion_pixels",
    "motion_index",
    "mean_absdiff",
    "centroid_x",
    "centroid_y",
    "bbox_x1",
    "bbox_y1",
    "bbox_x2",
    "bbox_y2",
    "motion_zone",
    "edge_sharpness",
    "immobile_flag",
    "blur_risk_flag",
]


@dataclass(frozen=True)
class MotionSettings:
    width: int = 1280
    height: int = 720
    fps: float = 30.0
    diff_threshold: int = 18
    immobile_motion_index_threshold: float = 0.002
    blur_motion_index_threshold: float = 0.01
    blur_edge_sharpness_threshold: float = 6.0
    edge_stride: int = 8


@dataclass(frozen=True)
class MovementSummary:
    video_path: str
    timestamp_csv: str
    movement_csv: str
    summary_json: str
    width: int
    height: int
    fps: float
    video_frames_read: int
    timestamp_rows: int
    analyzed_rows: int
    frame_count_match: bool
    movement_frames: int
    immobile_frames: int
    blur_risk_frames: int
    average_motion_index: float
    max_motion_index: float
    warnings: list[str]


def movement_csv_path(video_path: Path) -> Path:
    return video_path.with_name(f"{video_path.stem}_movement.csv")


def movement_summary_path(video_path: Path) -> Path:
    return video_path.with_name(f"{video_path.stem}_movement_summary.json")


def timestamp_csv_for_video(video_path: Path) -> Path:
    return video_path.with_name(matching_timestamp_name(video_path.name))


def ffmpeg_gray_command(video_path: Path, *, width: int, height: int) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-an",
        "-sn",
        "-vf",
        f"scale={width}:{height}",
        "-pix_fmt",
        "gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ]


def iter_gray_frames_ffmpeg(video_path: Path, settings: MotionSettings) -> Iterator[bytes]:
    frame_size = settings.width * settings.height
    process = subprocess.Popen(
        ffmpeg_gray_command(video_path, width=settings.width, height=settings.height),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        while True:
            data = process.stdout.read(frame_size)
            if not data:
                break
            if len(data) != frame_size:
                raise RuntimeError(f"Incomplete decoded frame: {len(data)} of {frame_size} bytes")
            yield data
    finally:
        stderr = b""
        if process.stderr is not None:
            stderr = process.stderr.read()
        returncode = process.wait(timeout=30)
        if returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "ffmpeg decode failed")


def edge_sharpness(frame: bytes, width: int, height: int, stride: int) -> float:
    if width <= stride or height <= stride:
        return 0.0
    total = 0
    count = 0
    for y in range(stride, height, stride):
        row = y * width
        previous_row = (y - stride) * width
        for x in range(stride, width, stride):
            value = frame[row + x]
            total += abs(value - frame[row + x - stride])
            total += abs(value - frame[previous_row + x])
            count += 2
    return total / count if count else 0.0


def motion_zone(centroid_x: float | None, width: int) -> str:
    if centroid_x is None:
        return ""
    third = width / 3.0
    if centroid_x < third:
        return "left"
    if centroid_x > third * 2:
        return "right"
    return "center"


def motion_metrics(
    previous: bytes | None,
    current: bytes,
    *,
    settings: MotionSettings,
) -> dict[str, object]:
    sharpness = edge_sharpness(current, settings.width, settings.height, settings.edge_stride)
    if previous is None:
        return {
            "motion_pixels": 0,
            "motion_index": 0.0,
            "mean_absdiff": 0.0,
            "centroid_x": "",
            "centroid_y": "",
            "bbox_x1": "",
            "bbox_y1": "",
            "bbox_x2": "",
            "bbox_y2": "",
            "motion_zone": "",
            "edge_sharpness": sharpness,
            "immobile_flag": True,
            "blur_risk_flag": False,
        }

    width = settings.width
    height = settings.height
    frame_pixels = width * height
    threshold = settings.diff_threshold
    motion_pixels = 0
    absdiff_sum = 0
    sum_x = 0
    sum_y = 0
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1

    for y in range(height):
        row = y * width
        for x in range(width):
            index = row + x
            diff = abs(current[index] - previous[index])
            absdiff_sum += diff
            if diff >= threshold:
                motion_pixels += 1
                sum_x += x
                sum_y += y
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y

    motion_index = motion_pixels / frame_pixels
    centroid_x: float | None = None
    centroid_y: float | None = None
    if motion_pixels:
        centroid_x = sum_x / motion_pixels
        centroid_y = sum_y / motion_pixels
    immobile = motion_index < settings.immobile_motion_index_threshold
    blur_risk = (
        motion_index >= settings.blur_motion_index_threshold
        and sharpness < settings.blur_edge_sharpness_threshold
    )
    return {
        "motion_pixels": motion_pixels,
        "motion_index": motion_index,
        "mean_absdiff": absdiff_sum / frame_pixels,
        "centroid_x": "" if centroid_x is None else centroid_x,
        "centroid_y": "" if centroid_y is None else centroid_y,
        "bbox_x1": "" if motion_pixels == 0 else min_x,
        "bbox_y1": "" if motion_pixels == 0 else min_y,
        "bbox_x2": "" if motion_pixels == 0 else max_x,
        "bbox_y2": "" if motion_pixels == 0 else max_y,
        "motion_zone": motion_zone(centroid_x, width),
        "edge_sharpness": sharpness,
        "immobile_flag": immobile,
        "blur_risk_flag": blur_risk,
    }


def _timestamp_field(row: dict[str, str], key: str) -> str:
    return row.get(key, "")


def movement_rows(
    frames: Iterable[bytes],
    timestamp_rows: list[dict[str, str]],
    *,
    settings: MotionSettings,
) -> Iterator[dict[str, object]]:
    previous: bytes | None = None
    for index, frame in enumerate(frames):
        timestamp = timestamp_rows[index] if index < len(timestamp_rows) else {}
        metrics = motion_metrics(previous, frame, settings=settings)
        previous = frame
        yield {
            "segment_filename": _timestamp_field(timestamp, "segment_filename"),
            "global_frame_index": _timestamp_field(timestamp, "global_frame_index") or index,
            "segment_frame_index": _timestamp_field(timestamp, "segment_frame_index") or index,
            "wall_time_iso8601": _timestamp_field(timestamp, "wall_time_iso8601"),
            "wall_time_unix_ns": _timestamp_field(timestamp, "wall_time_unix_ns"),
            "monotonic_ns": _timestamp_field(timestamp, "monotonic_ns"),
            "elapsed_from_recording_start_s": _timestamp_field(timestamp, "elapsed_from_recording_start_s"),
            "elapsed_from_segment_start_s": _timestamp_field(timestamp, "elapsed_from_segment_start_s"),
            "inter_frame_interval_ms": _timestamp_field(timestamp, "inter_frame_interval_ms"),
            **metrics,
        }


def analyze_frames(
    *,
    video_path: Path,
    timestamp_csv: Path,
    output_csv: Path,
    summary_json: Path,
    frames: Iterable[bytes],
    settings: MotionSettings,
) -> MovementSummary:
    timestamp_rows = list(iter_csv_rows(timestamp_csv))
    analyzed_rows = 0
    movement_frames = 0
    immobile_frames = 0
    blur_risk_frames = 0
    motion_index_sum = 0.0
    max_motion_index = 0.0

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MOVEMENT_COLUMNS)
        writer.writeheader()
        for row in movement_rows(frames, timestamp_rows, settings=settings):
            motion_index_value = float(row["motion_index"])
            motion_index_sum += motion_index_value
            max_motion_index = max(max_motion_index, motion_index_value)
            if not row["immobile_flag"]:
                movement_frames += 1
            else:
                immobile_frames += 1
            if row["blur_risk_flag"]:
                blur_risk_frames += 1
            writer.writerow(row)
            analyzed_rows += 1

    warnings: list[str] = []
    if analyzed_rows != len(timestamp_rows):
        warnings.append(f"decoded frame count {analyzed_rows} != timestamp rows {len(timestamp_rows)}")
    if blur_risk_frames:
        warnings.append(f"{blur_risk_frames} frames had motion blur risk")
    summary = MovementSummary(
        video_path=str(video_path),
        timestamp_csv=str(timestamp_csv),
        movement_csv=str(output_csv),
        summary_json=str(summary_json),
        width=settings.width,
        height=settings.height,
        fps=settings.fps,
        video_frames_read=analyzed_rows,
        timestamp_rows=len(timestamp_rows),
        analyzed_rows=analyzed_rows,
        frame_count_match=analyzed_rows == len(timestamp_rows),
        movement_frames=movement_frames,
        immobile_frames=immobile_frames,
        blur_risk_frames=blur_risk_frames,
        average_motion_index=motion_index_sum / analyzed_rows if analyzed_rows else 0.0,
        max_motion_index=max_motion_index,
        warnings=warnings,
    )
    summary_json.write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
    return summary


def analyze_video(
    video_path: Path,
    *,
    timestamp_csv: Path | None = None,
    output_csv: Path | None = None,
    summary_json: Path | None = None,
    settings: MotionSettings = MotionSettings(),
) -> MovementSummary:
    timestamp_path = timestamp_csv or timestamp_csv_for_video(video_path)
    if not timestamp_path.exists():
        raise FileNotFoundError(f"Timestamp CSV not found: {timestamp_path}")
    return analyze_frames(
        video_path=video_path,
        timestamp_csv=timestamp_path,
        output_csv=output_csv or movement_csv_path(video_path),
        summary_json=summary_json or movement_summary_path(video_path),
        frames=iter_gray_frames_ffmpeg(video_path, settings),
        settings=settings,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--timestamps", type=Path)
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--diff-threshold", type=int, default=18)
    parser.add_argument("--immobile-motion-index-threshold", type=float, default=0.002)
    parser.add_argument("--blur-motion-index-threshold", type=float, default=0.01)
    parser.add_argument("--blur-edge-sharpness-threshold", type=float, default=6.0)
    parser.add_argument("--edge-stride", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = MotionSettings(
        width=args.width,
        height=args.height,
        fps=args.fps,
        diff_threshold=args.diff_threshold,
        immobile_motion_index_threshold=args.immobile_motion_index_threshold,
        blur_motion_index_threshold=args.blur_motion_index_threshold,
        blur_edge_sharpness_threshold=args.blur_edge_sharpness_threshold,
        edge_stride=args.edge_stride,
    )
    try:
        summary = analyze_video(
            args.video,
            timestamp_csv=args.timestamps,
            output_csv=args.output_csv,
            summary_json=args.summary_json,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Movement CSV: {summary.movement_csv}")
    print(f"Summary JSON: {summary.summary_json}")
    print(
        "Frames: "
        f"{summary.video_frames_read} video / {summary.timestamp_rows} timestamp rows; "
        f"match={summary.frame_count_match}"
    )
    print(
        "Movement: "
        f"{summary.movement_frames} moving frames, {summary.immobile_frames} immobile frames, "
        f"max motion index={summary.max_motion_index:.6f}"
    )
    if summary.warnings:
        print("Warnings: " + "; ".join(summary.warnings))
    return 0 if summary.frame_count_match else 2


if __name__ == "__main__":
    raise SystemExit(main())
