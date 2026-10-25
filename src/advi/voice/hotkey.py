"""A hidden, system-wide hotkey through Win32 RegisterHotKey.

No keyboard hook and no extra package: Windows delivers WM_HOTKEY to this
thread only for the registered combination, and refuses the registration if
another program already owns it (so ADVI never steals an existing shortcut).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_HOTKEY = "ctrl+alt+space"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
VK_ESCAPE = 0x1B
_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "insert": 0x2D, "home": 0x24, "end": 0x23,
         "pause": 0x13, "scrolllock": 0x91}


class HotkeyError(ValueError):
    pass


def parse_hotkey(text: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modifiers, virtual key)."""
    parts = [p.strip().casefold() for p in str(text or "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")
    *mods, key = parts
    modifiers = 0
    for mod in mods:
        if mod not in _MODIFIERS:
            raise HotkeyError(f"unknown modifier {mod!r}")
        modifiers |= _MODIFIERS[mod]
    if key in _KEYS:
        vk = _KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    else:
        raise HotkeyError(f"unknown key {key!r}")
    if not modifiers and not (0x70 <= vk <= 0x87):
        raise HotkeyError("a hotkey needs a modifier unless it is a function key")
    return modifiers, vk


def key_is_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


class GlobalHotkey:
    """Calls ``callback`` (on a fresh thread) each time the hotkey is pressed."""

    _ID = 0xAD71

    def __init__(self, hotkey: str, callback: Callable[[], None]) -> None:
        self.hotkey = hotkey
        self.modifiers, self.vk = parse_hotkey(hotkey)
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.error: str | None = None

    def start(self, timeout: float = 3.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="advi-voice-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.error is None and self._ready.is_set()

    def key_down(self) -> bool:
        return key_is_down(self.vk)

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, self._ID, self.modifiers | MOD_NOREPEAT, self.vk):
            code = ctypes.get_last_error()
            self.error = "taken by another program" if code == 1409 else f"Windows error {code}"
            log.info("Voice hotkey %s could not be registered: %s", self.hotkey, self.error)
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self._ID:
                    threading.Thread(target=self._fire, name="advi-voice-turn", daemon=True).start()
        finally:
            user32.UnregisterHotKey(None, self._ID)

    def _fire(self) -> None:
        try:
            self.callback()
        except Exception:
            log.exception("Voice hotkey handler failed.")

    def close(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
