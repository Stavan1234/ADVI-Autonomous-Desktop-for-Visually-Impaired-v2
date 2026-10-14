"""Playwright browser layer: compact snapshots, verified navigation, browser launch routing."""
from __future__ import annotations

from types import SimpleNamespace

from advi.agent.session import AgentSession
from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.browser.pw_session import ActResult, Snapshot
from advi.core.verification import ToolVerifier
from advi.providers.base import LLMToolCall
from advi.tools.contracts import ToolCall, ToolResult, ToolSpec


def _snapshot():
    elements = [
        {"ref": "e1", "role": "button", "name": "Open Menu", "inView": True},
        {"ref": "e2", "role": "link", "name": "Play Daily Lives of High School Boys", "inView": True,
         "href": "/watch/KyWCLceOR8GGSkUnLCf3EuaE6yW4ILcp/danshi-koukousei-no-nichijou"},
        {"ref": "e3", "role": "combobox", "name": "Search Anime", "inView": True, "value": "daily"},
        {"ref": "e4", "role": "link", "name": "Footer", "inView": False, "href": "/about"},
        {"ref": "f1e1", "role": "button", "name": "Play", "inView": True},
    ]
    return Snapshot(3, "https://www.miruro.to/search", "Search", elements)


def test_snapshot_lines_are_compact_and_put_inputs_then_players_first():
    lines = _snapshot().lines()
    assert lines[0] == 'e3 combobox "Search Anime" value="daily"'
    assert lines[1] == 'f1e1 button "Play"'
    assert lines[-1] == 'e4 link "Footer" (offscreen) -> /about'
    assert any(line.startswith('e2 link "Play Daily Lives of High School Boys" -> /watch/') and line.endswith("…") for line in lines)


def test_navigation_to_a_bare_domain_that_redirects_to_www_is_verified(monkeypatch):
    # Log 2026-10-14: "miruro.to" -> "https://www.miruro.to/" was a definitive failure, twice.
    monkeypatch.setattr("advi.capabilities.browser.cdp.CDPBrowser.get_tab_info",
                        lambda self, tab_id: {"id": tab_id, "url": "https://www.miruro.to/"})
    call = ToolCall("n", "navigate", {"url": "miruro.to"})
    result = ToolResult(call_id="n", name="navigate", success=True, metadata={"expected_url": "miruro.to", "tab_id": "t1"})
    verified = ToolVerifier().verify(call, result)
    assert verified.verified is True


def test_navigation_to_another_site_is_uncertain_not_definitive(monkeypatch):
    monkeypatch.setattr("advi.capabilities.browser.cdp.CDPBrowser.get_tab_info",
                        lambda self, tab_id: {"id": tab_id, "url": "https://miruro.tv/"})
    verifier = ToolVerifier()
    ok, details = verifier._verify_navigate(
        ToolCall("n", "navigate", {"url": "miruro.to"}),
        ToolResult(call_id="n", name="navigate", success=True, metadata={"expected_url": "miruro.to", "tab_id": "t1"}),
    )
    assert ok is False and details["definitive_failure"] is False


def test_open_application_chrome_is_redirected_to_advis_browser():
    # Log 2026-10-14: open_application("Chrome") opened the personal profile picker.
    session = AgentSession.__new__(AgentSession)
    session.tools = SimpleNamespace(
        runtime_name=lambda name: name,
        get=lambda name: ToolSpec(name, name, "browser_control") if name == "open_browser" else None,
    )
    for app in ("Chrome", "Google Chrome", "browser"):
        call = session._normalize_native_call(LLMToolCall("c", "open_application", {"application": app}))
        assert call.name == "open_browser", app
    other = session._normalize_native_call(LLMToolCall("c", "open_application", {"application": "Notepad"}))
    assert other.name == "open_application"


def test_injected_cdp_clients_never_start_playwright(monkeypatch):
    cap = BrowserCapability()
    cap.cdp = SimpleNamespace()  # what the legacy tests inject
    monkeypatch.setattr(cap, "_pw", lambda: (_ for _ in ()).throw(AssertionError("Playwright must not start")))
    monkeypatch.setattr(cap, "_legacy_inspect_web_page", lambda action: ToolResult(call_id="i", name="inspect_web_page", success=True, output=[]))
    assert cap._execute_inspect_web_page(ToolCall("i", "inspect_web_page", {})).success


