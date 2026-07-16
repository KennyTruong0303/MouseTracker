"""FFmpeg webcam recorder with per-frame timestamp CSV files."""

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
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from .camera import (
    CameraDevice,
    DSHOW_CAMERA_DIAGNOSTIC,
    WSL_CAMERA_DIAGNOSTIC,
    apply_controls,
    discover_camera,
    list_camera_devices,
    list_controls,
    load_control_config,
    resolve_backend,
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
from .validation import recover_partial_segment, recover_partials_in_dir, validate_segment


LOGGER = logging.getLogger("rfid_tracking.recording")
DEFAULT_OUTPUT_DIR = Path(r"D:\MouseTracker\data\mota") if os.name == "nt" else Path("/mnt/d/MouseTracker/data/mota")
SENTINEL = object()
SEGMENT_BREAK = object()


class SegmentState(Enum):
    OPEN = "OPEN"
    FINALIZING = "FINALIZING"
    FINALIZED = "FINALIZED"
    FAILED = "FAILED"


@dataclass
class RecorderStats:
    queue_high_water_mark: int = 0
    current_queue_size: int = 0
    incomplete_frames: int = 0
    queue_full_events: int = 0
    unwritten_frames: int = 0
    frame_gaps: int = 0
    suspicious_short_intervals: int = 0
    segments: list[Path] = field(default_factory=list)
    total_frames: int = 0
    reconnect_count: int = 0
    selected_camera: str | None = None
    selected_encoder: str | None = None
    recording_start_wall_time_iso8601: str | None = None
    recording_start_monotonic_ns: int | None = None
    current_segment_filename: str | None = None
    current_segment_start_monotonic_ns: int | None = None
    last_validation_result: str | None = None


@dataclass
class RecorderState:
    stop_event: threading.Event
    failure_event: threading.Event
    stats: RecorderStats
    error_message: str | None = None
    shutdown_source: str | None = None

    @property
    def shutdown_requested(self) -> threading.Event:
        return self.stop_event

    def request_shutdown(self, source: str = "unknown") -> bool:
        first_request = not self.shutdown_requested.is_set()
        if first_request:
            self.shutdown_source = source
        self.shutdown_requested.set()
        return first_request

    def fail(self, message: str) -> None:
        self.error_message = message
        self.failure_event.set()
        self.request_shutdown(f"failure: {message}")


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


def ensure_tools(*, backend: str, include_camera_tools: bool) -> list[tuple[str, bool, str]]:
    checks = []
    tools = ["ffmpeg", "ffprobe"]
    if include_camera_tools and backend == "v4l2":
        tools.append("v4l2-ctl")
    for tool in tools:
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


def timezone_offset_string() -> str:
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return "unknown"
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def log_clock_diagnostics(expected_offset: str | None) -> None:
    now = datetime.now().astimezone()
    current_offset = timezone_offset_string()
    LOGGER.info("Windows/local time at recording start: %s", now.isoformat())
    LOGGER.info("Local timezone names: %s", time.tzname)
    LOGGER.info("Local UTC offset: %s", current_offset)
    if expected_offset and current_offset != expected_offset:
        LOGGER.warning(
            "Local timezone offset %s does not match expected experimental clock offset %s",
            current_offset,
            expected_offset,
        )


STOP_COMMANDS = {"q", "stop", "quit", "exit"}


def default_stop_file(output_dir: Path) -> Path:
    return output_dir / "STOP_RECORDING"


def should_enable_keyboard_stop(requested: bool | None) -> bool:
    if requested is not None:
        return requested
    return sys.stdin is not None and sys.stdin.isatty()


def print_stop_instructions(output_dir: Path) -> None:
    print("Recording started.")
    print("Type q and press Enter to stop safely.")
    print("Alternative from another PowerShell:")
    print(
        "python -m rfid_tracking.recording.ffmpeg_recorder --request-stop "
        f'--output-dir "{output_dir}"'
    )


def keyboard_stop_thread(state: RecorderState, input_stream=None) -> None:
    stream = input_stream if input_stream is not None else sys.stdin
    while not state.shutdown_requested.is_set():
        line = stream.readline()
        if line == "":
            return
        command = line.strip().lower()
        if command in STOP_COMMANDS:
            if state.request_shutdown(f"keyboard {command} command"):
                LOGGER.info("Shutdown requested by keyboard command, disabling capture recovery")
            return


def stop_file_watcher_thread(state: RecorderState, stop_file: Path, interval_s: float = 0.25) -> None:
    while not state.shutdown_requested.is_set():
        if stop_file.exists():
            if state.request_shutdown(f"stop file {stop_file}"):
                LOGGER.info("Shutdown requested by stop file, disabling capture recovery")
            try:
                stop_file.rename(stop_file.with_suffix(stop_file.suffix + ".handled"))
            except OSError:
                try:
                    stop_file.unlink()
                except OSError:
                    LOGGER.warning("Could not remove stop file %s", stop_file)
            return
        state.shutdown_requested.wait(interval_s)


def request_stop_file(output_dir: Path, stop_file: Path | None = None) -> Path:
    path = stop_file or default_stop_file(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop\n", encoding="utf-8")
    return path


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
        self.state = SegmentState.FINALIZED
        self._stdin_closed = False

    def open_for_packet(self, packet: FramePacket) -> None:
        if self.state == SegmentState.FINALIZING:
            raise RuntimeError("Cannot open a segment while another segment is finalizing")
        self.paths = segment_paths(self.output_dir, packet.wall_time_unix_ns)
        self.segment_start_monotonic_ns = packet.monotonic_ns
        self.segment_frame_index = 0
        self.encoded_frames = 0
        self._stdin_closed = False
        self.state = SegmentState.OPEN
        command = build_encode_command(
            encoder=self.encoder,
            output_path=self.paths.part_video,
            width=self.width,
            height=self.height,
            fps=self.fps,
        )
        LOGGER.info("Opening segment %s", self.paths.segment_filename)
        LOGGER.info("Encoder command: %s", " ".join(command))
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        self.csv = TimestampCsvWriter(self.paths.part_csv)

    def write_packet(self, packet: FramePacket, stats: RecorderStats) -> None:
        if self.recording_start_monotonic_ns is None:
            self.recording_start_monotonic_ns = packet.monotonic_ns
            stats.recording_start_monotonic_ns = packet.monotonic_ns
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
        stats.current_segment_filename = self.paths.segment_filename
        stats.current_segment_start_monotonic_ns = self.segment_start_monotonic_ns

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
        if self.paths is None or self.state in {SegmentState.FINALIZED, SegmentState.FAILED}:
            return
        if self.state == SegmentState.FINALIZING:
            LOGGER.warning("Ignoring repeated finalize_current call while segment is already finalizing")
            return
        assert self.process is not None
        assert self.csv is not None
        paths = self.paths
        self.state = SegmentState.FINALIZING
        LOGGER.info("Closing segment %s with %s frames", paths.segment_filename, self.encoded_frames)
        returncode: int | None = None
        stderr_text = ""
        try:
            try:
                if self.process.stdin is not None and not self._stdin_closed:
                    self.process.stdin.close()
                    self._stdin_closed = True
                returncode = self.process.wait(timeout=60)
                stderr_text = self._read_encoder_stderr()
            finally:
                self.csv.close()

            if returncode != 0:
                LOGGER.error(
                    "FFmpeg encoder exited with code %s for %s\n%s",
                    returncode,
                    paths.part_video,
                    stderr_text.strip(),
                )
                if self.validate:
                    recovery = recover_partial_segment(
                        paths.part_video,
                        width=self.width,
                        height=self.height,
                        fps=self.fps,
                    )
                    if recovery.ok:
                        LOGGER.warning(
                            "Recovered valid segment after non-zero FFmpeg exit: %s",
                            recovery.final_video.name,
                        )
                        stats.segments.append(recovery.final_video)
                        stats.last_validation_result = f"Recovered {recovery.final_video.name}"
                        self._mark_finalized_and_reset()
                        stats.current_segment_filename = None
                        stats.current_segment_start_monotonic_ns = None
                        return
                    LOGGER.error(
                        "Partial segment was not recoverable: %s",
                        "; ".join(recovery.validation.errors),
                    )
                self.state = SegmentState.FAILED
                raise RuntimeError(f"FFmpeg encoder exited with code {returncode} for {paths.part_video}")

            if self.validate:
                result = validate_segment(
                    paths.part_video,
                    paths.part_csv,
                    width=self.width,
                    height=self.height,
                    fps=self.fps,
                )
                if not result.ok:
                    self.state = SegmentState.FAILED
                    stats.last_validation_result = "; ".join(result.errors)
                    raise RuntimeError(
                        f"Segment validation failed for {paths.part_video}: {'; '.join(result.errors)}"
                    )
                stats.last_validation_result = (
                    f"Validated {paths.segment_filename}: "
                    f"{result.video_frames} video frames, {result.csv_rows} CSV rows"
                )

            os.replace(paths.part_video, paths.final_video)
            os.replace(paths.part_csv, paths.final_csv)
            stats.segments.append(paths.final_video)
            LOGGER.info("Finalized segment %s", paths.final_video.name)
            self._mark_finalized_and_reset()
            stats.current_segment_filename = None
            stats.current_segment_start_monotonic_ns = None
        except Exception:
            if self.state != SegmentState.FINALIZED:
                self.state = SegmentState.FAILED
            raise

    def _read_encoder_stderr(self) -> str:
        if self.process is None or self.process.stderr is None:
            return ""
        try:
            data = self.process.stderr.read()
        except Exception as exc:  # noqa: BLE001
            return f"<failed to read encoder stderr: {exc}>"
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)

    def _mark_finalized_and_reset(self) -> None:
        self.state = SegmentState.FINALIZED
        self.paths = None
        self.process = None
        self.csv = None
        self.segment_start_monotonic_ns = None
        self.segment_frame_index = 0
        self.encoded_frames = 0
        self._stdin_closed = False


def read_exact(stream, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.read(size - len(parts))
        if not chunk:
            break
        parts.extend(chunk)
    return bytes(parts)


def capture_command(camera: CameraDevice, width: int, height: int, fps: float) -> list[str]:
    if camera.backend == "dshow":
        input_args = (
            ["-vcodec", "mjpeg"]
            if camera.preferred_input_format == "mjpeg"
            else ["-pixel_format", camera.preferred_input_format]
        )
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "dshow",
            *input_args,
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(fps),
            "-i",
            f"video={camera.name}",
            "-an",
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "v4l2",
        "-input_format",
        camera.preferred_input_format,
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        camera.path,
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
        queue_size = packets.qsize()
        state.stats.current_queue_size = queue_size
        state.stats.queue_high_water_mark = max(state.stats.queue_high_water_mark, queue_size)
        state.stats.total_frames = max(state.stats.total_frames, packet.global_frame_index + 1)
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
    requested_backend: str,
    camera: CameraDevice,
    width: int,
    height: int,
    fps: float,
    preview_callback: Callable[[FramePacket], None] | None = None,
    preview_interval_s: float = 0.125,
) -> None:
    frame_bytes = width * height * 3
    index = 0
    retry_delay = 1.0
    next_preview_ns = 0
    preview_interval_ns = int(preview_interval_s * 1_000_000_000)
    try:
        while not state.shutdown_requested.is_set():
            command = capture_command(camera, width, height, fps)
            LOGGER.info("Capture command: %s", " ".join(command))
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(command, stdout=subprocess.PIPE, creationflags=creationflags)
            capture_failed = False
            try:
                assert process.stdout is not None
                while not state.shutdown_requested.is_set():
                    data = read_exact(process.stdout, frame_bytes)
                    if len(data) == 0:
                        if state.shutdown_requested.is_set():
                            LOGGER.info("Capture FFmpeg ended after shutdown request; closing current segment")
                        else:
                            capture_failed = True
                            LOGGER.error("Capture FFmpeg ended unexpectedly; closing current segment")
                        packets.put(SEGMENT_BREAK)
                        break
                    if len(data) != frame_bytes:
                        state.stats.incomplete_frames += 1
                        if state.shutdown_requested.is_set():
                            LOGGER.info(
                                "Incomplete frame received after shutdown request; closing current segment"
                            )
                        else:
                            capture_failed = True
                            LOGGER.error("Incomplete frame buffer: %s of %s bytes", len(data), frame_bytes)
                        packets.put(SEGMENT_BREAK)
                        break
                    wall_ns = time.time_ns()
                    monotonic_ns = time.monotonic_ns()
                    packet = FramePacket(data, index, wall_ns, monotonic_ns)
                    if not put_packet(packets, packet, state):
                        break
                    if preview_callback and monotonic_ns >= next_preview_ns:
                        try:
                            preview_callback(packet)
                        except Exception as exc:  # noqa: BLE001
                            LOGGER.warning("Preview callback failed; dropping preview frame: %s", exc)
                        next_preview_ns = monotonic_ns + preview_interval_ns
                    index += 1
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if state.shutdown_requested.is_set():
                LOGGER.info("Shutdown requested, disabling capture recovery")
                break
            if not capture_failed:
                break
            LOGGER.info("Retrying camera discovery after %.1f seconds", retry_delay)
            time.sleep(retry_delay)
            if state.shutdown_requested.is_set():
                LOGGER.info("Shutdown requested, disabling capture recovery")
                break
            retry_delay = min(retry_delay * 2, 30.0)
            try:
                camera = discover_camera(
                    device=requested_device,
                    width=width,
                    height=height,
                    fps=fps,
                    backend=requested_backend,
                )
                retry_delay = 1.0
                state.stats.reconnect_count += 1
                LOGGER.info("Reconnected camera: %s %s", camera.backend, camera.name)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Camera rediscovery failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - worker failures must reach main thread.
        LOGGER.exception("Capture thread failed")
        state.fail(f"Capture thread failed: {exc}")
    finally:
        state.request_shutdown()
        packets.put(SENTINEL)


def synthetic_capture_thread(
    *,
    packets: "queue.Queue[FramePacket | object]",
    state: RecorderState,
    width: int,
    height: int,
    fps: float,
    duration_seconds: float,
    preview_callback: Callable[[FramePacket], None] | None = None,
    preview_interval_s: float = 0.125,
) -> None:
    total_frames = int(duration_seconds * fps)
    frame_interval_ns = int(1_000_000_000 / fps)
    start_wall_ns = time.time_ns()
    start_monotonic_ns = time.monotonic_ns()
    frame = bytes(width * height * 3)
    next_preview_ns = start_monotonic_ns
    preview_interval_ns = int(preview_interval_s * 1_000_000_000)
    try:
        for index in range(total_frames):
            if state.shutdown_requested.is_set():
                break
            packet = FramePacket(
                data=frame,
                global_frame_index=index,
                wall_time_unix_ns=start_wall_ns + index * frame_interval_ns,
                monotonic_ns=start_monotonic_ns + index * frame_interval_ns,
            )
            if not put_packet(packets, packet, state):
                break
            if preview_callback and packet.monotonic_ns >= next_preview_ns:
                try:
                    preview_callback(packet)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Preview callback failed; dropping preview frame: %s", exc)
                next_preview_ns = packet.monotonic_ns + preview_interval_ns
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Synthetic capture failed")
        state.fail(f"Synthetic capture failed: {exc}")
    finally:
        state.request_shutdown()
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
            state.stats.current_queue_size = packets.qsize()
            if item is SENTINEL:
                break
            if item is SEGMENT_BREAK:
                writer.finalize_current(state.stats)
                continue
            assert isinstance(item, FramePacket)
            writer.write_packet(item, state.stats)
        while not packets.empty():
            item = packets.get_nowait()
            state.stats.current_queue_size = packets.qsize()
            if item is SEGMENT_BREAK:
                writer.finalize_current(state.stats)
            elif item is not SENTINEL:
                assert isinstance(item, FramePacket)
                writer.write_packet(item, state.stats)
        writer.finalize_current(state.stats)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Writer thread failed")
        state.fail(f"Writer thread failed: {exc}")


def preflight(args: argparse.Namespace) -> int:
    rows: list[tuple[str, bool, str]] = []
    backend = resolve_backend(args.backend)
    for tool, ok, detail in ensure_tools(backend=backend, include_camera_tools=not args.synthetic):
        rows.append((tool, ok, detail))
    out_ok, out_detail = check_output_dir(args.output_dir)
    rows.append(("output dir", out_ok, out_detail))
    if out_ok:
        rows.append(("disk free", disk_free_bytes(args.output_dir) > 5_000_000_000, f"{disk_free_bytes(args.output_dir)} bytes"))
    try:
        selected = select_hevc_encoder(args.encoder, backend=backend)
        rows.append(("HEVC encoder", True, selected.name))
    except Exception as exc:  # noqa: BLE001
        rows.append(("HEVC encoder", False, str(exc)))
    if not args.synthetic:
        try:
            camera = discover_camera(
                device=args.device,
                width=args.width,
                height=args.height,
                fps=args.fps,
                backend=backend,
            )
            rows.append(
                (
                    "camera",
                    True,
                    f"{camera.backend} {camera.name} {camera.preferred_input_format} "
                    f"{args.width}x{args.height}@{args.fps:g}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            if "No V4L2 camera" in detail:
                detail = WSL_CAMERA_DIAGNOSTIC
            if "No DirectShow" in detail:
                detail = DSHOW_CAMERA_DIAGNOSTIC
            rows.append(("camera", False, detail.replace("\n", " ")))
    frame_bytes = args.width * args.height * 3
    rows.append(("raw frame size", frame_bytes == args.width * args.height * 3, f"{frame_bytes} bytes"))
    print_table(rows)
    return 0 if all(ok for _name, ok, _detail in rows) else 1


def recover_partials(args: argparse.Namespace) -> int:
    results = recover_partials_in_dir(
        args.output_dir,
        width=args.width,
        height=args.height,
        fps=args.fps,
    )
    if not results:
        print(f"No .part MP4 files found in {args.output_dir}")
        return 0
    ok = True
    for result in results:
        if result.ok:
            print(f"RECOVERED {result.final_video.name} and {result.final_csv.name}")
        else:
            ok = False
            print(
                f"FAILED {result.part_video.name}: "
                + "; ".join(result.validation.errors)
            )
    return 0 if ok else 1


def run_recording(
    args: argparse.Namespace,
    *,
    state: RecorderState | None = None,
    preview_callback: Callable[[FramePacket], None] | None = None,
) -> int:
    output_dir = args.output_dir
    backend = resolve_backend(args.backend)
    log_path = configure_logging(output_dir)
    LOGGER.info("FFmpeg: %s", ffmpeg_version())
    LOGGER.info("Camera backend: %s", backend)
    log_clock_diagnostics(args.expected_timezone_offset)
    selected_encoder = select_hevc_encoder(args.encoder, backend=backend)
    for result in selected_encoder.probe_results:
        LOGGER.info(
            "Encoder probe %s ok=%s rc=%s stderr=%s",
            result.encoder,
            result.ok,
            result.returncode,
            result.stderr.strip(),
        )

    if state is None:
        state = RecorderState(
            stop_event=threading.Event(),
            failure_event=threading.Event(),
            stats=RecorderStats(),
        )
    state.stats.selected_encoder = selected_encoder.name
    state.stats.recording_start_wall_time_iso8601 = datetime.now().astimezone().isoformat()

    camera = None
    controls_text = ""
    if not args.synthetic:
        camera = discover_camera(
            device=args.device,
            width=args.width,
            height=args.height,
            fps=args.fps,
            backend=backend,
        )
        controls_text = list_controls(camera.path, backend=backend)
        LOGGER.info("Selected camera: %s %s", camera.backend, camera.name)
        LOGGER.info("Camera input format: %s", camera.preferred_input_format)
        LOGGER.info("Camera options/controls:\n%s", controls_text)
        if backend == "v4l2":
            for warning in warn_about_auto_controls(controls_text):
                LOGGER.warning(warning)
            apply_controls(camera.path, load_control_config(args.camera_controls_config), backend=backend)
        elif args.camera_controls_config:
            raise RuntimeError("--camera-controls-config is only supported with --backend v4l2")
        state.stats.selected_camera = camera.name
    else:
        state.stats.selected_camera = "synthetic"

    stop_file = args.stop_file or default_stop_file(output_dir)
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
        if state.request_shutdown("console signal"):
            LOGGER.info("Shutdown requested, disabling capture recovery")

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    previous_sigbreak = signal.signal(signal.SIGBREAK, request_stop) if hasattr(signal, "SIGBREAK") else None
    try:
        print_stop_instructions(output_dir)
        helpers: list[threading.Thread] = []
        if should_enable_keyboard_stop(args.keyboard_stop):
            helper = threading.Thread(target=keyboard_stop_thread, args=(state,), daemon=True)
            helper.start()
            helpers.append(helper)
        else:
            LOGGER.info("Keyboard stop disabled because stdin is not interactive")
        stop_helper = threading.Thread(
            target=stop_file_watcher_thread,
            args=(state, stop_file),
            daemon=True,
        )
        stop_helper.start()
        helpers.append(stop_helper)
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
                    "preview_callback": preview_callback,
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
                    "requested_backend": backend,
                    "camera": camera,
                    "width": args.width,
                    "height": args.height,
                    "fps": args.fps,
                    "preview_callback": preview_callback,
                },
            )
        writer.start()
        capture.start()
        capture.join()
        writer.join()
        state.request_shutdown("recording complete")
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        if previous_sigbreak is not None:
            signal.signal(signal.SIGBREAK, previous_sigbreak)

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
    parser.add_argument("--backend", choices=("auto", "dshow", "v4l2"), default="auto")
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
    parser.add_argument("--recover-partials", action="store_true")
    parser.add_argument("--request-stop", action="store_true")
    keyboard_group = parser.add_mutually_exclusive_group()
    keyboard_group.add_argument("--keyboard-stop", dest="keyboard_stop", action="store_true")
    keyboard_group.add_argument("--no-keyboard-stop", dest="keyboard_stop", action="store_false")
    parser.set_defaults(keyboard_stop=None)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--camera-controls-config")
    parser.add_argument("--expected-timezone-offset", default="+08:00")
    parser.add_argument("--skip-validation", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_devices:
        print(list_camera_devices(args.width, args.height, args.fps, backend=args.backend))
        return 0
    if args.list_camera_controls:
        backend = resolve_backend(args.backend)
        camera = discover_camera(device=args.device, width=args.width, height=args.height, fps=args.fps, backend=backend)
        print(list_controls(camera.path, backend=backend))
        return 0
    if args.preflight:
        return preflight(args)
    if args.recover_partials:
        try:
            require_tool("ffprobe")
            return recover_partials(args)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    if args.request_stop:
        path = request_stop_file(args.output_dir, args.stop_file)
        print(f"Stop requested via {path}")
        return 0
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
