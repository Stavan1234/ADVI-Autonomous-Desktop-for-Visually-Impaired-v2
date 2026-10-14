"""Structured desktop perception: interactive elements with IDs, and verified actions on them.

The generic loop used to see a window through OCR text (no roles, no focus, no
identity) and act by coordinates. Here the model calls ``observe_window`` and
gets the window's on-screen interactive elements, each with a short ID, role,
name, value and focus flag. It then calls ``interact(element_id, action)``,
which acts through UI Automation patterns (Invoke, Toggle, SelectionItem,
Value) before falling back to a real mouse click, and re-observes so the
result says what actually changed.

IDs belong to one observation. Using an ID from an older observation is
refused, because the element may have moved or disappeared.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .uia import UiaNode

MAX_ELEMENTS = 45  # ~90 chars each; model-facing output is capped at 5000 chars
MAX_NAME_CHARS = 80
MAX_VALUE_CHARS = 120
TEXT_ROLES = {"Edit", "ComboBox", "Document"}
ACTIONS = ("click", "type", "set_value", "select", "toggle", "expand", "focus")


class ObserverDriver(Protocol):
    def window_for_hwnd(self, hwnd: int) -> UiaNode | None: ...
    def foreground_window(self) -> UiaNode | None: ...
    def find_window_titled(self, title_contains: str) -> UiaNode | None: ...
    def content_root(self, window: UiaNode) -> UiaNode: ...
    def interactive_elements(self, root: UiaNode) -> list[UiaNode]: ...
    def is_enabled(self, node: UiaNode) -> bool: ...
    def has_focus(self, node: UiaNode) -> bool: ...
    def focus(self, node: UiaNode) -> bool: ...
    def value(self, node: UiaNode) -> str | None: ...
    def set_value(self, node: UiaNode, text: str) -> bool: ...
    def invoke(self, node: UiaNode) -> bool: ...
    def select(self, node: UiaNode) -> bool: ...
    def toggle(self, node: UiaNode) -> bool: ...
    def expand(self, node: UiaNode) -> bool: ...
    def click(self, node: UiaNode) -> None: ...
    def type_text(self, text: str) -> None: ...
    def post_text(self, window: UiaNode, text: str) -> bool: ...
    def press(self, keys: str) -> None: ...
    def bring_to_front(self, window: UiaNode) -> bool: ...


@dataclass
class Observation:
    number: int
    window: UiaNode
    elements: list[dict[str, Any]]
    nodes: dict[str, UiaNode]
    state_hash: str
    truncated: bool
    total: int = 0

    def payload(self) -> dict[str, Any]:
        return {
            "observation": self.number,
            "window": self.window.name,
            "elements": self.elements,
            "element_count": self.total,
            "truncated": self.truncated,
            "state_hash": self.state_hash,
        }


@dataclass
class ActionOutcome:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class UiObserver:
    def __init__(
        self,
        driver: ObserverDriver,
        *,
        target_hwnd: Callable[[], int | None] = lambda: None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.driver = driver
        self.target_hwnd = target_hwnd
        self.sleep = sleep
        self._counter = 0
        self.latest: Observation | None = None

    # --- observe -----------------------------------------------------------
    def _resolve_window(self, application: str | None) -> UiaNode | None:
        if application:
            return self.driver.find_window_titled(application)
        hwnd = self.target_hwnd()
        if hwnd:
            window = self.driver.window_for_hwnd(hwnd)
            if window is not None:
                return window
        return self.driver.foreground_window()

    def observe(self, application: str | None = None, *, window: UiaNode | None = None) -> Observation | None:
        window = window or self._resolve_window(application)
        if window is None:
            return None
        root = self.driver.content_root(window)
        raw = self.driver.interactive_elements(root)

        seen: set[tuple[str, str, tuple[int, int, int, int]]] = set()
        candidates: list[tuple[UiaNode, bool, str | None]] = []
        for node in raw:
            left, top, right, bottom = node.rect
            if right - left <= 1 or bottom - top <= 1:
                continue
            key = (node.control_type, node.name, node.rect)
            if key in seen:
                continue
            seen.add(key)
            focused = self.driver.has_focus(node)
            value = self.driver.value(node) if node.control_type in TEXT_ROLES else None
            if not node.name.strip() and not focused and node.control_type not in TEXT_ROLES:
                continue  # nameless icon wrappers carry no meaning for the model
            candidates.append((node, focused, value))

        kept = _select_within_budget(candidates)

        self._counter += 1
        elements: list[dict[str, Any]] = []
        nodes: dict[str, UiaNode] = {}
        for index, (node, focused, value) in enumerate(kept, start=1):
            element_id = f"e{index}"
            nodes[element_id] = node
            entry: dict[str, Any] = {"id": element_id, "role": node.control_type, "name": _clip(node.name, MAX_NAME_CHARS)}
            if value:
                entry["value"] = _clip(value, MAX_VALUE_CHARS)
            if focused:
                entry["focused"] = True
            if not self.driver.is_enabled(node):
                entry["disabled"] = True
            elements.append(entry)

        digest = hashlib.sha1(
            repr([(e["role"], e["name"], e.get("value"), e.get("focused")) for e in elements]).encode("utf-8")
        ).hexdigest()[:12]
        self.latest = Observation(
            number=self._counter,
            window=window,
            elements=elements,
            nodes=nodes,
            state_hash=digest,
            truncated=len(candidates) > MAX_ELEMENTS,
            total=len(candidates),
        )
        return self.latest

    # --- interact ----------------------------------------------------------
    def interact(self, element_id: str, action: str, text: str | None = None) -> ActionOutcome:
        action = str(action or "").strip().casefold()
        if action not in ACTIONS:
            return ActionOutcome(False, f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.")
        observation = self.latest
        if observation is None:
            return ActionOutcome(False, "No observation yet. Call observe_window first.")
        node = observation.nodes.get(str(element_id or "").strip())
        if node is None:
            return ActionOutcome(
                False,
                f"Element '{element_id}' is not in observation {observation.number}. Call observe_window again and use a current ID.",
            )
        if action in ("type", "set_value") and not text:
            return ActionOutcome(False, f"Action '{action}' needs text.")
        if not self.driver.is_enabled(node):
            return ActionOutcome(False, f"'{node.name or element_id}' is disabled.")

        self.driver.bring_to_front(observation.window)
        method = self._perform(node, action, text or "")
        if method is None:
            return ActionOutcome(False, f"Could not {action} '{node.name or element_id}'.")

        self.sleep(0.35)
        details: dict[str, Any] = {"element": element_id, "action": action, "method": method, "target": node.name}
        verified = True
        if action in ("type", "set_value"):
            current = self.driver.value(node)
            if current is None:
                verified = False
                details["value_readable"] = False
            else:
                verified = _norm(text or "") in _norm(current)
                details["value_after"] = _clip(current, MAX_VALUE_CHARS)
        if action == "focus":
            verified = self.driver.has_focus(node)

        before = observation.state_hash
        after = self.observe(window=observation.window)
        if after is not None:
            details["screen_changed"] = after.state_hash != before
            details["observation"] = after.payload()
        if action in ("click", "select", "toggle", "expand"):
            # An accepted input is not proof; an observable change is. A click
            # that opens a separate window reads as unconfirmed, not failed.
            verified = bool(details.get("screen_changed"))
        message = f"{action} on '{node.name or element_id}' via {method}."
        if not verified:
            message += " The result could not be confirmed."
        return ActionOutcome(verified, message, details)

    def _perform(self, node: UiaNode, action: str, text: str) -> str | None:
        d = self.driver
        if action == "click":
            if d.invoke(node):
                return "invoke"
            # Not SelectionItem.Select: on web content it can move accessibility
            # selection without firing the page's click handler.
            d.click(node)
            return "mouse_click"
        if action == "select":
            return "select" if d.select(node) else None
        if action == "toggle":
            return "toggle" if d.toggle(node) else None
        if action == "expand":
            return "expand" if d.expand(node) else None
        if action == "focus":
            return "focus" if d.focus(node) else None
        if action == "set_value":
            return "set_value" if d.set_value(node, text) else None
        if action == "type":
            # WebView2/Chromium hosts (WhatsApp Desktop, Teams, …) drop injected
            # keystrokes; characters posted to the host window reach the focused
            # element. Native apps have no such host and use the keyboard.
            window = self.latest.window if self.latest is not None else None
            if window is not None and d.focus(node) and d.post_text(window, text):
                self.sleep(0.3)
                current = d.value(node)
                if current is None or _norm(text) in _norm(current):
                    return "posted_chars"
            d.click(node)
            self.sleep(0.15)
            if not d.focus(node) and not d.has_focus(node):
                return None
            d.type_text(text)
            return "keyboard"
        return None


_INPUT_ROLES = {"Edit", "ComboBox", "CheckBox", "RadioButton", "Slider", "Document"}
_CONTENT_ROLES = {"ListItem", "DataItem", "TreeItem", "Hyperlink"}
_INPUT_QUOTA = 10
_CONTENT_QUOTA = 25


def _select_within_budget(candidates: list[tuple[UiaNode, bool, str | None]]) -> list[tuple[UiaNode, bool, str | None]]:
    """Keep a useful mix when a window has more elements than the budget.

    Plain top-to-bottom order filled File Explorer's budget with 28 ribbon
    buttons and a single file. Keep the focused element, then inputs, then
    content items (files, rows, links), then controls; present by position.
    """
    by_position = sorted(candidates, key=lambda item: (item[0].rect[1], item[0].rect[0]))
    if len(by_position) <= MAX_ELEMENTS:
        return by_position
    focused = [item for item in by_position if item[1]]
    rest = [item for item in by_position if not item[1]]
    inputs = [item for item in rest if item[0].control_type in _INPUT_ROLES][:_INPUT_QUOTA]
    content = [item for item in rest if item[0].control_type in _CONTENT_ROLES][:_CONTENT_QUOTA]
    chosen = focused + inputs + content
    chosen_ids = {id(item) for item in chosen}
    for item in rest:
        if len(chosen) >= MAX_ELEMENTS:
            break
        if id(item) not in chosen_ids:
            chosen.append(item)
            chosen_ids.add(id(item))
    return sorted(chosen[:MAX_ELEMENTS], key=lambda item: (item[0].rect[1], item[0].rect[0]))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()
