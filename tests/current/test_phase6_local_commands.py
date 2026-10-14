"""Common commands without AI quota, from the 2026-10-14 night log (daily quota exhausted)."""
from __future__ import annotations

from pathlib import Path

from advi.agent.contracts import TaskStatus
from advi.agent.local_intents import match_local_intent
from advi.fallback.intent_service import IntentService
from advi.tools.contracts import ToolResult

from test_phase0_integrity import _RecordingEngine, _ScriptedProvider, _session


# --- parsing ---

def test_log_phrases_parse_to_the_right_tool():
    cases = {
        "send an email to stavan using david fallback saying good evening": ("david_fallback", {"to": "stavan", "body": "good evening"}),
        "search wireless earbuds on amazon": ("navigate", None),
        "play overflow on youtube": ("play_media", None),
        "organize my downloads by type": ("organize_folder", None),
        "undo that": ("undo_file_operation", None),
        "open notepad": ("open_application", None),
    }
    for phrase, (tool, args) in cases.items():
        intent = match_local_intent(phrase)
        assert intent is not None, phrase
        assert intent.tool == tool, (phrase, intent.tool)
        if args:
            assert {k: intent.arguments.get(k) for k in args} == args


def test_ambiguous_or_multi_step_requests_go_to_the_model():
    for phrase in (
        "send a message to Joel saying hi",  # which app?
        "play something nice",
        "open word and write a speech about courage",
        "what's the weather like",
    ):
        assert match_local_intent(phrase) is None, phrase


def test_david_email_request_is_not_mistaken_for_a_web_search():
    # Log: "Open Gmail, search for email ..., compose and send" became a Google search.
    intent = IntentService._deterministic_email_intent(
        'Open Gmail, search for email stavankalkumbe@gmail.com, compose and send with subject "(none)" saying "good evening"'
    )
    assert intent is not None and intent.goal == "send_email"
    assert intent.entities == {"recipient_email": "stavankalkumbe@gmail.com", "subject": "", "content": "good evening"}


# --- session handling: nothing is ever sent without a yes ---

class _ContactEngine(_RecordingEngine):
    def __init__(self, contacts, outputs=None):
        super().__init__()
        self.contacts = contacts
        self.outputs = outputs or {}

    def execute_tool_call(self, call, *, approval_granted=False):
        self.calls.append(call)
        if call.name == "contact_lookup":
            return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True, output=self.contacts)
        if call.name in self.outputs:
            return self.outputs[call.name](call)
        return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True, human_readable=f"{call.name} done")


def _email_session(engine):
    provider = _ScriptedProvider([])
    session, _ = _session(provider, {
        "email": {"email_draft_create", "email_send", "contact_lookup"},
        "fallback": {"david_fallback"},
        "messaging": {"whatsapp_send_message", "telegram_send_message"},
        "browser_control": {"navigate", "open_browser", "play_media"},
    })
    session.tool_runner.execution_engine = engine
    session.local_commands_mode = "first"  # these tests exercise the router itself
    return session, provider


def test_email_by_name_drafts_and_waits_for_yes():
    draft = lambda call: ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True, output={"draft_id": "d1"})
    engine = _ContactEngine([{"name": "Stavan", "email": "stavan@example.com"}], {"email_draft_create": draft})
    session, provider = _email_session(engine)

    response = session.respond("send an email to stavan saying good evening")

    assert [c.name for c in engine.calls] == ["contact_lookup", "email_draft_create"]
    assert "stavan@example.com" in response.text and "yes" in response.text.casefold()
    assert session.pending_confirmation.name == "email_send"
    assert session.task.status == TaskStatus.AWAITING_CONFIRMATION
    assert provider.offered == []


