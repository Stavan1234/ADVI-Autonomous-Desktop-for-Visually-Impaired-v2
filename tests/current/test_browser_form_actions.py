from __future__ import annotations

from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.desktop.context import DesktopContext
from advi.core.verification import ToolVerifier
from advi.tools import ToolCall


class FakeCDP:
    def __init__(self):
        self.calls = []

    def list_tabs(self):
        return [{"id": "tab-1", "url": "https://example.test", "title": "Example"}]

    def evaluate_js(self, tab_id, expression, **kwargs):
        self.calls.append((tab_id, expression))
        if "const selectors" in expression:
            return [{"selector":"#message", "label":"Message", "editable":True}]
        if "document.querySelector" in expression:
            return {"success": True, "before": "", "after": "hello", "selector": "#message", "tag": "input", "role": None}
        return ""


def test_inspect_web_page_returns_bounded_inventory(monkeypatch):
    ctx = DesktopContext()
    ctx.set_target_tab("tab-1", "https://example.test", "Example")
    cap = BrowserCapability(context=ctx)
    fake = FakeCDP()
    cap.cdp = fake
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)

    result = cap.execute(ToolCall("1", "inspect_web_page", {"limit": 20}))
    assert result.success is True
    assert result.verified is True
    assert result.metadata["tab_id"] == "tab-1"


def test_type_web_element_preserves_exact_target_and_verifies(monkeypatch):
    ctx = DesktopContext()
    ctx.set_target_tab("tab-1", "https://example.test", "Example")
    cap = BrowserCapability(context=ctx)
    fake = FakeCDP()
    cap.cdp = fake
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)

    result = cap.execute(ToolCall("2", "type_web_element", {"selector": "#message", "text": "hello"}))
    assert result.success is True
    assert result.verified is True
    assert result.metadata["selector"] == "#message"
    verified = ToolVerifier().verify(ToolCall("2", "type_web_element", {"selector": "#message", "text": "hello"}), result)
    assert verified.verified is True


def test_inspect_web_page_prioritizes_youtube_video_results(monkeypatch):
    from advi.capabilities.browser.executor import BrowserCapability
    from advi.capabilities.desktop.context import DesktopContext
    from advi.tools.contracts import ToolCall

    class YouTubeInspectCDP:
        def __init__(self):
            self.script = ""
        def list_tabs(self):
            return [{"id": "tab-1", "url": "https://www.youtube.com/results?search_query=python", "title": "python - YouTube"}]
        def evaluate_js(self, tab_id, script, **kwargs):
            self.script = script
            return [{
                "index": 0,
                "kind": "video_result",
                "result_index": 1,
                "label": "First video",
                "href": "https://www.youtube.com/watch?v=abc",
                "selector": 'a[href*="/watch?v=abc"]',
            }]

    cap = BrowserCapability(context=DesktopContext(target_tab_id="tab-1"))
    cap.cdp = YouTubeInspectCDP()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall("inspect", "inspect_web_page", {"limit": 20}))
    assert result.success
    assert result.output[0]["kind"] == "video_result"
    assert "youtube\\.com\\/results" in cap.cdp.script
    assert "video_result" in cap.cdp.script
