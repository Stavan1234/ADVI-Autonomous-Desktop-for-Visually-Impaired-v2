from __future__ import annotations

from types import SimpleNamespace

from advi.agent.session import AgentSession
from advi.agent.contracts import TaskState, TaskStatus
from advi.capabilities.desktop.context import ApplicationSessionState, DesktopContext
from advi.capabilities.desktop.executor import DesktopCapability
from advi.capabilities.browser.executor import BrowserCapability
from advi.core.context_router import ContextRouter
from advi.tools import ToolCall
from tests.current.test_runtime_excellence_regressions import _session_for_tools


class FakeControl:
    def __init__(self, *, visible=True, enabled=True, exists=True):
        self._visible = visible
        self._enabled = enabled
        self._exists = exists
        self.focused = False
        self.clicked = False

    def is_visible(self): return self._visible
    def is_enabled(self): return self._enabled
    def exists(self, *args, **kwargs): return self._exists
    def has_focus(self): return self.focused
    def set_focus(self): self.focused = True
    def click_input(self): self.clicked = True; self.focused = True


def test_desktop_actionability_gate_rejects_disabled_control(monkeypatch):
    control = FakeControl(enabled=False)
    cap = DesktopCapability(context=DesktopContext(current_application="Word", target_hwnd=10))
    monkeypatch.setattr(cap, "_ensure_target_window", lambda **kwargs: (True, {}))
    monkeypatch.setattr(cap, "_resolve_pointer_target", lambda target, action=None: control)
    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: SimpleNamespace(click=lambda *a: None))
    result = cap.execute(ToolCall("1", "click", {"target": "Save"}))
    assert result.success is False
    assert result.verification_details["method"] == "desktop_actionability_gate"


def test_task_does_not_immediately_repeat_failed_tool():
    session = _session_for_tools()
    session.task = TaskState(goal="find calculator", task_id="t1", status=TaskStatus.IN_PROGRESS)
    session.last_tool = "find_application"
    session.last_tool_success = False
    session.last_tool_summary = "failed"
    names = {x["function"]["name"] for x in session._select_model_tools("find calculator", "find_application")}
    assert "find_application" not in names


def test_pwa_browser_bookkeeping_preserves_application_identity():
    ctx = DesktopContext(current_application="YouTube Music")
    s = ctx.start_application_session("YouTube Music", record={"runtime_type": "pwa", "app_id": "Brave._crx_music"})
    s.bind_window(hwnd=22, pid=33, title="YouTube Music", process_name="brave.exe", state=ApplicationSessionState.READY)
    ctx.set_target_tab("tab1", "https://music.youtube.com/search?q=Never+Say+Never", "YouTube Music")
    assert ctx.current_application == "YouTube Music"
    assert ctx.application_session is s


def test_youtube_music_play_helper_uses_current_search_query(monkeypatch):
    ctx = DesktopContext(current_application="YouTube Music", target_tab_id="tab1", target_tab_url="https://music.youtube.com/search?q=Never+Say+Never")
    browser = BrowserCapability(context=ctx)
    script_results = []
    browser.cdp = SimpleNamespace(
        get_tab_info=lambda tab_id: {"url": ctx.target_tab_url, "title": "YouTube Music"},
        evaluate_js=lambda tab_id, script: {"value": {"found": True, "actionable": True, "query": "Never Say Never", "before": {"label": "Play"}}},
    )
    monkeypatch.setattr(browser, "_ensure_youtube_music_playing", lambda tab_id, timeout=10.0: {"music": True, "playing": True, "currentTime": 1.0})
    result = browser._youtube_music_play_requested_result(ToolCall("1", "click_web_element", {"selector": "ytmusic-play-button-renderer[aria-label=\"Play\"]"}), "tab1")
    assert result is not None
    assert result.success is True
    assert result.verified is True
    assert result.verification_details["query"] == "Never Say Never"


def test_pwa_application_session_survives_context_router():
    ctx = DesktopContext(current_application="YouTube Music")
    s = ctx.start_application_session("YouTube Music", record={"runtime_type": "pwa", "command": "shell:AppsFolder\\Brave._crx_music"})
    s.bind_window(hwnd=1, pid=2, title="YouTube Music", process_name="brave.exe", state=ApplicationSessionState.READY)
    ctx.set_target_tab("tab", "https://music.youtube.com/", "YouTube Music")
    packet = ContextRouter(max_context_chars=6000).build(observation=ctx.observation_snapshot())
    assert packet.sections["observation"]["application_session"]["identity"]["runtime_type"] == "pwa"
    assert packet.sections["observation"]["active_application"] == "YouTube Music"


def test_browser_click_rejects_unactionable_target(monkeypatch):
    ctx = DesktopContext(current_application="Chrome", target_tab_id="tab1", target_tab_url="https://example.com")
    browser = BrowserCapability(context=ctx)
    browser.cdp = SimpleNamespace(
        list_tabs=lambda: [{"id":"tab1", "url":ctx.target_tab_url, "title":"Example"}],
        get_tab_info=lambda tab_id: {"url":ctx.target_tab_url, "title":"Example"},
        evaluate_script=lambda tab_id, script: {"value": {"success": False, "reason": "element_not_actionable"}},
    )
    result = browser.execute(ToolCall("1", "click_web_element", {"selector": "button#save"}))
    assert result.success is False
    assert "not found" not in (result.error or "").lower()


def test_browser_click_music_selector_routes_to_row_specific_helper(monkeypatch):
    ctx = DesktopContext(current_application="YouTube Music", target_tab_id="tab1", target_tab_url="https://music.youtube.com/search?q=Never+Say+Never")
    browser = BrowserCapability(context=ctx)
    browser.cdp = SimpleNamespace(
        list_tabs=lambda: [{"id":"tab1", "url":ctx.target_tab_url, "title":"YouTube Music"}],
        get_tab_info=lambda tab_id: {"url":ctx.target_tab_url, "title":"YouTube Music"},
    )
    expected = object()
    monkeypatch.setattr(browser, "_youtube_music_play_requested_result", lambda action, tab_id: expected)
    result = browser.execute(ToolCall("1", "click_web_element", {"selector": "ytmusic-play-button-renderer[aria-label=\"Play\"]"}))
    assert result is expected
