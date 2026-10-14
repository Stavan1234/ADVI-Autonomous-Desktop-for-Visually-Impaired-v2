from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.desktop.context import DesktopContext
from advi.tools.contracts import ToolCall


class WorkflowCDP:
    def __init__(self):
        self.url = "https://example.com"
        self.title = "Example"
        self.calls = []

    def list_tabs(self):
        return [{"id": "tab-1", "type": "page", "url": self.url, "title": self.title}]

    def get_tab_info(self, tab_id):
        assert tab_id == "tab-1"
        return {"id": tab_id, "type": "page", "url": self.url, "title": self.title}

    def navigate_tab(self, tab_id, url):
        self.calls.append(("navigate", tab_id, url))
        self.url = url
        self.title = "Navigated"
        return True

    def evaluate_script(self, tab_id, script):
        self.calls.append(("evaluate", tab_id, script))
        if "document.querySelector" in script:
            return {"value": {"success": True, "before": {"url": self.url, "title": self.title}}}
        if "innerText" in script:
            return {"value": "Example page body"}
        return {"value": True}


def make_cap():
    cap = BrowserCapability(context=DesktopContext())
    cap.context.set_target_tab("tab-1", url="https://example.com", title="Example")
    cdp = WorkflowCDP()
    cap.cdp = cdp
    return cap, cdp


def test_navigate_tracks_actual_tab_state(monkeypatch):
    cap, cdp = make_cap()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall(call_id="test", name="navigate", arguments={"url": "https://example.com/docs"}))
    assert result.success
    assert result.data == "https://example.com/docs"
    assert result.metadata["tab_id"] == "tab-1"
    assert result.metadata["current_url"] == "https://example.com/docs"
    assert cap.context.target_tab_url == "https://example.com/docs"


def test_click_records_before_and_after_state(monkeypatch):
    cap, _ = make_cap()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall(call_id="test", name="click_web_element", arguments={"selector": "#submit"}))
    assert result.success
    assert result.metadata["before_url"] == "https://example.com"
    assert result.metadata["tab_id"] == "tab-1"


def test_read_page_returns_page_identity(monkeypatch):
    cap, _ = make_cap()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall(call_id="test", name="read_web_page"))
    assert result.success
    assert result.data == "Example page body"
    assert result.metadata["current_url"] == "https://example.com"
    assert result.metadata["tab_title"] == "Example"


def test_cdp_real_class_exposes_executor_aliases():
    from advi.capabilities.browser.cdp import CDPBrowser
    assert hasattr(CDPBrowser, "evaluate_script")
    assert hasattr(CDPBrowser, "navigate_tab")


def test_search_does_not_redirect_to_google_when_in_page_search_fails(monkeypatch):
    cap, cdp = make_cap()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)

    def failed_search(tab_id, script):
        cdp.calls.append(("evaluate", tab_id, script))
        return {"value": False}

    cdp.evaluate_script = failed_search
    result = cap.execute(
        ToolCall(call_id="search-fail", name="search", arguments={"query": "private query", "target": "site search box"})
    )

    assert result.success is False
    assert "will not silently redirect" in (result.error or "")
    assert not any(call[0] == "navigate" for call in cdp.calls)


def test_cdp_activate_tab_requires_success_response(monkeypatch):
    import json
    from advi.capabilities.browser.cdp import CDPBrowser

    class WS:
        def __init__(self, response): self.response = response
        def send(self, payload): self.payload = json.loads(payload)
        def recv(self): return json.dumps(self.response)
        def close(self): pass

    ws = WS({"id": 1, "error": {"message": "tab gone"}})
    browser = CDPBrowser()
    monkeypatch.setattr(browser, "_http_get_json", lambda path: {"webSocketDebuggerUrl": "ws://test"})
    monkeypatch.setattr("advi.capabilities.browser.cdp.websocket.create_connection", lambda *args, **kwargs: ws)
    monkeypatch.setattr("advi.capabilities.browser.cdp.time.time", lambda: 0.001)
    assert browser.activate_tab("missing") is False


def test_cdp_activate_tab_accepts_success_response(monkeypatch):
    import json
    from advi.capabilities.browser.cdp import CDPBrowser

    class WS:
        def send(self, payload): self.payload = json.loads(payload)
        def recv(self): return json.dumps({"id": self.payload["id"], "result": {}})
        def close(self): pass

    browser = CDPBrowser()
    monkeypatch.setattr(browser, "_http_get_json", lambda path: {"webSocketDebuggerUrl": "ws://test"})
    monkeypatch.setattr("advi.capabilities.browser.cdp.websocket.create_connection", lambda *args, **kwargs: WS())
    assert browser.activate_tab("owned") is True


