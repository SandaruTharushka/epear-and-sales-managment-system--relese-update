"""
desktop_runtime.py
------------------
Runtime utilities shared by desktop.py and launcher.py:
  - Port availability check (wait for Flask to be ready)
  - Scanner bridge watchdog (auto-restart on crash)
  - Frozen-path helpers (sys._MEIPASS support)
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("desktop_runtime")

# ── frozen path helper ─────────────────────────────────────────────────────────

def base_path(*parts: str) -> Path:
    """Resolve a path relative to the application root (handles PyInstaller freeze)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base.joinpath(*parts)


def config_path(*parts: str) -> Path:
    """Resolve a writable config path (uses executable dir, not _MEIPASS)."""
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return exe_dir.joinpath(*parts)


# ── Flask readiness probe ──────────────────────────────────────────────────────

def wait_for_flask(host: str = "127.0.0.1", port: int = 5000,
                   timeout: float = 30.0, interval: float = 0.25) -> bool:
    """Block until Flask is accepting connections, or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(interval)
    log.error("Flask did not start within %.1f s", timeout)
    return False


# ── Scanner bridge watchdog ────────────────────────────────────────────────────

class ScannerWatchdog(threading.Thread):
    """
    Monitors the scanner bridge subprocess and restarts it on crash.
    Runs as a daemon thread – stops automatically when main process exits.
    Emits a warning callback when the workshop scanner disconnects.
    """

    RESTART_DELAY_S  = 5.0
    MAX_RESTARTS     = 20   # give up after this many rapid restarts

    def __init__(
        self,
        on_disconnect: Optional[callable] = None,
    ) -> None:
        super().__init__(daemon=True, name="ScannerWatchdog")
        self._on_disconnect = on_disconnect
        self._stop_event    = threading.Event()
        self._restarts      = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("ScannerWatchdog started")
        while not self._stop_event.is_set():
            from scanner_launcher import _bridge_proc, start_scanner_bridge

            proc = _bridge_proc
            if proc is not None and proc.poll() is not None:
                # Process has exited
                rc = proc.returncode
                log.warning("Scanner bridge exited (rc=%d), restarting in %.1fs …",
                            rc, self.RESTART_DELAY_S)

                if self._on_disconnect:
                    try:
                        self._on_disconnect("workshop scanner disconnected – reconnecting …")
                    except Exception:
                        pass

                self._restarts += 1
                if self._restarts > self.MAX_RESTARTS:
                    log.error(
                        "Scanner bridge has restarted %d times – giving up to protect POS stability",
                        self._restarts,
                    )
                    break

                self._stop_event.wait(self.RESTART_DELAY_S)
                if not self._stop_event.is_set():
                    start_scanner_bridge()
                    self._restarts = 0   # reset counter after successful restart attempt

            self._stop_event.wait(3.0)

        log.info("ScannerWatchdog stopped")


_watchdog: Optional[ScannerWatchdog] = None


def start_watchdog(on_disconnect: Optional[callable] = None) -> None:
    """Start the scanner bridge watchdog thread."""
    global _watchdog
    if _watchdog is not None and _watchdog.is_alive():
        return
    _watchdog = ScannerWatchdog(on_disconnect=on_disconnect)
    _watchdog.start()


def stop_watchdog() -> None:
    global _watchdog
    if _watchdog:
        _watchdog.stop()
        _watchdog = None
