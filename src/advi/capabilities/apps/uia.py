"""Thin Windows UI Automation driver for app skills.

App skills query UIA with native conditions (``FindFirst`` scoped to a known
root) instead of walking the tree. Measured on WhatsApp Desktop (WebView2): a
full ``descendants()`` walk takes ~15 s and returns ~18,800 nodes because the
web tree is exposed many times over, while a scoped ``FindFirst`` takes ~0.02 s.

Skills depend on the small ``UiaDriver`` surface below so they can be tested
against a fake; ``WindowsUiaDriver`` is the real implementation.
"""
from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class UiaNode:
    """Snapshot handle for one UIA element."""

    raw: Any
    name: str
    control_type: str
    automation_id: str
    rect: tuple[int, int, int, int]
    hwnd: int | None = None

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


class UiaDriver(Protocol):
    def find_window(self, title_contains: str, process_names: tuple[str, ...]) -> UiaNode | None: ...
    def bring_to_front(self, window: UiaNode) -> bool: ...
    def find_first(self, root: UiaNode, *, control_type: str | None = None, name: str | None = None,
                   automation_id: str | None = None) -> UiaNode | None: ...
    def find_all(self, root: UiaNode, *, control_type: str | None = None, name: str | None = None) -> list[UiaNode]: ...
    def children(self, node: UiaNode) -> list[UiaNode]: ...
    def has_focus(self, node: UiaNode) -> bool: ...
    def focus(self, node: UiaNode) -> bool: ...
    def value(self, node: UiaNode) -> str | None: ...
    def set_value(self, node: UiaNode, text: str) -> bool: ...
    def invoke(self, node: UiaNode) -> bool: ...
    def post_text(self, window: UiaNode, text: str) -> bool: ...
    def post_backspaces(self, window: UiaNode, count: int) -> bool: ...
    def click(self, node: UiaNode) -> None: ...
    def type_text(self, text: str) -> None: ...
    def press(self, keys: str) -> None: ...
    def launch(self, target: str) -> None: ...


_CONTROL_TYPES = {
    "Button": 50000, "CheckBox": 50002, "ComboBox": 50003, "Edit": 50004, "Hyperlink": 50005,
    "List": 50008, "ListItem": 50007, "MenuItem": 50011, "RadioButton": 50013, "Slider": 50015,
    "TabItem": 50019, "Text": 50020, "TreeItem": 50024, "Document": 50030, "DataGrid": 50028,
    "DataItem": 50029, "SplitButton": 50031, "Group": 50026, "Pane": 50033, "Window": 50032,
}
_CONTROL_NAMES = {value: key for key, value in _CONTROL_TYPES.items()}

# Controls a user can act on. One native OR-condition FindAll over these, limited
# to on-screen elements, measured 0.01-0.17 s per window on this machine.
INTERACTIVE_TYPES = (
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem", "RadioButton",
    "Slider", "TabItem", "TreeItem", "DataItem", "SplitButton",
    # Windows 11 Notepad's editor is a Document named "Text editor", not an Edit.
    "Document",
)

_CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "Shell_TrayWnd", "Progman", "WorkerW"}