def test_david_email_stops_at_the_send_confirmation():
    prepared = lambda call: ToolResult(
        call_id=call.call_id, name=call.name, success=True, verified=True,
        human_readable="Draft created and ready to send. Should I send it, sir?",
        metadata={"awaiting_send_confirmation": True},
    )
    engine = _ContactEngine([{"name": "Stavan", "email": "stavan@example.com"}], {"david_fallback": prepared})
    session, provider = _email_session(engine)

    response = session.respond("send an email to stavan using david fallback saying good evening")

    assert engine.calls[-1].name == "david_fallback"
    assert 'send email to stavan@example.com saying "good evening"' == engine.calls[-1].arguments["request"]
    assert session._pending_david_email_send is not None
    assert "send it" in response.text
    assert provider.offered == []


def test_unknown_or_ambiguous_recipient_asks_instead_of_guessing():
    session, _ = _email_session(_ContactEngine([]))
    assert "don't have an email address" in session.respond("send an email to zed saying hi").text

    two = [{"name": "Sam A", "email": "a@example.com"}, {"name": "Sam B", "email": "b@example.com"}]
    session, _ = _email_session(_ContactEngine(two))
    assert "more than one" in session.respond("send an email to sam saying hi").text


def test_whatsapp_send_asks_first_and_runs_nothing():
    engine = _ContactEngine([])
    session, _ = _email_session(engine)
    response = session.respond("send hello to New Chapter on whatsapp")
    assert engine.calls == []
    assert session.pending_confirmation.name == "whatsapp_send_message"
    assert "confirmation" in response.text


def test_site_search_runs_without_the_model():
    engine = _ContactEngine([])
    session, provider = _email_session(engine)
    response = session.respond("search wireless earbuds on amazon")
    assert [c.name for c in engine.calls] == ["navigate"]
    assert "amazon" in engine.calls[0].arguments["url"]
    assert "wireless earbuds" in response.text
    assert provider.offered == []


# --- quota and provider honesty ---

def test_gemini_native_interactions_are_off_by_default(monkeypatch):
    import advi.providers.gemini as gemini

    monkeypatch.delenv("ADVI_GEMINI_INTERACTIONS", raising=False)
    source = Path(gemini.__file__).read_text(encoding="utf-8")
    assert 'ADVI_GEMINI_INTERACTIONS' in source


def test_media_folder_aliases_are_the_real_known_folders():
    from advi.capabilities.files.executor import FileCapability

    cap = FileCapability()
    for alias in ("videos", "music", "pictures"):
        resolved = cap.resolve_path(alias)
        assert "Desktop" not in resolved.parts, (alias, resolved)


# --- Telegram (Qt: no UI Automation, so keys + screen checks) ---

from advi.capabilities.apps.telegram import Box, TelegramSkill


class _FakeTelegram:
    """A tiny Telegram: search list, header, composer and message area as text."""

    def __init__(self, chats, *, signed_in=True, drop_typing=False, send_works=True):
        self.chats, self.signed_in, self.drop_typing, self.send_works = chats, signed_in, drop_typing, send_works
        self.search = self.open = self.draft = ""
        self.focus_on = "list"
        self.sent: list[tuple[str, str]] = []
        self.win = Box(0, 0, 900, 600)

    def window(self, launch):
        return self.win

    def focus(self):
        return True

    def keys(self, keys):
        if keys == "{ESC}{ESC}":
            self.open, self.search, self.focus_on = "", "", "list"
        elif keys == "{ENTER}" and self.focus_on == "list":
            match = [c for c in self.chats if self.search.casefold() in c.casefold()]
            if match:
                self.open, self.focus_on = match[0], "composer"
        elif keys == "{ENTER}" and self.focus_on == "composer" and self.send_works:
            self.sent.append((self.open, self.draft))
            self.draft = ""
        elif keys == "^a{BACKSPACE}":
            self.draft = ""

    def type(self, text):
        if self.focus_on == "list":
            self.search += text
        elif not self.drop_typing:
            self.draft += text

    def read(self, box):
        if not self.signed_in:
            return "Telegram Desktop\nWelcome to the official Telegram Desktop app."
        regions = TelegramSkill._regions(self.win)
        if box == regions["list"]:
            return "\n".join(c for c in self.chats if self.search and self.search.casefold() in c.casefold())
        if box == regions["header"]:
            return self.open
        if box == regions["composer"]:
            return self.draft or "Write a message..."
        if box == regions["messages"]:
            return "\n".join(t for c, t in self.sent if c == self.open)
        return "Chats"


