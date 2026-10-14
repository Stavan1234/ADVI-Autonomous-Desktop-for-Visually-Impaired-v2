"""WhatsApp Desktop skill: open a chat or send a message as one verified operation.

The runtime log shows the generic loop failing this task twice: it clicked
"Search", typed into nothing, never opened the chat, typed the message into
nothing, pressed Enter, and reported "Pressed key 'enter'". Each GUI step was
a separate LLM round trip, and OCR could not tell where focus was.

Here every step has a precondition and a postcondition checked through UI
Automation (WhatsApp Desktop is WebView2; its web content is fully exposed):

    window ready -> chat list visible (logged in) -> search box focused and
    holding the query -> exactly one matching chat -> chat header shows it ->
    composer focused and holding the exact text -> Enter -> composer empty

A step whose postcondition fails stops the skill and reports which step. Text
is never sent to a chat the skill could not positively identify.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .uia import UiaDriver, UiaNode

PROCESS_NAMES = ("WhatsApp.Root.exe", "WhatsApp.exe")
LAUNCH_TARGET = "whatsapp:"
SEARCH_BOX_NAME = "Search or start a new chat"
CHAT_LIST_NAMES = ("Search results.", "Search results", "Chat list")


@dataclass
class SkillOutcome:
    ok: bool
    stage: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)


class StepFailed(Exception):
    def __init__(self, stage: str, message: str, **evidence: Any) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.evidence = evidence


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


class WhatsAppSkill:
    def __init__(
        self,
        driver: UiaDriver,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        launch_timeout: float = 20.0,
        step_timeout: float = 5.0,
    ) -> None:
        self.driver = driver
        self.sleep = sleep
        self.clock = clock
        self.launch_timeout = launch_timeout
        self.step_timeout = step_timeout

    # --- public operations ---------------------------------------------
    def open_chat(self, chat: str) -> SkillOutcome:
        return self._run(chat, text=None, send=False)

    def send_message(self, chat: str, text: str) -> SkillOutcome:
        return self._run(chat, text=text, send=True)

    # --- flow ------------------------------------------------------------
    def _run(self, chat: str, *, text: str | None, send: bool) -> SkillOutcome:
        evidence: dict[str, Any] = {"chat_query": chat}
        try:
            window, doc = self._ready_window(evidence)
            title = self._open_chat(window, doc, chat, evidence)
            if text is None:
                return SkillOutcome(True, "chat_opened", f"Opened the WhatsApp chat '{title}'.", evidence)
            composer = self._compose(window, doc, text, evidence)
            if not send:
                return SkillOutcome(True, "composed", f"Typed the message in '{title}' without sending.", evidence)
            self._send(window, doc, composer, evidence)
            return SkillOutcome(True, "sent", f"Sent '{text}' to '{title}' on WhatsApp.", evidence)
        except StepFailed as failure:
            evidence.update(failure.evidence)
            return SkillOutcome(
                False, failure.stage, failure.message, evidence,
                candidates=list(failure.evidence.get("candidates") or []),
            )

    def _wait(self, probe: Callable[[], Any], timeout: float | None = None) -> Any:
        deadline = self.clock() + (self.step_timeout if timeout is None else timeout)
        while True:
            value = probe()
            if value:
                return value
            if self.clock() >= deadline:
                return value
            self.sleep(0.25)

    def _find_window(self) -> UiaNode | None:
        return self.driver.find_window("WhatsApp", PROCESS_NAMES)

    def _ready_window(self, evidence: dict[str, Any]) -> tuple[UiaNode, UiaNode]:
        window = self._find_window()
        if window is None:
            self.driver.launch(LAUNCH_TARGET)
            evidence["launched"] = True
            window = self._wait(self._find_window, self.launch_timeout)
            if window is None:
                raise StepFailed("app_ready", "WhatsApp did not open. Is WhatsApp Desktop installed?")
        if not self.driver.bring_to_front(window):
            raise StepFailed("app_ready", "WhatsApp is open but I couldn't bring its window to the front.")
        # Elements of a minimized window report off-screen rectangles; re-query
        # everything after restoring it.
        window = self._find_window() or window
        doc = self._wait(lambda: self.driver.find_first(window, control_type="Document", automation_id="RootWebArea"))
        if doc is None:
            raise StepFailed("app_ready", "WhatsApp's window is open but its content isn't loading.")
        search = self._wait(lambda: self._search_box(doc), self.launch_timeout if evidence.get("launched") else None)
        if search is None:
            raise StepFailed(
                "logged_in",
                "WhatsApp is open but the chat list isn't showing. It may be logged out or still loading; "
                "please check WhatsApp, link your phone if asked, and try again.",
            )
        evidence["logged_in"] = True
        return window, doc

    # --- element lookup ---------------------------------------------------
    # Names observed on WhatsApp Desktop (WebView2), 2026-10-14:
    #   search box:  "Search or start a new chat" while empty, "" once it holds text
    #   composer:    "Type a message to group New Chapter" / "Type a message to Anurodh"
    #   chat cells:  "New Chapter 21/07/2025" (title + last-activity stamp); rows add a preview

    def _edits(self, doc: UiaNode) -> list[UiaNode]:
        return self.driver.find_all(doc, control_type="Edit")

    def _search_box(self, doc: UiaNode) -> UiaNode | None:
        named = self.driver.find_first(doc, control_type="Edit", name=SEARCH_BOX_NAME)
        if named is not None:
            return named
        # Its accessible name is a placeholder that disappears once text is typed.
        for edit in self._edits(doc):
            if not _is_composer_name(edit.name):
                return edit
        return None

    def _composer(self, doc: UiaNode) -> UiaNode | None:
        for edit in self._edits(doc):
            if _is_composer_name(edit.name):
                return edit
        return None

    def _results(self, doc: UiaNode) -> UiaNode | None:
        for name in CHAT_LIST_NAMES:
            grid = self.driver.find_first(doc, control_type="DataGrid", name=name)
            if grid is not None:
                return grid
        return None

    def _row_title(self, row: UiaNode) -> str:
        """The chat title for a result row: its label up to the first time/date stamp."""
        names = [row.name]
        frontier = [row]
        for _depth in range(3):
            next_frontier: list[UiaNode] = []
            for node in frontier:
                for child in self.driver.children(node)[:4]:
                    if child.name.strip():
                        names.append(child.name)
                    next_frontier.append(child)
            frontier = next_frontier
        row_name = row.name.strip()
        titles = [chat_title_from_label(name) for name in names if row_name.startswith(name.strip())]
        titles = [title for title in titles if title]
        return min(titles, key=len) if titles else row_name

    # --- input ------------------------------------------------------------
    def _enter_text(
        self,
        window: UiaNode,
        field_finder: Callable[[], UiaNode | None],
        text: str,
        *,
        clear: bool,
        allow_set_value: bool,
    ) -> bool:
        """Put ``text`` into a field and confirm it by reading the field back.

        WhatsApp Desktop is WebView2 in visual hosting mode: injected
        keystrokes land on its host window and are dropped, even while UIA
        reports focus (the cause of every failed trial). Routes, in order:

        1. ValuePattern.SetValue, where safe (plain inputs such as search; never
           the rich-text composer, which ignores it).
        2. UIA focus + characters posted to the WebView2 host window.
        3. A real click + keystrokes, with no UIA SetFocus afterwards.
        4. Route 2 again after a short settle.
        Which of 2 and 3 works depends on whether the window was just clicked
        (measured live, 2026-10-14), so every attempt is read back.
        """
        expected = _norm(text)

        def current_value() -> str:
            field = field_finder()
            return (self.driver.value(field) or "") if field is not None else ""

        def holds() -> bool:
            return _norm(current_value()) == expected

        if allow_set_value:
            field = field_finder()
            if field is not None and self.driver.set_value(field, text) and self._wait(holds, 2.0):
                return True

        def length() -> int:
            return len(current_value().rstrip("\n"))

        def remove_added(baseline: int) -> None:
            # Undo only this attempt's characters, by whichever route currently works.
            added = length() - baseline
            if added <= 0:
                return
            self.driver.post_backspaces(window, added)
            self.sleep(0.3)
            added = length() - baseline
            if added > 0:
                self.driver.press("{BACKSPACE " + str(added) + "}")

        def posted() -> bool:
            # Route A: UIA focus + WM_CHAR to the WebView2 host. Dropped for
            # ~2 s after a real mouse click into the window.
            field = field_finder()
            if field is None or not self.driver.focus(field):
                return False
            if clear and _norm(current_value()):
                if not (allow_set_value and self.driver.set_value(field, "")):
                    self.driver.post_backspaces(window, length())
            baseline = length()
            if self.driver.post_text(window, text) and self._wait(holds, 1.5):
                return True
            remove_added(baseline)
            return False

        def keyboard() -> bool:
            # Route B: real click + injected keystrokes, without UIA SetFocus
            # (SetFocus after the click pulls keyboard focus back to the host).
            field = field_finder()
            if field is None:
                return False
            self.driver.click(field)
            self.sleep(0.3)
            if clear:
                self.driver.press("^a{BACKSPACE}")
            baseline = length()
            self.driver.type_text(text)
            if self._wait(holds, 1.5):
                return True
            remove_added(baseline)
            return False

        if posted() or keyboard():
            return True
        self.sleep(2.0)
        return posted()

    # --- steps ------------------------------------------------------------
    def _open_chat(self, window: UiaNode, doc: UiaNode, chat: str, evidence: dict[str, Any]) -> str:
        if not self._enter_text(window, lambda: self._search_box(doc), chat, clear=True, allow_set_value=True):
            search = self._search_box(doc)
            raise StepFailed("search", "WhatsApp's search box didn't accept the chat name.",
                             search_value=self.driver.value(search) if search is not None else None)

        query = _norm(chat)

        def matches() -> list[tuple[UiaNode, str]]:
            grid = self._results(doc)
            if grid is None:
                return []
            rows = [(row, self._row_title(row)) for row in self.driver.children(grid)]
            return [(row, title) for row, title in rows if _norm(title).startswith(query)]

        found = self._wait(matches)
        if not found:
            raise StepFailed("search", f"I couldn't find a WhatsApp chat named '{chat}'.")
        exact = [(row, title) for row, title in found if _norm(title) == query]
        chosen = exact if exact else found
        titles = list(dict.fromkeys(title for _row, title in chosen))
        if len(titles) != 1:
            raise StepFailed(
                "search",
                f"More than one WhatsApp chat matches '{chat}': {', '.join(titles[:5])}. Which one did you mean?",
                candidates=titles[:5],
            )
        row, title = chosen[0]
        evidence["chat_title"] = title
        self.driver.click(row)

        if not self._wait(lambda: self._chat_is_open(doc, title)):
            composer = self._composer(doc)
            raise StepFailed("chat_opened", f"I clicked '{title}' but couldn't confirm that chat opened.",
                             composer_name=composer.name if composer is not None else None)
        evidence["chat_open_verified"] = True
        return title

    def _chat_is_open(self, doc: UiaNode, title: str) -> bool:
        """The composer names its chat: "Type a message to [group] <title>"."""
        composer = self._composer(doc)
        if composer is None:
            return False
        target = re.sub(r"^type a message to (?:group )?", "", _norm(composer.name))
        return target == _norm(title)

    def _compose(self, window: UiaNode, doc: UiaNode, text: str, evidence: dict[str, Any]) -> UiaNode:
        composer = self._wait(lambda: self._composer(doc))
        if composer is None:
            raise StepFailed("composed", "The chat opened but I couldn't find its message box.")
        if _norm(self.driver.value(composer) or ""):
            # An unsent draft is the user's; never wipe or append to it.
            raise StepFailed("composed", "That chat already has unsent text in its message box, so I left it alone.")
        # A newline character would send early; dictated text is one paragraph.
        single_line = re.sub(r"\s*\n\s*", " ", text).strip()
        if not self._enter_text(window, lambda: self._composer(doc), single_line, clear=False, allow_set_value=False):
            leftover = (self.driver.value(self._composer(doc) or composer) or "").rstrip("\n")
            if leftover and self.driver.focus(self._composer(doc) or composer):
                # The box was empty before; remove only what this attempt typed.
                self.driver.post_backspaces(window, len(leftover))
            raise StepFailed("composed", "I typed the message but couldn't confirm it in the message box, so I didn't send it.")
        evidence["composer_text_verified"] = True
        return self._composer(doc) or composer

    def _send(self, window: UiaNode, doc: UiaNode, composer: UiaNode, evidence: dict[str, Any]) -> None:
        def cleared() -> bool:
            latest = self._composer(doc) or composer
            return _norm(self.driver.value(latest) or "") == ""

        # WhatsApp's own Send button (shown once the composer has text) is
        # invoked through UIA; no keystroke has to reach the page.
        send_button = self.driver.find_first(doc, control_type="Button", name="Send")
        if send_button is not None and self.driver.invoke(send_button):
            evidence["send_method"] = "send_button_invoke"
        else:
            current = self._composer(doc) or composer
            if not self.driver.has_focus(current) and not self.driver.focus(current):
                raise StepFailed("sent", "I couldn't reach WhatsApp's Send button or the message box, so I didn't send it.")
            self.driver.press("{ENTER}")
            evidence["send_method"] = "enter_key"

        if not self._wait(cleared):
            raise StepFailed("sent", "I tried to send but the message is still in the box; it may not have been sent.")
        evidence["composer_cleared_after_send"] = True


_STAMP = re.compile(
    r"\s+(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}:\d{2}(?:\s*[ap]\.?m\.?)?|yesterday|today|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?=\s|$)",
    re.I,
)


def chat_title_from_label(label: str) -> str:
    """Strip the activity stamp and preview: "New Chapter 21/07/2025 You: hi" -> "New Chapter"."""
    return _STAMP.split(str(label or "").strip(), maxsplit=1)[0].strip()


def _is_composer_name(name: str) -> bool:
    return _norm(name).startswith("type a message")
