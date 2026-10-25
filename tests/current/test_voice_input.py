"""Phase 11 voice input: endpointing, Whisper guard, hotkey, voice turns, input hub."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from advi.voice.hotkey import MOD_ALT, MOD_CONTROL, HotkeyError, parse_hotkey
from advi.voice.listener import spoken_hotkey
from advi.voice.recorder import FRAME, FRAME_SECONDS, Endpointing, Recorder, Recording
from advi.voice.transcriber import LocalTranscriber

SPEECH, QUIET = 0.5, 0.0  # frame amplitude markers; the fake VAD reads them back


def frame(level):
    return np.full(FRAME, level, dtype="float32")


def frames_for(seconds, level):
    return [frame(level) for _ in range(round(seconds / FRAME_SECONDS))]


class FakeSource:
    def __init__(self, frames, preroll=()):
        self.frames = list(frames)
        self.preroll = list(preroll)
        self.ended = False

    def begin(self):
        return list(self.preroll)

    def read(self, timeout):
        return self.frames.pop(0) if self.frames else None

    def end(self):
        self.ended = True

    def close(self):
        pass


def fake_vad(f):
    return 1.0 if f[0] > 0.1 else 0.0


def record(frames, preroll=(), **kwargs):
    source = FakeSource(frames, preroll)
    result = Recorder(source, vad=fake_vad).record(**kwargs)
    assert source.ended
    return result


# --- endpointing -----------------------------------------------------------

def test_command_ends_after_short_silence():
    result = record(frames_for(1.0, SPEECH) + frames_for(3.0, QUIET))
    assert result.reason == "done"
    seconds = result.audio.size / 16000
    assert 2.15 <= seconds <= 2.3  # 1 s of speech + 1.2 s of silence, then it stops


def test_long_dictation_allows_a_longer_pause():
    pause = frames_for(1.1, QUIET)  # would end a short command
    result = record(frames_for(5.0, SPEECH) + pause + frames_for(1.0, SPEECH) + frames_for(3.0, QUIET))
    assert result.reason == "done" and result.speech_seconds > 5.5


def test_nothing_said_closes_after_six_seconds():
    result = record(frames_for(10.0, QUIET))
    assert result.reason == "no_speech"
    assert result.audio.size / 16000 == pytest.approx(6.0, abs=0.05)


def test_a_click_is_not_speech():
    result = record(frames_for(0.1, SPEECH) + frames_for(8.0, QUIET))
    assert result.reason == "no_speech"


def test_thirty_second_limit():
    result = record(frames_for(40.0, SPEECH))
    assert result.reason == "done" and result.audio.size / 16000 == pytest.approx(30.0, abs=0.05)


def test_preroll_keeps_the_first_word():
    preroll = frames_for(0.3, 0.7)
    result = record(frames_for(1.0, SPEECH) + frames_for(1.0, QUIET), preroll=preroll)
    assert result.audio[0] == pytest.approx(0.7)


def test_holding_the_key_ignores_pauses_until_release():
    held = {"down": True}
    frames = frames_for(1.0, SPEECH) + frames_for(2.0, QUIET) + frames_for(1.0, SPEECH) + frames_for(5.0, QUIET)
    source = FakeSource(frames)
    count = {"n": 0}

    def read(timeout):
        count["n"] += 1
        if count["n"] * FRAME_SECONDS > 4.5:
            held["down"] = False
        return source.frames.pop(0) if source.frames else None

    source.read = read
    result = Recorder(source, vad=fake_vad).record(key_down=lambda: held["down"])
    assert result.reason == "done" and result.speech_seconds >= 1.9  # the 2 s pause did not end it


def test_escape_cancels_and_second_press_ends():
    result = record(frames_for(2.0, SPEECH), cancelled=lambda: True)
    assert result.reason == "cancelled"
    stop = threading.Event()
    stop.set()
    assert record(frames_for(1.0, SPEECH), stop=stop).reason == "no_speech"  # stopped before any speech


# --- transcriber -------------------------------------------------------------

def seg(text, no_speech=0.0, logprob=-0.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob)


def transcriber_with(segments):
    calls = []

    class Model:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)

        def transcribe(self, audio, **kwargs):
            calls.append(kwargs)
            return iter(segments), None

    return LocalTranscriber("small.en", model_factory=Model), calls


def test_transcriber_returns_confident_text_and_loads_locally_first():
    t, calls = transcriber_with([seg(" Open Notepad.")])
    assert t.transcribe(np.ones(16000, dtype="float32")) == "Open Notepad."
    assert calls[0]["local_files_only"] is True
    # No word-list prompt: it made small models loop on short commands.
    assert calls[1]["language"] == "en" and calls[1]["initial_prompt"] is None


@pytest.mark.parametrize("segments", [
    [seg(" Thank you.")],
    [seg(" Thanks for watching!")],
    [seg(" open the door", no_speech=0.9, logprob=-0.9)],
    [seg(" mumble", logprob=-2.0)],
])
def test_transcriber_drops_whisper_hallucinations(segments):
    t, _ = transcriber_with(segments)
    assert t.transcribe(np.ones(16000, dtype="float32")) == ""


def test_transcriber_falls_back_to_download_when_not_local():
    attempts = []

    def factory(*args, **kwargs):
        attempts.append(kwargs["local_files_only"])
        if kwargs["local_files_only"]:
            raise FileNotFoundError("not downloaded")
        return SimpleNamespace(transcribe=lambda audio, **kw: (iter([seg("hi there")]), None))

    t = LocalTranscriber("small.en", model_factory=factory)
    assert t.preload() and attempts == [True, False]


# --- online Turbo first, local fallback ----------------------------------------

def groq_error(name, status=None):
    exc = type(name, (Exception,), {})("boom")
    exc.status_code = status
    return exc


class FakeGroq:
    """One fake client per key; ``plan`` maps key -> list of results or exceptions."""

    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    def factory(self, key):
        def create(**kwargs):
            self.calls.append((key, kwargs))
            outcome = self.plan[key].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return SimpleNamespace(text=outcome, segments=[{"text": outcome, "no_speech_prob": 0.0, "avg_logprob": -0.1}])

        return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))


class FakeLocal:
    def __init__(self, text="open notepad"):
        self.text, self.calls, self.ready = text, 0, True

    def preload(self):
        return True

    def transcribe(self, audio):
        self.calls += 1
        return self.text


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def dual(plan, keys=("k1", "k2")):
    from advi.voice.transcriber import CloudTranscriber, DualTranscriber

    clock, groq, local, said = Clock(), FakeGroq(plan), FakeLocal(), []
    cloud = CloudTranscriber(keys, client_factory=groq.factory, clock=clock)
    return DualTranscriber(local, cloud, notify=said.append, clock=clock), groq, local, said, clock


AUDIO = np.ones(16000, dtype="float32") * 0.1


def test_online_turbo_is_used_when_reachable():
    d, groq, local, said, _ = dual({"k1": ["Open Notepad."]})
    assert d.transcribe(AUDIO) == "Open Notepad."
    key, kwargs = groq.calls[0]
    assert kwargs["model"] == "whisper-large-v3-turbo" and kwargs["response_format"] == "verbose_json"
    assert kwargs["file"][1][:4] == b"RIFF" and local.calls == 0 and said == []
    assert d.status_sentence() is None


def test_no_internet_falls_back_to_local_and_says_so_once():
    d, groq, local, said, clock = dual({"k1": [groq_error("APIConnectionError"), "Back online."], "k2": []})
    assert d.transcribe(AUDIO) == "open notepad"
    assert len(groq.calls) == 1  # another key can't fix the network
    assert len(said) == 1 and "no internet connection" in said[0] and "slower" in said[0]
    assert "no internet connection" in d.status_sentence()

    clock.now += 5  # still cooling down: the cloud is not tried, no second notice
    assert d.transcribe(AUDIO) == "open notepad" and len(groq.calls) == 1 and len(said) == 1

    clock.now += 30  # retried after the cooldown and back online
    assert d.transcribe(AUDIO) == "Back online." and d.mode == "cloud" and d.status_sentence() is None


def test_a_rate_limited_key_hands_over_to_the_next_key():
    d, groq, local, said, clock = dual({"k1": [groq_error("RateLimitError", 429)], "k2": ["first", "second"]})
    assert d.transcribe(AUDIO) == "first" and said == [] and local.calls == 0
    assert d.transcribe(AUDIO) == "second"
    assert [k for k, _ in groq.calls] == ["k1", "k2", "k2"]  # k1 rests during its cooldown


def test_all_keys_out_of_quota_uses_local_with_the_reason():
    d, _, local, said, _ = dual({"k1": [groq_error("RateLimitError", 429)], "k2": [groq_error("RateLimitError", 429)]})
    assert d.transcribe(AUDIO) == "open notepad" and local.calls == 1
    assert "free online speech limit" in said[0]


def test_cloud_hallucination_is_dropped_too():
    d, *_ = dual({"k1": ["Thank you."]})
    assert d.transcribe(AUDIO) == ""


def test_cloud_can_be_turned_off(monkeypatch, tmp_path):
    from advi.voice.listener import transcriber_from_env

    assert transcriber_from_env(tmp_path, ("key",)).cloud is not None
    monkeypatch.setenv("ADVI_STT_CLOUD", "off")
    off = transcriber_from_env(tmp_path, ("key",))
    assert off.cloud is None and off.mode == "local"
    assert transcriber_from_env(tmp_path, ()).cloud is None


@pytest.mark.parametrize("text, matches", [
    ("why are you so slow?", True),
    ("Why is it taking so long", True),
    ("why did that take so long", True),
    ("you're so slow", True),
    ("why is voice slow", True),
    ("why is my laptop slow", False),
    ("open the slow motion video", False),
])
def test_why_are_you_slow_is_answered_locally(text, matches):
    from advi.agent.session import AgentSession

    me = SimpleNamespace(_SLOW_RE=AgentSession._SLOW_RE, _last_failure=None,
                         speech_status=lambda: "I'm understanding your voice offline, because there's no internet connection.")
    answer = AgentSession._handle_slowness_question(me, text)
    assert (answer is not None) == matches
    if matches:
        assert "no internet connection" in answer.text
    me.speech_status = lambda: None  # nothing known: the AI answers instead
    assert AgentSession._handle_slowness_question(me, text) is None


# --- hotkey ------------------------------------------------------------------

def test_hotkey_parsing():
    assert parse_hotkey("ctrl+alt+space") == (MOD_CONTROL | MOD_ALT, 0x20)
    assert parse_hotkey("Ctrl + Alt + V") == (MOD_CONTROL | MOD_ALT, ord("V"))
    assert parse_hotkey("f9") == (0, 0x78)
    for bad in ("", "ctrl+alt+nokey", "hyper+a", "a"):
        with pytest.raises(HotkeyError):
            parse_hotkey(bad)
    assert spoken_hotkey("ctrl+alt+space") == "Control Alt Space"


# --- input hub and output -------------------------------------------------------

def test_input_hub_merges_typed_and_voice():
    from advi.io.console import InputHub

    lines = iter(["hello"])

    def reader():
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    hub = InputHub(reader=reader)
    hub.put_voice("what time is it")
    assert hub.busy
    hub.start()
    got = [hub.get(), hub.get(), hub.get()]
    assert ("voice", "what time is it") in got and ("typed", "hello") in got and ("eof", "") in got


def test_silence_drops_queued_speech():
    from advi.io.output import AdviResponse, OutputManager

    spoken, gate = [], threading.Event()

    class TTS:
        def speak(self, text):
            gate.wait(2)
            spoken.append(text)
            return True

        def stop(self):
            spoken.append("<stopped>")

        def is_speaking(self):
            return False

        def close(self):
            pass

    out = OutputManager(tts=TTS())
    out.deliver(AdviResponse("first"))
    out.deliver(AdviResponse("second"))
    out.silence()
    gate.set()
    assert out.wait_quiet(3)
    out.close()
    assert "second" not in spoken and "<stopped>" in spoken


