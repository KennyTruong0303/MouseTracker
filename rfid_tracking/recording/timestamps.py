"""Timestamp and segment bookkeeping for video recordings."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


EXPECTED_FPS = 30.0
EXPECTED_INTERVAL_MS = 1000.0 / EXPECTED_FPS
CSV_COLUMNS = [
    "segment_filename",
    "global_frame_index",
    "segment_frame_index",
    "wall_time_iso8601",
    "wall_time_unix_ns",
    "monotonic_ns",
    "elapsed_from_recording_start_s",
    "elapsed_from_segment_start_s",
    "inter_frame_interval_ms",
    "expected_interval_ms",
    "frame_gap_ms",
    "gap_flag",
]


@dataclass(frozen=True)
class FramePacket:
    """One decoded frame plus the two clocks captured with it."""

    data: bytes
    global_frame_index: int
    wall_time_unix_ns: int
    monotonic_ns: int


@dataclass(frozen=True)
class SegmentPaths:
    """Final and temporary paths for one segment."""

    started_at_ns: int
    stem: str
    final_video: Path
    final_csv: Path
    part_video: Path
    part_csv: Path

    @property
    def segment_filename(self) -> str:
        return self.final_video.name


def local_datetime_from_ns(wall_time_unix_ns: int) -> datetime:
    return datetime.fromtimestamp(wall_time_unix_ns / 1_000_000_000).astimezone()


def segment_stamp(wall_time_unix_ns: int) -> str:
    return local_datetime_from_ns(wall_time_unix_ns).strftime("%Y%m%d_%H%M%S")


def segment_paths(output_dir: Path, wall_time_unix_ns: int) -> SegmentPaths:
    stamp = segment_stamp(wall_time_unix_ns)
    stem = f"record_{stamp}"
    return SegmentPaths(
        started_at_ns=wall_time_unix_ns,
        stem=stem,
        final_video=output_dir / f"{stem}.mp4",
        final_csv=output_dir / f"{stem}_timestamps.csv",
        part_video=output_dir / f"{stem}.part.mp4",
        part_csv=output_dir / f"{stem}_timestamps.part.csv",
    )


def matching_timestamp_name(video_filename: str) -> str:
    if not video_filename.endswith(".mp4"):
        raise ValueError(f"Expected an .mp4 filename, got {video_filename!r}")
    return f"{video_filename[:-4]}_timestamps.csv"


def ns_to_seconds(delta_ns: int) -> float:
    return delta_ns / 1_000_000_000.0


def ns_to_ms(delta_ns: int) -> float:
    return delta_ns / 1_000_000.0


def timestamp_row(
    packet: FramePacket,
    *,
    segment_filename: str,
    segment_frame_index: int,
    recording_start_monotonic_ns: int,
    segment_start_monotonic_ns: int,
    previous_frame_monotonic_ns: int | None,
    expected_interval_ms: float,
    gap_threshold_ms: float,
) -> dict[str, str | int | float | bool]:
    if previous_frame_monotonic_ns is None:
        interval_ms = ""
        frame_gap_ms = ""
        gap_flag = False
    else:
        interval_ms = ns_to_ms(packet.monotonic_ns - previous_frame_monotonic_ns)
        frame_gap_ms = interval_ms - expected_interval_ms
        gap_flag = interval_ms > gap_threshold_ms

    return {
        "segment_filename": segment_filename,
        "global_frame_index": packet.global_frame_index,
        "segment_frame_index": segment_frame_index,
        "wall_time_iso8601": local_datetime_from_ns(packet.wall_time_unix_ns).isoformat(),
        "wall_time_unix_ns": packet.wall_time_unix_ns,
        "monotonic_ns": packet.monotonic_ns,
        "elapsed_from_recording_start_s": ns_to_seconds(
            packet.monotonic_ns - recording_start_monotonic_ns
        ),
        "elapsed_from_segment_start_s": ns_to_seconds(
            packet.monotonic_ns - segment_start_monotonic_ns
        ),
        "inter_frame_interval_ms": interval_ms,
        "expected_interval_ms": expected_interval_ms,
        "frame_gap_ms": frame_gap_ms,
        "gap_flag": gap_flag,
    }


class TimestampCsvWriter:
    """CSV writer that flushes periodically and atomically finalizes files."""

    def __init__(self, path: Path, flush_every: int = 30) -> None:
        self.path = path
        self.flush_every = flush_every
        self.rows_written = 0
        self._handle = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=CSV_COLUMNS)
        self._writer.writeheader()

    def write_row(self, row: dict[str, object]) -> None:
        self._writer.writerow(row)
        self.rows_written += 1
        if self.rows_written % self.flush_every == 0:
            self.flush()

    def flush(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        self.flush()
        self._handle.close()

    def __enter__(self) -> "TimestampCsvWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def count_csv_rows(path: Path) -> int:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return sum(1 for _ in reader)


def iter_csv_rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)