def _tg(fake):
    return TelegramSkill(fake, sleep=lambda s: None, clock=iter(range(0, 10_000)).__next__)


def test_telegram_sends_only_after_every_check_passes():
    fake = _FakeTelegram(["New Chapter", "Mom"])
    outcome = _tg(fake).send_message("new chapter", "hello all")
    assert outcome.ok and outcome.stage == "sent"
    assert fake.sent == [("New Chapter", "hello all")]


def test_telegram_signed_out_says_so_and_types_nothing():
    fake = _FakeTelegram(["Mom"], signed_in=False)
    outcome = _tg(fake).send_message("Mom", "hi")
    assert not outcome.ok and outcome.stage == "logged_in" and "log in" in outcome.message
    assert fake.search == "" and fake.sent == []


def test_telegram_unknown_chat_is_never_messaged():
    fake = _FakeTelegram(["Mom"])
    outcome = _tg(fake).send_message("Joel", "hi")
    assert not outcome.ok and outcome.stage == "search" and fake.sent == []


def test_telegram_unconfirmed_draft_is_cleared_not_sent():
    fake = _FakeTelegram(["Mom"], drop_typing=True)
    outcome = _tg(fake).send_message("Mom", "hi there")
    assert not outcome.ok and outcome.stage == "composed" and fake.sent == [] and fake.draft == ""


def test_telegram_send_needs_confirmation_in_the_session():
    engine = _ContactEngine([])
    session, _ = _email_session(engine)
    response = session.respond("send hello to New Chapter on telegram")
    assert engine.calls == []
    assert session.pending_confirmation.name == "telegram_send_message"
    assert "Telegram message 'hello' to New Chapter" in response.text


def test_natural_yes_sends_the_prepared_david_draft():
    # "yes send it" was read as a new task, so the David draft was silently left unsent.
    def david(call):
        if call.arguments.get("stage") == "send_prepared":
            return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True,
                              human_readable="The email was sent successfully through the Gmail interface.")
        return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True,
                          human_readable="Draft created and ready to send. Should I send it, sir?",
                          metadata={"awaiting_send_confirmation": True, "recipient": "stavan@example.com",
                                    "subject": "", "body": "good evening"})

    for reply in ("yes send it", "send it", "sure", "Yes, please send the email now."):
        engine = _ContactEngine([{"name": "Stavan", "email": "stavan@example.com"}], {"david_fallback": david})
        session, provider = _email_session(engine)
        session.respond("send an email to stavan using david fallback saying good evening")
        response = session.respond(reply)
        assert engine.calls[-1].arguments.get("stage") == "send_prepared", reply
        assert engine.calls[-1].arguments["protected_parameters"]["recipient"] == "stavan@example.com"
        assert "sent" in response.text, reply
        assert provider.offered == []


def test_a_no_or_a_change_never_sends():
    from advi.agent.session import _CONFIRM_RE

    for reply in ("no", "don't send it", "send it to Rahul instead", "yes but change the subject", "go"):
        assert not _CONFIRM_RE.fullmatch(reply), reply


