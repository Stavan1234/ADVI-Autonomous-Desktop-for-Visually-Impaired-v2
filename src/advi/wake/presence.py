"""Who is running: named Windows mutexes held by ADVI and by the wake listener.

Windows releases a mutex when its process ends, even after a crash, so a
stale "ADVI is running" can't block the wake word.
"""

from __future__ import annotations

import ctypes
import os

APP_MUTEX = "Local\\ADVI_App_Running"
WAKE_MUTEX = "Local\\ADVI_Wake_Listener"
_SYNCHRONIZE = 0x00100000
_ERROR_ALREADY_EXISTS = 183


def hold(name: str) -> tuple[int, bool]:
    """Create (or open) the mutex and keep it for this process: (handle, someone else already had it)."""
    if os.name != "nt":
        return 0, False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    return int(handle or 0), already


def release(handle: int) -> None:
    if handle and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def running(name: str) -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


def app_running() -> bool:
    return running(APP_MUTEX)


def wake_listener_running() -> bool:
    return running(WAKE_MUTEX)