def test_browser_search_delegates_to_canonical_cdp_search(monkeypatch):
    from advi.capabilities.browser.executor import BrowserCapability
    from advi.capabilities.desktop.context import DesktopContext
    from advi.tools.contracts import ToolCall

    class FakeCDP:
        def list_tabs(self):
            return [{"id": "owned", "url": "https://example.test", "title": "Example"}]
        def execute_in_site_search(self, tab_id, query):
            assert tab_id == "owned"
            assert query == "ADVI"
            return True
        def get_tab_info(self, tab_id):
            return {"id": tab_id, "url": "https://example.test/search?q=ADVI", "title": "Search"}

    context = DesktopContext(target_tab_id="owned", target_tab_url="https://example.test")
    cap = BrowserCapability(context=context)
    cap.cdp = FakeCDP()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall("1", "search", {"query": "ADVI"}))
    assert result.success is True
    assert result.verified is True
    assert result.metadata["method"] == "in_page"


def test_youtube_result_recovery_opens_first_video_and_verifies_playback(monkeypatch):
    class YouTubeCDP:
        def __init__(self):
            self.url = "https://www.youtube.com/results?search_query=baby+shark"
            self.play_clicked = False
            self.state_calls = 0
            self.calls = []

        def get_tab_info(self, tab_id):
            return {"id": tab_id, "url": self.url, "title": "YouTube"}

        def evaluate_js(self, tab_id, script):
            self.calls.append(script)
            if 'a[href*="/watch?v="]' in script:
                return {"value": {"found": True, "href": "https://www.youtube.com/watch?v=abc123", "text": "Baby Shark"}}
            if "const video = document.querySelector('video')" in script:
                self.state_calls += 1
                if self.play_clicked:
                    return {"value": {"watch": True, "ready": True, "paused": False, "currentTime": 1.2, "playing": True, "playFound": True}}
                return {"value": {"watch": True, "ready": True, "paused": True, "currentTime": 0, "playing": False, "playFound": True}}
            if "const play = buttons.find" in script:
                self.play_clicked = True
                return {"value": {"clicked": True}}
            return {"value": True}

        def navigate_tab(self, tab_id, url, timeout=10.0):
            self.calls.append(("navigate", tab_id, url))
            self.url = url
            return True

        def list_tabs(self):
            return [{"id": "tab-1", "url": self.url, "title": "YouTube"}]

    cap = BrowserCapability(context=DesktopContext(target_tab_id="tab-1", target_tab_url="https://www.youtube.com/results?search_query=baby+shark"))
    cdp = YouTubeCDP()
    cap.cdp = cdp
    monkeypatch.setattr("advi.capabilities.browser.executor.time.sleep", lambda _seconds: None)

    result = cap._youtube_play_first_result(
        ToolCall("yt-1", "click_web_element", {"selector": "ytd-video-renderer a#video-title"}),
        "ytd-video-renderer a#video-title",
        "tab-1",
    )

    assert result is not None
    assert result.success is True
    assert result.verified is True
    assert result.metadata["url"] == "https://www.youtube.com/watch?v=abc123"
    assert cdp.play_clicked is True


def test_click_accepts_direct_cdp_javascript_result_shape(monkeypatch):
    class DirectCDP(WorkflowCDP):
        def evaluate_script(self, tab_id, script):
            self.calls.append(("evaluate", tab_id, script))
            if "document.querySelector" in script:
                return {"success": True, "before": {"url": self.url, "title": self.title, "href": None, "rect": {"x": 1, "y": 1, "width": 10, "height": 10}}}
            if "url:window.location.href" in script:
                return {"url": self.url, "title": self.title}
            return {"found": True}

    cap = BrowserCapability(context=DesktopContext(target_tab_id="tab-1", target_tab_url="https://example.com", target_tab_title="Example"))
    cdp = DirectCDP()
    cap.cdp = cdp
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall("direct-1", "click_web_element", {"selector": "#submit"}))
    assert result.success is True


def test_search_reuses_existing_matching_results_without_repeating_search(monkeypatch):
    class SearchCDP:
        def __init__(self):
            self.calls = 0
            self.url = "https://www.youtube.com/results?search_query=praises+song"
        def list_tabs(self):
            return [{"id": "tab-1", "url": self.url, "title": "praises song - YouTube"}]
        def execute_in_site_search(self, tab_id, query):
            self.calls += 1
            return True
        def get_tab_info(self, tab_id):
            return {"id": tab_id, "url": self.url, "title": "praises song - YouTube"}
    context = DesktopContext(target_tab_id="tab-1", target_tab_url="https://www.youtube.com/results?search_query=praises+song", target_tab_title="praises song - YouTube")
    cap = BrowserCapability(context=context)
    cdp = SearchCDP()
    cap.cdp = cdp
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall("search-1", "search", {"query": "praises song"}))
    assert result.success is True
    assert result.metadata["method"] == "existing_results_reused"
    assert cdp.calls == 0


