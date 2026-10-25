"""Phase 12: misheard names, spelling just the name, corrections at a confirmation, offline commands.

Live session 2026-10-24: "Siyu" was heard as "CU"; "It is S I Y U, not C U"
got "I need an explicit yes or no"; offline, "send message to Anurad saying
hello" and "what time is it" were refused.
"""
from __future__ import annotations

import pytest

from advi.agent import AgentSession
from advi.agent.local_intents import match_local_intent
from advi.agent.names import corrected_name, corrected_text, phonetic_key, resolve_chat, sounds_like, spelled_names
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.memory.long_term import LongTermMemory
from advi.memory.service import MemoryService
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.providers.errors import LLMRateLimitError
from advi.tools import ToolGuard, ToolRegistry, ToolRunner
from advi.tools.contracts import ToolResult


# --- names -------------------------------------------------------------------------

def test_letters_written_by_whisper_sound_like_the_name():
    assert phonetic_key("CU") == phonetic_key("Siyu") == phonetic_key("See You")
    assert sounds_like("CU", "Siyu") == 1.0
    assert sounds_like("Anurad", "Anurag") > 0.8
    assert sounds_like("CU", "Rahul") == 0.0


def test_spelled_names_keep_sentence_breaks_and_put_negated_runs_last():
    assert spelled_names("It is C U. S I Y U. Not the C U.") == ["Cu", "Siyu", "Cu"]
    assert spelled_names("S. I. Y. U.") == ["Siyu"]
    assert spelled_names("s, i, y, u") == ["Siyu"]
    assert spelled_names("S-I-Y-U") == ["Siyu"]
    assert spelled_names("I said A. Then B.") == []


@pytest.mark.parametrize("reply, expected", [
    ("It is C U. S I Y U. Not the C U. C and U.", "Siyu"),
    ("send it to Siyu instead", "Siyu"),
    ("the name is Siyu", "Siyu"),
    ("no, not CU, it's Siyu", "Siyu"),
    ("yes", None),
    ("what will it say", None),
])
def test_corrected_name(reply, expected):
    assert corrected_name(reply, "CU") == expected


def test_corrected_text():
    assert corrected_text("change the message to see you at 5") == "see you at 5"
    assert corrected_text("say good night instead") == "good night"
    assert corrected_text("yes send it") is None


def test_resolve_chat_prefers_exact_then_learned_alias_then_sound():
    known = [("Siyu", ""), ("Siyu", "cu"), ("Rahul", "")]
    assert resolve_chat("rahul", known) == ("Rahul", "exact")
    assert resolve_chat("CU", known) == ("Siyu", "alias")
    assert resolve_chat("See you", [("Siyu", "")]) == ("Siyu", "sounds_like")
    assert resolve_chat("Priya", known) == ("Priya", "new")


# --- the session: correct, spell, learn ----------------------------------------------

class Provider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "fake-model"

    def chat(self, messages, *, purpose="chat"):
        self.calls.append(messages)
        return self._next()

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.calls.append(messages)
        return self._next()

    def _next(self):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ChatApp:
    """WhatsApp and Telegram that only know the chat 'Siyu'."""

    def __init__(self):
        self.sent = []

    def execute_tool_call(self, call, *, approval_granted=False):
        chat = str(call.arguments.get("chat"))
        if chat.casefold() != "siyu":
            return ToolResult(call_id=call.call_id, name=call.name, success=False,
                              error=f"I couldn't find a WhatsApp chat named '{chat}'.", verified=True, verification_status="failed")
        self.sent.append((call.name, chat, call.arguments.get("text")))
        return ToolResult(call_id=call.call_id, name=call.name, success=True, human_readable=f"Sent to {chat}.",
                          verified=True, verification_status="verified", metadata={"chat_title": "Siyu"})


def session_with(responses, tmp_path):
    registry = CapabilityRegistry()
    registry.register(Capability(name="apps", description="apps", status=CapabilityStatus.AVAILABLE, handler=object(),
                                 supported_actions={"whatsapp_send_message", "telegram_send_message"}))
    app = ChatApp()
    tools = ToolRegistry(registry)
    runner = ToolRunner(app, ToolGuard(tools))
    store = LongTermMemory(tmp_path / "memory.db")
    provider = Provider(responses)
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=runner.guard,
                           memory=MemoryService(store, None))
    session.local_intents_enabled = False
    return session, app, store, provider


def send(chat, text="hello from ADVI", name="whatsapp_send_message"):
    return LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("c1", name, {"chat": chat, "text": text}),))


def done(text="Sent."):
    return LLMResponse(text=text, provider="fake", model="m")


def test_spelled_correction_changes_the_name_asks_again_and_learns_the_alias(tmp_path):
    session, app, store, _ = session_with([send("CU"), done()], tmp_path)
    first = session.respond("send a whatsapp message to CU saying hello from ADVI")
    assert "to CU" in first.text and session.pending_confirmation is not None

    changed = session.respond("It is C U. S I Y U. Not the C U.")
    assert "changed it" in changed.text and "to Siyu" in changed.text and "Say yes" in changed.text
    assert app.sent == []  # nothing goes out without a new yes

    session.respond("yes")
    assert app.sent == [("whatsapp_send_message", "Siyu", "hello from ADVI")]
    assert ("Siyu", "cu") in store.known_chats("whatsapp")


