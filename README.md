# MouseTracker FFmpeg Recorder

This repository includes a native Windows and optional Linux/WSL2 webcam recorder for mouse behavior sessions. It captures decoded frames from FFmpeg, timestamps each frame in Python with wall-clock and monotonic clocks, encodes H.265/HEVC MP4 segments, and writes one matching timestamp CSV per segment.

## Backends

The recorder supports:

- `dshow`: Windows DirectShow USB webcams
- `v4l2`: Linux/WSL2 Video4Linux2 webcams
- `auto`: DirectShow on native Windows, V4L2 elsewhere

Use:

```powershell
py -m rfid_tracking.recording.ffmpeg_recorder --backend auto
```

## Windows Requirements

Install FFmpeg for Windows and ensure both `ffmpeg` and `ffprobe` are on `PATH`.

Check DirectShow cameras:

```powershell
ffmpeg -hide_banner -list_devices true -f dshow -i dummy
```

Inspect modes for a camera:

```powershell
ffmpeg -hide_banner -list_options true -f dshow -i video="CAMERA NAME"
```

The recorder will not silently change resolution or frame rate. The selected camera must expose exactly 1280x720 at 30 fps.

## Default Output Directory

Native Windows default:

```text
D:\MouseTracker\data\mota
```

Linux/WSL2 default:

```text
/mnt/d/MouseTracker/data/mota
```

Each segment uses the wall-clock start time in the filename:

```text
record_20260715_144320.mp4
record_20260715_144320_timestamps.csv
```

## Preflight

Run preflight before a real mouse session:

```powershell
py -m rfid_tracking.recording.ffmpeg_recorder `
  --backend auto `
  --device auto `
  --output-dir D:\MouseTracker\data\mota `
  --width 1280 `
  --height 720 `
  --fps 30 `
  --segment-seconds 60 `
  --encoder auto `
  --gap-threshold-ms 50 `
  --preflight
```

On Windows, preflight verifies `ffmpeg`, `ffprobe`, output-directory writability, disk space, exact DirectShow camera mode support, and a functional HEVC encoder. It does not require WSL, `/dev/video*`, or `v4l2-ctl`.

## Recording Command

```powershell
py -m rfid_tracking.recording.ffmpeg_recorder `
  --backend auto `
  --device auto `
  --output-dir D:\MouseTracker\data\mota `
  --width 1280 `
  --height 720 `
  --fps 30 `
  --segment-seconds 60 `
  --encoder auto `
  --gap-threshold-ms 50
```

For a specific Windows camera, pass the DirectShow camera name:

```powershell
--backend dshow --device "USB Camera"
```

## Synthetic Test Recording

This mode does not need a physical webcam, but it still needs FFmpeg and a working HEVC encoder:

```powershell
py -m rfid_tracking.recording.ffmpeg_recorder `
  --synthetic `
  --segment-seconds 5 `
  --duration-seconds 12 `
  --output-dir C:\Temp\mouse_recorder_test
```

At 30 fps this should create three MP4 segments and three matching timestamp CSV files.

## Encoder Selection

On native Windows, `--encoder auto` probes real short HEVC encodes in this order:

```text
hevc_nvenc
hevc_qsv
hevc_amf
libx265
```

On Linux/V4L2, VAAPI is also considered:

```text
hevc_nvenc
hevc_qsv
hevc_vaapi
hevc_amf
libx265
```

The first encoder that completes a synthetic HEVC MP4 encode is selected. The recorder stops if no HEVC encoder works and never falls back to H.264.

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

Wall-clock time is used for filenames and correlation with RFID event timestamps. Monotonic time is used for frame intervals, gap detection, segment rollover, and movement-timing calculations.

## Validation

After each segment closes, the recorder runs `ffprobe` and verifies:

- HEVC codec
- width 1280
- height 720
- nominal 30 fps
- video frame count matches CSV row count when FFprobe can count frames

Invalid segments are preserved and receive a `.invalid` marker with diagnostics.

## File Lifecycle

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

Completed segments remain independently playable if the current process crashes.

## Optional V4L2/WSL2 Use

Install these inside the Linux environment:

```bash
sudo apt update
sudo apt install ffmpeg v4l-utils usbutils python3
```

List V4L2 devices:

```bash
python3 -m rfid_tracking.recording.ffmpeg_recorder --backend v4l2 --list-devices
```

USB webcam access in WSL2 may require `usbipd-win` on the Windows host. The recorder does not run privileged Windows or USB attachment commands automatically.

## Tests

The automated unit tests do not require a physical camera or FFmpeg:

```powershell
py -m unittest discover -s tests
```

