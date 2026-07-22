from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rfid_tracking.analysis.movement import (
    MotionSettings,
    analyze_frames,
    build_parser,
    ffmpeg_gray_command,
    main,
    motion_metrics,
    movement_csv_path,
    movement_rows,
    timestamp_csv_for_video,
)
from rfid_tracking.recording.service import RecorderService


TIMESTAMP_HEADER = (
    "segment_filename,global_frame_index,segment_frame_index,wall_time_iso8601,"
    "wall_time_unix_ns,monotonic_ns,elapsed_from_recording_start_s,"
    "elapsed_from_segment_start_s,inter_frame_interval_ms,expected_interval_ms,"
    "frame_gap_ms,gap_flag\n"
)


def write_timestamp_csv(path: Path, rows: int) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(TIMESTAMP_HEADER)
        for index in range(rows):
            handle.write(
                f"record_20260715_144320.mp4,{index},{index},2026-07-15T14:43:20+08:00,"
                f"{index},{index},{index / 30:.6f},{index / 30:.6f},33.333333,33.333333,,False\n"
            )


class MovementAnalysisTests(unittest.TestCase):
    def test_motion_metrics_detect_centroid_bbox_and_zone(self):
        settings = MotionSettings(width=6, height=4, diff_threshold=10, edge_stride=2)
        previous = bytes(24)
        current = bytearray(24)
        current[2 + 1 * 6] = 255
        current[3 + 1 * 6] = 255
        current[2 + 2 * 6] = 255
        metrics = motion_metrics(previous, bytes(current), settings=settings)
        self.assertEqual(metrics["motion_pixels"], 3)
        self.assertAlmostEqual(metrics["centroid_x"], 7 / 3)
        self.assertEqual(metrics["bbox_x1"], 2)
        self.assertEqual(metrics["bbox_y1"], 1)
        self.assertEqual(metrics["bbox_x2"], 3)
        self.assertEqual(metrics["bbox_y2"], 2)
        self.assertEqual(metrics["motion_zone"], "center")
        self.assertFalse(metrics["immobile_flag"])

    def test_movement_rows_preserve_timestamp_indices(self):
        settings = MotionSettings(width=2, height=2, diff_threshold=1, immobile_motion_index_threshold=0.1)
        rows = [
            {"global_frame_index": "10", "segment_frame_index": "0", "wall_time_iso8601": "t0"},
            {"global_frame_index": "11", "segment_frame_index": "1", "wall_time_iso8601": "t1"},
        ]
        frames = [bytes([0, 0, 0, 0]), bytes([255, 0, 0, 0])]
        output = list(movement_rows(frames, rows, settings=settings))
        self.assertEqual(output[1]["global_frame_index"], "11")
        self.assertEqual(output[1]["segment_frame_index"], "1")
        self.assertEqual(output[1]["motion_pixels"], 1)

    def test_analyze_frames_writes_movement_csv_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "record_20260715_144320.mp4"
            timestamps = root / "record_20260715_144320_timestamps.csv"
            output_csv = root / "record_20260715_144320_movement.csv"
            summary_json = root / "record_20260715_144320_movement_summary.json"
            video.write_bytes(b"fake")
            write_timestamp_csv(timestamps, 3)
            frames = [
                bytes([0, 0, 0, 0]),
                bytes([255, 0, 0, 0]),
                bytes([255, 0, 255, 0]),
            ]
            summary = analyze_frames(
                video_path=video,
                timestamp_csv=timestamps,
                output_csv=output_csv,
                summary_json=summary_json,
                frames=frames,
                settings=MotionSettings(width=2, height=2, diff_threshold=1, immobile_motion_index_threshold=0.1),
            )
            self.assertTrue(summary.frame_count_match)
            self.assertEqual(summary.video_frames_read, 3)
            self.assertEqual(summary.movement_frames, 2)
            with output_csv.open("r", encoding="utf-8", newline="") as handle:
                written = list(csv.DictReader(handle))
            self.assertEqual(len(written), 3)
            saved_summary = json.loads(summary_json.read_text(encoding="utf-8"))
            self.assertEqual(saved_summary["movement_frames"], 2)

    def test_analyze_frames_warns_on_timestamp_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "record_20260715_144320.mp4"
            timestamps = root / "record_20260715_144320_timestamps.csv"
            write_timestamp_csv(timestamps, 2)
            summary = analyze_frames(
                video_path=video,
                timestamp_csv=timestamps,
                output_csv=root / "movement.csv",
                summary_json=root / "summary.json",
                frames=[bytes([0, 0, 0, 0])],
                settings=MotionSettings(width=2, height=2),
            )
            self.assertFalse(summary.frame_count_match)
            self.assertIn("decoded frame count 1 != timestamp rows 2", summary.warnings)

    def test_default_output_paths_match_recording_names(self):
        video = Path("record_20260715_144320.mp4")
        self.assertEqual(timestamp_csv_for_video(video).name, "record_20260715_144320_timestamps.csv")
        self.assertEqual(movement_csv_path(video).name, "record_20260715_144320_movement.csv")

    def test_ffmpeg_gray_command_decodes_raw_grayscale(self):
        command = ffmpeg_gray_command(Path("input.mp4"), width=1280, height=720)
        self.assertIn("-pix_fmt", command)
        self.assertIn("gray", command)
        self.assertEqual(command[-1], "pipe:1")

    def test_cli_uses_analyze_video_and_returns_success(self):
        with mock.patch("rfid_tracking.analysis.movement.analyze_video") as analyze:
            analyze.return_value = mock.Mock(
                movement_csv="movement.csv",
                summary_json="summary.json",
                video_frames_read=3,
                timestamp_rows=3,
                frame_count_match=True,
                movement_frames=2,
                immobile_frames=1,
                max_motion_index=0.5,
                warnings=[],
            )
            rc = main(["record_20260715_144320.mp4"])
        self.assertEqual(rc, 0)
        analyze.assert_called_once()

    def test_service_analyzes_latest_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "record_20260715_144320.mp4"
            newer = root / "record_20260715_144420.mp4"
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            with mock.patch("rfid_tracking.recording.service.analyze_video") as analyze:
                analyze.return_value = mock.Mock(frame_count_match=True)
                RecorderService().analyze_latest_movement(root)
        self.assertEqual(analyze.call_args.args[0], newer)

    def test_parser_accepts_tracking_thresholds(self):
        args = build_parser().parse_args(
            [
                "record_20260715_144320.mp4",
                "--diff-threshold",
                "12",
                "--immobile-motion-index-threshold",
                "0.004",
            ]
        )
        self.assertEqual(args.diff_threshold, 12)
        self.assertEqual(args.immobile_motion_index_threshold, 0.004)


if __name__ == "__main__":
    unittest.main()
