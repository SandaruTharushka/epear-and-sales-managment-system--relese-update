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

# Python 3.11 on Windows dropped wintypes.LRESULT; provide a fallback.
if not hasattr(wintypes, "LRESULT"):
    wintypes.LRESULT = ctypes.c_ssize_t

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
log.info("Scanner Bridge starting... (frozen=%s, root=%s)", _IS_FROZEN, _WRITABLE_ROOT)

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

# HWND_MESSAGE: creates a message-only window (no display, no Z-order)
HWND_MESSAGE = -3

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

    # ── Win32 function prototypes ─────────────────────────────────────────────
    # Setting argtypes/restype is critical on 64-bit Windows: without them ctypes
    # defaults to c_int (32-bit) returns, which truncates HWND / HINSTANCE values
    # and causes CreateWindowExW error 1400 (ERROR_INVALID_WINDOW_HANDLE).

    _kernel32.GetModuleHandleW.restype  = wintypes.HINSTANCE
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    _user32.RegisterClassExW.restype  = ctypes.c_ushort   # ATOM = WORD
    _user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]

    _user32.UnregisterClassW.restype  = wintypes.BOOL
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]

    _user32.CreateWindowExW.restype  = wintypes.HWND
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,      # dwExStyle
        wintypes.LPCWSTR,    # lpClassName
        wintypes.LPCWSTR,    # lpWindowName
        wintypes.DWORD,      # dwStyle
        ctypes.c_int,        # X
        ctypes.c_int,        # Y
        ctypes.c_int,        # nWidth
        ctypes.c_int,        # nHeight
        wintypes.HWND,       # hWndParent  ← HWND_MESSAGE = -3 passed here
        wintypes.HANDLE,     # hMenu
        wintypes.HINSTANCE,  # hInstance
        ctypes.c_void_p,     # lpParam
    ]

    _user32.DestroyWindow.restype  = wintypes.BOOL
    _user32.DestroyWindow.argtypes = [wintypes.HWND]

    _user32.PostMessageW.restype  = wintypes.BOOL
    _user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]

    _user32.GetMessageW.restype  = wintypes.BOOL
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]

    _user32.TranslateMessage.restype  = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]

    _user32.DispatchMessageW.restype  = wintypes.LRESULT
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]

    _user32.DefWindowProcW.restype  = wintypes.LRESULT
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]

    _user32.PostQuitMessage.restype  = None
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]

    _user32.RegisterRawInputDevices.restype  = wintypes.BOOL
    _user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT,
    ]

    _user32.GetRawInputData.restype  = wintypes.UINT
    _user32.GetRawInputData.argtypes = [
        wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
        ctypes.POINTER(wintypes.UINT), wintypes.UINT,
    ]

    _user32.SetWindowsHookExW.restype  = wintypes.HANDLE
    _user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, _HOOKPROCTYPE, wintypes.HINSTANCE, wintypes.DWORD,
    ]

    _user32.UnhookWindowsHookEx.restype  = wintypes.BOOL
    _user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]

    _user32.CallNextHookEx.restype  = wintypes.LRESULT
    _user32.CallNextHookEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    ]

    # ToUnicodeEx: converts VK + scan code to Unicode characters using the
    # active keyboard layout — more reliable than any static VK→char table.
    _user32.ToUnicodeEx.restype  = ctypes.c_int
    _user32.ToUnicodeEx.argtypes = [
        wintypes.UINT,                   # wVirtKey
        wintypes.UINT,                   # wScanCode
        ctypes.POINTER(ctypes.c_ubyte),  # lpKeyState (256 bytes)
        wintypes.LPWSTR,                 # pwszBuff
        ctypes.c_int,                    # cchBuff
        wintypes.UINT,                   # wFlags
        ctypes.c_void_p,                 # dwhkl  (HKL – opaque handle)
    ]

    _user32.GetKeyboardLayout.restype  = ctypes.c_void_p
    _user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]

    _user32.PeekMessageW.restype  = wintypes.BOOL
    _user32.PeekMessageW.argtypes = [
        ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
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


def _scancode_to_char(vkey: int, scan_code: int, shift_down: bool) -> Optional[str]:
    """
    Convert a VK code + hardware scan code to the actual typed character by
    calling ToUnicodeEx with the current keyboard layout.  This is correct for
    any layout and avoids hard-coded VK→char tables entirely.

    Returns the printable character string, or None if the key produces no
    printable output (modifiers, function keys, dead keys, etc.).
    """
    if not IS_WINDOWS:
        return None

    kb_state = (ctypes.c_ubyte * 256)()
    if shift_down:
        kb_state[VK_SHIFT]  = 0x80
        kb_state[VK_LSHIFT] = 0x80

    buf    = ctypes.create_unicode_buffer(8)
    layout = _user32.GetKeyboardLayout(0)   # layout of the calling thread (foreground)
    n      = _user32.ToUnicodeEx(vkey, scan_code, kb_state, buf, 8, 0, layout)

    if n > 0:
        ch = buf.value[:n]
        return ch if ch.isprintable() else None

    if n == -1:
        # Dead-key state was set; flush it so future calls are not affected.
        _user32.ToUnicodeEx(VK_ESCAPE, 0x01, kb_state, buf, 8, 0, layout)

    return None


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

    def key_down(self, handle: int, vkey: int, scan_code: int = 0) -> Optional[str]:
        log.info(
            "RAW_KEY_EVENT device=0x%X vkey=0x%02X scan=0x%02X shift=%s",
            handle, vkey, scan_code, self._shifted[handle],
        )

        if vkey in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT):
            self._shifted[handle] = True
            return None

        if vkey == VK_RETURN:
            # Enter terminates the scan; it must not contribute a character.
            barcode = "".join(self._chars[handle])
            self._chars[handle]   = []
            self._shifted[handle] = False
            if barcode:
                log.info("FINAL_BARCODE device=0x%X barcode=%r len=%d", handle, barcode, len(barcode))
            return barcode or None

        if vkey == VK_BACK:
            if self._chars[handle]:
                self._chars[handle].pop()
            return None

        if vkey == VK_ESCAPE:
            self._chars[handle]   = []
            self._shifted[handle] = False
            return None

        # Prefer ToUnicodeEx (layout-aware); fall back to the static VK map.
        ch = _scancode_to_char(vkey, scan_code, self._shifted[handle])
        if ch is None:
            ch = _vkey_to_char(vkey, self._shifted[handle])

        if ch:
            self._chars[handle].append(ch)
            log.info(
                "APPENDED_CHAR device=0x%X vkey=0x%02X scan=0x%02X char=%r buffer=%r",
                handle, vkey, scan_code, ch, "".join(self._chars[handle]),
            )
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

        log.info("FINAL_BARCODE accepted: %r  len=%d", barcode, len(barcode))

        now  = time.monotonic()
        last = self._last.get(barcode, 0.0)
        if now - last < self._debounce:
            log.debug("Workshop scan duplicate suppressed: %r (debounce %.0fms)", barcode, self._debounce * 1000)
            return
        self._last[barcode] = now

        log.info("Workshop scan received: %r → queuing API POST to %s", barcode, self.api_url)
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
            log.info("API_SENT_BARCODE %r → POST %s  payload=%r", barcode, self.api_url, payload)
            resp = _req.post(self.api_url, json=payload, timeout=5)
            resp.raise_for_status()
            log.info("API POST success: HTTP %d ← barcode %r", resp.status_code, barcode)
            if self._status_cb:
                self._status_cb("last_workshop_scan", barcode)
                self._status_cb("last_error", "")
        except Exception as exc:
            import traceback
            msg = f"API POST failed for {barcode!r}: {exc}"
            log.error("%s\n%s", msg, traceback.format_exc())
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
      2. Routes each WM_INPUT keystroke:
           sales role     → pass through unchanged (no suppression)
           workshop role  → buffer until Enter, then POST exact barcode to API
    No WH_KEYBOARD_LL hook; no keystroke suppression of any kind.
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

        self._hwnd:     Optional[int] = None
        self._thread:   Optional[threading.Thread] = None
        self._running   = False

        # Keep callable ref alive so the GC doesn't free it
        self._wndproc_ref: Optional[object] = None

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
        import traceback as _tb

        def _fail(msg: str, fallback: bool = False) -> None:
            """Log failure, write to log file, update UI status."""
            log.error("RawInputBridge: %s", msg)
            try:
                with open(str(_LOG_DIR / "scanner_bridge.log"), "a", encoding="utf-8") as _f:
                    _f.write(f"[WIN32 FAIL] {msg}\n")
            except Exception:
                pass
            if self._status_cb:
                if fallback:
                    self._status_cb("bridge_status", "Fallback keyboard mode")
                else:
                    self._status_cb("bridge_status", f"Error: {msg}")

        try:
            log.info("RawInputBridge: message loop thread started (tid=%d)", threading.get_ident())

            # ── device enumeration ───────────────────────────────────────────────
            log.info("RawInputBridge: enumerating keyboard HID devices...")
            self._refresh_handle_map()
            log.info("RawInputBridge: %d keyboard device(s) enumerated", len(self._handle_map))

            # ── obtain module instance handle ────────────────────────────────────
            hinstance = _kernel32.GetModuleHandleW(None)
            log.info("RawInputBridge: hInstance=0x%X", hinstance or 0)
            if not hinstance:
                err = ctypes.GetLastError()
                _fail(f"GetModuleHandleW returned NULL (error {err})", fallback=True)
                return

            # ── build window class ───────────────────────────────────────────────
            wndproc           = _WNDPROCTYPE(self._wnd_proc)
            self._wndproc_ref = wndproc          # keep callable alive (GC guard)

            wc = WNDCLASSEXW()
            wc.cbSize        = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc   = ctypes.cast(wndproc, ctypes.c_void_p)
            wc.hInstance     = hinstance
            wc.lpszClassName = self._WND_CLASS
            log.info(
                "RawInputBridge: registering window class %r (cbSize=%d, hInstance=0x%X)",
                self._WND_CLASS, wc.cbSize, hinstance,
            )

            atom = _user32.RegisterClassExW(ctypes.byref(wc))
            reg_err = ctypes.GetLastError()
            log.info("RawInputBridge: RegisterClassExW → atom=%d, GetLastError=%d", atom, reg_err)

            if not atom:
                if reg_err == 1410:             # ERROR_CLASS_ALREADY_EXISTS
                    # Unregister the stale class (left from a previous failed run in
                    # this same process) and try once more.
                    log.info(
                        "RawInputBridge: class already exists – unregistering and re-registering"
                    )
                    _user32.UnregisterClassW(self._WND_CLASS, hinstance)
                    atom = _user32.RegisterClassExW(ctypes.byref(wc))
                    reg_err = ctypes.GetLastError()
                    log.info(
                        "RawInputBridge: re-register → atom=%d, GetLastError=%d", atom, reg_err
                    )

                if not atom:
                    _fail(
                        f"RegisterClassExW failed (error {reg_err})", fallback=True
                    )
                    return

            log.info("RawInputBridge: window class registered (atom=%d)", atom)

            # ── create hidden message-only window ────────────────────────────────
            # HWND_MESSAGE (-3) instructs Windows to create a message-only window:
            # no screen position, no Z-order, receives only posted/sent messages.
            # argtypes on CreateWindowExW ensure -3 is passed as a full-width HWND
            # (pointer-sized) rather than a 32-bit c_int, avoiding error 1400 on
            # 64-bit Windows.
            log.info(
                "RawInputBridge: calling CreateWindowExW(hWndParent=HWND_MESSAGE=%d, hInstance=0x%X)",
                HWND_MESSAGE, hinstance,
            )
            hwnd = _user32.CreateWindowExW(
                0,                  # dwExStyle
                self._WND_CLASS,    # lpClassName
                "Scanner Bridge",   # lpWindowName
                0,                  # dwStyle
                0, 0, 0, 0,         # x, y, w, h
                HWND_MESSAGE,       # hWndParent  ← typed as HWND via argtypes
                None,               # hMenu
                hinstance,          # hInstance   ← full 64-bit value via argtypes
                None,               # lpParam
            )
            create_err = ctypes.GetLastError()
            log.info(
                "RawInputBridge: CreateWindowExW → hwnd=0x%X, GetLastError=%d",
                hwnd or 0, create_err,
            )

            if not hwnd:
                _fail(
                    f"CreateWindowExW failed (error {create_err}) – "
                    f"hwnd=NULL, hInstance=0x{hinstance:X}, hWndParent={HWND_MESSAGE}",
                    fallback=True,
                )
                return

            self._hwnd = hwnd
            log.info("RawInputBridge: message-only window created (hwnd=0x%X)", hwnd)

            # ── register Raw Input (all keyboards, RIDEV_INPUTSINK) ──────────────
            rid = RAWINPUTDEVICE()
            rid.usUsagePage = HID_USAGE_PAGE_GENERIC
            rid.usUsage     = HID_USAGE_GENERIC_KEYBOARD
            rid.dwFlags     = RIDEV_INPUTSINK
            rid.hwndTarget  = hwnd
            log.info(
                "RawInputBridge: calling RegisterRawInputDevices "
                "(usUsagePage=0x%X, usUsage=0x%X, dwFlags=0x%X, hwndTarget=0x%X)",
                rid.usUsagePage, rid.usUsage, rid.dwFlags, hwnd,
            )
            ok = _user32.RegisterRawInputDevices(
                ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)
            )
            rid_err = ctypes.GetLastError()
            log.info(
                "RawInputBridge: RegisterRawInputDevices → ok=%s, GetLastError=%d",
                bool(ok), rid_err,
            )

            if not ok:
                _fail(
                    f"RegisterRawInputDevices failed (error {rid_err})", fallback=True
                )
                # Destroy the window we created; do not leave a dangling handle.
                _user32.DestroyWindow(self._hwnd)
                self._hwnd = None
                return

            log.info("RawInputBridge: WM_INPUT registration successful (RIDEV_INPUTSINK)")

            log.info("RawInputBridge: listener running (hwnd=0x%X)", hwnd)
            if self._status_cb:
                self._status_cb("bridge_status", "Listening")

            # ── message pump ────────────────────────────────────────────────────
            # Runs on this dedicated thread; does NOT block tkinter's mainloop.
            msg = MSG()
            while self._running:
                r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            # ── cleanup ──────────────────────────────────────────────────────────
            if self._hwnd:
                _user32.DestroyWindow(self._hwnd)
                self._hwnd = None
                log.info("RawInputBridge: message window destroyed")

            log.info("RawInputBridge: stopped cleanly")
            if self._status_cb:
                self._status_cb("bridge_status", "Stopped")

        except Exception as exc:
            tb = _tb.format_exc()
            log.error("RawInputBridge: fatal exception in message loop:\n%s", tb)
            try:
                with open(str(_LOG_DIR / "scanner_bridge.log"), "a", encoding="utf-8") as _f:
                    _f.write(f"[TRACEBACK]\n{tb}\n")
            except Exception:
                pass
            if self._status_cb:
                self._status_cb("bridge_status", "Fallback keyboard mode")

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
        scan_code = raw.data.keyboard.MakeCode
        is_down   = not bool(flags & RI_KEY_BREAK)

        role = self._role_for_handle(handle)

        log.info(
            "RAW_KEY_EVENT handle=0x%X vkey=0x%02X scan=0x%02X flags=0x%02X "
            "is_down=%s role=%s",
            handle, vkey, scan_code, flags, is_down, role or "unmapped",
        )

        if role is None:
            return                              # unmapped device – pass through

        if role == "workshop":
            if is_down:
                barcode = self._buffer.key_down(handle, vkey, scan_code)
                if barcode:
                    log.info(
                        "FINAL_BARCODE device=0x%X barcode=%r len=%d",
                        handle, barcode, len(barcode),
                    )
                    self._workshop.handle_scan(barcode)
            else:
                self._buffer.key_up(handle, vkey)

        # sales role: keystrokes pass through normally


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
            "bridge_status":     "Starting...",
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

    def run(self, on_ready: Optional[Callable[[], None]] = None) -> None:
        """Start the tkinter mainloop.  If *on_ready* is provided it is called
        200 ms after the window is displayed — use this to start the Raw Input
        bridge *after* the UI is fully initialised (task requirement #8)."""
        self.root = tk.Tk()
        self.root.title("Scanner Bridge")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        # Pop to front briefly, then release always-on-top
        self.root.attributes("-topmost", True)
        self.root.after(1500, lambda: self.root.attributes("-topmost", False))

        self._build_ui()
        self._refresh()

        if on_ready:
            # Delay bridge start until after tkinter has processed its first
            # events.  200 ms is enough for the window to become visible.
            self.root.after(200, on_ready)

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
                if val == "Listening":
                    lbl.config(foreground="green")
                elif val.startswith("Error") or val == "No scanners detected":
                    lbl.config(foreground="red")
                else:
                    lbl.config(foreground="orange")

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
# API connectivity check (runs in background thread)
# ──────────────────────────────────────────────────────────────────────────────

