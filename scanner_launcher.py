"""
scanner_launcher.py
-------------------
Drop-in helper for the main PyWebView desktop app.

Usage (in your main app's startup code):
    from scanner_launcher import start_scanner_bridge

    def on_app_ready():
        start_scanner_bridge()   # non-blocking; safe to call on any platform

The bridge runs as a detached subprocess.  If it fails or is unavailable
the main app continues normally – the billing scanner still works via
standard keyboard input.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_bridge_proc: Optional[subprocess.Popen] = None   # type: ignore[type-arg]


def start_scanner_bridge() -> bool:
    """
    Launch scanner_bridge.py in a detached subprocess.

    Returns True if the process started, False otherwise.
    The caller must NOT crash on False – the bridge is optional.
    """
    global _bridge_proc

    if sys.platform != "win32":
        log.debug("scanner_launcher: non-Windows platform – bridge skipped")
        return False

    script = Path(__file__).parent / "scanner_bridge.py"
    if not script.exists():
        log.warning("scanner_bridge.py not found at %s – bridge skipped", script)
        return False

    try:
        _bridge_proc = subprocess.Popen(
            [sys.executable, str(script)],
            creationflags=(
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            ),
            close_fds=True,
        )
        log.info("Scanner bridge started (pid=%d)", _bridge_proc.pid)
        return True
    except Exception as exc:
        log.warning("Could not start scanner bridge: %s – app will run normally", exc)
        return False


def stop_scanner_bridge() -> None:
    """Terminate the bridge subprocess gracefully on app exit."""
    global _bridge_proc
    if _bridge_proc is not None:
        try:
            _bridge_proc.terminate()
            _bridge_proc.wait(timeout=3)
        except Exception:
            pass
        finally:
            _bridge_proc = None
