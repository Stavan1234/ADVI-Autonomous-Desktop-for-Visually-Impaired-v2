"""WhatsApp and honesty fixes from the 2026-10-21 live log."""
from __future__ import annotations

import time

from advi.agent.local_intents import match_local_intent
from advi.agent.session import AgentSession
from advi.capabilities.apps.whatsapp import WhatsAppSkill, chat_title_from_label
from advi.tools.contracts import ToolCall


def _bare(said: str) -> AgentSession:
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.last_user_message = said
    return session


def test_unread_prefix_does_not_hide_a_chat():
    # "anurodh" was "not found": the row read "2 unread messages Anurodh Yesterday ...".
    assert chat_title_from_label("2 unread messages Anurodh Yesterday Repo*") == "Anurodh"
    assert chat_title_from_label("1 unread message Mom 10:17 hi") == "Mom"
    assert chat_title_from_label("~David FCRIT College 09:00") == "David FCRIT College"
    assert chat_title_from_label("New Chapter 21/07/2025 You: hi") == "New Chapter"


class _Node:
    def __init__(self, name):
        self.name = name


def test_unsaved_contact_chat_opened_under_its_number_is_accepted():
    # Clicked 'David FCRIT College'; composer said "Type a message to +91 95944 18687".
    skill = WhatsAppSkill.__new__(WhatsAppSkill)
    composers = iter([_Node("Type a message to +91 95944 18687")])
    skill._composer = lambda doc: next(composers)
    evidence = {}
    assert skill._chat_is_open(None, "David FCRIT College", "type a message to new chapter", evidence)
    assert evidence["chat_number"] == "+91 95944 18687"


def test_an_unchanged_or_different_named_chat_is_not_accepted():
    skill = WhatsAppSkill.__new__(WhatsAppSkill)
    skill._composer = lambda doc: _Node("Type a message to +91 95944 18687")
    # The composer was already on that number before the click: nothing proves the click worked.
    assert not skill._chat_is_open(None, "David FCRIT College", "type a message to +91 95944 18687", {})
    skill._composer = lambda doc: _Node("Type a message to Rahul")
    assert not skill._chat_is_open(None, "David FCRIT College", "", {})


def test_the_chat_name_the_user_said_is_kept():
    # The model turned "David" into "David Almeida" (an email contact).
    session = _bare("send message to David saying Hello from advi , on whatsapp.")
    fixed = session._restore_spoken_chat(ToolCall("c", "whatsapp_send_message", {"chat": "David Almeida", "text": "x"}))
    assert fixed.arguments["chat"] == "David"
    same = _bare("send hello to New Chapter on whatsapp")._restore_spoken_chat(
        ToolCall("c", "whatsapp_send_message", {"chat": "New Chapter", "text": "hello"}))
    assert same.arguments["chat"] == "New Chapter"


def test_log_phrasings_parse_with_the_right_chat():
    cases = {
        "send message to David saying Hello from advi , on whatsapp.": ("David", "Hello from advi"),
        "send an message to David on whatsapp, saying Hello from advi": ("David", "Hello from advi"),
        "send message to anurodh saying hello from advi on whatsapp.": ("anurodh", "hello from advi"),
        "send a message to David Fcrit on whatsapp saying Hello from advi": ("David Fcrit", "Hello from advi"),
    }
    for phrase, (chat, text) in cases.items():
        intent = match_local_intent(phrase)
        assert intent is not None and intent.arguments == {"chat": chat, "text": text}, (phrase, intent)


def test_no_sent_claim_without_a_successful_send():
    # "okay send message to david" -> "I've sent the WhatsApp message" with no tool call.
    session = _bare("okay send message to david")
    session._turn_started_at = time.monotonic()
    session._last_send_at = None
    for claim in ("I've sent the WhatsApp message to David.", "I’ve sent it.", "Your email has been sent."):
        assert session._no_unbacked_send_claim(claim).startswith("I haven't sent anything yet"), claim
    session._last_send_at = time.monotonic()
    assert session._no_unbacked_send_claim("I've sent it.") == "I've sent it."
    assert session._no_unbacked_send_claim("Opened YouTube.") == "Opened YouTube."


def test_email_confirmation_says_who_and_what():
    session = _bare("send an email to anurodh saying this is testing email.")
    text = session._confirmation_description(ToolCall("c", "email_send", {"to": "a@example.com", "subject": "Testing Email", "body": "this is testing email."}))
    assert "a@example.com" in text and "this is testing email." in text and "saved recipient" not in text
