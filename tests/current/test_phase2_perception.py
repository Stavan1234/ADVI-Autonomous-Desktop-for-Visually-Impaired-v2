"""Structured perception: observe_window / interact over a fake UIA window."""
from __future__ import annotations

from types import SimpleNamespace

from advi.agent import AgentSession
from advi.capabilities.apps import DesktopUiCapability
from advi.capabilities.apps.ui_observer import MAX_ELEMENTS, UiObserver
from advi.capabilities.apps.uia import UiaNode
from advi.tools.contracts import ToolCall, ToolSpec


def _n(name, role, top, left=10, key=None):
    return UiaNode(raw=None, name=name, control_type=role, automation_id=key or f"{role}:{name}:{top}",
                   rect=(left, top, left + 100, top + 20), hwnd=1)


class FakeWindow:
    """A small notepad-like window: a text area, a Save button, a toggle, a menu."""

    def __init__(self, extra=()):
        self.window = UiaNode(raw=None, name="Untitled - Notepad", control_type="Window", automation_id="w", rect=(0, 0, 800, 600), hwnd=7)
        self.values = {"Edit:Text editor:100": ""}
        self.saved = False
        self.word_wrap = False
        self.focused = None
        self.disabled = {"Button:Print:40"}
        self.invoke_supported = {"Button:Save:40"}
        self.extra = list(extra)
        self.typed_lost = False

    def _elements(self):
        nodes = [
            _n("Save", "Button", 40), _n("Print", "Button", 40, left=120),
            _n("Word wrap", "CheckBox", 70), _n("Text editor", "Edit", 100),
            _n("", "Button", 40, left=300),             # nameless icon wrapper: dropped
            _n("Save", "Button", 40),                   # exact duplicate: dropped
        ]
        if self.saved:
            nodes.append(_n("Saved", "Hyperlink", 500))
        return nodes + self.extra

    # ObserverDriver
    def window_for_hwnd(self, hwnd): return self.window if hwnd == 7 else None
    def foreground_window(self): return self.window
    def find_window_titled(self, title): return self.window if title.casefold() in self.window.name.casefold() else None
    def content_root(self, window): return window
    def interactive_elements(self, root): return self._elements()
    def is_enabled(self, node): return node.automation_id not in self.disabled
    def has_focus(self, node): return self.focused == node.automation_id
    def focus(self, node):
        self.focused = node.automation_id
        return True
    def value(self, node): return self.values.get(node.automation_id)
    def set_value(self, node, text):
        if node.automation_id in self.values:
            self.values[node.automation_id] = text
            return True
        return False
    def invoke(self, node):
        if node.automation_id in self.invoke_supported:
            self.saved = True
            return True
        return False
    def select(self, node): return False
    def toggle(self, node):
        if node.control_type == "CheckBox":
            self.word_wrap = not self.word_wrap
            self.extra = [_n("Wrapped", "Hyperlink", 520)] if self.word_wrap else []
            return True
        return False
    def expand(self, node): return False
    def click(self, node):
        if node.control_type == "Edit":
            self.focused = node.automation_id
    def type_text(self, text):
        if not self.typed_lost and self.focused in self.values:
            self.values[self.focused] += text
    def press(self, keys): pass
    def post_text(self, window, text): return False  # native app: no Chromium host
    def bring_to_front(self, window): return True


def _observer(app):
    return UiObserver(app, target_hwnd=lambda: 7, sleep=lambda _s: None)


def test_observation_lists_named_interactive_elements_with_ids():
    obs = _observer(FakeWindow()).observe()
    names = [(e["id"], e["role"], e["name"]) for e in obs.elements]
    assert names == [("e1", "Button", "Save"), ("e2", "Button", "Print"), ("e3", "CheckBox", "Word wrap"), ("e4", "Edit", "Text editor")]
    assert obs.elements[1]["disabled"] is True
    assert obs.payload()["window"] == "Untitled - Notepad"


def test_click_through_invoke_is_verified_by_a_visible_change():
    app = FakeWindow()
    observer = _observer(app)
    observer.observe()
    outcome = observer.interact("e1", "click")
    assert outcome.ok and outcome.details["method"] == "invoke"
    assert outcome.details["screen_changed"] is True
    assert any(e["name"] == "Saved" for e in outcome.details["observation"]["elements"])


