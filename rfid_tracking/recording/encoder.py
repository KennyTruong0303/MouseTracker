"""HEVC encoder probing and FFmpeg command construction."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


LOGGER = logging.getLogger(__name__)
HEVC_ENCODER_ORDER = ("hevc_nvenc", "hevc_qsv", "hevc_vaapi", "hevc_amf", "libx265")


@dataclass(frozen=True)
class EncoderProbeResult:
    encoder: str
    ok: bool
    command: list[str]
    stdout: str
    stderr: str
    returncode: int | None


@dataclass(frozen=True)
class SelectedEncoder:
    name: str
    probe_results: list[EncoderProbeResult]


Runner = Callable[..., subprocess.CompletedProcess[str]]


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required tool {name!r} was not found on PATH")
    return path


def ffmpeg_version(runner: Runner = subprocess.run) -> str:
    completed = runner(
        ["ffmpeg", "-version"],
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    first_line = (completed.stdout or completed.stderr).splitlines()
    return first_line[0] if first_line else "ffmpeg version unavailable"


def encoder_options(encoder: str) -> list[str]:
    if encoder == "libx265":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"]
    if encoder == "hevc_nvenc":
        return [
            "-c:v",
            "hevc_nvenc",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            "23",
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
    if encoder == "hevc_qsv":
        return ["-c:v", "hevc_qsv", "-global_quality", "23", "-look_ahead", "1"]
    if encoder == "hevc_vaapi":
        return ["-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi", "-qp", "23"]
    if encoder == "hevc_amf":
        return ["-c:v", "hevc_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]
    raise ValueError(f"Unsupported HEVC encoder {encoder!r}")


def probe_encoder(
    encoder: str,
    *,
    runner: Runner = subprocess.run,
    timeout: float = 20.0,
) -> EncoderProbeResult:
    with tempfile.TemporaryDirectory(prefix="mouse_encoder_probe_") as tmpdir:
        out = Path(tmpdir) / "probe.mp4"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x72:rate=30",
            "-frames:v",
            "15",
            *encoder_options(encoder),
            "-tag:v",
            "hvc1",
            "-movflags",
            "+faststart",
            str(out),
        ]
        try:
            completed = runner(
                command,
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return EncoderProbeResult(
                encoder=encoder,
                ok=False,
                command=command,
                stdout="",
                stderr=str(exc),
                returncode=None,
            )
        ok = completed.returncode == 0 and out.exists() and out.stat().st_size > 0
        return EncoderProbeResult(
            encoder=encoder,
            ok=ok,
            command=command,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


def select_hevc_encoder(
    requested: str = "auto",
    *,
    runner: Runner = subprocess.run,
) -> SelectedEncoder:
    candidates = HEVC_ENCODER_ORDER if requested == "auto" else (requested,)
    results: list[EncoderProbeResult] = []
    for encoder in candidates:
        result = probe_encoder(encoder, runner=runner)
        results.append(result)
        if result.ok:
            LOGGER.info("Selected HEVC encoder: %s", encoder)
            return SelectedEncoder(name=encoder, probe_results=results)
        LOGGER.warning(
            "Rejected HEVC encoder %s: rc=%s stderr=%s",
            encoder,
            result.returncode,
            result.stderr.strip(),
        )
    attempted = ", ".join(result.encoder for result in results)
    raise RuntimeError(f"No functional HEVC encoder found. Attempted: {attempted}")


def build_encode_command(
    *,
    encoder: str,
    output_path: Path,
    width: int,
    height: int,
    fps: float,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s:v",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        *encoder_options(encoder),
        "-tag:v",
        "hvc1",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