class WindowsUiaDriver:
    """UIA via pywinauto's COM bindings; keyboard/mouse via pywinauto."""

    def __init__(self) -> None:
        from pywinauto.uia_defines import IUIA

        self._iuia = IUIA()
        self._uia = self._iuia.UIA_dll
        self._client = self._iuia.iuia
        self._walker = self._client.ControlViewWalker

    # --- element helpers -------------------------------------------------
    def _node(self, element: Any, hwnd: int | None = None) -> UiaNode | None:
        if not element:
            return None
        try:
            rect = element.CurrentBoundingRectangle
            return UiaNode(
                raw=element,
                name=str(element.CurrentName or ""),
                control_type=_CONTROL_NAMES.get(int(element.CurrentControlType), str(element.CurrentControlType)),
                automation_id=str(element.CurrentAutomationId or ""),
                rect=(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                hwnd=hwnd,
            )
        except Exception:
            return None

    def _condition(self, *, control_type: str | None, name: str | None, automation_id: str | None) -> Any:
        parts = []
        if control_type:
            parts.append(self._client.CreatePropertyCondition(self._uia.UIA_ControlTypePropertyId, _CONTROL_TYPES[control_type]))
        if name is not None:
            parts.append(self._client.CreatePropertyCondition(self._uia.UIA_NamePropertyId, name))
        if automation_id is not None:
            parts.append(self._client.CreatePropertyCondition(self._uia.UIA_AutomationIdPropertyId, automation_id))
        if not parts:
            return self._client.CreateTrueCondition()
        condition = parts[0]
        for extra in parts[1:]:
            condition = self._client.CreateAndCondition(condition, extra)
        return condition

    # --- generic perception ---------------------------------------------
    def window_for_hwnd(self, hwnd: int) -> UiaNode | None:
        try:
            return self._node(self._client.ElementFromHandle(int(hwnd)), hwnd=int(hwnd))
        except Exception:
            return None

    def foreground_window(self) -> UiaNode | None:
        """The foreground top-level window, skipping ADVI's own console and the shell."""
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        if buffer.value in _CONSOLE_CLASSES:
            return None
        return self.window_for_hwnd(hwnd)

    def find_window_titled(self, title_contains: str) -> UiaNode | None:
        from pywinauto import Desktop

        wanted = title_contains.casefold()
        for window in Desktop(backend="uia").windows():
            info = window.element_info
            if info.visible and wanted in str(info.name or "").casefold() and info.class_name not in _CONSOLE_CLASSES:
                return self._node(info.element, hwnd=int(info.handle or 0) or None)
        return None

    def content_root(self, window: UiaNode) -> UiaNode:
        """Web apps (WebView2/Chrome) expose their page as a RootWebArea document.

        Searching from it avoids the duplicated web trees seen under the window
        root (WhatsApp exposed ~95 copies).
        """
        doc = self.find_first(window, control_type="Document", automation_id="RootWebArea")
        return doc or window

    def interactive_elements(self, root: UiaNode) -> list[UiaNode]:
        type_conditions = [
            self._client.CreatePropertyCondition(self._uia.UIA_ControlTypePropertyId, _CONTROL_TYPES[name])
            for name in INTERACTIVE_TYPES
        ]
        condition = type_conditions[0]
        for extra in type_conditions[1:]:
            condition = self._client.CreateOrCondition(condition, extra)
        condition = self._client.CreateAndCondition(
            condition, self._client.CreatePropertyCondition(self._uia.UIA_IsOffscreenPropertyId, False)
        )
        found = root.raw.FindAll(self._uia.TreeScope_Descendants, condition)
        nodes = [self._node(found.GetElement(i), root.hwnd) for i in range(found.Length)]
        return [node for node in nodes if node is not None]

    def is_enabled(self, node: UiaNode) -> bool:
        try:
            return bool(node.raw.CurrentIsEnabled)
        except Exception:
            return False

    def _pattern(self, node: UiaNode, pattern_id: int, interface: Any) -> Any:
        try:
            pattern = node.raw.GetCurrentPattern(pattern_id)
            return pattern.QueryInterface(interface) if pattern else None
        except Exception:
            return None

    def invoke(self, node: UiaNode) -> bool:
        pattern = self._pattern(node, self._uia.UIA_InvokePatternId, self._uia.IUIAutomationInvokePattern)
        if pattern is None:
            return False
        try:
            pattern.Invoke()
            return True
        except Exception:
            return False

    def select(self, node: UiaNode) -> bool:
        pattern = self._pattern(node, self._uia.UIA_SelectionItemPatternId, self._uia.IUIAutomationSelectionItemPattern)
        if pattern is None:
            return False
        try:
            pattern.Select()
            return True
        except Exception:
            return False

    def toggle(self, node: UiaNode) -> bool:
        pattern = self._pattern(node, self._uia.UIA_TogglePatternId, self._uia.IUIAutomationTogglePattern)
        if pattern is None:
            return False
        try:
            pattern.Toggle()
            return True
        except Exception:
            return False

    def expand(self, node: UiaNode) -> bool:
        pattern = self._pattern(node, self._uia.UIA_ExpandCollapsePatternId, self._uia.IUIAutomationExpandCollapsePattern)
        if pattern is None:
            return False
        try:
            pattern.Expand()
            return True
        except Exception:
            return False

    # --- UiaDriver -------------------------------------------------------
    def find_window(self, title_contains: str, process_names: tuple[str, ...]) -> UiaNode | None:
        import psutil
        from pywinauto import Desktop

        wanted = {name.casefold() for name in process_names}
        for window in Desktop(backend="uia").windows():
            info = window.element_info
            if title_contains.casefold() not in str(info.name or "").casefold():
                continue
            try:
                process = psutil.Process(int(info.process_id)).name().casefold()
            except Exception:
                process = ""
            if wanted and process not in wanted:
                continue
            return self._node(info.element, hwnd=int(info.handle or 0) or None)
        return None

    def bring_to_front(self, window: UiaNode) -> bool:
        if os.name != "nt" or not window.hwnd:
            return False
        user32 = ctypes.windll.user32
        if user32.IsIconic(window.hwnd):
            user32.ShowWindow(window.hwnd, 9)
        user32.SetForegroundWindow(window.hwnd)
        time.sleep(0.2)
        return int(user32.GetForegroundWindow()) == int(window.hwnd)

    def find_first(self, root, *, control_type=None, name=None, automation_id=None):
        element = root.raw.FindFirst(
            self._uia.TreeScope_Descendants,
            self._condition(control_type=control_type, name=name, automation_id=automation_id),
        )
        return self._node(element, root.hwnd)

    def find_all(self, root, *, control_type=None, name=None):
        found = root.raw.FindAll(
            self._uia.TreeScope_Descendants,
            self._condition(control_type=control_type, name=name, automation_id=None),
        )
        nodes = [self._node(found.GetElement(i), root.hwnd) for i in range(found.Length)]
        return [node for node in nodes if node is not None]

    def children(self, node):
        result = []
        child = self._walker.GetFirstChildElement(node.raw)
        while child:
            converted = self._node(child, node.hwnd)
            if converted is not None:
                result.append(converted)
            child = self._walker.GetNextSiblingElement(child)
        return result

    def has_focus(self, node):
        try:
            return bool(node.raw.CurrentHasKeyboardFocus)
        except Exception:
            return False

    def focus(self, node):
        try:
            node.raw.SetFocus()
        except Exception:
            return False
        time.sleep(0.1)
        return self.has_focus(node)

    def value(self, node):
        """Field text via ValuePattern, else TextPattern (contenteditable composers)."""
        try:
            pattern = node.raw.GetCurrentPattern(self._uia.UIA_ValuePatternId)
            if pattern:
                value_pattern = pattern.QueryInterface(self._uia.IUIAutomationValuePattern)
                return str(value_pattern.CurrentValue or "")
        except Exception:
            pass
        try:
            pattern = node.raw.GetCurrentPattern(self._uia.UIA_TextPatternId)
            if pattern:
                text_pattern = pattern.QueryInterface(self._uia.IUIAutomationTextPattern)
                return str(text_pattern.DocumentRange.GetText(-1) or "")
        except Exception:
            pass
        return None

    def set_value(self, node, text):
        try:
            pattern = node.raw.GetCurrentPattern(self._uia.UIA_ValuePatternId)
            if not pattern:
                return False
            pattern.QueryInterface(self._uia.IUIAutomationValuePattern).SetValue(text)
            return True
        except Exception:
            return False

    # --- WebView2 text delivery -------------------------------------------
    # WhatsApp Desktop hosts WebView2 in visual (composition) mode. Injected
    # keystrokes (SendInput) land on the host's Chrome_WidgetWin_0 and are
    # dropped, so typed text never reached the page even with focus reported.
    # Posting WM_CHAR to that host window delivers characters to whichever DOM
    # element has focus (set first through UIA SetFocus). Verified live,
    # 2026-10-14, on the search box and message composer.
    _WM_KEYDOWN, _WM_KEYUP, _WM_CHAR, _VK_BACK = 0x0100, 0x0101, 0x0102, 0x08

    def _webview_host(self, window: UiaNode) -> int | None:
        if os.name != "nt" or not window.hwnd:
            return None
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        found: list[int] = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def callback(hwnd, _lparam):
            buffer = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buffer, 64)
            if buffer.value.startswith("Chrome_WidgetWin") and user32.IsWindowVisible(hwnd):
                found.append(int(hwnd))
                return False
            return True

        user32.EnumChildWindows(int(window.hwnd), proc(callback), 0)
        return found[0] if found else None

    def post_text(self, window, text):
        host = self._webview_host(window)
        if host is None:
            return False
        user32 = ctypes.windll.user32
        # One message per UTF-16 code unit, so emoji and other non-BMP text survive.
        units = text.encode("utf-16-le")
        for index in range(0, len(units), 2):
            code_unit = int.from_bytes(units[index:index + 2], "little")
            if not user32.PostMessageW(host, self._WM_CHAR, code_unit, 1):
                return False
        return True

    def post_backspaces(self, window, count):
        host = self._webview_host(window)
        if host is None:
            return False
        user32 = ctypes.windll.user32
        for _ in range(max(0, int(count))):
            user32.PostMessageW(host, self._WM_KEYDOWN, self._VK_BACK, 0x000E0001)
            user32.PostMessageW(host, self._WM_CHAR, self._VK_BACK, 0x000E0001)
            user32.PostMessageW(host, self._WM_KEYUP, self._VK_BACK, 0xC00E0001)
        return True

    def click(self, node):
        from pywinauto import mouse

        mouse.click(coords=node.center)

    def type_text(self, text):
        from pywinauto import keyboard

        # Escape pywinauto's special characters; newlines become Shift+Enter so
        # chat composers insert a line break instead of sending.
        escaped = "".join("{%s}" % ch if ch in "{}+^%~()" else ch for ch in text)
        escaped = escaped.replace("\r\n", "\n").replace("\n", "+{ENTER}")
        keyboard.send_keys(escaped, with_spaces=True, with_newlines=False, pause=0.01)

    def press(self, keys):
        from pywinauto import keyboard

        keyboard.send_keys(keys)

    def launch(self, target):
        os.startfile(target)  # type: ignore[attr-defined]