def test_click_with_no_visible_change_is_not_claimed_as_verified():
    app = FakeWindow(extra=[_n("Help", "Button", 40, left=500)])
    observer = _observer(app)
    obs = observer.observe()
    help_id = next(e["id"] for e in obs.elements if e["name"] == "Help")
    outcome = observer.interact(help_id, "click")  # accepted, but nothing on screen changes
    assert not outcome.ok and outcome.details["method"] == "mouse_click"
    assert outcome.details["screen_changed"] is False
    assert "could not be confirmed" in outcome.message


def test_typing_is_read_back():
    app = FakeWindow()
    observer = _observer(app)
    observer.observe()
    assert observer.interact("e4", "type", "hello").ok
    app.typed_lost = True
    app.values["Edit:Text editor:100"] = ""
    observer.observe()
    lost = observer.interact("e4", "type", "again")
    assert not lost.ok and lost.details["value_after"] == ""


def test_toggle_disabled_unknown_and_stale_ids():
    app = FakeWindow()
    observer = _observer(app)
    first = observer.observe()
    assert observer.interact("e3", "toggle").ok
    assert not observer.interact("e2", "click").ok          # disabled
    assert "Unknown action" in observer.interact("e1", "launch").message
    assert observer.latest.number > first.number
    assert "observe_window again" in observer.interact("e99", "click").message


def test_budget_keeps_inputs_and_content_not_just_toolbar_buttons():
    toolbar = [_n(f"Tool {i}", "Button", 10, left=i * 5) for i in range(80)]
    files = [_n(f"file{i}.pdf", "ListItem", 300 + i * 20) for i in range(30)]
    obs = _observer(FakeWindow(extra=toolbar + files)).observe()
    roles = [e["role"] for e in obs.elements]
    assert len(obs.elements) == MAX_ELEMENTS and obs.truncated
    assert roles.count("ListItem") >= 20 and "Edit" in roles


def test_capability_wraps_observation_and_interaction():
    app = FakeWindow()
    capability = DesktopUiCapability(observer_factory=lambda: _observer(app))
    observed = capability.execute(ToolCall("c1", "observe_window", {}))
    assert observed.success and observed.output["elements"][0]["id"] == "e1"
    clicked = capability.execute(ToolCall("c2", "interact", {"element_id": "e1", "action": "click"}))
    assert clicked.verified and clicked.output["screen_changed"] is True
    missing = capability.execute(ToolCall("c3", "observe_window", {"application": "Photoshop"}))
    assert not missing.success and "Photoshop" in missing.error


def test_gui_surfaces_also_offer_structured_perception():
    names = ["open_application", "click", "type_text", "press_key", "read_screen", "inspect_window",
             "list_windows", "focus_window", "wait", "observe_window", "interact"]
    caps = {n: ("desktop_ui" if n in {"observe_window", "interact"} else "desktop_control") for n in names}
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.tools = SimpleNamespace(model_tools=lambda: [ToolSpec(n, n, caps[n]) for n in names])
    offered = {t["function"]["name"] for t in session._select_model_tools("open notepad and write hello")}
    assert {"observe_window", "interact"} <= offered


def test_compound_app_request_gets_structured_tools_beyond_the_launch():
    names = ["open_application", "observe_window", "interact"]
    caps = {"open_application": "desktop_control", "observe_window": "desktop_ui", "interact": "desktop_ui"}
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.tools = SimpleNamespace(model_tools=lambda: [ToolSpec(n, n, caps[n]) for n in names])
    offered = {t["function"]["name"] for t in session._select_model_tools("open settings and turn on dark mode")}
    assert {"open_application", "observe_window", "interact"} <= offered


def test_webview_style_typing_uses_posted_characters():
    app = FakeWindow()
    app.typed_lost = True  # injected keystrokes dropped, as in WhatsApp's WebView2
    posted = []

    def post_text(window, text):
        posted.append(text)
        app.values[app.focused] += text
        return True

    app.post_text = post_text
    observer = _observer(app)
    observer.observe()
    outcome = observer.interact("e4", "type", "hello")
    assert outcome.ok and outcome.details["method"] == "posted_chars" and posted == ["hello"]