class FakeBrowser:
    def __init__(self, elements, plays=True):
        self.snap = Snapshot(1, "https://music.youtube.com/search?q=x", "YouTube Music", elements)
        self.clicked = []
        self.plays = plays
        self.page = SimpleNamespace(url=self.snap.url)

    def wait_dom_stable(self, **kwargs):
        return True

    def snapshot(self, **kwargs):
        return self.snap

    def act(self, ref, action, text=None):
        self.clicked.append(ref)
        return ActResult(True, f"{action} on {ref}.", {})

    def media_state(self):
        return {"playing": self.plays and bool(self.clicked), "time": 3}


def test_best_match_play_tolerates_live_and_artist_suffixes(monkeypatch):
    cap = BrowserCapability()
    browser = FakeBrowser([
        {"ref": "e1", "role": "button", "name": "Play Overflow (Live) - Todd Dulaney"},
        {"ref": "e2", "role": "button", "name": "Play Overflow (Live) - Transformation Worship"},
        {"ref": "e3", "role": "link", "name": "Transformation Worship", "href": "channel/x"},
    ])
    monkeypatch.setattr(cap, "_pw_ready", lambda: (browser, browser.page))
    result = cap._pw_play_best_match(ToolCall("p", "play_media", {"query": "Overflow Transformation Worship"}), "Overflow Transformation Worship")
    assert result.success and browser.clicked == ["e2"]
    assert result.verification_details["playing"] is True


def test_best_match_play_refuses_weak_matches_and_unverified_playback(monkeypatch):
    cap = BrowserCapability()
    weak = FakeBrowser([{"ref": "e1", "role": "button", "name": "Play Something Else"}])
    monkeypatch.setattr(cap, "_pw_ready", lambda: (weak, weak.page))
    assert cap._pw_play_best_match(ToolCall("p", "play_media", {}), "Overflow Transformation Worship") is None

    silent = FakeBrowser([{"ref": "e1", "role": "button", "name": "Play Overflow - Transformation Worship"}], plays=False)
    monkeypatch.setattr(cap, "_pw_ready", lambda: (silent, silent.page))
    result = cap._pw_play_best_match(ToolCall("p", "play_media", {}), "Overflow Transformation Worship")
    assert result is not None and not result.success and "did not start" in result.human_readable


def _gated_browser(monkeypatch, label):
    cap = BrowserCapability()
    browser = FakeBrowser([{"ref": "e1", "role": "button", "name": label}])
    browser.latest = browser.snap
    browser.page = SimpleNamespace(url="https://shop.example/cart?x=1")
    monkeypatch.setattr(cap, "_pw_enabled", lambda: True)
    monkeypatch.setattr(cap, "_pw_ready", lambda: (browser, browser.page))
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    return cap, browser


def test_consequential_web_clicks_need_confirmation(monkeypatch):
    cap, browser = _gated_browser(monkeypatch, "Place order")
    blocked = cap.execute(ToolCall("c", "click_web_element", {"selector": "e1"}))
    assert blocked.requires_confirmation and browser.clicked == []
    assert "Place order" in blocked.confirmation_reason

    approved = cap.execute(ToolCall("c", "click_web_element", {"selector": "e1"}), approval_granted=True)
    assert browser.clicked == ["e1"] and not approved.requires_confirmation


def test_ordinary_web_clicks_are_not_gated(monkeypatch):
    cap, browser = _gated_browser(monkeypatch, "Play Daily Lives of High School Boys")
    result = cap.execute(ToolCall("c", "click_web_element", {"selector": "e1"}))
    assert not result.requires_confirmation and browser.clicked == ["e1"]


def test_a_bot_check_page_is_reported_not_verified():
    from advi.capabilities.browser import executor as ex

    for title in ("Reddit - Prove your humanity", "Just a moment...", "Google: unusual traffic"):
        assert ex._HUMAN_CHECK_TITLE.search(title), title
    for title in ("Amazon.in : wireless earbuds", "Inbox (189) - Gmail", "python tips : r/learnpython"):
        assert not ex._HUMAN_CHECK_TITLE.search(title), title
