#!/usr/bin/env python3
"""
Windows Raw Input Scanner Bridge
=========================================
Identifies which physical barcode scanner sent each keystroke using the
Windows Raw Input API, then routes input differently per device role:

  * Sales / Billing scanner (USB wired)    → normal keyboard pass-through
  * Workshop scanner (2.4G wireless)       → silently captured & POSTed to API

Requires: requests   (pip install requests)
Optional: pystray    (pip install pystray pillow)  – enables system-tray icon
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import sys
import threading
import time
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Guard – Windows only
# ──────────────────────────────────────────────────────────────────────────────
IS_WINDOWS = sys.platform == "win32"

# ──────────────────────────────────────────────────────────────────────────────
# Frozen-path support (PyInstaller)
# ──────────────────────────────────────────────────────────────────────────────
# When packaged, sys._MEIPASS points to the unpacked bundle.
# Writable files (logs, config) must live beside the .exe, not in _MEIPASS.
_IS_FROZEN = getattr(sys, "frozen", False)
if _IS_FROZEN:
    # Writable root = directory that contains the .exe
    _WRITABLE_ROOT = Path(sys.executable).parent
else:
    _WRITABLE_ROOT = Path(__file__).parent

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
_LOG_DIR = _WRITABLE_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_DIR / "scanner_bridge.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("scanner_bridge")
log.info("Scanner bridge initialising (frozen=%s, root=%s)", _IS_FROZEN, _WRITABLE_ROOT)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
_CFG_DIR  = _WRITABLE_ROOT / "config"
_CFG_PATH = _CFG_DIR / "scanner_devices.json"

_DEFAULT_CFG: dict = {
    "sales_scanner_device_id":    None,
    "workshop_scanner_device_id": None,
    "api_url":       "http://127.0.0.1:5000/api/workshop-usage/scan",
    "debounce_ms":   700,
    "sales_prefix":  "",          # e.g. "SALE-" or ""
}


def load_config() -> dict:
    if _CFG_PATH.exists():
        try:
            with open(_CFG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {**_DEFAULT_CFG, **data}
        except Exception as exc:
            log.warning("Config read error (%s) – using defaults", exc)
    return _DEFAULT_CFG.copy()


def save_config(cfg: dict) -> None:
    _CFG_DIR.mkdir(exist_ok=True)
    with open(_CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    log.info("Config saved → %s", _CFG_PATH)


# ──────────────────────────────────────────────────────────────────────────────
# Windows constants
# ──────────────────────────────────────────────────────────────────────────────
WM_INPUT         = 0x00FF
WM_DESTROY       = 0x0002
WM_CLOSE         = 0x0010
WM_CREATE        = 0x0001

RIM_TYPEMOUSE    = 0
RIM_TYPEKEYBOARD = 1
RIM_TYPEHID      = 2

RID_INPUT        = 0x10000003
RIDEV_INPUTSINK  = 0x00000100   # receive input even when not in focus

RIDI_DEVICENAME  = 0x20000007
RIDI_DEVICEINFO  = 0x2000000B

HID_USAGE_PAGE_GENERIC    = 0x01
HID_USAGE_GENERIC_KEYBOARD = 0x06

RI_KEY_MAKE  = 0x0000            # key-down
RI_KEY_BREAK = 0x0001            # key-up

VK_RETURN = 0x0D
VK_BACK   = 0x08
VK_ESCAPE = 0x1B
VK_SHIFT  = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_CAPITAL = 0x14

WH_KEYBOARD_LL = 13
WM_KEYDOWN     = 0x0100
WM_SYSKEYDOWN  = 0x0104
HC_ACTION      = 0

# ──────────────────────────────────────────────────────────────────────────────
# ctypes structures
# ──────────────────────────────────────────────────────────────────────────────

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage",     wintypes.USHORT),
        ("dwFlags",     wintypes.DWORD),
        ("hwndTarget",  wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType",  wintypes.DWORD),
        ("dwSize",  wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam",  wintypes.WPARAM),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode",         wintypes.USHORT),
        ("Flags",            wintypes.USHORT),
        ("Reserved",         wintypes.USHORT),
        ("VKey",             wintypes.USHORT),
        ("Message",          wintypes.UINT),
        ("ExtraInformation", ctypes.c_ulong),
    ]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags",            wintypes.USHORT),
        ("usButtonFlags",      wintypes.USHORT),
        ("usButtonData",       wintypes.USHORT),
        ("ulRawButtons",       ctypes.c_ulong),
        ("lLastX",             ctypes.c_long),
        ("lLastY",             ctypes.c_long),
        ("ulExtraInformation", ctypes.c_ulong),
    ]


class RAWHID(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", wintypes.DWORD),
        ("dwCount",   wintypes.DWORD),
        ("bRawData",  ctypes.c_byte),
    ]


class _RAWINPUT_DATA(ctypes.Union):
    _fields_ = [
        ("keyboard", RAWKEYBOARD),
        ("mouse",    RAWMOUSE),
        ("hid",      RAWHID),
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("data",   _RAWINPUT_DATA),
    ]


class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [
        ("hDevice", wintypes.HANDLE),
        ("dwType",  wintypes.DWORD),
    ]


class RID_DEVICE_INFO_KEYBOARD(ctypes.Structure):
    _fields_ = [
        ("dwType",                 wintypes.DWORD),
        ("dwSubType",              wintypes.DWORD),
        ("dwKeyboardMode",         wintypes.DWORD),
        ("dwNumberOfFunctionKeys", wintypes.DWORD),
        ("dwNumberOfIndicators",   wintypes.DWORD),
        ("dwNumberOfKeysTotal",    wintypes.DWORD),
    ]


class RID_DEVICE_INFO_MOUSE(ctypes.Structure):
    _fields_ = [
        ("dwId",              wintypes.DWORD),
        ("dwNumberOfButtons", wintypes.DWORD),
        ("dwSampleRate",      wintypes.DWORD),
        ("fHasHorizontalWheel", wintypes.BOOL),
    ]


class RID_DEVICE_INFO_HID(ctypes.Structure):
    _fields_ = [
        ("dwVendorId",      wintypes.DWORD),
        ("dwProductId",     wintypes.DWORD),
        ("dwVersionNumber", wintypes.DWORD),
        ("usUsagePage",     wintypes.USHORT),
        ("usUsage",         wintypes.USHORT),
    ]


class _RID_DEVICE_INFO_DATA(ctypes.Union):
    _fields_ = [
        ("keyboard", RID_DEVICE_INFO_KEYBOARD),
        ("mouse",    RID_DEVICE_INFO_MOUSE),
        ("hid",      RID_DEVICE_INFO_HID),
    ]


class RID_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("data",   _RID_DEVICE_INFO_DATA),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode",      wintypes.DWORD),
        ("scanCode",    wintypes.DWORD),
        ("flags",       wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulong),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize",        wintypes.UINT),
        ("style",         wintypes.UINT),
        ("lpfnWndProc",   ctypes.c_void_p),
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     wintypes.HINSTANCE),
        ("hIcon",         wintypes.HICON),
        ("hCursor",       wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName",  wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm",       wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd",    wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam",  wintypes.WPARAM),
        ("lParam",  wintypes.LPARAM),
        ("time",    wintypes.DWORD),
        ("pt",      wintypes.POINT),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Windows API function types
# ──────────────────────────────────────────────────────────────────────────────
if IS_WINDOWS:
    _WNDPROCTYPE = ctypes.WINFUNCTYPE(
        wintypes.LRESULT,
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    )
    _HOOKPROCTYPE = ctypes.WINFUNCTYPE(
        wintypes.LRESULT,
        ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    )
    _user32   = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
else:
    _WNDPROCTYPE  = None
    _HOOKPROCTYPE = None
    _user32       = None
    _kernel32     = None

# ──────────────────────────────────────────────────────────────────────────────
# VKey → character mapping
# ──────────────────────────────────────────────────────────────────────────────
_VK_MAP: Dict[int, Tuple[str, str]] = {
    0x30: ("0", ")"), 0x31: ("1", "!"), 0x32: ("2", "@"),
    0x33: ("3", "#"), 0x34: ("4", "$"), 0x35: ("5", "%"),
    0x36: ("6", "^"), 0x37: ("7", "&"), 0x38: ("8", "*"),
    0x39: ("9", "("),
    **{i: (chr(i + 32), chr(i)) for i in range(0x41, 0x5B)},  # a-z / A-Z
    0x60: ("0", "0"), 0x61: ("1", "1"), 0x62: ("2", "2"),
    0x63: ("3", "3"), 0x64: ("4", "4"), 0x65: ("5", "5"),
    0x66: ("6", "6"), 0x67: ("7", "7"), 0x68: ("8", "8"),
    0x69: ("9", "9"),
    0x6A: ("*", "*"), 0x6B: ("+", "+"), 0x6D: ("-", "-"),
    0x6E: (".", "."), 0x6F: ("/", "/"),
    0xBB: ("=", "+"), 0xBD: ("-", "_"), 0xBC: (",", "<"),
    0xBE: (".", ">"), 0xBF: ("/", "?"), 0xC0: ("`", "~"),
    0xDB: ("[", "{"), 0xDC: ("\\", "|"), 0xDD: ("]", "}"),
    0xDE: ("'", '"'),  0xBA: (";", ":"),
    0x20: (" ", " "),
}


def _vkey_to_char(vkey: int, shifted: bool) -> Optional[str]:
    entry = _VK_MAP.get(vkey)
    return (entry[1] if shifted else entry[0]) if entry else None


# ──────────────────────────────────────────────────────────────────────────────
# Device enumeration
# ──────────────────────────────────────────────────────────────────────────────

def enumerate_keyboards() -> List[dict]:
    """Return a list of connected keyboard HID devices."""
    if not IS_WINDOWS:
        return []

    count  = wintypes.UINT(0)
    item_sz = ctypes.sizeof(RAWINPUTDEVICELIST)

    _user32.GetRawInputDeviceList(None, ctypes.byref(count), item_sz)
    if count.value == 0:
        return []

    buf    = (RAWINPUTDEVICELIST * count.value)()
    result = _user32.GetRawInputDeviceList(buf, ctypes.byref(count), item_sz)
    if result == ctypes.c_uint(-1).value:
        log.error("GetRawInputDeviceList failed: %d", ctypes.GetLastError())
        return []

    devices = []
    for item in buf:
        if item.dwType != RIM_TYPEKEYBOARD:
            continue

        # Device path (e.g. \\?\HID#VID_xxxx&PID_xxxx#...)
        name_len = wintypes.UINT(0)
        _user32.GetRawInputDeviceInfoW(item.hDevice, RIDI_DEVICENAME, None, ctypes.byref(name_len))
        name_buf = ctypes.create_unicode_buffer(name_len.value + 1)
        _user32.GetRawInputDeviceInfoW(item.hDevice, RIDI_DEVICENAME, name_buf, ctypes.byref(name_len))
        device_path = name_buf.value

        # Keyboard info
        info = RID_DEVICE_INFO()
        info.cbSize = ctypes.sizeof(RID_DEVICE_INFO)
        info_sz = wintypes.UINT(ctypes.sizeof(RID_DEVICE_INFO))
        _user32.GetRawInputDeviceInfoW(item.hDevice, RIDI_DEVICEINFO, ctypes.byref(info), ctypes.byref(info_sz))

        friendly = _registry_friendly_name(device_path)
        if not friendly:
            parts = [p for p in device_path.replace("\\\\?\\", "").split("#") if p]
            friendly = parts[1] if len(parts) > 1 else "Unknown Keyboard"

        devices.append({
            "handle":        item.hDevice,
            "device_id":     device_path,
            "friendly_name": friendly,
            "keys_total":    info.data.keyboard.dwNumberOfKeysTotal,
        })

    return devices


def _registry_friendly_name(device_path: str) -> Optional[str]:
    """Look up a human-readable device name from the Windows registry."""
    try:
        import winreg
        if not device_path.startswith("\\\\?\\"):
            return None
        raw = device_path[4:].replace("#", "\\")
        reg_path = "SYSTEM\\CurrentControlSet\\Enum\\" + raw
        key  = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
        desc, _ = winreg.QueryValueEx(key, "DeviceDesc")
        winreg.CloseKey(key)
        return desc.split(";")[-1].strip() if ";" in desc else desc.strip()
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Per-device keystroke buffer  →  barcode string
# ──────────────────────────────────────────────────────────────────────────────

class ScannerBuffer:
    """Accumulates key-down events per device handle; returns barcode on Enter."""

    def __init__(self) -> None:
        self._chars:  Dict[int, List[str]] = defaultdict(list)
        self._shifted: Dict[int, bool]     = defaultdict(bool)

    def key_down(self, handle: int, vkey: int) -> Optional[str]:
        if vkey in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT):
            self._shifted[handle] = True
            return None

        if vkey == VK_RETURN:
            barcode = "".join(self._chars[handle])
            self._chars[handle]   = []
            self._shifted[handle] = False
            return barcode or None

        if vkey == VK_BACK:
            if self._chars[handle]:
                self._chars[handle].pop()
            return None

        if vkey == VK_ESCAPE:
            self._chars[handle]   = []
            self._shifted[handle] = False
            return None

        ch = _vkey_to_char(vkey, self._shifted[handle])
        if ch:
            self._chars[handle].append(ch)
        return None

    def key_up(self, handle: int, vkey: int) -> None:
        if vkey in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT):
            self._shifted[handle] = False

    def clear(self, handle: int) -> None:
        self._chars.pop(handle, None)
        self._shifted.pop(handle, None)


# ──────────────────────────────────────────────────────────────────────────────
# Workshop HTTP handler
# ──────────────────────────────────────────────────────────────────────────────

class WorkshopHandler:
    """POSTs barcode to the workshop usage API with duplicate-scan protection."""

    def __init__(
        self,
        api_url:      str,
        debounce_ms:  int,
        status_cb:    Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.api_url    = api_url
        self._debounce  = debounce_ms / 1000.0
        self._status_cb = status_cb
        self._last: Dict[str, float] = {}   # barcode → last-sent monotonic time

    def handle_scan(self, barcode: str) -> None:
        barcode = barcode.strip()
        if not barcode:
            return

        now  = time.monotonic()
        last = self._last.get(barcode, 0.0)
        if now - last < self._debounce:
            log.debug("Workshop duplicate suppressed: %r", barcode)
            return
        self._last[barcode] = now

        log.info("Workshop scan: %r", barcode)
        threading.Thread(
            target=self._post, args=(barcode,), daemon=True, name="WorkshopPost"
        ).start()

    def _post(self, barcode: str) -> None:
        try:
            import requests as _req
            payload = {
                "barcode":  barcode,
                "quantity": 1,
                "source":   "raw_input_workshop_scanner",
            }
            resp = _req.post(self.api_url, json=payload, timeout=5)
            resp.raise_for_status()
            log.info("Workshop POST OK %d ← %r", resp.status_code, barcode)
            if self._status_cb:
                self._status_cb("last_workshop_scan", barcode)
                self._status_cb("last_error", "")
        except Exception as exc:
            msg = f"POST failed for {barcode!r}: {exc}"
            log.error(msg)
            if self._status_cb:
                self._status_cb("last_error", msg)


# ──────────────────────────────────────────────────────────────────────────────
# Raw Input Bridge  (hidden Win32 window + low-level keyboard hook)
# ──────────────────────────────────────────────────────────────────────────────

class RawInputBridge:
    """
    Spawns a background thread that:
      1. Creates a hidden message-only Win32 window registered for WM_INPUT
         (RIDEV_INPUTSINK – receives input even when app is not focused).
      2. Installs a WH_KEYBOARD_LL hook to suppress workshop-scanner keystrokes
         before they reach the focused window.
      3. Routes each keystroke:
           sales role     → pass through unchanged
           workshop role  → buffer until Enter, then POST; keystroke suppressed
    """

    _WND_CLASS = "ScannerBridgeWndClass"

    def __init__(
        self,
        cfg:       dict,
        status_cb: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._cfg       = cfg
        self._status_cb = status_cb

        self._sales_id:    Optional[str] = cfg.get("sales_scanner_device_id")
        self._workshop_id: Optional[str] = cfg.get("workshop_scanner_device_id")

        self._handle_map: Dict[int, str] = {}   # os handle → device_id
        self._buffer   = ScannerBuffer()
        self._workshop = WorkshopHandler(
            cfg.get("api_url",     _DEFAULT_CFG["api_url"]),
            cfg.get("debounce_ms", _DEFAULT_CFG["debounce_ms"]),
            status_cb=status_cb,
        )

        # Keys pending suppression: vkey → expiry (monotonic)
        self._suppress:      Dict[int, float] = {}
        self._suppress_lock  = threading.Lock()

        self._hwnd:     Optional[int] = None
        self._hook:     Optional[int] = None
        self._thread:   Optional[threading.Thread] = None
        self._running   = False

        # Keep callable refs alive so the GC doesn't free them
        self._wndproc_ref:  Optional[object] = None
        self._hookproc_ref: Optional[object] = None

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not IS_WINDOWS:
            log.warning("RawInputBridge: non-Windows platform – bridge disabled")
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._message_loop, daemon=True, name="RawInputBridge"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def wait(self, timeout: float = 3.0) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def update_config(self, cfg: dict) -> None:
        self._cfg          = cfg
        self._sales_id     = cfg.get("sales_scanner_device_id")
        self._workshop_id  = cfg.get("workshop_scanner_device_id")
        self._workshop.api_url   = cfg.get("api_url",     _DEFAULT_CFG["api_url"])
        self._workshop._debounce = cfg.get("debounce_ms", _DEFAULT_CFG["debounce_ms"]) / 1000.0

    # ── internals ─────────────────────────────────────────────────────────────

    def _refresh_handle_map(self) -> None:
        self._handle_map = {d["handle"]: d["device_id"] for d in enumerate_keyboards()}

    def _role_for_handle(self, handle: int) -> Optional[str]:
        dev_id = self._handle_map.get(handle)
        if dev_id is None:
            self._refresh_handle_map()
            dev_id = self._handle_map.get(handle)
        if not dev_id:
            return None
        if dev_id == self._sales_id:
            return "sales"
        if dev_id == self._workshop_id:
            return "workshop"
        return None

    def _message_loop(self) -> None:
        self._refresh_handle_map()

        # ── register window class ────────────────────────────────────────────
        wndproc      = _WNDPROCTYPE(self._wnd_proc)
        self._wndproc_ref = wndproc          # prevent GC

        wc = WNDCLASSEXW()
        wc.cbSize       = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc  = ctypes.cast(wndproc, ctypes.c_void_p)
        wc.hInstance    = _kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self._WND_CLASS

        atom = _user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            err = ctypes.GetLastError()
            if err != 1410:             # ERROR_CLASS_ALREADY_EXISTS – acceptable
                log.error("RegisterClassExW failed: %d", err)
                return

        # ── create hidden message-only window ────────────────────────────────
        hwnd = _user32.CreateWindowExW(
            0, self._WND_CLASS, "Scanner Bridge",
            0, 0, 0, 0, 0,
            -3,     # HWND_MESSAGE
            None, _kernel32.GetModuleHandleW(None), None,
        )
        if not hwnd:
            log.error("CreateWindowExW failed: %d", ctypes.GetLastError())
            return
        self._hwnd = hwnd

        # ── register Raw Input (all keyboards, background capture) ───────────
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = HID_USAGE_PAGE_GENERIC
        rid.usUsage     = HID_USAGE_GENERIC_KEYBOARD
        rid.dwFlags     = RIDEV_INPUTSINK
        rid.hwndTarget  = hwnd
        if not _user32.RegisterRawInputDevices(
            ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)
        ):
            log.error("RegisterRawInputDevices failed: %d", ctypes.GetLastError())

        # ── install low-level keyboard hook (for workshop suppression) ───────
        hookproc = _HOOKPROCTYPE(self._keyboard_hook)
        self._hookproc_ref = hookproc

        self._hook = _user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, hookproc, None, 0
        )
        if not self._hook:
            log.warning(
                "SetWindowsHookExW failed (%d) – workshop keystroke suppression disabled",
                ctypes.GetLastError(),
            )

        log.info("RawInputBridge running (hwnd=%d)", hwnd)
        if self._status_cb:
            self._status_cb("bridge_status", "Running")

        # ── message pump ────────────────────────────────────────────────────
        msg = MSG()
        while self._running:
            r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        # ── cleanup ──────────────────────────────────────────────────────────
        if self._hook:
            _user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._hwnd:
            _user32.DestroyWindow(self._hwnd)
            self._hwnd = None

        log.info("RawInputBridge stopped")
        if self._status_cb:
            self._status_cb("bridge_status", "Stopped")

    # ── WndProc ───────────────────────────────────────────────────────────────

    def _wnd_proc(
        self,
        hwnd:   int,
        msg:    int,
        wparam: int,
        lparam: int,
    ) -> int:
        if msg == WM_INPUT:
            try:
                self._process_raw_input(lparam)
            except Exception as exc:
                log.debug("_process_raw_input error: %s", exc)
            return 0
        if msg in (WM_CLOSE, WM_DESTROY):
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _process_raw_input(self, lparam: int) -> None:
        size = wintypes.UINT(0)
        hdr_sz = ctypes.sizeof(RAWINPUTHEADER)
        _user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), hdr_sz)
        if size.value == 0:
            return

        buf    = ctypes.create_string_buffer(size.value)
        filled = _user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), hdr_sz)
        if filled != size.value:
            return

        raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType != RIM_TYPEKEYBOARD:
            return

        handle    = raw.header.hDevice
        flags     = raw.data.keyboard.Flags
        vkey      = raw.data.keyboard.VKey
        is_down   = not bool(flags & RI_KEY_BREAK)

        role = self._role_for_handle(handle)
        if role is None:
            return                              # unmapped device – pass through

        if role == "workshop":
            if is_down:
                barcode = self._buffer.key_down(handle, vkey)
                if barcode:
                    self._workshop.handle_scan(barcode)
                # Mark vkey for suppression in the keyboard hook
                with self._suppress_lock:
                    self._suppress[vkey] = time.monotonic() + 0.08   # 80 ms window
            else:
                self._buffer.key_up(handle, vkey)

        # sales role: nothing extra to do – keystrokes pass through normally

    # ── Keyboard hook (runs in the same message loop thread) ─────────────────

    def _keyboard_hook(self, n_code: int, wparam: int, lparam: int) -> int:
        if n_code == HC_ACTION and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb   = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vkey = kb.vkCode
            with self._suppress_lock:
                expiry = self._suppress.get(vkey, 0.0)
            if expiry and time.monotonic() < expiry:
                with self._suppress_lock:
                    self._suppress.pop(vkey, None)
                return 1        # suppress: do NOT call CallNextHookEx

        return _user32.CallNextHookEx(self._hook or 0, n_code, wparam, lparam)


# ──────────────────────────────────────────────────────────────────────────────
# Device-setup dialog
# ──────────────────────────────────────────────────────────────────────────────

class SetupDialog:
    """
    Modal dialog that lets the user assign physical scanners to roles.
    Shows all detected keyboard HID devices in a list with their device paths.
    """

    def __init__(
        self,
        parent:    tk.Misc,
        cfg:       dict,
        on_save:   Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._cfg     = cfg.copy()
        self._on_save = on_save
        self._devices = enumerate_keyboards()

        self.win = tk.Toplevel(parent)
        self.win.title("Configure Scanner Devices")
        self.win.resizable(False, False)
        self.win.grab_set()
        self._build()

    def _build(self) -> None:
        F   = ttk.Frame(self.win, padding=14)
        pad = {"padx": 8, "pady": 5}
        F.pack(fill="both", expand=True)

        ttk.Label(
            F, text="Map physical scanners to roles",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        options    = ["— Not mapped —"] + [
            f"{d['friendly_name']}  [{d['device_id'][-44:]}]"
            for d in self._devices
        ]
        device_ids = [None] + [d["device_id"] for d in self._devices]

        def current_idx(key: str) -> int:
            cur = self._cfg.get(key)
            return device_ids.index(cur) if cur in device_ids else 0

        # Sales scanner
        ttk.Label(F, text="Sales / Billing scanner (USB wired):").grid(
            row=1, column=0, sticky="w", **pad
        )
        self._sales_var = tk.StringVar()
        self._sales_cb  = ttk.Combobox(
            F, textvariable=self._sales_var,
            values=options, width=60, state="readonly",
        )
        self._sales_cb.current(current_idx("sales_scanner_device_id"))
        self._sales_cb.grid(row=1, column=1, **pad)

        # Workshop scanner
        ttk.Label(F, text="Workshop scanner (2.4 GHz wireless):").grid(
            row=2, column=0, sticky="w", **pad
        )
        self._ws_var = tk.StringVar()
        self._ws_cb  = ttk.Combobox(
            F, textvariable=self._ws_var,
            values=options, width=60, state="readonly",
        )
        self._ws_cb.current(current_idx("workshop_scanner_device_id"))
        self._ws_cb.grid(row=2, column=1, **pad)

        # API URL
        ttk.Label(F, text="Workshop API endpoint:").grid(row=3, column=0, sticky="w", **pad)
        self._api_var = tk.StringVar(value=self._cfg.get("api_url", _DEFAULT_CFG["api_url"]))
        ttk.Entry(F, textvariable=self._api_var, width=62).grid(row=3, column=1, **pad)

        # Debounce
        ttk.Label(F, text="Duplicate protection (ms):").grid(row=4, column=0, sticky="w", **pad)
        self._deb_var = tk.StringVar(
            value=str(self._cfg.get("debounce_ms", _DEFAULT_CFG["debounce_ms"]))
        )
        ttk.Entry(F, textvariable=self._deb_var, width=10).grid(row=4, column=1, sticky="w", **pad)

        # Sales prefix
        ttk.Label(F, text="Sales barcode prefix (optional):").grid(row=5, column=0, sticky="w", **pad)
        self._pfx_var = tk.StringVar(value=self._cfg.get("sales_prefix", ""))
        ttk.Entry(F, textvariable=self._pfx_var, width=20).grid(row=5, column=1, sticky="w", **pad)

        # Detected device list (read-only)
        sep = ttk.Separator(F, orient="horizontal")
        sep.grid(row=6, column=0, columnspan=2, sticky="ew", pady=8)

        ttk.Label(
            F, text="Detected keyboard HID devices:",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 2))

        lb_frame = ttk.Frame(F)
        lb_frame.grid(row=8, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="ew")

        sb = ttk.Scrollbar(lb_frame, orient="vertical")
        lb = tk.Listbox(
            lb_frame, width=90, height=6,
            font=("Consolas", 8),
            yscrollcommand=sb.set,
        )
        sb.config(command=lb.yview)
        sb.pack(side="right", fill="y")
        lb.pack(side="left", fill="both", expand=True)

        if self._devices:
            for d in self._devices:
                lb.insert("end", f"{d['friendly_name']:<40s}  {d['device_id']}")
        else:
            lb.insert("end", "(No keyboard devices found – running on non-Windows?)")

        self._device_ids = device_ids

        # Buttons
        btn_f = ttk.Frame(F)
        btn_f.grid(row=9, column=0, columnspan=2, pady=(4, 0))
        ttk.Button(btn_f, text="Refresh",  command=self._refresh).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Save",     command=self._save).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Cancel",   command=self.win.destroy).pack(side="left", padx=4)

    def _refresh(self) -> None:
        self.win.destroy()
        SetupDialog(self.win.master, self._cfg, on_save=self._on_save)

    def _save(self) -> None:
        try:
            deb = int(self._deb_var.get() or str(_DEFAULT_CFG["debounce_ms"]))
        except ValueError:
            deb = _DEFAULT_CFG["debounce_ms"]

        new_cfg = {
            **self._cfg,
            "sales_scanner_device_id":    self._device_ids[self._sales_cb.current()],
            "workshop_scanner_device_id": self._device_ids[self._ws_cb.current()],
            "api_url":      self._api_var.get().strip(),
            "debounce_ms":  deb,
            "sales_prefix": self._pfx_var.get(),
        }
        if self._on_save:
            self._on_save(new_cfg)
        self.win.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# Status window  (tkinter, runs on the main thread)
# ──────────────────────────────────────────────────────────────────────────────

class StatusWindow:
    """
    Small always-visible window showing live bridge status:
      • Bridge running/stopped
      • Sales scanner configured / connected
      • Workshop scanner configured / connected
      • Last workshop barcode scanned
      • Last error message
    """

    def __init__(self, bridge: RawInputBridge, cfg: dict) -> None:
        self._bridge = bridge
        self._cfg    = cfg
        self._vals: Dict[str, str] = {
            "bridge_status":     "Starting…",
            "sales_scanner":     "Configured" if cfg.get("sales_scanner_device_id") else "Not configured",
            "workshop_scanner":  "Configured" if cfg.get("workshop_scanner_device_id") else "Not configured",
            "last_workshop_scan": "—",
            "last_error":        "",
        }
        self._labels: Dict[str, ttk.Label] = {}
        self.root: Optional[tk.Tk] = None

    def status_cb(self, key: str, value: str) -> None:
        """Thread-safe callback from bridge / workshop handler."""
        self._vals[key] = value
        if self.root:
            self.root.after(0, self._refresh)

    def run(self) -> None:
        self.root = tk.Tk()
        self.root.title("Scanner Bridge")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        # Pop to front briefly, then release always-on-top
        self.root.attributes("-topmost", True)
        self.root.after(1500, lambda: self.root.attributes("-topmost", False))

        self._build_ui()
        self._refresh()
        self.root.mainloop()

    def _build_ui(self) -> None:
        F   = ttk.Frame(self.root, padding=14)
        pad = {"padx": 8, "pady": 3}
        F.pack(fill="both", expand=True)

        ttk.Label(
            F, text="Scanner Bridge",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        rows = [
            ("Bridge status:",        "bridge_status"),
            ("Sales scanner:",        "sales_scanner"),
            ("Workshop scanner:",     "workshop_scanner"),
            ("Last workshop scan:",   "last_workshop_scan"),
            ("Last error:",           "last_error"),
        ]
        for r, (caption, key) in enumerate(rows, start=1):
            ttk.Label(F, text=caption, foreground="#555").grid(
                row=r, column=0, sticky="w", **pad
            )
            lbl = ttk.Label(F, text="—", wraplength=340)
            lbl.grid(row=r, column=1, sticky="w", **pad)
            self._labels[key] = lbl

        sep = ttk.Separator(F, orient="horizontal")
        sep.grid(row=len(rows)+1, column=0, columnspan=2, sticky="ew", pady=8)

        btn_f = ttk.Frame(F)
        btn_f.grid(row=len(rows)+2, column=0, columnspan=2)
        ttk.Button(btn_f, text="Configure Devices", command=self._open_setup).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Auto-start: Enable",  command=self._autostart_on).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Auto-start: Disable", command=self._autostart_off).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Exit", command=self._quit).pack(side="left", padx=4)

    def _refresh(self) -> None:
        # Mirror cfg state into display values
        self._vals["sales_scanner"]    = (
            "Configured" if self._cfg.get("sales_scanner_device_id") else "Not configured"
        )
        self._vals["workshop_scanner"] = (
            "Configured" if self._cfg.get("workshop_scanner_device_id") else "Not configured"
        )
        for key, lbl in self._labels.items():
            val = self._vals.get(key, "")
            lbl.config(text=val or "—")
            if key == "last_error":
                lbl.config(foreground="red" if val else "black")
            elif key == "bridge_status":
                lbl.config(foreground="green" if val == "Running" else "orange")

    def _open_setup(self) -> None:
        SetupDialog(self.root, self._cfg, on_save=self._on_config_saved)

    def _on_config_saved(self, new_cfg: dict) -> None:
        self._cfg.update(new_cfg)
        save_config(self._cfg)
        self._bridge.update_config(new_cfg)
        self._refresh()
        messagebox.showinfo(
            "Saved",
            "Device mapping saved.\n\n"
            "The bridge has been updated with the new mapping – no restart needed.",
            parent=self.root,
        )

    def _autostart_on(self) -> None:
        ok = _register_autostart()
        if ok:
            messagebox.showinfo("Auto-start", "Scanner Bridge will start with Windows.", parent=self.root)
        else:
            messagebox.showerror("Auto-start", "Failed to register auto-start (check permissions).", parent=self.root)

    def _autostart_off(self) -> None:
        _remove_autostart()
        messagebox.showinfo("Auto-start", "Auto-start removed.", parent=self.root)

    def _quit(self) -> None:
        self._bridge.stop()
        if self.root:
            self.root.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# Windows auto-start via registry
# ──────────────────────────────────────────────────────────────────────────────
_AUTOSTART_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "ScannerBridge"


def _register_autostart() -> bool:
    try:
        import winreg
        exe    = sys.executable
        script = str(Path(__file__).resolve())
        cmd    = f'"{exe}" "{script}"'
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY,
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        log.info("Auto-start registered: %s", cmd)
        return True
    except Exception as exc:
        log.error("Auto-start registration failed: %s", exc)
        return False


def _remove_autostart() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY,
            0, winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, _AUTOSTART_NAME)
        winreg.CloseKey(key)
        log.info("Auto-start removed")
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Subprocess launcher  (called from the main desktop app)
# ──────────────────────────────────────────────────────────────────────────────

def launch_bridge_subprocess() -> Optional[object]:
    """
    Launch scanner_bridge.py as a detached subprocess.
    Returns the Popen handle, or None if launch fails.
    The main app MUST NOT crash if this returns None.

    Handles both normal (script) and frozen (PyInstaller) execution.
    """
    import subprocess
    try:
        if _IS_FROZEN:
            # Frozen: look for a sidecar scanner_bridge.exe, else re-run main exe
            sidecar = Path(sys.executable).parent / "scanner_bridge.exe"
            if sidecar.exists():
                cmd = [str(sidecar)]
            else:
                # Re-invoke the same frozen EXE – it must handle a --bridge flag
                cmd = [sys.executable, "--bridge"]
        else:
            script = Path(__file__).resolve()
            cmd = [sys.executable, str(script)]

        proc = subprocess.Popen(
            cmd,
            creationflags=(
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            ) if IS_WINDOWS else 0,
            close_fds=True,
        )
        log.info("Scanner bridge subprocess launched (pid=%d)", proc.pid)
        return proc
    except Exception as exc:
        log.warning("Could not launch scanner bridge: %s (continuing without it)", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Single-instance guard
# ──────────────────────────────────────────────────────────────────────────────
_MUTEX_NAME = "ScannerBridgeSingleInstance"
_mutex_handle = None


def _acquire_single_instance() -> bool:
    """Returns False if another instance is already running."""
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    try:
        _mutex_handle = _kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        err = ctypes.GetLastError()
        return err != 183    # ERROR_ALREADY_EXISTS
    except Exception:
        return True


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not IS_WINDOWS:
        print("scanner_bridge.py: Windows is required.  Exiting.")
        sys.exit(1)

    if not _acquire_single_instance():
        print("Scanner bridge is already running.")
        sys.exit(0)

    cfg = load_config()

    # First-run: open setup dialog immediately if no devices are mapped
    first_run = not (cfg.get("sales_scanner_device_id") or cfg.get("workshop_scanner_device_id"))

    # Status routing – the bridge and window share this callback
    status_win_holder: List[Optional[StatusWindow]] = [None]

    def status_cb(key: str, value: str) -> None:
        sw = status_win_holder[0]
        if sw:
            sw.status_cb(key, value)

    bridge = RawInputBridge(cfg, status_cb=status_cb)
    bridge.start()

    sw = StatusWindow(bridge, cfg)
    status_win_holder[0] = sw

    if first_run:
        # Defer the setup dialog until the main window is ready
        sw.root = tk.Tk()   # we build it early so after() works
        sw.root.withdraw()
        sw.root.after(200, lambda: (sw.root.deiconify(), sw._build_ui(), sw._refresh(), sw._open_setup()))
        sw.root.title("Scanner Bridge")
        sw.root.resizable(False, False)
        sw.root.protocol("WM_DELETE_WINDOW", sw._quit)
        sw.root.attributes("-topmost", True)
        sw.root.after(1500, lambda: sw.root.attributes("-topmost", False))
        sw.root.mainloop()
    else:
        sw.run()

    bridge.stop()
    bridge.wait()


if __name__ == "__main__":
    main()
