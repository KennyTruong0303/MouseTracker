"""WSL2 FFmpeg webcam recorder with per-frame timestamp CSV files."""

from __future__ import annotations

import argparse
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .camera import (
    WSL_CAMERA_DIAGNOSTIC,
    apply_controls,
    discover_camera,
    list_camera_devices,
    list_controls,
    load_control_config,
    warn_about_auto_controls,
)
from .encoder import (
    build_encode_command,
    ffmpeg_version,
    require_tool,
    select_hevc_encoder,
)
from .timestamps import (
    EXPECTED_INTERVAL_MS,
    FramePacket,
    SegmentPaths,
    TimestampCsvWriter,
    segment_paths,
    timestamp_row,
)
from .validation import validate_segment


LOGGER = logging.getLogger("rfid_tracking.recording")
DEFAULT_OUTPUT_DIR = Path("/mnt/d/MouseTracker/data/mota")
SENTINEL = object()
SEGMENT_BREAK = object()


@dataclass
class RecorderStats:
    queue_high_water_mark: int = 0
    incomplete_frames: int = 0
    queue_full_events: int = 0
    unwritten_frames: int = 0
    frame_gaps: int = 0
    suspicious_short_intervals: int = 0
    segments: list[Path] = field(default_factory=list)


@dataclass
class RecorderState:
    stop_event: threading.Event
    failure_event: threading.Event
    stats: RecorderStats
    error_message: str | None = None

    def fail(self, message: str) -> None:
        self.error_message = message
        self.failure_event.set()
        self.stop_event.set()


