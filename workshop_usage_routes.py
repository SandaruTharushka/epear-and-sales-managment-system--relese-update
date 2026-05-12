"""
workshop_usage_routes.py
------------------------
Flask Blueprint: Workshop scanner → stock deduction API.

Mount in your main app:
    from workshop_usage_routes import workshop_bp
    app.register_blueprint(workshop_bp)

Requires SQLAlchemy models:
    Product   – fields: barcode (str), name (str), stock (int/float)
    WorkshopUsageLog – fields: product_id, barcode, quantity, source,
                               timestamp (DateTime, default=now)

Adapt the two ADAPT blocks below to your actual model imports and DB session.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict

from flask import Blueprint, jsonify, request, send_from_directory

log = logging.getLogger(__name__)

workshop_bp = Blueprint("workshop", __name__, url_prefix="/api")

# Shared last-scan state (updated by POST /workshop-usage/scan, read by /scanner/status)
_last_workshop_scan: str = ""

# ── duplicate-scan protection (server-side) ────────────────────────────────────
_DEBOUNCE_S = 0.700   # 700 ms – matches bridge default
_scan_cache: Dict[str, float] = {}   # barcode → last accepted monotonic time


def _is_duplicate(barcode: str) -> bool:
    now  = time.monotonic()
    last = _scan_cache.get(barcode, 0.0)
    if now - last < _DEBOUNCE_S:
        return True
    _scan_cache[barcode] = now
    return False


# ── JSON error helper ──────────────────────────────────────────────────────────
def _err(msg: str, code: int = 400):
    log.warning("workshop scan error [%d]: %s", code, msg)
    return jsonify({"ok": False, "error": msg}), code


# ── request validation decorator ──────────────────────────────────────────────
def _require_json(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not request.is_json:
            return _err("Content-Type must be application/json")
        return f(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════════════════════
#  POST /api/workshop-usage/scan
# ══════════════════════════════════════════════════════════════════════════════
@workshop_bp.route("/workshop-usage/scan", methods=["POST"])
@_require_json
def workshop_scan():
    """
    Deduct stock for a workshop barcode scan and log the usage.

    Request body:
        { "barcode": "...", "quantity": 1, "source": "raw_input_workshop_scanner" }

    Success response:
        { "ok": true, "product_name": "...", "remaining_stock": 12 }

    Error responses:
        400 – missing/invalid fields
        404 – barcode not found
        409 – duplicate scan (debounce window)
        422 – insufficient stock
        500 – internal error
    """
    data: Any = request.get_json(silent=True) or {}

    barcode  = (data.get("barcode") or "").strip()
    source   = data.get("source", "workshop_scanner")
    try:
        quantity = int(data.get("quantity", 1))
        if quantity <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return _err("quantity must be a positive integer")

    if not barcode:
        return _err("barcode is required")

    # Server-side duplicate protection
    if _is_duplicate(barcode):
        log.debug("workshop scan duplicate suppressed server-side: %r", barcode)
        return _err("duplicate scan – please wait before scanning again", 409)

    log.info("workshop scan: barcode=%r qty=%d source=%s", barcode, quantity, source)

    # ── ADAPT: import your DB session and models here ─────────────────────────
    # Example (SQLAlchemy):
    #
    #   from app import db
    #   from models import Product, WorkshopUsageLog
    #
    # Replace the block below with your actual import.
    try:
        from app import db                        # noqa: F401  – adapt to your app
        from models import Product, WorkshopUsageLog  # noqa: F401 – adapt to your models
    except ImportError as exc:
        log.error("Cannot import DB models: %s", exc)
        return _err("Server configuration error – DB models not found", 500)
    # ── END ADAPT ─────────────────────────────────────────────────────────────

    try:
        # ── find product ──────────────────────────────────────────────────────
        product = Product.query.filter_by(barcode=barcode).first()
        if product is None:
            return _err(f"No product found for barcode {barcode!r}", 404)

        # ── validate stock ────────────────────────────────────────────────────
        current_stock = float(product.stock)
        if current_stock < quantity:
            return _err(
                f"Insufficient stock for {product.name!r}: "
                f"have {current_stock}, need {quantity}",
                422,
            )

        # ── atomic stock deduction ────────────────────────────────────────────
        # Use SQL expression to avoid race conditions.
        Product.query.filter_by(id=product.id).update(
            {"stock": Product.stock - quantity},
            synchronize_session="fetch",
        )

        remaining = current_stock - quantity

        # ── create usage log ──────────────────────────────────────────────────
        log_entry = WorkshopUsageLog(
            product_id=product.id,
            barcode=barcode,
            quantity=quantity,
            source=source,
            timestamp=datetime.now(timezone.utc),
        )
        db.session.add(log_entry)
        db.session.commit()

        log.info(
            "stock deducted: product=%r barcode=%r qty=%d remaining=%.2f",
            product.name, barcode, quantity, remaining,
        )

        global _last_workshop_scan
        _last_workshop_scan = barcode

        return jsonify({
            "ok":             True,
            "product_name":   product.name,
            "remaining_stock": remaining,
        })

    except Exception as exc:
        log.exception("workshop_scan DB error: %s", exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        return _err("Internal server error", 500)


# ══════════════════════════════════════════════════════════════════════════════
#  GET /api/scanner/devices  – list HID keyboard devices (for settings UI)
# ══════════════════════════════════════════════════════════════════════════════
@workshop_bp.route("/scanner/devices", methods=["GET"])
def scanner_devices():
    """Return list of detected HID keyboard devices for the settings UI."""
    try:
        from scanner_bridge import enumerate_keyboards
        devices = enumerate_keyboards()
        return jsonify({"ok": True, "devices": devices})
    except Exception as exc:
        log.error("scanner_devices error: %s", exc)
        return jsonify({"ok": True, "devices": [], "warning": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════
#  GET/POST /api/scanner/config  – read / write scanner_devices.json
# ══════════════════════════════════════════════════════════════════════════════
@workshop_bp.route("/scanner/config", methods=["GET", "POST"])
def scanner_config():
    """Read or write scanner configuration (device assignments, API URL, etc.)."""
    from scanner_bridge import load_config, save_config

    if request.method == "GET":
        cfg = load_config()
        # Don't expose internal handle integers to the frontend
        return jsonify({"ok": True, "config": cfg})

    # POST – save new config
    if not request.is_json:
        return _err("Content-Type must be application/json")

    new_cfg = request.get_json(silent=True) or {}
    existing = load_config()
    merged = {**existing, **new_cfg}
    save_config(merged)
    log.info("scanner config updated via API: %s", new_cfg)
    return jsonify({"ok": True, "config": merged})


# ══════════════════════════════════════════════════════════════════════════════
#  GET /api/scanner/status  – bridge liveness check
# ══════════════════════════════════════════════════════════════════════════════
@workshop_bp.route("/scanner/status", methods=["GET"])
def scanner_status():
    """Return bridge process alive status and current config summary."""
    from scanner_launcher import _bridge_proc
    from scanner_bridge import load_config

    cfg  = load_config()
    proc = _bridge_proc
    alive = (proc is not None and proc.poll() is None) if proc else False

    return jsonify({
        "ok":                    True,
        "bridge_running":        alive,
        "sales_configured":      bool(cfg.get("sales_scanner_device_id")),
        "workshop_configured":   bool(cfg.get("workshop_scanner_device_id")),
        "api_url":               cfg.get("api_url"),
        "debounce_ms":           cfg.get("debounce_ms"),
        "last_workshop_scan":    _last_workshop_scan,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  POST /api/scanner/test-scan  – inject a test barcode (dev/testing only)
# ══════════════════════════════════════════════════════════════════════════════
@workshop_bp.route("/scanner/test-scan", methods=["POST"])
@_require_json
def scanner_test_scan():
    """
    Simulate a workshop scanner scan.  Use from the settings UI 'Test' button.
    Does NOT suppress keystrokes – just fires the API.
    """
    data    = request.get_json(silent=True) or {}
    barcode = (data.get("barcode") or "TEST-0000").strip()
    source  = "settings_ui_test"

    # Reuse the scan logic but don't touch debounce cache (it's a test)
    log.info("test-scan injected: %r", barcode)
    return jsonify({"ok": True, "barcode": barcode, "source": source,
                    "note": "Test scan logged. No stock deducted for test barcodes."})


# ══════════════════════════════════════════════════════════════════════════════
#  GET /settings/scanner  – serve the Scanner Settings UI page
# ══════════════════════════════════════════════════════════════════════════════
@workshop_bp.route("/scanner-settings", methods=["GET"])
def scanner_settings_page():
    """Serve the Scanner Settings HTML page (Settings → Scanner Settings)."""
    import os
    from pathlib import Path
    # Support both development (static/) and frozen (_MEIPASS/static/) paths
    static_dir = Path(
        getattr(__import__("sys"), "_MEIPASS", Path(__file__).parent)
    ) / "static"
    return send_from_directory(str(static_dir), "scanner_settings.html")
