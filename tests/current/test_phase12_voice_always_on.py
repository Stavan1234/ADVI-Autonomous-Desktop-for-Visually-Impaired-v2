"""Phase 12: always-on listening, sleep (closes ADVI since 2026-10-25), the name gate during music, sounds."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from advi.voice.conversation import VoiceConversation, is_sleep_request, strip_name
from advi.voice.earcons import RATE, Earcons, synthesize
from advi.voice.recorder import FRAME, Recorder, Recording
from advi.voice.system_audio import SystemAudioMonitor


@pytest.mark.parametrize("text, command, named", [
    ("ADVI, pause the music", "pause the music", True),
    ("Hey Adhvi, what's up?", "what's up", True),
    ("Yo, Atwi, open notepad", "open notepad", True),
    ("Okay advi.", "", True),
    ("pause the music", "pause the music", False),
    ("I told David about it", "I told David about it", False),
])
def test_the_name_is_found_in_the_first_words_and_removed(text, command, named):
    assert strip_name(text) == (command, named)


@pytest.mark.parametrize("text, sleeps", [
    ("Sleep.", True), ("go to sleep", True), ("ADVI, stop listening", True), ("Okay, you can sleep now", True),
    ("put the computer to sleep", False), ("sleep timer for 10 minutes", False), ("I can't sleep", False),
])
def test_sleep_words(text, sleeps):
    assert is_sleep_request(text) == sleeps


class Tones:
    def __init__(self):
        self.played = []
        self.playing = False

    def play(self, name, wait=False):
        self.played.append(name)


def conversation(**overrides):
    log = {"submitted": [], "said": [], "events": []}
    state = {"busy": False, "quiet": True, "audio": False}
    kwargs = dict(
        recorder=SimpleNamespace(), transcriber=SimpleNamespace(transcribe=lambda audio: ""),
        submit=log["submitted"].append, is_busy=lambda: state["busy"], say=log["said"].append,
        is_quiet=lambda: state["quiet"], stop_speech=lambda: log["events"].append("silenced"),
        request_stop=lambda: log["events"].append("task stopped"), earcons=Tones(),
        other_audio=lambda: state["audio"],
    )
    kwargs.update(overrides)
    return VoiceConversation(**kwargs), log, state


def test_start_listens_right_away_and_falls_back_to_a_free_key(monkeypatch):
    import advi.voice.listener as listener

    class Hotkey:
        def __init__(self, text, callback):
            self.text, self.error = text, "taken by another program"

        def start(self):
            return self.text != "ctrl+alt+space"

        def close(self):
            pass

    started = []
    monkeypatch.setattr(listener, "GlobalHotkey", Hotkey)
    monkeypatch.setattr(listener, "open_microphone", lambda source=None: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(listener.SystemAudioMonitor, "start", lambda self: False)
    monkeypatch.setattr(listener.VoiceConversation, "start", lambda self: started.append(True))
    monkeypatch.delenv("ADVI_VOICE_HOTKEY", raising=False)
    kwargs = dict(submit=print, is_busy=lambda: False, say=print, is_quiet=lambda: True, stop_speech=lambda: None)
    voice, message = listener.start_voice(SimpleNamespace(ready=True), wake_word=True, **kwargs)
    assert voice is not None and started and voice.awake
    assert message.startswith("I'm listening; just talk to me.") and "Control Alt Z stops me talking" in message
    assert "Say sleep or exit to close me" in message and "Say wake up ADVI to bring me back" in message
    assert voice.wake_hint == "Say wake up ADVI when you need me."
    assert not voice.echo_cancelled  # no shared audio stream was passed

    monkeypatch.setenv("ADVI_VOICE_HOTKEY", "ctrl+alt+space")  # an explicit choice is not swapped
    voice, message = listener.start_voice(SimpleNamespace(ready=True), **kwargs)
    assert voice is not None and "Control Alt Space is already used" in message
    assert voice.wake_hint == "Start ADVI again when you need me." and "wake up ADVI" not in message


def test_a_sentence_is_a_command_without_any_key():
    voice, log, _ = conversation()
    assert voice.handle_text("open notepad") == "submitted"
    assert log["submitted"] == ["open notepad"] and voice.earcons.played == ["got_it"]


def test_sleep_closes_advi_completely():
    closed = []
    voice, log, _ = conversation(request_exit=lambda: closed.append(True))
    assert voice.handle_text("Sleep.") == "sleep"
    assert not voice.awake and log["submitted"] == [] and closed == [True]
    assert log["said"] == ["Going to sleep. Say wake up ADVI when you need me."]
    assert voice.earcons.played[-1] == "sleep"


def test_the_hotkey_never_puts_advi_to_sleep():
    voice, log, _ = conversation()
    voice.on_hotkey()  # quiet: it is already listening, so just "your turn"
    assert voice.awake and voice.earcons.played == ["your_turn"] and log["events"] == []


def test_the_hotkey_interrupts_advi_while_it_talks():
    voice, log, state = conversation()
    state["quiet"] = False
    voice.on_hotkey()
    assert log["events"] == ["silenced"] and voice.awake


def test_during_music_only_sentences_with_the_name_count():
    voice, log, state = conversation()
    state["audio"] = True
    assert voice.handle_text("never say never") == "ignored_audio"
    assert voice.handle_text("ADVI, pause") == "submitted"
    assert log["submitted"] == ["pause"]


def test_while_busy_only_stop_acts():
    voice, log, state = conversation()
    state["busy"] = True
    assert voice.handle_text("play a song") == "busy"
    assert log["said"] == ["I'm still on the last one. Say stop to cancel it."]
    assert voice.handle_text("stop") == "stop"
    assert log["events"] == ["task stopped", "silenced"] and log["submitted"] == []


class ScriptedRecorder:
    def __init__(self, recordings):
        self.recordings = list(recordings)
        self.calls = 0
        self.on_speech_start = None

    def record(self, stop, **kwargs):
        self.calls += 1
        if self.recordings:
            return self.recordings.pop(0)
        stop.wait(0.05)
        return Recording(audio=np.zeros(0, dtype="float32"), reason="no_speech")


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_the_listen_loop_hears_one_sentence_after_another():
    heard = lambda: Recording(audio=np.ones(8000, dtype="float32"), reason="done", speech_seconds=0.6)
    texts = ["open notepad", "what time is it"]
    voice, log, _ = conversation(recorder=ScriptedRecorder([heard(), heard()]),
                                 transcriber=SimpleNamespace(transcribe=lambda audio: texts.pop(0)), quiet_tail=0.0)
    voice.start()
    try:
        assert wait_for(lambda: len(log["submitted"]) == 2)
        assert log["submitted"] == ["open notepad", "what time is it"]
    finally:
        voice.close()


def test_talking_over_advi_takes_the_floor_but_its_own_voice_does_not():
    spoken = "Once upon a time there was a little fox who lived in a quiet forest. Say yes to continue."
    voice, log, state = conversation(spoken_recently=lambda: spoken)
    state["quiet"] = False
    assert voice.handle_text("a little fox who lived in a quiet forest", while_speaking=True) == "echo"
    assert log["events"] == [] and log["submitted"] == []
    assert voice.handle_text("Stop.", while_speaking=True) == "interrupted"
    assert voice.handle_text("open notepad please", while_speaking=True) == "submitted"
    assert log["submitted"] == ["open notepad please"] and log["events"] == ["silenced", "silenced"]
    # "yes" may be ADVI's own "say yes" leaking in: stop and hand over, never confirm with it.
    assert voice.handle_text("Yes.", while_speaking=True) == "say_again"
    assert log["submitted"] == ["open notepad please"] and voice.earcons.played[-1] == "your_turn"


def test_noise_words_are_not_requests():
    voice, log, _ = conversation()
    assert voice.handle_text("He") == "noise"
    assert voice.handle_text("Yes.") == "submitted"  # a real one-word answer still counts
    assert log["submitted"] == ["Yes."]


def test_who_had_the_floor_is_decided_when_the_user_starts():
    voice, _, state = conversation()
    state["quiet"] = False  # ADVI talking when the recording began...
    state["quiet"] = True   # ...but finished before the user spoke
    voice._on_speech_start()
    assert voice._onset_while_speaking is False and voice.earcons.played[-1] == "heard"
    state["quiet"] = False
    voice._on_speech_start()
    assert voice._onset_while_speaking is True


def test_sound_guide_request():
    from advi.voice.conversation import is_sound_guide_request
    from advi.voice.earcons import GUIDE, _SOUNDS

    assert is_sound_guide_request("What do the sounds mean?")
    assert is_sound_guide_request("ADVI, explain your sounds")
    assert not is_sound_guide_request("play some sounds of rain")
    assert {name for name, _ in GUIDE} == set(_SOUNDS)  # every sound is taught


def test_stop_while_working_and_talking_also_stops_the_task():
    voice, log, state = conversation()
    state["busy"], state["quiet"] = True, False
    assert voice.handle_text("okay stop", while_speaking=True) == "interrupted"
    assert log["events"] == ["silenced", "task stopped"]


def test_busy_notice_is_said_once_then_only_the_working_sound():
    voice, log, state = conversation()
    state["busy"] = True
    voice.handle_text("are you stuck?")
    voice.handle_text("hello?")
    assert log["said"] == ["I'm still on the last one. Say stop to cancel it."]  # once, not every time


def test_exit_and_sleep_words_both_close_advi():
    closed = []
    voice, log, _ = conversation(request_exit=lambda: closed.append(True))
    assert voice.handle_text("ADVI, exit") == "exit" and closed == [True]
    assert log["said"] == ["Closing ADVI. Goodbye."]
    assert voice.handle_text("close advi") == "exit"
    assert voice.handle_text("sleep") == "sleep" and len(closed) == 2


def test_plain_media_commands_work_during_music_but_lyrics_do_not():
    voice, log, state = conversation()
    state["audio"] = True
    assert voice.handle_text("Pause the song.") == "submitted"
    assert voice.handle_text("volume down") == "submitted"
    assert voice.handle_text("I'm going to love you forever") == "ignored_audio"


@pytest.mark.parametrize("text, unfinished", [
    ("Send a message to Siyu saying", True), ("I don't understand why did you open the...", True),
    ("Search for", True), ("open notepad", False), ("What time is it?", False),
])
def test_unfinished_sentences_get_a_continuation_window(text, unfinished):
    from advi.voice.conversation import looks_unfinished

    assert looks_unfinished(text) == unfinished


def test_an_unfinished_sentence_is_joined_with_the_rest():
    first = Recording(audio=np.ones(8000, dtype="float32"), reason="done", speech_seconds=1.5)
    rest = Recording(audio=np.ones(8000, dtype="float32"), reason="done", speech_seconds=0.8)
    texts = ["Send a message to Siyu saying", "hello from ADVI."]
    voice, log, _ = conversation(recorder=ScriptedRecorder([first, rest]),
                                 transcriber=SimpleNamespace(transcribe=lambda audio: texts.pop(0)))
    voice.start()
    try:
        assert wait_for(lambda: log["submitted"])
        assert log["submitted"] == ["Send a message to Siyu saying hello from ADVI."]
    finally:
        voice.close()


def test_your_turn_chime_after_advi_finishes_speaking():
    voice, _, state = conversation(quiet_tail=0.05)
    state["quiet"] = False
    voice.start()
    try:
        time.sleep(0.3)
        assert "your_turn" not in voice.earcons.played
        state["quiet"] = True
        assert wait_for(lambda: "your_turn" in voice.earcons.played)
    finally:
        voice.close()


def test_a_stalled_microphone_still_ends_the_turn():
    class Stalls:
        def __init__(self):
            self.frames = [np.full(FRAME, 0.5, dtype="float32") for _ in range(20)]

        def begin(self):
            return []

        def read(self, timeout):
            return self.frames.pop(0) if self.frames else None

        def end(self):
            pass

    result = Recorder(Stalls(), vad=lambda f: 1.0 if f[0] > 0.1 else 0.0).record()
    assert result.reason == "done"  # speech, then no frames at all: ended, not stuck


def test_speech_start_plays_the_soft_heard_sound_once():
    starts = []

    class Source:
        frames = [np.full(FRAME, 0.5, dtype="float32") for _ in range(15)] + [np.zeros(FRAME, dtype="float32") for _ in range(40)]

        def begin(self):
            return []

        def read(self, timeout):
            return self.frames.pop(0) if self.frames else None

        def end(self):
            pass

    Recorder(Source(), vad=lambda f: 1.0 if f[0] > 0.1 else 0.0, on_speech_start=lambda: starts.append(1)).record()
    assert starts == [1]


def test_earcons_are_soft_bounded_and_click_free():
    for name in ("your_turn", "wake", "heard", "got_it", "working", "not_heard", "stopped", "sleep"):
        sound = synthesize(name)
        assert sound.dtype == np.float32 and 0 < sound.size < RATE * 1.5  # short
        assert abs(float(sound[-1])) < 1e-3  # fades out: no click at the end
        assert float(np.max(np.abs(sound))) <= 0.25  # gentle
        assert abs(float(sound[0])) < 0.01  # fades in: no click
    played = []
    tones = Earcons(player=lambda sound, rate: played.append((sound.size, rate)))
    tones.play("heard")
    assert played and played[0][1] == RATE and tones.playing


def test_other_audio_means_most_of_the_last_two_seconds_were_loud():
    now = [100.0]
    levels = iter([0.2, 0.3, 0.25, 0.0, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0])
    own = [False]
    monitor = SystemAudioMonitor(read_peak=lambda: next(levels), clock=lambda: now[0], own_sound=lambda: own[0])
    for _ in range(5):
        monitor.sample()
        now[0] += 0.1
    assert monitor.other_audio_playing()
    own[0] = True  # ADVI's own speech is not sampled
    monitor.sample()
    own[0] = False
    for _ in range(5):
        monitor.sample()
        now[0] += 0.1
    now[0] += 1.0
    assert not monitor.other_audio_playing()
