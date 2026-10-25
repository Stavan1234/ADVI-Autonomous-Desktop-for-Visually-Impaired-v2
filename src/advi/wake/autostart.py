"""Start the wake listener when Windows signs in, and keep exactly one running.

It is registered under HKEY_CURRENT_USER\\...\\Run (no administrator rights
needed), the way voice assistants and chat apps start at sign-in. Programs in
that key start after sign-in, not at the lock screen: a PC with a password
must still be signed in first.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys

from advi.wake.presence import wake_listener_running

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ADVI Wake"
_OFF = {"0", "off", "false", "no"}


def wake_enabled() -> bool:
    return os.getenv("ADVI_WAKE", "on").strip().casefold() not in _OFF


def windowless_python() -> Path:
    """pythonw.exe next to the running interpreter: no console window for a background listener."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def command_line() -> str:
    return f'"{windowless_python()}" -m advi.wake'


def installed() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return str(value) == command_line()


def install() -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command_line())


def uninstall() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except OSError:
        return False


def start_detached() -> bool:
    """Start the listener in the background, outliving whoever started it."""
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    try:
        subprocess.Popen([str(windowless_python()), "-m", "advi.wake"], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError as exc:
        log.warning("Wake listener could not start: %s", exc)
        return False


def ensure_running() -> bool:
    """Called by ADVI at start-up: after "sleep" closes ADVI, the wake word must already be listening."""
    if not wake_enabled() or os.name != "nt":
        return False
    if wake_listener_running():
        return True
    return start_detached()


def stop_running() -> int:
    """End the wake listener processes (supervisor and worker); returns how many were stopped."""
    try:
        import psutil
    except ImportError:
        return 0
    stopped = 0
    me = os.getpid()
    for process in psutil.process_iter(["pid", "cmdline"]):
        cmdline = process.info.get("cmdline") or []
        if process.info["pid"] != me and "-m" in cmdline and "advi.wake" in cmdline \
                and not {"install", "uninstall", "status"} & set(cmdline):
            try:
                process.terminate()
                stopped += 1
            except psutil.Error:
                pass
    return stopped