def test_more_everyday_phrasings_run_locally():
    cases = {
        "message Rahul on whatsapp saying hello bro": ("whatsapp_send_message", {"chat": "Rahul", "text": "hello bro"}),
        "send an email to a@b.com subject hello saying how are you": ("email_draft_create", {"to": "a@b.com", "subject": "hello", "body": "how are you"}),
        "open documents": ("open_folder", {"path": "Documents"}),
        "close notepad": ("close_application", {"application": "Notepad"}),
        "open youtube and play believer": ("play_media", {"query": "believer", "service": "youtube"}),
        "search for cheap flights to goa": ("navigate", None),
        "volume up": ("media_control", {"action": "volume_up"}),
        "mute": ("media_control", {"action": "mute"}),
        "create a folder called projects on desktop": ("create_folder", {"path": "Desktop/projects"}),
        "open notepad and type hello from advi": ("open_application", {"application": "Notepad"}),
    }
    for phrase, (tool, args) in cases.items():
        intent = match_local_intent(phrase)
        assert intent is not None and intent.tool == tool, (phrase, intent)
        if args:
            assert {k: intent.arguments.get(k) for k in args} == args, phrase
    for phrase in ("open notepad and write a poem about rain", "play my playlist", "search for my resume file", "open music"):
        assert match_local_intent(phrase) is None, phrase


def test_image_and_video_words_select_their_extensions():
    from advi.capabilities.files import bulk

    assert {"jpg", "png"} <= bulk._normalize_extensions(["images"])
    assert {"mp4", "mkv"} <= bulk._normalize_extensions("videos")
    assert bulk._normalize_extensions(["pdf"]) == {"pdf"}


def test_bare_play_prefers_a_matching_local_file(tmp_path, monkeypatch):
    import advi.agent.session as session_module
    from advi.capabilities.files.executor import FileCapability

    videos = tmp_path / "Videos"
    videos.mkdir()
    (videos / "HERE I AM TO WORSHIP - Hillsong.mp4").write_text("x")
    monkeypatch.setattr(FileCapability, "resolve_path", lambda self, name: tmp_path / name.title())
    assert session_module._local_media_match("here i am to worship") is not None
    assert session_module._local_media_match("believer") is None


def test_confirmation_prompt_reads_as_a_sentence():
    engine = _ContactEngine([])
    session, _ = _email_session(engine)
    text = session.respond("send hello to New Chapter on telegram").text
    assert "New Chapter. Say yes" in text



# --- AI first; built-in commands only when the AI is unavailable (default) ---

def _default_session(script, engine=None):
    from advi.providers.errors import LLMRateLimitError  # noqa: F401  (documented failure type)

    provider = _ScriptedProvider(script)
    session, recording = _session(provider, {"browser_control": {"navigate", "play_media"}, "desktop_control": {"open_application"}})
    if engine is not None:
        session.tool_runner.execution_engine = engine
    return session, provider, engine or recording


def test_when_the_ai_works_it_handles_the_request():
    from test_phase0_integrity import _text

    session, provider, engine = _default_session([_text("Sure, opening Amazon search.")])
    session.respond("search wireless earbuds on amazon")
    assert len(provider.offered) == 1  # the AI was asked
    assert engine.calls == []          # the built-in router did not act on its own


def test_when_every_ai_provider_fails_a_clear_command_still_works():
    from advi.providers.errors import LLMRateLimitError

    session, provider, engine = _default_session([LLMRateLimitError("tokens per day (TPD) limit exceeded")])
    response = session.respond("search wireless earbuds on amazon")
    assert [c.name for c in engine.calls] == ["navigate"]
    assert "built-in command" in response.text and "wireless earbuds" in response.text


def test_unclear_requests_are_never_guessed_when_the_ai_fails():
    from advi.providers.errors import LLMRateLimitError

    session, provider, engine = _default_session([LLMRateLimitError("tokens per day (TPD) limit exceeded")])
    response = session.respond("send a message to Joel saying hi")  # which app? never guessed
    assert engine.calls == [] and "quota" in response.text


def test_router_can_be_turned_off(monkeypatch):
    from advi.providers.errors import LLMRateLimitError

    monkeypatch.setenv("ADVI_LOCAL_COMMANDS", "off")
    session, provider, engine = _default_session([LLMRateLimitError("tokens per day (TPD) limit exceeded")])
    session.respond("search wireless earbuds on amazon")
    assert engine.calls == []
