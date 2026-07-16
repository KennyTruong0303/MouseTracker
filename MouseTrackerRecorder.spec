# Optional PyInstaller build spec.
#
# Build from the repository root after installing GUI dependencies:
#   pyinstaller MouseTrackerRecorder.spec
#
# FFmpeg and FFprobe are intentionally not bundled here. The application uses
# PATH, and the GUI can be extended to save a user-selected FFmpeg bin folder.

from PyInstaller.utils.hooks import collect_submodules


a = Analysis(
    ["rfid_tracking/recording/gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=collect_submodules("PySide6"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MouseTrackerRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
