"""Telegram Desktop skill: open a chat or send a message as one verified operation.

Telegram Desktop is a Qt app that exposes almost nothing to UI Automation (no
text fields, no chat list items), so the WhatsApp approach cannot work here.
Telegram does accept ordinary keyboard input, so the flow is driven by keys and
every step is checked by reading the screen region it should have changed:

    window up -> signed in (not the welcome / login screen) -> search shows the
    chat -> conversation header shows the chat -> composer shows the exact
    draft -> Enter -> draft gone from the composer and visible in the chat

A step whose check fails stops the skill and says which one. A draft that was
typed but not sent is cleared again, so nothing is left half-written.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .whatsapp import SkillOutcome, StepFailed

PROCESS_NAME = "telegram.exe"
LAUNCH_TARGETS = (r"shell:AppsFolder\Telegram.TelegramDesktop",
                  r"shell:AppsFolder\TelegramMessengerLLP.TelegramDesktop_t4vj0pshhgkwm!Telegram.TelegramDesktop.Store")
# Not "start messaging": a signed-in window with no chat open says
# "Select a chat to start messaging".
SIGNED_OUT_MARKERS = ("welcome to the official telegram", "log in by phone", "scan from mobile",
                      "your phone number", "log in to telegram", "quick log in using qr")


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int


class TelegramScreen(Protocol):
    def window(self, launch: bool) -> Box | None: ...
    def focus(self) -> bool: ...
    def keys(self, keys: str) -> None: ...
    def type(self, text: str) -> None: ...
    def read(self, box: Box) -> str: ...


def _squash(text: str) -> str:
    """OCR-tolerant form: letters and digits only, lowercase."""
    return re.sub(r"[^0-9a-z]+", "", str(text or "").casefold())


def _shows(screen_text: str, wanted: str) -> bool:
    wanted_key = _squash(wanted)[:40]
    return bool(wanted_key) and wanted_key in _squash(screen_text)


def _no_results(screen_text: str) -> bool:
    # 'No Results / There were no results for "<query>"' echoes the query.
    return "noresults" in _squash(screen_text)


class TelegramSkill:
    def __init__(
        self,
        screen: TelegramScreen,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        launch_timeout: float = 20.0,
    ) -> None:
        self.screen = screen
        self.sleep = sleep
        self.clock = clock
        self.launch_timeout = launch_timeout

    def open_chat(self, chat: str) -> SkillOutcome:
        return self._run(chat, None)

    def send_message(self, chat: str, text: str) -> SkillOutcome:
        return self._run(chat, text)

    # --- layout ------------------------------------------------------------
    @staticmethod
    def _regions(win: Box) -> dict[str, Box]:
        width = win.right - win.left
        # Folder strip (~72 px) + chat list (~260 px) at the default layout.
        split = win.left + min(max(width * 42 // 100, 300), 440)
        return {
            # Below the search box, which echoes the query back.
            "list": Box(win.left, win.top + 75, split, win.bottom),
            "header": Box(split, win.top, win.right, win.top + 110),
            "messages": Box(split, win.top + 60, win.right, win.bottom - 60),
            "composer": Box(split, win.bottom - 90, win.right, win.bottom),
        }

    def _wait_for(self, box: Box, check: Callable[[str], bool], timeout: float) -> tuple[bool, str]:
        deadline = self.clock() + timeout
        text = ""
        while True:
            text = self.screen.read(box)
            if check(text):
                return True, text
            if self.clock() >= deadline:
                return False, text
            self.sleep(0.4)

    # --- flow --------------------------------------------------------------
    def _run(self, chat: str, text: str | None) -> SkillOutcome:
        evidence: dict[str, object] = {"chat_query": chat}
        typed = False
        try:
            win = self._ready_window()
            regions = self._regions(win)
            self.screen.keys("{ESC}{ESC}")
            self.sleep(0.3)
            self.screen.type(chat)
            found, _ = self._wait_for(regions["list"], lambda seen: _shows(seen, chat) and not _no_results(seen), 3.0)
            if not found:
                self.screen.keys("{ESC}")
                raise StepFailed("search", f"I couldn't find a Telegram chat called {chat}.")
            self.screen.keys("{ENTER}")
            opened, _ = self._wait_for(regions["header"], lambda seen: _shows(seen, chat), 3.0)
            if not opened:
                raise StepFailed("chat_opened", f"I searched Telegram for {chat} but couldn't confirm that chat opened, so I stopped.")
            evidence["chat_title"] = chat
            if text is None:
                return SkillOutcome(True, "chat_opened", f"Opened the Telegram chat with {chat}.", evidence)

            self.screen.type(text)
            typed = True
            composed, _ = self._wait_for(regions["composer"], lambda seen: _shows(seen, text), 2.0)
            if not composed:
                raise StepFailed("composed", "I typed the message but couldn't confirm it in the Telegram message box, so I didn't send it.")
            self.screen.keys("{ENTER}")
            typed = False
            cleared, _ = self._wait_for(regions["composer"], lambda seen: not _shows(seen, text), 3.0)
            posted, _ = self._wait_for(regions["messages"], lambda seen: _shows(seen, text), 3.0)
            if cleared and posted:
                return SkillOutcome(True, "sent", f"Sent your Telegram message to {chat}.", evidence)
            raise StepFailed("sent", f"I pressed send in the {chat} chat but couldn't confirm the message went through. Please check Telegram before I try again.")
        except StepFailed as failure:
            if typed:
                self.screen.keys("^a{BACKSPACE}")
            evidence.update(failure.evidence)
            return SkillOutcome(False, failure.stage, failure.message, evidence)

    def _ready_window(self) -> Box:
        win = self.screen.window(launch=True)
        deadline = self.clock() + self.launch_timeout
        while win is None and self.clock() < deadline:
            self.sleep(0.5)
            win = self.screen.window(launch=False)
        if win is None:
            raise StepFailed("window", "Telegram didn't open.")
        if not self.screen.focus():
            raise StepFailed("window", "I couldn't bring Telegram to the front.")
        self.sleep(0.4)
        whole = self.screen.read(win).casefold()
        if any(marker in whole for marker in SIGNED_OUT_MARKERS):
            raise StepFailed("logged_in", "Telegram isn't signed in on this computer. Please open Telegram and log in, then ask me again.")
        return win


class WindowsTelegramScreen:
    """Real screen access: pywinauto for the window and keys, Tesseract for reading."""

    def __init__(self) -> None:
        self._window = None

    def window(self, launch: bool) -> Box | None:
        import subprocess

        import psutil
        from pywinauto import Desktop

        pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").casefold() == PROCESS_NAME}
        windows = [w for w in Desktop(backend="uia").windows() if w.element_info.process_id in pids] if pids else []
        if not windows:
            if launch:
                for target in LAUNCH_TARGETS:
                    try:
                        subprocess.Popen(["explorer.exe", target])
                        break
                    except OSError:
                        continue
            return None
        self._window = windows[0]
        rect = self._window.rectangle()
        if rect.width() < 200 or rect.height() < 200:
            return None
        return Box(rect.left, rect.top, rect.right, rect.bottom)

    def focus(self) -> bool:
        if self._window is None:
            return False
        try:
            if self._window.is_minimized():
                self._window.restore()
            self._window.set_focus()
            return True
        except Exception:
            return False

    def keys(self, keys: str) -> None:
        from pywinauto.keyboard import send_keys

        send_keys(keys)

    def type(self, text: str) -> None:
        from pywinauto.keyboard import send_keys

        escaped = re.sub(r"([{}+^%~()\[\]])", r"{\1}", text)
        send_keys(escaped, with_spaces=True, pause=0.01)

    def read(self, box: Box) -> str:
        import pytesseract
        from PIL import ImageGrab

        from advi.core.ocr import configure_tesseract

        configure_tesseract(pytesseract)  # finds Program Files installs not on PATH
        image = ImageGrab.grab(bbox=(box.left, box.top, box.right, box.bottom), all_screens=True)
        return pytesseract.image_to_string(image)
