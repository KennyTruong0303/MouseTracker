@echo off
setlocal
cd /d "%~dp0"
python -m rfid_tracking.recording.gui
if errorlevel 1 (
  echo.
  echo MouseTracker Recorder failed to start.
  pause
)