def test_learned_alias_maps_the_misheard_name_next_time(tmp_path):
    session, app, store, _ = session_with([send("CU")], tmp_path)
    store.remember_chat("whatsapp", "Siyu", heard_as="CU")
    reply = session.respond("send a whatsapp message to CU saying hello from ADVI")
    assert "to Siyu (I heard 'CU')" in reply.text


def test_chat_not_found_asks_to_spell_just_the_name(tmp_path):
    session, app, store, _ = session_with([send("CU"), done()], tmp_path)
    session.respond("send a whatsapp message to CU saying hello from ADVI")
    ask = session.respond("yes")
    assert "Could you spell just the name" in ask.text and app.sent == []

    again = session.respond("S I Y U")
    assert "to Siyu" in again.text and "Say yes" in again.text and app.sent == []
    session.respond("yes")
    assert app.sent == [("whatsapp_send_message", "Siyu", "hello from ADVI")]
    assert ("Siyu", "cu") in store.known_chats("whatsapp")


def test_message_text_can_be_changed_for_telegram_too(tmp_path):
    session, app, _, _ = session_with([send("Siyu", name="telegram_send_message"), done()], tmp_path)
    session.respond("send hi to Siyu on telegram")
    changed = session.respond("change the message to see you at 5")
    assert "Telegram message 'see you at 5' to Siyu" in changed.text
    session.respond("yes")
    assert app.sent == [("telegram_send_message", "Siyu", "see you at 5")]


def test_other_replies_go_to_the_ai_with_the_pending_action_spelled_out(tmp_path):
    session, app, _, provider = session_with([send("Siyu"), done("It will say hello from ADVI. Say yes or no.")], tmp_path)
    session.respond("send a whatsapp message to Siyu saying hello from ADVI")
    reply = session.respond("hmm what exactly will it say")
    assert "I need an explicit yes or no" not in reply.text
    assert "[PENDING, NOT DONE]" in str(provider.calls[-1])
    assert session.pending_confirmation is not None and app.sent == []


def test_without_the_ai_a_reply_explains_how_to_answer(tmp_path):
    session, app, _, _ = session_with([send("Siyu"), LLMRateLimitError("quota used up")], tmp_path)
    session.respond("send a whatsapp message to Siyu saying hello from ADVI")
    reply = session.respond("hmm what exactly will it say")
    assert "Say yes to do it, no to cancel" in reply.text and "send it to Siyu instead" in reply.text
    assert session.pending_confirmation is not None and app.sent == []


# --- offline commands ----------------------------------------------------------------------

@pytest.mark.parametrize("text, kind, arguments", [
    ("Send message to Anurad saying hello from Atwi.", "whatsapp", {"chat": "Anurad", "text": "hello from Atwi"}),
    ("Play, play Never Say Never on YouTube.", "play", {"query": "Never Say Never", "service": "youtube"}),
    ("Yo, Adhvi, open notepad", "open_app", {"application": "Notepad"}),
    ("Well, can you play believer on youtube?", "play", {"query": "believer", "service": "youtube"}),
    ("what time is it", "answer", {}),
    ("What is the date today?", "answer", {}),
])
def test_offline_commands_from_the_live_session_are_understood(text, kind, arguments):
    intent = match_local_intent(text, offline=True)
    assert intent is not None and intent.kind == kind and intent.arguments == arguments


def test_no_app_named_is_whatsapp_only_offline():
    # With the AI up, it decides which app (phase 6 rule); offline, WhatsApp beats doing nothing.
    assert match_local_intent("send a message to Joel saying hi") is None
    assert match_local_intent("send a message to Joel saying hi", offline=True).tool == "whatsapp_send_message"
    assert match_local_intent("send an email to bob saying hi", offline=True).kind == "email"
    assert match_local_intent("send a message to mom on telegram saying hi", offline=True).tool == "telegram_send_message"


def test_offline_message_without_an_app_reaches_the_whatsapp_confirmation(tmp_path):
    session, app, _, _ = session_with([LLMRateLimitError("quota used up")], tmp_path)
    session.local_intents_enabled = True
    reply = session.respond("Send message to Anurad saying hello from Atwi.")
    assert "WhatsApp message 'hello from Atwi' to Anurad" in reply.text and app.sent == []


def test_time_works_without_the_ai(tmp_path):
    session, _, _, _ = session_with([LLMRateLimitError("quota used up")], tmp_path)
    session.local_intents_enabled = True
    reply = session.respond("what time is it")
    assert reply.text.startswith("It's")  # answered from the PC clock, before the AI is even asked


def test_a_command_that_needs_the_ai_says_so_plainly(tmp_path):
    session, _, _, _ = session_with([LLMRateLimitError("quota used up")], tmp_path)
    session.local_intents_enabled = True
    reply = session.respond("write me a poem about the sea")
    assert "it needs the AI" in reply.text and "'what time is it'" in reply.text
    assert "Simple commands still work" not in reply.text


def test_why_couldnt_you_is_answered_from_the_last_failure(tmp_path):
    session, _, _, _ = session_with([LLMRateLimitError("quota used up")], tmp_path)
    session.local_intents_enabled = True
    session.respond("write me a poem about the sea")
    reply = session.respond("Why couldn't you respond and you said I can still work?")
    assert reply.text.startswith("The problem is that")
