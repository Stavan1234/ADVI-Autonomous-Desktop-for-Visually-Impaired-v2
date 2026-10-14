"""Spoken progress: one phrase per meaningful step, never lagging behind the work."""
from __future__ import annotations

import threading
import time

from advi.io.output import AdviResponse, OutputManager
from advi.io.progress import progress_phrase


def test_phrases_name_the_thing_being_worked_on():
    assert progress_phrase("open_application", {"application": "Notepad"}) == "Opening Notepad."
    assert progress_phrase("whatsapp_send_message", {"chat": "New Chapter", "text": "hi"}) == "Sending your WhatsApp message to New Chapter."
    assert progress_phrase("create_file", {"path": r"C:\Users\HP\Desktop\wake_up.pdf"}) == "Creating wake_up.pdf."
    assert progress_phrase("navigate", {"url": "https://www.miruro.to/search"}) == "Opening miruro.to."
    assert progress_phrase("play_media", {"query": "Overflow"}) == "Looking up Overflow."
    assert progress_phrase("open_application", {}) == "Opening the app."


def test_low_level_steps_stay_silent():
    for tool in ("observe_window", "interact", "click", "type_text", "read_screen", "wait", "inspect_web_page"):
        assert progress_phrase(tool, {"text": "x"}) is None


class SlowTTS:
    """Speaks one phrase at a time; the first call blocks until released."""

    def __init__(self):
        self.spoken: list[str] = []
        self.release = threading.Event()
        self.started = threading.Event()

    def speak(self, text):
        self.started.set()
        if not self.spoken:
            self.release.wait(2)
        self.spoken.append(text)
        return True

    def close(self):
        pass


def test_stale_progress_is_dropped_and_the_answer_always_spoken(capsys):
    tts = SlowTTS()
    out = OutputManager(tts=tts)
    out.narrate("Opening WhatsApp.")          # starts speaking, blocks
    tts.started.wait(1)
    out.narrate("Searching for New Chapter.")  # queued...
    out.narrate("Searching for New Chapter.")  # duplicate: ignored
    out.deliver(AdviResponse("Sent 'test' to New Chapter."))  # ...superseded by the answer
    tts.release.set()
    deadline = time.monotonic() + 2
    while len(tts.spoken) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    out.close()
    assert tts.spoken == ["Opening WhatsApp.", "Sent 'test' to New Chapter."]
    assert "... Searching for New Chapter." in capsys.readouterr().out


def test_session_announces_tools_through_the_callback():
    from test_phase0_integrity import _ScriptedProvider, _session, _text, _tool_call

    provider = _ScriptedProvider([
        _tool_call("create_folder", {"path": "ADVI-TEST"}, "c1"),
        _text("Done."),
    ])
    session, _engine = _session(provider, {"file_management": {"create_folder"}})
    heard: list[str] = []
    session.progress_callback = heard.append

    session.respond("create a folder called ADVI-TEST on the desktop")

    assert heard == ["Creating the ADVI-TEST folder."]
