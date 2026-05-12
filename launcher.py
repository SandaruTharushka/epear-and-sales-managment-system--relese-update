"""
launcher.py
-----------
Top-level entry point for the packaged Garage / Repair POS installer.

This script is listed as the `console_scripts` / PyInstaller entry point.
It delegates to desktop.py after setting up the frozen-path environment.

When frozen (PyInstaller):
  sys._MEIPASS = unpacked bundle directory
  sys.executable = path to the .exe
  cwd is set to the directory containing the .exe

Usage:
    python launcher.py            # run normally
    python launcher.py --dev      # development mode
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── frozen path fix: ensure config/ and logs/ resolve relative to exe dir ───
if getattr(sys, "frozen", False):
    import os
    exe_dir = Path(sys.executable).parent
    os.chdir(exe_dir)

# ── add bundle root to sys.path when frozen ───────────────────────────────────
if getattr(sys, "_MEIPASS", None):
    _meipass = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    if str(_meipass) not in sys.path:
        sys.path.insert(0, str(_meipass))

# ── start the desktop app ─────────────────────────────────────────────────────
from desktop import main  # noqa: E402

if __name__ == "__main__":
    main()
