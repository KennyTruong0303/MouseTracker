# MouseTracker FFmpeg Recorder

This repository includes a WSL2-compatible webcam recorder for mouse behavior sessions. It captures decoded frames from a V4L2 USB webcam, timestamps each frame in Python with wall-clock and monotonic clocks, encodes H.265/HEVC MP4 segments with FFmpeg, and writes one matching timestamp CSV per segment.

## WSL2 Requirements

Install these inside the WSL2 Linux distribution that will run the recorder:

```bash
sudo apt update
sudo apt install ffmpeg v4l-utils usbutils python3
```

USB webcam access in WSL2 usually requires `usbipd-win` on the Windows host. The recorder does not run privileged Windows commands automatically. Use these diagnostics:

```bash
lsusb
ls /dev/video*
v4l2-ctl --list-devices
```

If no `/dev/video*` device appears, attach the webcam from Windows with the `usbipd-win` flow, then re-check inside WSL.

## Preflight

Run preflight before a real mouse session:

```bash
python -m rfid_tracking.recording.ffmpeg_recorder \
  --device auto \
  --output-dir /mnt/d/MouseTracker/data/mota \
  --width 1280 \
  --height 720 \
  --fps 30 \
  --segment-seconds 60 \
  --encoder auto \
  --gap-threshold-ms 50 \
  --preflight
```

Preflight verifies `ffmpeg`, `ffprobe`, `v4l2-ctl`, output-directory writability, disk space, exact 1280x720 at 30 fps support, and a functional HEVC encoder.

## Camera Discovery

List usable V4L2 devices:

```bash
python -m rfid_tracking.recording.ffmpeg_recorder --list-devices
```

The recorder searches `/dev/video*`, inspects each device with `v4l2-ctl`, prefers MJPEG at exactly 1280x720 and 30 fps, and fails clearly when no exact mode is available. Override the automatic choice with:

```bash
--device /dev/video2
```

## Recording Command

```bash
python -m rfid_tracking.recording.ffmpeg_recorder \
  --device auto \
  --output-dir /mnt/d/MouseTracker/data/mota \
  --width 1280 \
  --height 720 \
  --fps 30 \
  --segment-seconds 60 \
  --encoder auto \
  --gap-threshold-ms 50
```

The default output directory is:

```text
/mnt/d/MouseTracker/data/mota/
```

Each segment uses the wall-clock start time in the filename:

```text
record_20260715_144320.mp4
record_20260715_144320_timestamps.csv
```

Example paths:

```text
video_path:
/mnt/d/MouseTracker/data/mota/record_20260116_132044.mp4

timestamps_path:
/mnt/d/MouseTracker/data/mota/record_20260116_132044_timestamps.csv
```

## Timestamp Columns

The CSV contains one row for every encoded frame:

```text
segment_filename
global_frame_index
segment_frame_index
wall_time_iso8601
wall_time_unix_ns
monotonic_ns
elapsed_from_recording_start_s
elapsed_from_segment_start_s
inter_frame_interval_ms
expected_interval_ms
frame_gap_ms
gap_flag
```

Wall-clock time is used for filenames and correlation with RFID event timestamps. Monotonic time is used for frame intervals, gap detection, segment rollover, and movement-timing calculations. The recorder never reconstructs timing from `frame_index / fps`.

## Encoder Selection

`--encoder auto` probes real short HEVC encodes in this order:

```text
hevc_nvenc
hevc_qsv
hevc_vaapi
hevc_amf
libx265
```

The first encoder that completes a synthetic HEVC MP4 encode is selected. Manual overrides are supported:

```bash
--encoder libx265
--encoder hevc_nvenc
```

The recorder stops if no HEVC encoder works. It does not fall back to H.264.

## Camera Controls

Inspect camera controls:

```bash
python -m rfid_tracking.recording.ffmpeg_recorder \
  --device auto \
  --list-camera-controls
```

Fixed lighting is recommended. The recorder logs current controls and warns when auto exposure, auto white balance, or autofocus appears active. Optional controls can be applied from a JSON object:

```json
{
  "exposure_auto": 1,
  "exposure_absolute": 120,
  "gain": 20,
  "white_balance_temperature_auto": 0,
  "white_balance_temperature": 4500,
  "focus_auto": 0,
  "focus_absolute": 10
}
```

Use it with:

```bash
--camera-controls-config camera_controls.json
```

Only controls reported by the selected camera are applied. Unsupported requested controls cause a clear failure.

## Synthetic Test Recording

This mode does not need a physical webcam, but it still needs FFmpeg and an HEVC encoder:

```bash
python -m rfid_tracking.recording.ffmpeg_recorder \
  --synthetic \
  --segment-seconds 5 \
  --duration-seconds 12 \
  --output-dir /tmp/mouse_recorder_test
```

At 30 fps this should create three MP4 segments and three matching timestamp CSV files.

## File Lifecycle and Recovery

Segments are written with temporary names:

```text
record_20260715_144320.part.mp4
record_20260715_144320_timestamps.part.csv
```

After FFmpeg closes successfully, the files are atomically renamed to:

```text
record_20260715_144320.mp4
record_20260715_144320_timestamps.csv
```

Completed segments remain independently playable if the current process crashes. If `.part` files remain after a crash, keep them for diagnostics; they indicate the current segment was not finalized.

## Validation

After each segment closes, the recorder runs `ffprobe` and verifies:

- HEVC codec
- width 1280
- height 720
- nominal 30 fps
- video frame count matches CSV row count when FFprobe can count frames

Invalid segments are preserved and receive a `.invalid` marker with diagnostics.

## Shutdown

`Ctrl+C` and `SIGTERM` request graceful shutdown. The recorder stops capture, drains accepted frames when safe, finalizes the current MP4 and CSV, closes logs, and returns non-zero on failures such as queue overflow, encoder failure, disk-full errors, incomplete frame buffers, or broken pipes.

## Tests

The automated tests do not require a physical camera or FFmpeg:

```bash
python -m unittest discover -s tests
```