def _check_api_connectivity(api_url: str) -> None:
    """Fire-and-forget: log whether the workshop API server is reachable."""
    def _check() -> None:
        try:
            import requests as _req
            from urllib.parse import urlparse
            parsed = urlparse(api_url)
            base   = f"{parsed.scheme}://{parsed.netloc}/"
            resp   = _req.get(base, timeout=3)
            log.info("API connectivity check: %s → HTTP %d", base, resp.status_code)
        except Exception as exc:
            log.warning("API connectivity check failed (%s): %s", api_url, exc)

    threading.Thread(target=_check, daemon=True, name="ApiConnCheck").start()


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

    log.info("Scanner Bridge starting...")

    # Status routing – the bridge and window share this callback.
    # status_win_holder is populated BEFORE bridge.start() to avoid a race
    # where the bridge thread fires status_cb before the window is registered.
    status_win_holder: List[Optional[StatusWindow]] = [None]

    def status_cb(key: str, value: str) -> None:
        sw = status_win_holder[0]
        if sw:
            sw.status_cb(key, value)

    try:
        cfg = load_config()
        log.info("Config loaded: api_url=%s  debounce_ms=%s",
                 cfg.get("api_url"), cfg.get("debounce_ms"))

        # Enumerate devices and log results
        log.info("Enumerating keyboard HID devices...")
        devices = enumerate_keyboards()
        if devices:
            log.info("Found %d keyboard device(s):", len(devices))
            for d in devices:
                log.info("  [%s] keys=%s  id=%s",
                         d["friendly_name"], d["keys_total"], d["device_id"])
        else:
            log.warning("No keyboard HID devices detected")

        # First-run: open setup dialog if no devices are mapped
        first_run = not (cfg.get("sales_scanner_device_id") or cfg.get("workshop_scanner_device_id"))

        bridge = RawInputBridge(cfg, status_cb=status_cb)
        sw = StatusWindow(bridge, cfg)

        # Register the window BEFORE starting the bridge to avoid the race
        # condition where status_cb fires with "Listening" before status_win_holder
        # is populated (which would leave the UI stuck on "Starting...").
        status_win_holder[0] = sw

        # API reachability check (background, non-blocking)
        _check_api_connectivity(cfg.get("api_url", _DEFAULT_CFG["api_url"]))

        if not devices:
            status_cb("bridge_status", "No scanners detected")

        # The Raw Input bridge starts AFTER tkinter is shown (requirement #8).
        # on_ready is called via root.after(200, …) so it never blocks the mainloop.
        def _start_bridge() -> None:
            log.info("Starting raw input listener thread (post-UI init)...")
            bridge.start()
            log.info("Raw input listener thread started")

        if first_run:
            # Defer the setup dialog until the main window is ready
            sw.root = tk.Tk()
            sw.root.withdraw()
            sw.root.after(200, lambda: (sw.root.deiconify(), sw._build_ui(), sw._refresh(), sw._open_setup()))
            sw.root.title("Scanner Bridge")
            sw.root.resizable(False, False)
            sw.root.protocol("WM_DELETE_WINDOW", sw._quit)
            sw.root.attributes("-topmost", True)
            sw.root.after(1500, lambda: sw.root.attributes("-topmost", False))
            sw.root.after(200, _start_bridge)
            sw.root.mainloop()
        else:
            sw.run(on_ready=_start_bridge)

        bridge.stop()
        bridge.wait()
        log.info("Scanner Bridge exited cleanly")

    except Exception:
        import traceback
        tb = traceback.format_exc()
        log.error("Fatal error during scanner bridge startup:\n%s", tb)
        status_cb("bridge_status", "Error: see scanner_bridge.log")
        try:
            from tkinter import messagebox as _mb
            _mb.showerror(
                "Scanner Bridge Error",
                f"A fatal error occurred at startup.\n\nSee logs/scanner_bridge.log for details.\n\n{tb}",
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
