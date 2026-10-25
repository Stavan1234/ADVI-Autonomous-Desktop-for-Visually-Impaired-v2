"""Fixes from the first live session with echo cancellation and the wake word (2026-10-25, 22:04-22:13).

1. ADVI's answer was cut off although nobody interrupted: a little of its own voice leaked
   through after the volume went up, the onset stopped the speech, and the transcript was
   then recognised as its own words. Now the onset only pauses; echo and noise resume.
2. Read-only questions ("status of laptop", "is internet connected?") were answered "I
   couldn't verify the final state" because an earlier look had failed or been rejected.
3. There was no tool for "is internet connected?" (the AI opened example.com).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from advi.agent import AgentSession
from advi.agent.local_intents import match_local_intent
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.capabilities.system import SystemCapability
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolGuard, ToolRegistry, ToolRunner
from advi.tools.contracts import ToolCall, ToolResult
from advi.voice.audio_io import BLOCK, DuplexAudio
from advi.voice.conversation import VoiceConversation

from test_phase15_echo_and_wake import FakeStream, HalvingCanceller


# --- 1. pause at the onset, resume for echo ----------------------------------------------------

def conversation():
    log = {"events": [], "submitted": []}
    state = {"quiet": False}
    voice = VoiceConversation(
        recorder=SimpleNamespace(), transcriber=SimpleNamespace(transcribe=lambda audio: ""),
        submit=log["submitted"].append, is_busy=lambda: False, say=lambda text: None,
        is_quiet=lambda: state["quiet"], stop_speech=lambda: log["events"].append("stopped"),
        pause_speech=lambda: log["events"].append("paused"), resume_speech=lambda: log["events"].append("resumed"),
        spoken_recently=lambda: "I opened example.com just to see whether a page could load. So the page opened.",
        earcons=SimpleNamespace(play=lambda name, wait=False: None, playing=False), echo_cancelled=True,
    )
    return voice, log


def test_the_onset_only_pauses_advi():
    voice, log = conversation()
    voice._on_speech_start()
    assert log["events"] == ["paused"]


def test_its_own_leaked_voice_resumes_the_answer():
    voice, log = conversation()
    voice._on_speech_start()
    outcome = voice.handle_text("page so did page opened", while_speaking=True)
    voice._settle_pause(outcome)
    assert outcome == "echo" and log["events"] == ["paused", "resumed"] and log["submitted"] == []


@pytest.mark.parametrize("outcome", [None, "noise", "empty", "ignored_audio"])
def test_noise_or_nothing_understood_resumes(outcome):
    voice, log = conversation()
    voice._on_speech_start()
    voice._settle_pause(outcome)
    assert log["events"] == ["paused", "resumed"]


def test_real_speech_ends_the_answer():
    voice, log = conversation()
    voice._on_speech_start()
    outcome = voice.handle_text("Can you increase the volume?", while_speaking=True)
    voice._settle_pause(outcome)
    assert outcome == "submitted" and log["events"][-1] == "stopped" and "resumed" not in log["events"]


def test_paused_speech_holds_its_place_and_continues():
    streams = []
    audio = DuplexAudio(stream_factory=lambda **kw: streams.append(FakeStream(**kw)) or streams[-1],
                        canceller_factory=HalvingCanceller)
    try:
        ramp = np.arange(BLOCK * 3, dtype="float32") / (BLOCK * 30)
        sound = audio.play(ramp, 16000, kind="speech")
        first = streams[0].tick()
        audio.pause("speech")
        assert np.allclose(streams[0].tick(), 0.0) and audio.playing("speech") and not sound.finished.is_set()
        audio.resume("speech")
        assert np.allclose(streams[0].tick(), ramp[BLOCK:2 * BLOCK])  # exactly where it paused
        assert np.allclose(first, ramp[:BLOCK])
        audio.pause("speech")
        audio.stop("speech")  # stopping clears the pause, so the next answer plays
        assert not audio.paused("speech")
    finally:
        audio.close()


# --- 2. read-only answers after a failed look -----------------------------------------------------

class Provider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "m"

    def chat(self, messages, *, purpose="chat"):
        return self.responses.pop(0)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        return self.responses.pop(0)


class Machine:
    def execute_tool_call(self, call, *, approval_granted=False):
        if call.name == "research_web":
            return ToolResult(call_id=call.call_id, name=call.name, success=False, verified=False,
                              verification_status="uncertain", error="Search completed but no usable result links were found.")
        if call.name == "open_browser":
            return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True,
                              verification_status="verified", output="https://www.example.com/",
                              human_readable="Opened the browser at https://www.example.com/.")
        if call.name == "get_system_info":
            return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True,
                              verification_status="verified", human_readable="System: Windows 11.")
        raise AssertionError(call.name)


def session_with(responses):
    registry = CapabilityRegistry()
    registry.register(Capability(name="machine", description="machine", status=CapabilityStatus.AVAILABLE,
                                 handler=object(), supported_actions={"research_web", "open_browser", "get_system_info"}))
    tools = ToolRegistry(registry)
    runner = ToolRunner(Machine(), ToolGuard(tools))
    session = AgentSession([Provider(responses)], tool_registry=tools, tool_runner=runner, tool_guard=runner.guard)
    session.local_intents_enabled = False
    return session


def call(name, **arguments):
    return LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall(f"c-{name}", name, arguments),))


def say(text):
    return LLMResponse(text=text, provider="fake", model="m")


def test_a_failed_look_before_the_answer_does_not_make_it_uncertain():
    session = session_with([call("research_web", query="test connectivity"), call("open_browser", url="https://www.example.com"),
                            say("Yes, you're online: a test page opened.")])
    reply = session.respond("Is the network up right now, can you check")
    assert reply.text == "Yes, you're online: a test page opened."


def test_a_rejected_parameter_before_the_answer_does_not_make_it_uncertain():
    session = session_with([call("get_system_info", detail="full"), call("get_system_info"),
                            say("You're on Windows 11.")])
    reply = session.respond("give me a full description of this machine")
    assert reply.text == "You're on Windows 11."


# --- 3. a real internet check ----------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Is internet connected?", "am I online", "is the wifi working", "internet status"])
def test_internet_questions_use_the_network_tool(text):
    assert AgentSession._is_local_system_request(text) == "get_network_status"


def test_status_of_laptop_is_a_system_question():
    assert AgentSession._is_local_system_request("Tell me the status of laptop.") == "get_system_status"


def test_offline_internet_question_is_a_local_answer():
    intent = match_local_intent("is the internet connected", offline=True)
    assert intent is not None and intent.tool == "get_network_status"


@pytest.mark.parametrize("data, expected", [
    ({"internet": True, "dns": True, "wifi": "Home", "adapters_up": ["Wi-Fi"]}, 'Yes, the internet is connected through Wi-Fi "Home".'),
    ({"internet": True, "dns": False, "wifi": None, "adapters_up": ["Ethernet"]}, "aren't resolving"),
    ({"internet": False, "dns": False, "wifi": "Cafe", "adapters_up": ["Wi-Fi"]}, "can't reach the internet"),
    ({"internet": False, "dns": False, "wifi": None, "adapters_up": []}, "no network connection is active"),
])
def test_network_answers(data, expected):
    assert expected in SystemCapability._network_text(data)


def test_network_tool_returns_a_verified_read_only_result(monkeypatch):
    monkeypatch.setattr(SystemCapability, "_network", classmethod(lambda cls: {
        "internet": True, "dns": True, "wifi": None, "adapters_up": ["Ethernet"]}))
    result = SystemCapability().execute(ToolCall(call_id="n1", name="get_network_status", arguments={}))
    assert result.success and result.verified and result.human_readable == "Yes, the internet is connected."
