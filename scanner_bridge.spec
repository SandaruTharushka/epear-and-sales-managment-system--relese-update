# scanner_bridge.spec
# -------------------
# PyInstaller spec for the standalone scanner_bridge sidecar executable.
#
# Build:
#   pyinstaller scanner_bridge.spec
#
# Output: dist/scanner_bridge/scanner_bridge.exe

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

a = Analysis(
    ["scanner_bridge.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=[
        # Bundle the default config alongside the exe
        ("config/scanner_devices.json", "config"),
    ],
    hiddenimports=[
        "requests",
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "winreg",
        "ctypes",
        "ctypes.wintypes",
        "threading",
        "json",
        "logging",
        "logging.handlers",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "pandas",
        "PIL",
        "cv2",
        "scipy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="scanner_bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,      # no console window; it logs to file
    icon=None,          # ADAPT: set to your .ico path if desired
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="scanner_bridge",
)
