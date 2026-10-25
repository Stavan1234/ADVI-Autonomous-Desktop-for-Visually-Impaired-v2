"""Phase 15: echo cancellation through ADVI's own audio stream, sleep closes ADVI, the wake word."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from advi.io.output import AdviResponse, speakable_paths
from advi.io.tts import PiperTTS, clean_for_speech, split_sentences
from advi.voice.audio_io import BLOCK, DuplexAudio, resample
from advi.voice.conversation import VoiceConversation
from advi.voice.recorder import FRAME, FRAME_SECONDS, Recorder, Recording
from advi.wake import autostart
from advi.wake.engines import PorcupineDetector, keyword_file
from advi.wake.listener import STARTUP_GRACE_SECONDS, WakeListener, supervise
from advi.wake.phrase import is_wake_phrase


# --- the wake phrase ----------------------------------------------------------------

@pytest.mark.parametrize("heard", [
    # Spellings local Whisper produced for "ADVI" (benchmark and live logs, 2026-10-24/25).
    "Wake up, ADVI.", "Wake up, Advi.", "Hey Adhvi.", "Atwi!", "Okay, ad-v.", "Wake up! At V!", "Wake up, adv.",
    "Hey Edv!", "Add V, wake up.", "Advee.", "ADVI wake up please", "Hello Advi",
])
def test_wake_phrases(heard):
    assert is_wake_phrase(heard)


@pytest.mark.parametrize("heard", [
    "Wake up.", "Advice for you.", "Add these to the list.", "I need to buy a TV.", "Buy a TV.", "Hey Eddie.",
    "That ATV ride was fun.", "Wake up, it's morning.", "Abby, come here.", "", "Look at V.", "Add v to it",
    "Advi, can you open notepad and write a long letter to my friend",  # a request, not a wake-up
])
def test_not_wake_phrases(heard):
    assert not is_wake_phrase(heard)


# --- the wake listener ----------------------------------------------------------------

class FakeRecorder:
    def __init__(self, recordings=(), frames=()):
        self.recordings = list(recordings)
        self.source = SimpleNamespace(begin=lambda: [], end=lambda: None,
                                      read=lambda timeout: self.frames.pop(0) if self.frames else None)
        self.frames = list(frames)
        self.closed = False

    def record(self, cancelled=None):
        return self.recordings.pop(0) if self.recordings else Recording(np.zeros(0, "float32"), "no_speech")

    def close(self):
        self.closed = True


def heard(seconds=1.0, reason="done"):
    return Recording(np.full(int(16000 * max(seconds, 1.0)), 0.2, "float32"), reason, speech_seconds=seconds)


def wake_listener(texts=(), recordings=(), *, running=None, detector=None, frames=()):
    state = {"running": False, "now": 0.0, "launched": 0, "opened": 0}
    recorders = []

    def open_recorder():
        state["opened"] += 1
        recorders.append(FakeRecorder(recordings, frames))
        return recorders[-1]

    def launch():
        state["launched"] += 1
        return True

    texts = list(texts)
    listener = WakeListener(
        transcriber=SimpleNamespace(transcribe=lambda audio: texts.pop(0) if texts else ""),
        open_recorder=open_recorder, app_running=running or (lambda: state["running"]), launch=launch,
        detector=detector, earcons=SimpleNamespace(played=[], play=lambda name: None),
        clock=lambda: state["now"],
    )
    return listener, state, recorders


def test_the_wake_phrase_starts_advi_once():
    listener, state, _ = wake_listener(["Wake up, ADVI.", "Wake up, ADVI."], [heard(), heard()])
    assert listener.step() == "woke" and state["launched"] == 1
    # While ADVI starts (it can take a minute), no second copy and no microphone.
    assert listener.step() == "starting" and state["launched"] == 1 and listener.recorder is None
    listener.wake()  # the hotkey during start-up
    assert state["launched"] == 1


def test_other_speech_does_not_start_advi_and_long_talk_is_not_transcribed():
    listener, state, _ = wake_listener(["What's the weather?"], [heard(1.0), heard(5.0)])
    assert listener.step() == "not_wake"
    assert listener.step() == "skipped"  # 5 s of speech: conversation, never checked
    assert state["launched"] == 0


def test_the_listener_lets_go_of_the_microphone_while_advi_runs():
    listener, state, recorders = wake_listener(["hello"], [heard()])
    listener.step()
    state["running"] = True
    assert listener.step() == "app_running" and recorders[0].closed and listener.recorder is None
    state["running"] = False
    state["now"] = STARTUP_GRACE_SECONDS + 1
    listener.step()
    assert state["opened"] == 2  # ADVI closed ("sleep"): listening again


def test_a_failed_launch_can_be_retried():
    listener, state, _ = wake_listener(["Hey ADVI"], [heard()])
    listener.launch = lambda: False
    assert listener.step() == "woke"
    assert listener._launched_at is None  # not stuck in "starting"


def test_a_dead_microphone_is_reopened():
    listener, state, recorders = wake_listener()
    outcomes = [listener.step() for _ in range(3)]  # no audio at all, three times
    assert outcomes[-1] == "stalled" and recorders[0].closed
    listener.step()
    assert state["opened"] == 2


def test_the_keyword_engine_checks_every_frame():
    class Detector:
        def __init__(self):
            self.seen = 0

        def process(self, frame):
            self.seen += 1
            return self.seen == 3

    frames = [np.zeros(FRAME, "float32")] * 5
    listener, state, _ = wake_listener(detector=Detector(), frames=frames)
    assert listener.step() == "woke" and state["launched"] == 1


def test_porcupine_gets_int16_frames_of_its_length():
    calls = []

    class Engine:
        frame_length = 512

        def process(self, pcm):
            calls.append(len(pcm))
            return 0 if len(calls) == 2 else -1

    detector = PorcupineDetector("key", "advi.ppn", factory=lambda **kwargs: Engine())
    assert not detector.process(np.zeros(300, "float32"))
    assert not detector.process(np.zeros(300, "float32"))  # 600 samples: one full frame
    assert detector.process(np.zeros(512, "float32"))
    assert calls == [512, 512]


def test_keyword_file_is_found_in_assets(tmp_path, monkeypatch):
    monkeypatch.delenv("ADVI_WAKE_KEYWORD_FILE", raising=False)
    assert keyword_file(tmp_path) is None
    (tmp_path / "wake").mkdir()
    (tmp_path / "wake" / "advi_windows.ppn").write_bytes(b"x")
    assert keyword_file(tmp_path).name == "advi_windows.ppn"


def test_the_supervisor_restarts_a_crashed_worker_with_growing_pauses(monkeypatch):
    import advi.wake.presence as presence

    monkeypatch.setattr(presence, "hold", lambda name: (0, False))
    monkeypatch.setattr("advi.wake.listener._configure_logging", lambda name: None)
    pauses, spawned = [], []

    def spawn():
        spawned.append(True)
        return SimpleNamespace(wait=lambda: 1)

    supervise(spawn, max_restarts=2, sleep=pauses.append, clock=lambda: 0.0)
    assert len(spawned) == 3 and pauses == [5.0, 10.0, 20.0]


def test_only_one_supervisor_runs(monkeypatch):
    import advi.wake.presence as presence

    monkeypatch.setattr(presence, "hold", lambda name: (0, True))
    monkeypatch.setattr("advi.wake.listener._configure_logging", lambda name: None)
    spawned = []
    assert supervise(lambda: spawned.append(True)) == 0 and spawned == []


def test_autostart_runs_the_listener_without_a_window(monkeypatch):
    assert autostart.command_line().endswith('" -m advi.wake')
    assert autostart.windowless_python().name.casefold() in {"pythonw.exe", "python.exe", "python"}
    monkeypatch.setenv("ADVI_WAKE", "off")
    started = []
    monkeypatch.setattr(autostart, "start_detached", lambda: started.append(True) or True)
    assert autostart.ensure_running() is False and started == []


# --- ADVI's own audio stream and echo cancellation --------------------------------------------

class FakeStream:
    latency = (0.03, 0.18)

    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.started = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True

    def tick(self, mic_level=0.0):
        indata = np.full((BLOCK, 1), mic_level, "float32")
        outdata = np.zeros((BLOCK, 1), "float32")
        self.callback(indata, outdata, BLOCK, None, None)
        return outdata[:, 0]


class HalvingCanceller:
    def __init__(self, delay_ms):
        self.delay_ms = delay_ms

    def process(self, played, mic):
        return mic * 0.5


def duplex(canceller=HalvingCanceller):
    streams = []

    def factory(**kwargs):
        streams.append(FakeStream(**kwargs))
        return streams[-1]

    audio = DuplexAudio(stream_factory=factory, canceller_factory=canceller)
    return audio, streams[0]


def drain(audio, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not audio._pairs.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)


def test_speech_and_sounds_are_mixed_into_the_output_and_finish():
    audio, stream = duplex()
    try:
        speech = audio.play(np.full(BLOCK, 0.3, "float32"), 16000, kind="speech")
        audio.play(np.full(BLOCK // 2, 0.2, "float32"), 16000, kind="earcon")
        out = stream.tick()
        assert np.isclose(out[0], 0.5) and np.isclose(out[-1], 0.3)
        assert speech.finished.is_set()
        assert audio.playing("speech")  # still leaving the speakers: output latency + room tail
        assert stream.started and audio.echo_cancelling and audio.canceller.delay_ms == 210
    finally:
        audio.close()


def test_stop_cuts_only_the_speech():
    audio, stream = duplex()
    try:
        speech = audio.play(np.ones(BLOCK * 10, "float32") * 0.1, 16000, kind="speech")
        earcon = audio.play(np.ones(BLOCK * 10, "float32") * 0.1, 16000, kind="earcon")
        audio.stop("speech")
        assert speech.finished.is_set() and not earcon.finished.is_set()
        assert np.isclose(stream.tick()[0], 0.1)
    finally:
        audio.close()


def test_the_microphone_is_cleaned_and_recut_into_vad_frames():
    audio, stream = duplex()
    try:
        source = audio.capture_source()
        source.begin()
        for _ in range(4):
            stream.tick(mic_level=0.4)
        drain(audio)
        frame = source.read(timeout=1.0)
        assert frame is not None and frame.size == FRAME and np.allclose(frame, 0.2)  # the canceller ran
        source.close()  # a recorder closing doesn't close ADVI's speech output
        assert not stream.closed
    finally:
        audio.close()
    assert stream.closed


def test_without_a_canceller_the_microphone_passes_through():
    def broken(delay_ms):
        raise ImportError("no livekit")

    audio, stream = duplex(broken)
    try:
        assert not audio.echo_cancelling
        audio.begin()
        for _ in range(2):
            stream.tick(mic_level=0.4)
        drain(audio)
        assert np.allclose(audio.read(timeout=1.0), 0.4)
    finally:
        audio.close()


def test_resample_keeps_the_duration():
    assert resample(np.zeros(24000, "float32"), 24000).size == 16000
    assert resample(np.zeros(22050, "float32"), 22050).size == 16000


# --- speech through the stream -------------------------------------------------------------------

class FakePlayer:
    def __init__(self):
        self.played, self.stopped = [], 0
        self.gate = threading.Event()
        self.gate.set()

    def play(self, samples, rate, kind="speech"):
        self.played.append(samples.size)
        sound = SimpleNamespace(finished=threading.Event())
        sound.wait = lambda timeout=None: self.gate.wait(2.0)
        return sound

    def stop(self, kind=None):
        self.stopped += 1
        self.gate.set()

    def playing(self, kind=None):
        return False


def tts_with_player(tmp_path):
    tts = PiperTTS(tmp_path / "piper.exe", tmp_path / "model.onnx", tmp_path / "out.wav")
    player = FakePlayer()
    tts.attach_player(player)
    tts._sapi_pcm = lambda text: (np.zeros(len(text), "float32"), 16000)
    return tts, player


def test_speech_is_made_sentence_by_sentence_and_played_through_advis_stream(tmp_path):
    tts, player = tts_with_player(tmp_path)
    assert tts.speak("I've created the files folder inside Documents. It holds your poem. Anything else?")
    assert len(player.played) == 2 and not tts.is_speaking()


def test_stop_drops_the_sentences_not_yet_spoken(tmp_path):
    tts, player = tts_with_player(tmp_path)
    player.gate.clear()
    done = []
    thread = threading.Thread(target=lambda: done.append(tts.speak(
        "This is the first long sentence of the answer. This is the second long sentence of the answer. "
        "And this is the third long sentence of it.")))
    thread.start()
    time.sleep(0.2)
    assert tts.is_speaking()
    tts.stop()
    thread.join(2.0)
    assert player.stopped == 1 and len(player.played) == 1 and not tts.is_speaking()


def test_sentences_are_split_and_short_ones_joined():
    assert split_sentences("Hi. I have created the files folder inside your documents. Done!") == [
        "Hi. I have created the files folder inside your documents.", "Done!"]


def test_paths_are_spoken_as_file_and_folder():
    assert speakable_paths(r"Saved to `C:\Users\Sam\Documents\files for testing\Poem.txt`.") == \
        "Saved to Poem.txt in files for testing."
    assert speakable_paths(r"Moved it to C:\Users\Sam\Desktop.") == "Moved it to Desktop."
    assert "backslash" not in AdviResponse(r"Done: C:\Users\Sam\Downloads\poem.txt").for_speech().casefold()
    assert "\\" not in clean_for_speech(r"left\over")


# --- turn-taking with echo cancellation ----------------------------------------------------------

def frames(seconds, level):
    return [np.full(FRAME, level, "float32") for _ in range(round(seconds / FRAME_SECONDS))]


def test_one_stray_frame_is_not_the_user_starting_to_talk():
    starts = []
    source = SimpleNamespace(begin=lambda: [], end=lambda: None)
    feed = frames(0.032, 0.5) + frames(0.5, 0.0) + frames(0.3, 0.5) + frames(1.5, 0.0)
    source.read = lambda timeout: feed.pop(0) if feed else None
    recorder = Recorder(source, vad=lambda f: 1.0 if f[0] > 0.1 else 0.0, on_speech_start=lambda: starts.append(1))
    recorder.record()
    assert starts == [1]  # only the real 0.3 s of speech


def conversation(**overrides):
    log = {"submitted": [], "said": [], "events": []}
    state = {"quiet": True}
    kwargs = dict(
        recorder=SimpleNamespace(), transcriber=SimpleNamespace(transcribe=lambda audio: ""),
        submit=log["submitted"].append, is_busy=lambda: False, say=log["said"].append,
        is_quiet=lambda: state["quiet"], stop_speech=lambda: log["events"].append("silenced"),
        earcons=SimpleNamespace(play=lambda name, wait=False: log["events"].append(name), playing=False),
        echo_cancelled=True,
    )
    kwargs.update(overrides)
    return VoiceConversation(**kwargs), log, state


def test_with_echo_cancellation_talking_over_advi_stops_it_at_once():
    voice, log, state = conversation()
    state["quiet"] = False
    voice._on_speech_start()
    assert log["events"] == ["silenced"]


def test_without_echo_cancellation_the_onset_alone_does_not_stop_advi():
    voice, log, state = conversation(echo_cancelled=False)
    state["quiet"] = False
    voice._on_speech_start()
    assert log["events"] == []


def test_with_echo_cancellation_a_yes_said_over_advi_is_heard():
    voice, log, _ = conversation(spoken_recently=lambda: "Shall I send hello to Siyu? Yes or no?")
    assert voice.handle_text("Yes.", while_speaking=True) == "submitted"
    assert log["submitted"] == ["Yes."]


def test_wake_up_advi_while_running_is_answered_locally():
    voice, log, _ = conversation()
    assert voice.handle_text("Wake up, ADVI.") == "already_awake"
    assert log["submitted"] == [] and log["said"] == ["I'm here. What can I do?"]
