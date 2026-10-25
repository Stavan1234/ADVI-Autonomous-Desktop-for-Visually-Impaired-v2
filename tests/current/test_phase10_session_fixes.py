"""Phase 10: fixes from the 2026-10-23 evening session log."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from advi.agent.contracts import TaskState, TaskStatus
from advi.agent.session import AgentSession
from advi.capabilities.desktop.screen_describer import ScreenDescriber
from advi.providers.errors import LLMRequestError
from advi.providers.groq import _repair_generated_call, _with_failed_call
from advi.tools.contracts import ToolCall, ToolResult
from tests.current.test_browser_workflows import WorkflowCDP
from advi.capabilities.apps.telegram import TelegramSkill
from tests.current.test_phase6_local_commands import _FakeTelegram, _tg


def _bare() -> AgentSession:
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.last_user_message = ""
    return session


# 1. Reading search results ------------------------------------------------


class _ReadCDP(WorkflowCDP):
    def __init__(self, text):
        super().__init__()
        self.text = text

    def evaluate_script(self, tab_id, script):
        assert "innerText" in script and "main" in script
        return self.text


def _browser(text, monkeypatch=None):
    from advi.capabilities.browser.executor import BrowserCapability
    from advi.capabilities.desktop.context import DesktopContext

    cap = BrowserCapability(context=DesktopContext())
    cap.context.set_target_tab("tab-1", url="https://example.com", title="Example")
    cap.cdp = _ReadCDP(text)
    cap._ensure_chrome_running = lambda: True
    return cap


def test_read_web_page_pages_through_long_results():
    page = "".join(f"Speaker {i} ₹{19000 + i * 100}\n" for i in range(400))
    cap = _browser(page)
    first = cap.execute(ToolCall("c", "read_web_page", {}))
    assert first.success and first.output.startswith("Speaker 0 ₹19000")
    assert "read_web_page start=3000 continues" in first.output
    second = cap.execute(ToolCall("c", "read_web_page", {"start": 3000}))
    assert second.output.startswith(page[3000:3020])


def test_short_page_has_no_continuation_note():
    assert "continues" not in _browser("Only a little text").execute(ToolCall("c", "read_web_page", {})).output


def test_read_web_page_is_offered_with_every_browsing_request():
    source = open("src/advi/agent/session.py", encoding="utf-8").read()
    assert 'add("navigate", "search", "inspect_web_page", "read_web_page",' in source
    assert "Never repeat the same search" in source


# 2. Remembering the user -------------------------------------------------


class _Memory:
    def __init__(self):
        self.facts = {}
        self.memory = self

    def remember(self, *, kind, scope, subject, key, value, statement, confidence=0.8, importance=5):
        self.facts[(kind, key)] = value

    def recall(self, category, key):
        value = self.facts.get((category, key))
        return SimpleNamespace(value=value) if value else None


def _profile_session():
    session = _bare()
    session.memory = _Memory()
    return session


def test_name_and_about_are_saved_from_the_logged_sentence():
    session = _profile_session()
    session._persist_user_profile_facts(
        "ohhh...My name is Stavan Kalkumbe, I am an engineering student at fcrit vashi final year. "
        "also I am one of the team member who has built you advi."
    )
    assert session.memory.facts[("user_profile", "name")] == "Stavan Kalkumbe"
    about = session.memory.facts[("user_profile", "about")]
    assert "engineering student at fcrit vashi" in about and "team member who has built you advi" in about
    assert session._user_profile_line().startswith("[USER] Stavan Kalkumbe; I am an engineering student")


def test_ordinary_sentences_are_not_profile_facts():
    session = _profile_session()
    session._persist_user_profile_facts("I am a bit confused, play a song")
    session._persist_user_profile_facts("can you search a good speaker")
    assert session.memory.facts == {}
    assert session._user_profile_line() == ""


def test_remember_claim_needs_a_memory_write_this_turn():
    session = _profile_session()
    session._turn_started_at = time.monotonic()
    claim = "Got it, Stavan. I'll remember that you're a final-year student."
    assert "haven't saved that" in session._no_unbacked_memory_claim(claim)
    session._last_memory_write_at = time.monotonic()
    assert session._no_unbacked_memory_claim(claim) == claim
    assert session._no_unbacked_memory_claim("Playing your song.") == "Playing your song."


def test_saying_your_name_backs_the_claim():
    session = _profile_session()
    session._turn_started_at = time.monotonic()
    session._persist_user_profile_facts("my name is Stavan")
    assert session._no_unbacked_memory_claim("I'll remember that, Stavan.") == "I'll remember that, Stavan."


# 3. Telegram chat-open check ---------------------------------------------


class _SelfChatTelegram(_FakeTelegram):
    """Searching your own name opens "Saved Messages"."""

    def keys(self, keys):
        super().keys(keys)
        if keys == "{ENTER}" and self.open == "stavan":
            self.open = "Saved Messages"


def test_own_name_opens_saved_messages_and_says_so():
    fake = _SelfChatTelegram(["stavan", "Mom"])
    outcome = _tg(fake).open_chat("stavan")
    assert outcome.ok and outcome.evidence["chat_title"] == "Saved Messages"
    assert "opened Saved Messages" in outcome.message


def test_a_message_for_stavan_is_not_sent_to_saved_messages():
    fake = _SelfChatTelegram(["stavan"])
    outcome = _tg(fake).send_message("stavan", "hello from advi")
    assert not outcome.ok and "Saved Messages" in outcome.message and fake.sent == []


def test_named_chat_still_opens_and_sends():
    fake = _FakeTelegram(["New Chapter"])
    assert _tg(fake).send_message("new chapter", "hi").ok
    assert fake.sent == [("New Chapter", "hi")]


# 4. Media resume waits for playback --------------------------------------


def test_resume_waits_for_playback_to_start(monkeypatch):
    from advi.capabilities.browser import executor, pw_session

    states = iter([False, False, False, False, True])

    class _Frame:
        def evaluate(self, script):
            return 1

    class _Page:
        frames = [_Frame()]

        def is_closed(self):
            return False

    class _Browser:
        page = _Page()

        def media_state(self):
            return {"playing": next(states, True)}

    monkeypatch.setattr(pw_session, "_shared", _Browser())
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    cap = _browser("")
    monkeypatch.setattr(cap, "_pw_enabled", lambda: True)
    result = cap.execute(ToolCall("c", "media_control", {"action": "resume"}))
    assert result.success and result.human_readable == "Resumed."


# 5. Garbled tool-call JSON -------------------------------------------------

_LOGGED = '{"name": "play_media", "arguments": {"query":"it\\\'s been a long long time","service":"youtube_music"}"}'


def test_garbled_call_from_the_log_is_repaired():
    assert _repair_generated_call(_LOGGED) == {
        "name": "play_media",
        "arguments": {"query": "it's been a long long time", "service": "youtube_music"},
    }


def test_repaired_call_is_attached_for_adoption():
    exc = SimpleNamespace(body={"error": {"message": "Failed to parse tool call arguments as JSON", "failed_generation": _LOGGED}})
    error = _with_failed_call(LLMRequestError("Groq rejected tool-calling request (400)"), exc)
    assert error.failed_call["name"] == "play_media"


def test_unrepairable_text_is_left_alone():
    assert _repair_generated_call("not json at all") is None


def test_garbled_answer_is_not_called_a_bug():
    session = _bare()
    session.provider_manager = SimpleNamespace(last_attempts=())
    session._describe_provider_failure(LLMRequestError("Groq rejected tool-calling request (400): Failed to parse tool call arguments as JSON"))
    assert "garbled" in session._last_failure["reason"]


# 6. Read-only answers stand ----------------------------------------------


def test_an_unverified_file_listing_keeps_the_models_answer():
    session = _bare()
    session.task = TaskState(goal="videos", task_id="t")
    session.task.status = TaskStatus.UNCERTAIN
    session.task.tool_outcomes.append({"tool": "list_files", "success": True, "verified": False})
    session.last_tool_summary = "Found 20 files and 0 folders in Videos matching .mp4 files."
    assert session._authoritative_task_response() is None


def test_an_unverified_side_effect_is_still_reported_honestly():
    session = _bare()
    session.task = TaskState(goal="click", task_id="t")
    session.task.status = TaskStatus.UNCERTAIN
    session.task.tool_outcomes.append({"tool": "click", "success": True, "verified": False})
    session.last_tool_summary = "Clicked."
    assert "couldn't verify" in session._authoritative_task_response()


# 7. Vision protects the quota ----------------------------------------------


class _Client:
    def __init__(self, errors):
        self.errors = errors
        self.calls = 0
        self.models = self

    def generate_content(self, *, model, contents, config=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(text="A page.")


def _image():
    from PIL import Image

    return Image.new("RGB", (64, 64), "white")


def test_quota_error_turns_the_key_off_until_its_retry_time():
    now = [0.0]
    client = _Client([RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '600s'}")])
    describer = ScreenDescriber(api_keys=["k"], model="m", client_factory=lambda key: client, clock=lambda: now[0])
    with pytest.raises(RuntimeError, match="quota"):
        describer.describe(_image(), "a")
    with pytest.raises(RuntimeError, match="quota"):
        describer.describe(_image(), "b")
    assert client.calls == 1  # no network call while the key is off
    now[0] = 601.0
    assert describer.describe(_image(), "c")[0] == "A page."


def test_busy_service_pauses_vision_for_two_minutes():
    now = [0.0]
    client = _Client([RuntimeError("503 UNAVAILABLE")])
    describer = ScreenDescriber(api_keys=["k"], model="m", client_factory=lambda key: client, clock=lambda: now[0])
    with pytest.raises(RuntimeError, match="busy"):
        describer.describe(_image(), "a")
    with pytest.raises(RuntimeError, match="busy"):
        describer.describe(_image(), "b")
    assert client.calls == 1
    now[0] = 121.0
    assert describer.describe(_image(), "c")[0] == "A page."


def test_vision_uses_its_own_lite_model(monkeypatch):
    from advi.capabilities.desktop import screen_describer

    monkeypatch.setenv("ADVI_VISION", "on")
    monkeypatch.delenv("ADVI_VISION_MODEL", raising=False)
    monkeypatch.setattr("advi.core.config.load_settings", lambda: SimpleNamespace(gemini_api_keys=("k",), gemini_model="gemini-3.8-flash"))
    assert screen_describer.describer_from_settings().model == "gemini-3.1-flash-lite"


def test_no_automatic_look_after_media_or_risky_keys():
    session = _bare()
    session.tools = SimpleNamespace(get=lambda name: SimpleNamespace(side_effect="high"))
    blind = ToolResult(call_id="o", name="observe_window", success=True, output={"elements": []})
    done = ToolResult(call_id="a", name="x", success=True, verified=False)
    assert session._vision_trigger(ToolCall("a", "media_control", {"action": "volume_up"}), done, blind, None) is None
    assert session._vision_trigger(ToolCall("a", "press_key", {"key": "space"}), done, blind, None) is None
    rich = ToolResult(call_id="o", name="observe_window", success=True, output={"elements": [{}] * 10})
    # High (not critical) side effects no longer trigger a final-state look.
    assert session._vision_trigger(ToolCall("a", "click", {}), done, rich, None) is None


# 8. Console noise ------------------------------------------------------------


def test_embeddings_never_print_progress_bars():
    from advi.memory.retriever import MemoryRetriever

    seen = []

    class _Model:
        def encode(self, texts, **kwargs):
            seen.append(kwargs)
            return [[0.0]]

    retriever = MemoryRetriever.__new__(MemoryRetriever)
    retriever._model, retriever._model_error = _Model(), None
    retriever._encode_documents(["x"])
    assert seen and seen[0].get("show_progress_bar") is False


def test_amazon_repeats_and_price_fragments_are_dropped():
    from advi.capabilities.browser.executor import BrowserCapability

    raw = ("Schneider HP60 Speaker | 60W - Black\nSchneider HP60 Speaker |…\n4.5 out of 5 stars.\n"
           "₹15,990.00\n₹15,990\n.\n00\nM.R.P: ₹35,990.00\nSchneider HP60 Speaker | 60W - Black\n")
    assert BrowserCapability._distill(raw) == (
        "Schneider HP60 Speaker | 60W - Black\n4.5 out of 5 stars.\n₹15,990.00\nM.R.P: ₹35,990.00")


class _ClippedHeaderTelegram(_FakeTelegram):
    """At an 800-px window OCR read the header 'Stavan Jio' as 'n Jio' (live, 2026-10-23)."""

    def read(self, box):
        text = super().read(box)
        if box == TelegramSkill._regions(self.win)["header"] and text:
            return text[5:]
        return text


def test_clipped_header_is_matched_against_the_top_result():
    fake = _ClippedHeaderTelegram(["Stavan Jio", "Mom"])
    outcome = _tg(fake).send_message("stavan", "hello from advi")
    assert outcome.ok and outcome.evidence["chat_title"] == "Stavan Jio"
    assert fake.sent == [("Stavan Jio", "hello from advi")]
