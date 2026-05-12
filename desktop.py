"""
desktop.py
----------
PyWebView desktop application entrypoint for the Garage / Repair POS.

Usage:
    python desktop.py            # production
    python desktop.py --dev      # development (auto-reload)

Integrates the scanner bridge automatically on Windows.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ── resolve paths when frozen by PyInstaller ────────────────────────────────
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))

log = logging.getLogger("desktop")


def _configure_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "desktop.log", encoding="utf-8"),
        ],
    )


def _start_flask() -> None:
    """Import and launch the Flask backend in a background thread."""
    import threading

    def _run():
        try:
            # ADAPT: replace with your actual Flask app import
            from app import app  # noqa: F401
            app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
        except Exception as exc:
            log.error("Flask server error: %s", exc)

    t = threading.Thread(target=_run, daemon=True, name="FlaskServer")
    t.start()
    log.info("Flask server thread started")


def _start_scanner_bridge() -> None:
    """Launch scanner bridge subprocess (Windows only, non-fatal on failure)."""
    try:
        from scanner_launcher import start_scanner_bridge
        ok = start_scanner_bridge()
        if ok:
            log.info("Scanner bridge started successfully")
        else:
            log.info("Scanner bridge not started (non-Windows or not configured)")
    except Exception as exc:
        log.warning("Scanner bridge launch failed (POS will run normally): %s", exc)


def main() -> None:
    _configure_logging()

    parser = argparse.ArgumentParser(description="Garage/Repair POS Desktop")
    parser.add_argument("--dev", action="store_true", help="Enable development mode")
    args = parser.parse_args()

    log.info("Desktop POS starting (dev=%s)", args.dev)

    # 1. Start Flask backend
    _start_flask()

    # 2. Start scanner bridge (background, non-fatal)
    _start_scanner_bridge()

    # 3. Wait briefly for Flask to be ready
    import time
    time.sleep(1.5)

    # 4. Launch PyWebView window
    try:
        import webview  # type: ignore[import]

        url = "http://127.0.0.1:5000"
        window = webview.create_window(
            title="Garage POS",
            url=url,
            width=1280,
            height=800,
            min_size=(1024, 600),
            resizable=True,
        )
        webview.start(debug=args.dev)
    except ImportError:
        log.error("pywebview not installed – cannot open desktop window")
        log.info("Flask server is running at http://127.0.0.1:5000 (browser mode)")
        # Keep alive if running without pywebview
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
    finally:
        # Clean up scanner bridge on exit
        try:
            from scanner_launcher import stop_scanner_bridge
            stop_scanner_bridge()
        except Exception:
            pass


if __name__ == "__main__":
    main()
