"""Dismiss Office's activation / licensing pop-ups.

On this PC Office reports "Product Activation Failed". A few seconds after Word,
Excel or PowerPoint starts, the "Microsoft Office Activation Wizard" opens over
it. While it is open the app cannot be closed or used, and a close attempt adds
"You cannot close Microsoft Word because a dialog box is open". Both are
dismissed with WM_CLOSE, which is the dialogs' own Close / OK: nothing is
activated, declined or changed.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable

logger = logging.getLogger(__name__)

OFFICE_PROCESSES = {"WINWORD.EXE", "EXCEL.EXE", "POWERPNT.EXE", "OUTLOOK.EXE", "ONENOTE.EXE", "MSACCESS.EXE"}
MAIN_WINDOW_CLASSES = {"OpusApp", "XLMAIN", "PPTFrameClass", "rctrl_renwnd32", "Framework::CFrame", "OMain"}
NAG_TITLE = re.compile(r"activation|unlicensed|activate|licen[cs]e|we're sorry|something went wrong", re.I)
# Shown when a close is attempted while the wizard is open; only dismissed then.
CANNOT_CLOSE_TITLE = re.compile(r"^Microsoft (?:Word|Excel|PowerPoint|Outlook|OneNote|Access)$")


DIALOG_CLASSES = {"NUIDialog", "#32770", "bosa_sdm_msword", "bosa_sdm_XL9", "bosa_sdm_Mso96"}


def is_nag(title: str, window_class: str) -> bool:
    """An activation pop-up, not an app window whose title carries "(Product Activation Failed)".

    The first live run closed PowerPoint's own "Opening - PowerPoint (Product
    Activation Failed)" loading window.
    """
    if window_class not in DIALOG_CLASSES:
        return False
    bare = re.sub(r"\(\s*product activation failed\s*\)", "", title, flags=re.I)
    return bool(NAG_TITLE.search(bare))


def _office_dialogs() -> list[tuple[int, str, str]]:
    import psutil
    import win32gui
    import win32process

    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").upper() in OFFICE_PROCESSES}
    found: list[tuple[int, str, str]] = []
    if not pids:
        return found

    def visit(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd)
        if cls in MAIN_WINDOW_CLASSES:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids:
            found.append((hwnd, win32gui.GetWindowText(hwnd), cls))
        return True

    win32gui.EnumWindows(visit, None)
    return found


def dismiss_once() -> list[str]:
    """Close any activation pop-up now; returns the titles closed."""
    try:
        import win32con
        import win32gui
    except ImportError:
        return []
    dialogs = _office_dialogs()
    nags = [d for d in dialogs if is_nag(d[1], d[2])]
    if not nags:
        return []
    closed: list[str] = []
    # The "cannot close" message is modal over the wizard: close it first.
    for hwnd, title, cls in dialogs:
        if cls == "NUIDialog" and CANNOT_CLOSE_TITLE.match(title):
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            closed.append(title)
    if closed:
        time.sleep(0.5)
    for hwnd, title, _ in nags:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        closed.append(title)
    for title in closed:
        logger.info("Closed Office pop-up: %s", title)
    return closed


def dismiss_for(seconds: float, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Watch for activation pop-ups for a while (they open a few seconds after launch)."""
    closed: list[str] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            for title in dismiss_once():
                if title not in closed:
                    closed.append(title)
        except Exception:
            logger.debug("Office pop-up check failed", exc_info=True)
        sleep(0.5)
    return closed


def watch_in_background(seconds: float = 25.0) -> None:
    import threading

    threading.Thread(target=dismiss_for, args=(seconds,), name="advi-office-nags", daemon=True).start()


def is_office_app(application: str) -> bool:
    return bool(re.search(r"\b(?:word|excel|powerpoint|outlook|onenote|access)\b", str(application or ""), re.I))
