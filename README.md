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

## DECXIN Camera Profile

On Kenny's Windows recording machine, the intended camera is:

```text
DECXIN CAMERA
USB VID:PID 1bcf:2cd1
Hardware IDs:
  USB\VID_1BCF&PID_2CD1&REV_9281&MI_00
  USB\VID_1BCF&PID_2CD1&MI_00
Device Instance Path:
  USB\VID_1BCF&PID_2CD1&MI_00\6&1BD18552&0&0000
Parent:
  USB\VID_1BCF&PID_2CD1\01.00.00
monochrome global-shutter camera
```

The observed Windows `Physical device object name` was `\Device\000001d2`.
That field is logged only as a diagnostic because Windows may assign a
different object name after reconnects or reboot.

DirectShow exposes its usable 1280x720 at 30 fps mode through MJPEG:

```text
vcodec=mjpeg min s=1280x720 fps=10 max s=1280x720 fps=120
```

The same resolution over YUYV is only exposed at 10 fps, so MouseTracker must
select MJPEG for 720p30. On Windows, `--device auto` is intentionally
DECXIN-specific: it prefers `DECXIN CAMERA` and refuses to silently use the
laptop webcam or OBS virtual camera.

Because the DECXIN camera is monochrome, MouseTracker decodes captured frames
internally as raw `gray` frames instead of triplicated RGB/BGR. A 1280x720 frame
is therefore 921,600 bytes in the Python capture pipeline, while the finalized
MP4 is still validated as HEVC 1280x720 at 30 fps.

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
timestamp_source
capture_arrival_wall_time_unix_ns
capture_arrival_monotonic_ns
capture_arrival_offset_ms
elapsed_from_recording_start_s
elapsed_from_segment_start_s
inter_frame_interval_ms
expected_interval_ms
frame_gap_ms
gap_flag
```

Wall-clock time is used for filenames and correlation with RFID event timestamps. Monotonic time is used for frame intervals, gap detection, segment rollover, and movement-timing calculations.

On Windows DirectShow, MouseTracker defaults to `timestamp_source=cadence`.
FFmpeg/DirectShow reports the DECXIN stream as constant 30 fps, but Python's
raw pipe receives decoded frames in scheduler-sized bursts. The cadence
timestamp source anchors the first accepted frame to wall/performance-counter
time, then timestamps frame `n` at `n / fps`. This matches the HEVC video frame
timeline and prevents Windows pipe-delivery jitter from looking like animal
movement timing jitter.

The raw delivery timing is still preserved for diagnostics:

- `capture_arrival_wall_time_unix_ns`
- `capture_arrival_monotonic_ns`
- `capture_arrival_offset_ms`

Use `--timestamp-source arrival` only when you specifically want the Python
raw-frame arrival time instead of the video frame cadence.

## Validation

After each segment closes, the recorder runs `ffprobe` and verifies:

- HEVC codec
- width 1280
- height 720
- nominal 30 fps
- video frame count matches CSV row count when FFprobe can count frames

Invalid segments are preserved and receive a `.invalid` marker with diagnostics.

## Movement Analysis

The next layer after recording is offline movement analysis. This is meant for
the CSDS problem where a blurry webcam recording can make the mouse look like a
soft ghost and hide minor movements.

For each finalized segment, the analyzer reads:

```text
record_YYYYMMDD_HHMMSS.mp4
record_YYYYMMDD_HHMMSS_timestamps.csv
```

and writes:

```text
record_YYYYMMDD_HHMMSS_movement.csv
record_YYYYMMDD_HHMMSS_movement_summary.json
```

Run it manually:

```powershell
py -m rfid_tracking.analysis.movement `
  D:\MouseTracker\data\mota\record_20260715_144320.mp4 `
  --width 1280 `
  --height 720 `
  --fps 30
```

The movement CSV keeps the original timestamp fields and adds per-frame
measurements:

- motion pixels
- motion index
- mean absolute frame difference
- motion centroid
- motion bounding box
- left/center/right motion zone
- edge sharpness
- immobility flag
- blur-risk flag

The GUI also includes an `Analyze Latest Movement` button, which analyzes the
newest finalized `record_*.mp4` in the selected output directory.

This analysis does not yet claim final CSDS scoring. It produces synchronized
movement features that can later feed freezing, velocity, zone occupancy,
approach/avoidance, and social-interaction scoring.

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