def test_browser_history_actions_use_owned_tab_and_verify_change(monkeypatch):
    class HistoryCDP:
        def __init__(self):
            self.url = "https://example.com/two"
            self.title = "Two"
            self.calls = []

        def list_tabs(self):
            return [{"id": "tab-1", "type": "page", "url": self.url, "title": self.title}]

        def get_tab_info(self, tab_id):
            return {"id": tab_id, "type": "page", "url": self.url, "title": self.title}

        def evaluate_script(self, tab_id, script, timeout=5.0):
            self.calls.append(script)
            if script == "history.back()":
                self.url, self.title = "https://example.com/one", "One"
            elif script == "history.forward()":
                self.url, self.title = "https://example.com/two", "Two"
            elif "readyState:document.readyState" in script:
                return {"value": {"readyState": "complete", "url": self.url, "title": self.title}}
            return {"value": True}

        def execute_cdp_command(self, tab_id, method, params=None, timeout=5.0):
            self.calls.append((method, params))
            assert method == "Page.reload"
            return {}

    cap = BrowserCapability(context=DesktopContext(target_tab_id="tab-1", target_tab_url="https://example.com/two", target_tab_title="Two"))
    cdp = HistoryCDP()
    cap.cdp = cdp
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    monkeypatch.setattr("advi.capabilities.browser.executor.time.sleep", lambda _seconds: None)

    back = cap.execute(ToolCall("back", "go_back", {}))
    assert back.success and back.verified
    assert back.output == "https://example.com/one"
    assert "history.back()" in cdp.calls

    forward = cap.execute(ToolCall("forward", "go_forward", {}))
    assert forward.success and forward.verified
    assert forward.output == "https://example.com/two"
    assert "history.forward()" in cdp.calls

    reload_result = cap.execute(ToolCall("reload", "reload_page", {}))
    assert reload_result.success and reload_result.verified
    assert any(isinstance(call, tuple) and call[0] == "Page.reload" for call in cdp.calls)


def test_youtube_human_workflow_navigate_search_click_and_verify_playback(monkeypatch):
    """Exercise the normal human path: YouTube -> search -> first result -> playback."""
    class HumanYouTubeCDP:
        def __init__(self):
            self.url = "about:blank"
            self.title = "New Tab"
            self.playing = False

        def list_tabs(self):
            return [{"id": "tab-1", "type": "page", "url": self.url, "title": self.title}]

        def get_tab_info(self, tab_id):
            return {"id": tab_id, "type": "page", "url": self.url, "title": self.title}

        def navigate_tab(self, tab_id, url, timeout=10.0):
            self.url = url
            self.title = "YouTube"
            return True

        def execute_in_site_search(self, tab_id, query):
            self.url = "https://www.youtube.com/results?search_query=" + query.replace(" ", "+")
            self.title = query + " - YouTube"
            return True

        def evaluate_js(self, tab_id, script, timeout=5.0):
            if "document.querySelector(requested)" in script and "resolvedTarget" in script:
                self.url = "https://www.youtube.com/watch?v=abc123"
                self.title = "Chosen video - YouTube"
                return {
                    "success": True,
                    "before": {
                        "url": "https://www.youtube.com/results?search_query=worship+music",
                        "title": "worship music - YouTube",
                        "href": "https://www.youtube.com/watch?v=abc123",
                        "rect": {"x": 10, "y": 10, "width": 100, "height": 40},
                        "tab_count": 1,
                    },
                    "resolvedTarget": "<a>video</a>",
                }
            if "url:window.location.href" in script:
                return {"url": self.url, "title": self.title}
            if "const requested =" in script:
                return {"found": False}
            if "const videos = queryDeep('video')" in script:
                return {"watch": True, "ready": True, "paused": False, "currentTime": 1.0, "playing": True, "playFound": True}
            if "const visible" in script and "video" in script:
                self.playing = True
                return True
            return True

        # BrowserCapability uses the alias in a few compatibility paths.
        def evaluate_script(self, tab_id, script, timeout=5.0):
            return self.evaluate_js(tab_id, script, timeout=timeout)

        def execute_cdp_command(self, *args, **kwargs):
            return True

    cap = BrowserCapability(context=DesktopContext(target_tab_id="tab-1", target_tab_url="about:blank"))
    cdp = HumanYouTubeCDP()
    cap.cdp = cdp
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    monkeypatch.setattr("advi.capabilities.browser.executor.time.sleep", lambda _seconds: None)

    opened = cap.execute(ToolCall("nav-1", "navigate", {"url": "https://www.youtube.com"}))
    searched = cap.execute(ToolCall("search-1", "search", {"query": "worship music"}))
    clicked = cap.execute(ToolCall("click-1", "click_web_element", {"selector": "ytd-video-renderer a#video-title"}))

    assert opened.success and opened.verified
    assert searched.success
    assert clicked.success and clicked.verified
    assert "youtube.com/watch?v=abc123" in str(clicked.metadata.get("current_url") or clicked.verification_details.get("current_url"))
