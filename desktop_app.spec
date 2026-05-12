# desktop_app.spec
# ----------------
# PyInstaller spec for the main Garage / Repair POS desktop application.
#
# Build:
#   pyinstaller desktop_app.spec
#
# Output: dist/GaragePOS/GaragePOS.exe

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ["launcher.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=[
        # Scanner bridge files
        ("scanner_bridge.py",   "."),
        ("scanner_launcher.py", "."),
        ("desktop_runtime.py",  "."),
        ("workshop_usage_routes.py", "."),
        ("config/scanner_devices.json", "config"),
        # Scanner settings UI
        ("static/scanner_settings.html", "static"),
        ("static/js/scanner_settings.js", "static/js"),
        # ADAPT: add your templates/, static/, and other resource dirs here
        # ("templates", "templates"),
        # ("static",    "static"),
    ],
    hiddenimports=[
        # Scanner bridge dependencies
        "requests",
        "requests.adapters",
        "urllib3",
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "winreg",
        "ctypes",
        "ctypes.wintypes",
        # Flask and web framework
        "flask",
        "flask.templating",
        "jinja2",
        "werkzeug",
        # ADAPT: add your app's hidden imports here
        # "sqlalchemy",
        # "sqlalchemy.dialects.sqlite",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "cv2",
        "PIL",
        "IPython",
        "jupyter",
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
    name="GaragePOS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,   # set True temporarily for debugging
    icon=None,       # ADAPT: "assets/icon.ico"
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GaragePOS",
)
