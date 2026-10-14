"""play_media: one verified call instead of 15-27 hand-driven browser steps per song."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from advi.agent import AgentSession
from advi.capabilities.browser.executor import BrowserCapability
from advi.core.execution_strategy import infer_execution_strategy, terminal_evidence_matches
from advi.tools.contracts import ToolCall, ToolResult, ToolSpec


def _browser(monkeypatch, *, tab="t1", navigate_ok=True):
    cap = BrowserCapability.__new__(BrowserCapability)
    cap.context = SimpleNamespace(target_tab_url=None)
    navigated = []
    cap.cdp = SimpleNamespace(
        navigate_tab=lambda tab_id, url, timeout=0: navigated.append(url) or navigate_ok,
        wait_for_dom_ready=lambda tab_id, timeout=0: True,
    )
    monkeypatch.setattr(cap, "_owned_tab_id", lambda: tab)
    playing = lambda action, *args: ToolResult(
        call_id=action.call_id, name=action.action, success=True, verified=True, verification_status="verified",
        human_readable="Playing.", verification_details={"playing": True, "playback": {"playing": True}},
    )
    monkeypatch.setattr(cap, "_youtube_play_first_result", playing)
    monkeypatch.setattr(cap, "_youtube_music_play_requested_result", playing)
    return cap, navigated


def test_youtube_play_builds_the_results_url_and_verifies(monkeypatch):
    cap, navigated = _browser(monkeypatch)
    result = cap._execute_play_media(ToolCall("c1", "play_media", {"query": "Never Say Never"}))
    assert result.success and result.verified
    assert navigated == ["https://www.youtube.com/results?search_query=Never+Say+Never"]


@pytest.mark.parametrize("service", ["youtube_music", "YouTube Music", "ytmusic"])
def test_youtube_music_uses_the_music_search(monkeypatch, service):
    cap, navigated = _browser(monkeypatch)
    result = cap._execute_play_media(ToolCall("c1", "play_media", {"query": "Overflow", "service": service}))
    assert result.success
    assert navigated == ["https://music.youtube.com/search?q=Overflow"]


def test_unsupported_service_and_missing_results_fail_clearly(monkeypatch):
    cap, navigated = _browser(monkeypatch)
    spotify = cap._execute_play_media(ToolCall("c1", "play_media", {"query": "x", "service": "spotify"}))
    assert not spotify.success and "YouTube" in spotify.error and navigated == []

    monkeypatch.setattr(cap, "_youtube_play_first_result", lambda *a: None)
    nothing = cap._execute_play_media(ToolCall("c2", "play_media", {"query": "zzzz"}))
    assert not nothing.success and "found nothing" in nothing.error


def test_verified_play_media_completes_a_media_task():
    goal = "play praise song on youtube"
    result = ToolResult(call_id="c1", name="play_media", success=True, verified=True,
                        verification_status="verified", verification_details={"playing": True})
    assert terminal_evidence_matches(infer_execution_strategy(goal), "play_media", result, goal)


def _routed(text):
    names = ["navigate", "search", "inspect_web_page", "click_web_element", "play_media", "play_local_media",
             "find_file", "list_files", "open_file", "whatsapp_send_message"]
    caps = {"find_file": "file_management", "list_files": "file_management", "open_file": "file_management",
            "whatsapp_send_message": "messaging"}
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.tools = SimpleNamespace(model_tools=lambda: [ToolSpec(n, n, caps.get(n, "browser_control")) for n in names])
    return {t["function"]["name"] for t in session._select_model_tools(text)}


def test_play_requests_offer_online_and_local_players():
    assert _routed("play praise song on youtube") == {"play_media", "play_local_media"}
    assert _routed("play Overflow by Transformation Worship on YouTube Music") == {"play_media", "play_local_media"}
    # Log 2026-10-14: the user meant the video in their Videos folder.
    local = _routed("play here i am to worship video from my videos folder")
    assert "play_local_media" in local and "play_media" not in local


def test_local_files_other_sites_and_whatsapp_are_not_rerouted():
    assert "play_media" not in _routed("play any audio from my music folder")
    assert "play_local_media" in _routed("play any audio from my music folder")
    assert "play_media" not in _routed("open chrome, go to Miruro.to and play the first episode")
    assert "play_media" not in _routed("tell Joel on whatsapp let's play tonight")