def configure_logging(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    log_path = output_dir / f"recording_run_{stamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    LOGGER.info("Run log: %s", log_path)
    return log_path


def ensure_tools() -> list[tuple[str, bool, str]]:
    checks = []
    for tool in ("ffmpeg", "ffprobe", "v4l2-ctl"):
        path = shutil.which(tool)
        checks.append((tool, bool(path), path or "not found"))
    return checks


def disk_free_bytes(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return usage.free


def check_output_dir(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".recorder_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, "writable"
    except OSError as exc:
        return False, str(exc)


def print_table(rows: Iterable[tuple[str, bool, str]]) -> None:
    width = max(len(name) for name, _ok, _detail in rows)
    for name, ok, detail in rows:
        print(f"{name:<{width}}  {'PASS' if ok else 'FAIL'}  {detail}")


class SegmentWriter:
    def __init__(
        self,
        *,
        output_dir: Path,
        width: int,
        height: int,
        fps: float,
        segment_seconds: float,
        encoder: str,
        gap_threshold_ms: float,
        validate: bool,
    ) -> None:
        self.output_dir = output_dir
        self.width = width
        self.height = height
        self.fps = fps
        self.segment_ns = int(segment_seconds * 1_000_000_000)
        self.encoder = encoder
        self.gap_threshold_ms = gap_threshold_ms
        self.validate = validate
        self.paths: SegmentPaths | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.csv: TimestampCsvWriter | None = None
        self.segment_start_monotonic_ns: int | None = None
        self.recording_start_monotonic_ns: int | None = None
        self.segment_frame_index = 0
        self.previous_frame_monotonic_ns: int | None = None
        self.encoded_frames = 0

    def open_for_packet(self, packet: FramePacket) -> None:
        self.paths = segment_paths(self.output_dir, packet.wall_time_unix_ns)
        self.segment_start_monotonic_ns = packet.monotonic_ns
        self.segment_frame_index = 0
        self.encoded_frames = 0
        command = build_encode_command(
            encoder=self.encoder,
            output_path=self.paths.part_video,
            width=self.width,
            height=self.height,
            fps=self.fps,
        )
        LOGGER.info("Opening segment %s", self.paths.segment_filename)
        LOGGER.info("Encoder command: %s", " ".join(command))
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE)
        self.csv = TimestampCsvWriter(self.paths.part_csv)

    def write_packet(self, packet: FramePacket, stats: RecorderStats) -> None:
        if self.recording_start_monotonic_ns is None:
            self.recording_start_monotonic_ns = packet.monotonic_ns
        if self.paths is None:
            self.open_for_packet(packet)

        assert self.segment_start_monotonic_ns is not None
        if packet.monotonic_ns - self.segment_start_monotonic_ns >= self.segment_ns:
            self.finalize_current(stats)
            self.open_for_packet(packet)

        assert self.paths is not None
        assert self.process is not None
        assert self.process.stdin is not None
        assert self.csv is not None
        assert self.recording_start_monotonic_ns is not None
        assert self.segment_start_monotonic_ns is not None

        try:
            self.process.stdin.write(packet.data)
        except BrokenPipeError as exc:
            raise RuntimeError("FFmpeg encoder pipe broke") from exc

        row = timestamp_row(
            packet,
            segment_filename=self.paths.segment_filename,
            segment_frame_index=self.segment_frame_index,
            recording_start_monotonic_ns=self.recording_start_monotonic_ns,
            segment_start_monotonic_ns=self.segment_start_monotonic_ns,
            previous_frame_monotonic_ns=self.previous_frame_monotonic_ns,
            expected_interval_ms=1000.0 / self.fps,
            gap_threshold_ms=self.gap_threshold_ms,
        )
        interval = row["inter_frame_interval_ms"]
        if isinstance(interval, float):
            if interval > self.gap_threshold_ms:
                stats.frame_gaps += 1
                LOGGER.warning("Frame gap %.3f ms at global frame %s", interval, packet.global_frame_index)
            if interval < (1000.0 / self.fps) / 2:
                stats.suspicious_short_intervals += 1
                LOGGER.warning(
                    "Suspicious short frame interval %.3f ms at global frame %s",
                    interval,
                    packet.global_frame_index,
                )
        self.csv.write_row(row)
        self.previous_frame_monotonic_ns = packet.monotonic_ns
        self.segment_frame_index += 1
        self.encoded_frames += 1

    def finalize_current(self, stats: RecorderStats) -> None:
        if self.paths is None:
            return
        assert self.process is not None
        assert self.csv is not None
        assert self.process.stdin is not None
        paths = self.paths
        LOGGER.info("Closing segment %s with %s frames", paths.segment_filename, self.encoded_frames)
        try:
            self.process.stdin.close()
            returncode = self.process.wait(timeout=60)
        finally:
            self.csv.close()
        if returncode != 0:
            raise RuntimeError(f"FFmpeg encoder exited with code {returncode} for {paths.part_video}")
        os.replace(paths.part_video, paths.final_video)
        os.replace(paths.part_csv, paths.final_csv)
        stats.segments.append(paths.final_video)
        if self.validate:
            result = validate_segment(
                paths.final_video,
                paths.final_csv,
                width=self.width,
                height=self.height,
                fps=self.fps,
            )
            if result.ok:
                LOGGER.info("Validated segment %s", paths.final_video.name)
            else:
                LOGGER.error("Invalid segment %s: %s", paths.final_video.name, "; ".join(result.errors))
        self.paths = None
        self.process = None
        self.csv = None
        self.segment_start_monotonic_ns = None
        self.segment_frame_index = 0
        self.encoded_frames = 0


def read_exact(stream, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.read(size - len(parts))
        if not chunk:
            break
        parts.extend(chunk)
    return bytes(parts)


def capture_command(device_path: str, input_format: str, width: int, height: int, fps: float) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "v4l2",
        "-input_format",
        input_format,
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        device_path,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-",
    ]


def put_packet(
    packets: "queue.Queue[FramePacket | object]",
    packet: FramePacket,
    state: RecorderState,
) -> bool:
    try:
        packets.put(packet, timeout=1.0)
        state.stats.queue_high_water_mark = max(state.stats.queue_high_water_mark, packets.qsize())
        return True
    except queue.Full:
        state.stats.queue_full_events += 1
        state.stats.unwritten_frames += 1
        state.fail("Frame queue became full; stopping to preserve timing integrity")
        LOGGER.critical("Frame queue full at global frame %s", packet.global_frame_index)
        return False


def capture_thread(
    *,
    packets: "queue.Queue[FramePacket | object]",
    state: RecorderState,
    requested_device: str,
    device_path: str,
    input_format: str,
    width: int,
    height: int,
    fps: float,
) -> None:
    frame_bytes = width * height * 3
    index = 0
    retry_delay = 1.0
    try:
        while not state.stop_event.is_set():
            command = capture_command(device_path, input_format, width, height, fps)
            LOGGER.info("Capture command: %s", " ".join(command))
            process = subprocess.Popen(command, stdout=subprocess.PIPE)
            try:
                assert process.stdout is not None
                while not state.stop_event.is_set():
                    data = read_exact(process.stdout, frame_bytes)
                    if len(data) == 0:
                        LOGGER.error("Capture FFmpeg ended unexpectedly; closing current segment")
                        packets.put(SEGMENT_BREAK)
                        break
                    if len(data) != frame_bytes:
                        state.stats.incomplete_frames += 1
                        LOGGER.error("Incomplete frame buffer: %s of %s bytes", len(data), frame_bytes)
                        packets.put(SEGMENT_BREAK)
                        break
                    wall_ns = time.time_ns()
                    monotonic_ns = time.monotonic_ns()
                    packet = FramePacket(data, index, wall_ns, monotonic_ns)
                    if not put_packet(packets, packet, state):
                        break
                    index += 1
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if state.stop_event.is_set():
                break
            LOGGER.info("Retrying camera discovery after %.1f seconds", retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)
            try:
                camera = discover_camera(device=requested_device, width=width, height=height, fps=fps)
                device_path = camera.path
                input_format = camera.preferred_input_format
                retry_delay = 1.0
                LOGGER.info("Reconnected camera: %s %s", camera.path, camera.name)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Camera rediscovery failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - worker failures must reach main thread.
        LOGGER.exception("Capture thread failed")
        state.fail(f"Capture thread failed: {exc}")
    finally:
        state.stop_event.set()
        packets.put(SENTINEL)


def synthetic_capture_thread(
    *,
    packets: "queue.Queue[FramePacket | object]",
    state: RecorderState,
    width: int,
    height: int,
    fps: float,
    duration_seconds: float,
) -> None:
    total_frames = int(duration_seconds * fps)
    frame_interval_ns = int(1_000_000_000 / fps)
    start_wall_ns = time.time_ns()
    start_monotonic_ns = time.monotonic_ns()
    frame = bytes(width * height * 3)
    try:
        for index in range(total_frames):
            if state.stop_event.is_set():
                break
            packet = FramePacket(
                data=frame,
                global_frame_index=index,
                wall_time_unix_ns=start_wall_ns + index * frame_interval_ns,
                monotonic_ns=start_monotonic_ns + index * frame_interval_ns,
            )
            if not put_packet(packets, packet, state):
                break
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Synthetic capture failed")
        state.fail(f"Synthetic capture failed: {exc}")
    finally:
        state.stop_event.set()
        packets.put(SENTINEL)


def writer_thread(
    *,
    packets: "queue.Queue[FramePacket | object]",
    state: RecorderState,
    writer: SegmentWriter,
) -> None:
    try:
        while True:
            item = packets.get()
            if item is SENTINEL:
                break
            if item is SEGMENT_BREAK:
                writer.finalize_current(state.stats)
                continue
            assert isinstance(item, FramePacket)
            writer.write_packet(item, state.stats)
        while not packets.empty():
            item = packets.get_nowait()
            if item is SEGMENT_BREAK:
                writer.finalize_current(state.stats)
            elif item is not SENTINEL:
                assert isinstance(item, FramePacket)
                writer.write_packet(item, state.stats)
        writer.finalize_current(state.stats)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Writer thread failed")
        state.fail(f"Writer thread failed: {exc}")
        try:
            writer.finalize_current(state.stats)
        except Exception:
            LOGGER.exception("Failed while finalizing after writer error")


def preflight(args: argparse.Namespace) -> int:
    rows: list[tuple[str, bool, str]] = []
    for tool, ok, detail in ensure_tools():
        rows.append((tool, ok, detail))
    out_ok, out_detail = check_output_dir(args.output_dir)
    rows.append(("output dir", out_ok, out_detail))
    if out_ok:
        rows.append(("disk free", disk_free_bytes(args.output_dir) > 5_000_000_000, f"{disk_free_bytes(args.output_dir)} bytes"))
    try:
        selected = select_hevc_encoder(args.encoder)
        rows.append(("HEVC encoder", True, selected.name))
    except Exception as exc:  # noqa: BLE001
        rows.append(("HEVC encoder", False, str(exc)))
    if not args.synthetic:
        try:
            camera = discover_camera(device=args.device, width=args.width, height=args.height, fps=args.fps)
            rows.append(("camera", True, f"{camera.path} {camera.name} {camera.preferred_input_format}"))
        except Exception as exc:  # noqa: BLE001
            detail = WSL_CAMERA_DIAGNOSTIC if "No V4L2 camera" in str(exc) else str(exc)
            rows.append(("camera", False, detail.replace("\n", " ")))
    frame_bytes = args.width * args.height * 3
    rows.append(("raw frame size", frame_bytes == args.width * args.height * 3, f"{frame_bytes} bytes"))
    print_table(rows)
    return 0 if all(ok for _name, ok, _detail in rows) else 1


def run_recording(args: argparse.Namespace) -> int:
    output_dir = args.output_dir
    log_path = configure_logging(output_dir)
    LOGGER.info("FFmpeg: %s", ffmpeg_version())
    selected_encoder = select_hevc_encoder(args.encoder)
    for result in selected_encoder.probe_results:
        LOGGER.info(
            "Encoder probe %s ok=%s rc=%s stderr=%s",
            result.encoder,
            result.ok,
            result.returncode,
            result.stderr.strip(),
        )

    camera = None
    controls_text = ""
    if not args.synthetic:
        camera = discover_camera(device=args.device, width=args.width, height=args.height, fps=args.fps)
        controls_text = list_controls(camera.path)
        LOGGER.info("Selected camera: %s %s", camera.path, camera.name)
        LOGGER.info("Camera input format: %s", camera.preferred_input_format)
        LOGGER.info("Camera controls:\n%s", controls_text)
        for warning in warn_about_auto_controls(controls_text):
            LOGGER.warning(warning)
        apply_controls(camera.path, load_control_config(args.camera_controls_config))

    state = RecorderState(
        stop_event=threading.Event(),
        failure_event=threading.Event(),
        stats=RecorderStats(),
    )
    packets: "queue.Queue[FramePacket | object]" = queue.Queue(maxsize=args.queue_size)
    segment_writer = SegmentWriter(
        output_dir=output_dir,
        width=args.width,
        height=args.height,
        fps=args.fps,
        segment_seconds=args.segment_seconds,
        encoder=selected_encoder.name,
        gap_threshold_ms=args.gap_threshold_ms,
        validate=not args.skip_validation,
    )

    def request_stop(_signum: int, _frame: object) -> None:
        LOGGER.info("Shutdown requested")
        state.stop_event.set()

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        writer = threading.Thread(target=writer_thread, kwargs={"packets": packets, "state": state, "writer": segment_writer})
        if args.synthetic:
            capture = threading.Thread(
                target=synthetic_capture_thread,
                kwargs={
                    "packets": packets,
                    "state": state,
                    "width": args.width,
                    "height": args.height,
                    "fps": args.fps,
                    "duration_seconds": args.duration_seconds,
                },
            )
        else:
            assert camera is not None
            capture = threading.Thread(
                target=capture_thread,
                kwargs={
                    "packets": packets,
                    "state": state,
                    "requested_device": args.device,
                    "device_path": camera.path,
                    "input_format": camera.preferred_input_format,
                    "width": args.width,
                    "height": args.height,
                    "fps": args.fps,
                },
            )
        writer.start()
        capture.start()
        capture.join()
        writer.join()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)

    LOGGER.info("Recording stopped. Run log: %s", log_path)
    LOGGER.info("Queue high-water mark: %s", state.stats.queue_high_water_mark)
    LOGGER.info("Frame gaps: %s", state.stats.frame_gaps)
    LOGGER.info("Incomplete frames: %s", state.stats.incomplete_frames)
    LOGGER.info("Queue full events: %s", state.stats.queue_full_events)
    if state.failure_event.is_set():
        LOGGER.error("Recording failed: %s", state.error_message)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--segment-seconds", type=float, default=60.0)
    parser.add_argument("--encoder", default="auto")
    parser.add_argument("--gap-threshold-ms", type=float, default=50.0)
    parser.add_argument("--queue-size", type=int, default=300)
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--list-camera-controls", action="store_true")
    parser.add_argument("--camera-controls-config")
    parser.add_argument("--skip-validation", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_devices:
        print(list_camera_devices(args.width, args.height, args.fps))
        return 0
    if args.list_camera_controls:
        camera = discover_camera(device=args.device, width=args.width, height=args.height, fps=args.fps)
        print(list_controls(camera.path))
        return 0
    if args.preflight:
        return preflight(args)
    if args.synthetic and args.duration_seconds <= 0:
        parser.error("--synthetic requires --duration-seconds greater than 0")
    try:
        require_tool("ffmpeg")
        require_tool("ffprobe")
        return run_recording(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
