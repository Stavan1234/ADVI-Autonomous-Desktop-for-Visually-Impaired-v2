"""Always-on voice: ADVI listens continuously while it runs, like Gemini Live or ChatGPT voice.

Sleep (fourth live session, 2026-10-25): "sleep" / "stop listening" closes ADVI
completely, like "exit"; the separate wake-word listener (advi/wake) starts it
again on "wake up ADVI".

Its own voice (2026-10-25): ADVI plays everything through one duplex stream with
WebRTC echo cancellation (audio_io.py), so its voice is removed from the
microphone before speech detection. With that on, speech that starts while ADVI
talks *pauses* ADVI at once (barge-in), so the rest of the sentence is heard
cleanly. When the transcript shows it was ADVI's own voice after all (some echo
can leak, e.g. right after the volume was turned up) or just noise, ADVI resumes
where it paused; only real speech ends its answer. Stopping at the onset lost the
rest of an answer when the onset was its own echo (live log 2026-10-25, 22:12).

Turn-taking (third live session, 2026-10-24):
- **Who had the floor is decided when you start talking.** Phase 13 marked a
  sentence "said while ADVI spoke" if ADVI talked at *any* point during the
  recording; a recording often begins while ADVI finishes its sentence, so
  "Yes", "S-I-Y-U" and "the table is not proper" were dropped and had to be
  repeated.
- **Talking over ADVI interrupts it**, the way voice assistants with echo
  cancellation behave: real speech stops ADVI's voice and is then handled.
  A transcript that is mostly what ADVI itself just said is still ignored, as a
  second guard. Without echo cancellation, a one- or two-word answer that
  might be ADVI's own echo ("yes" while it says "say yes to continue") stops
  ADVI and plays "your turn", so you say it again; a leaked "yes" can never
  confirm a send.
- **Your turn:** a soft rising "ting" when ADVI finishes speaking and is ready.
  While it works, a slow, quiet "breathing" tone (see earcons.py for all sounds;
  "what do the sounds mean?" plays them with their meaning).
- **Ending a sentence:** 1.2 s of silence (2 s once you've talked for 3 s);
  sentences that stop mid-thought get a short continuation window.
- **Music and video:** "ADVI, ..." and plain media commands count; lyrics don't.
- **While ADVI works:** "stop" cancels; other sentences get one "I'm still on
  the last one", then just the working sound.
- **Sleep** and **exit / quit / close ADVI** both close ADVI.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from typing import Any, Callable

from advi.agent.names import sounds_like
from advi.wake.phrase import is_wake_phrase, without_name
from advi.voice.earcons import GUIDE, Earcons

log = logging.getLogger(__name__)

_NAMES = {"advi", "aadvi", "adhvi", "adwi", "atwi", "advee", "adivi", "advie"}
_OPENERS = {"hey", "hi", "ok", "okay", "yo", "hello", "so", "well"}
_SLEEP = re.compile(
    r"^(?:(?:please|now|ok(?:ay)?|you can|can you)\s+)*(?:go to sleep|sleep|stop listening|mute yourself|"
    r"don't listen|stop listening to me|good ?night)(?:\s+(?:now|please|for now))*$",
    re.I,
)
_EXIT = re.compile(
    r"^(?:please\s+)?(?:exit|quit|close (?:advi|yourself|the program)|shut ?down (?:advi|yourself)|"
    r"turn (?:yourself |advi )?off|goodbye|bye bye|exit (?:advi|the program)|quit advi)(?:\s+(?:now|please))*$",
    re.I,
)
_STOP = re.compile(
    r"^(?:(?:ok(?:ay)?|wait|no)\s+)*(?:stop|stop it|stop that|stop talking|stop please|cancel|cancel (?:it|that|the task)|"
    r"never mind|abort|enough|be quiet|quiet|shut up|hold on|that's enough)(?:\s+(?:stop|please|now))*$",
    re.I,
)
_MEDIA = re.compile(
    r"^(?:pause|stop|resume|play|next|skip|previous|mute|unmute|volume (?:up|down)|louder|quieter|turn it (?:up|down))"
    r"(?:\s+(?:the|this))?(?:\s+(?:song|music|video|track|it))?$",
    re.I,
)
_SOUND_GUIDE = re.compile(
    r"^(?:what (?:do|are) (?:the|your|these) sounds?(?: mean)?|explain (?:the|your) sounds?|sound guide|"
    r"what does (?:that|this|each) sound mean|teach me the sounds|play (?:the|your) sounds)$",
    re.I,
)
_WAKE_ONLY = re.compile(r"(?:(?:hey|hi|ok(?:ay)?|please)\s+)*wake(?: up)?(?:\s+(?:please|now))*", re.I)
# Words a sentence doesn't end on: the speaker paused mid-thought.
_UNFINISHED = re.compile(
    r"(?:\.\.\.|…|—|-|,|\b(?:and|or|but|to|the|a|an|of|for|with|saying|that|about|in|on|my|your|is|are|"
    r"because|so|then|um+|uh+|like|which|who|from|at|send|tell|message|called|named))\s*$",
    re.I,
)
# A lone word like this is noise or a hesitation, not a request ("He" got
# "I'm not sure what you'd like me to do", live log 2026-10-24).
_FILLERS = {"he", "uh", "um", "umm", "hmm", "hm", "ah", "oh", "and", "the", "a", "i", "so", "huh", "mm", "eh",
            "hey", "you", "it", "er", "erm"}


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w' ]+", " ", str(text or "").replace("’", "'"))).strip()


def strip_name(text: str) -> tuple[str, bool]:
    """('ADVI, pause the music' -> 'pause the music', True). The name counts in the first few words."""
    words = str(text or "").split()
    for index, word in enumerate(words[:4]):
        bare = re.sub(r"[^\w]", "", word).casefold()
        if not bare:
            continue
        if bare in _NAMES or (4 <= len(bare) <= 6 and sounds_like(bare, "advi", 0.75)):
            rest = words[:index] + words[index + 1:]
            # Drop openers that only led up to the name ("Hey ADVI, ...").
            while rest and re.sub(r"[^\w]", "", rest[0]).casefold() in _OPENERS:
                rest = rest[1:]
            return " ".join(rest).strip(" ,.!?"), True
        if bare not in _OPENERS:
            break
    return str(text or "").strip(), False


def is_sleep_request(text: str) -> bool:
    command, _ = strip_name(text)
    return bool(_SLEEP.fullmatch(_plain(command)))


def is_exit_request(text: str) -> bool:
    command, _ = strip_name(text)
    return bool(_EXIT.fullmatch(_plain(command)))


def is_stop_request(text: str) -> bool:
    command, _ = strip_name(text)
    return bool(_STOP.fullmatch(_plain(command)))


def is_sound_guide_request(text: str) -> bool:
    command, _ = strip_name(text)
    return bool(_SOUND_GUIDE.fullmatch(_plain(command)))


def looks_unfinished(text: str) -> bool:
    return bool(_UNFINISHED.search(str(text or "").strip()))


def is_noise(text: str) -> bool:
    words = _plain(text).casefold().split()
    return not words or (len(words) == 1 and words[0] in _FILLERS)


def is_own_echo(heard: str, spoken: str) -> bool:
    """The sentence is (mostly) words ADVI itself just said: its voice leaking into the mic."""
    heard_words = _plain(heard).casefold().split()
    spoken_words = set(_plain(spoken).casefold().split())
    if len(heard_words) < 3 or not spoken_words:
        return False
    inside = sum(1 for w in heard_words if w in spoken_words) / len(heard_words)
    if inside >= 0.75:
        return True
    return difflib.SequenceMatcher(None, " ".join(heard_words), _plain(spoken).casefold()).find_longest_match().size >= 25


def might_be_echo(heard: str, spoken: str) -> bool:
    """A one- or two-word reply that ADVI also just said ("yes" in "say yes to continue")."""
    heard_words = _plain(heard).casefold().split()
    spoken_words = set(_plain(spoken).casefold().split())
    return 0 < len(heard_words) <= 2 and all(w in spoken_words for w in heard_words)


class VoiceConversation:
    def __init__(
        self,
        *,
        recorder: Any,
        transcriber: Any,
        submit: Callable[[str], Any],
        is_busy: Callable[[], bool],
        say: Callable[[str], Any],
        is_quiet: Callable[[], bool],
        stop_speech: Callable[[], Any],
        request_stop: Callable[[], Any] = lambda: None,
        request_exit: Callable[[], Any] = lambda: None,
        pause_speech: Callable[[], Any] | None = None,
        resume_speech: Callable[[], Any] = lambda: None,
        spoken_recently: Callable[[], str] = lambda: "",
        earcons: Earcons | None = None,
        other_audio: Callable[[], bool] = lambda: False,
        echo_cancelled: bool = False,
        wake_hint: str = "Say wake up ADVI when you need me.",
        quiet_tail: float = 0.3,
        continuation_seconds: float = 2.5,
    ) -> None:
        self.recorder = recorder
        self.transcriber = transcriber
        self.submit = submit
        self.is_busy = is_busy
        self.say = say
        self.is_quiet = is_quiet
        self.stop_speech = stop_speech
        self.request_stop = request_stop
        self.request_exit = request_exit
        self.pause_speech = pause_speech or stop_speech
        self.resume_speech = resume_speech
        self.spoken_recently = spoken_recently
        self.earcons = earcons or Earcons()
        self.other_audio = other_audio
        self.echo_cancelled = echo_cancelled
        self.wake_hint = wake_hint
        self.quiet_tail = quiet_tail
        self.continuation_seconds = continuation_seconds
        self.awake = True
        self.hotkey: Any = None
        self.monitor: Any = None
        self.last_timing: dict[str, float] = {}
        self._busy_told = False
        self._onset_while_speaking = False
        self._paused = False
        self._interrupt = threading.Event()
        self._closed = threading.Event()
        self._threads: list[threading.Thread] = []
        if hasattr(recorder, "on_speech_start"):
            recorder.on_speech_start = self._on_speech_start

    # --- control -----------------------------------------------------------------

    def start(self) -> None:
        for target, name in ((self._listen_loop, "advi-voice-listen"), (self._cue_loop, "advi-voice-cues")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def on_hotkey(self) -> None:
        """While ADVI talks: stop the talking. Otherwise: a 'your turn' chime (it is already listening)."""
        if not self.is_quiet():
            self.stop_speech()
            self.earcons.play("stopped")
            return
        self.earcons.play("your_turn")

    def sleep(self) -> None:
        """Sleep closes ADVI (2026-10-25); the wake-word listener starts it again."""
        if not self.awake:
            return
        self.awake = False
        self._interrupt.set()
        self.earcons.play("sleep")
        self.say(f"Going to sleep. {self.wake_hint}")
        log.info("Voice: sleep requested; closing ADVI.")
        self.request_exit()

    def play_sound_guide(self) -> None:
        """Say what each sound means, then play it: earcons work once people know them."""
        def run() -> None:
            self.say("Here are my sounds.")
            for name, meaning in GUIDE:
                self._wait_quiet(15.0)
                self.say(meaning)
                self._wait_quiet(15.0)
                time.sleep(0.25)
                self.earcons.play(name, wait=True)
                time.sleep(0.5)

        threading.Thread(target=run, name="advi-sound-guide", daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        self._interrupt.set()
        for part in (self.hotkey, self.monitor, self.recorder):
            close = getattr(part, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.debug("Voice part close failed", exc_info=True)

    # --- what a heard sentence does --------------------------------------------------

    def handle_text(self, text: str, *, while_speaking: bool = False) -> str:
        """Act on one transcript; returns what happened (for logs and tests)."""
        command, named = strip_name(text)
        if is_noise(text) and not named:
            return "noise"
        if not command and not named:
            return "empty"
        if while_speaking:
            # The user started talking while ADVI spoke. ADVI's own voice leaking into the
            # mic is recognised by its words; anything else takes the floor.
            spoken = self.spoken_recently()
            if is_own_echo(text, spoken):
                log.info("Ignored ADVI's own voice: %r", text)
                return "echo"
            if is_stop_request(text):
                self.stop_speech()
                if self.is_busy():
                    self.request_stop()
                self.earcons.play("stopped")
                return "interrupted"
            if not self.echo_cancelled and might_be_echo(command, spoken) and not named:
                # "yes" while ADVI says "say yes to continue": maybe the user, maybe its own
                # voice. Stop talking and give the floor, so the answer is said again clearly.
                self.stop_speech()
                self.earcons.play("your_turn")
                return "say_again"
            self.stop_speech()
        if is_exit_request(text):
            self.awake = False
            self.say("Closing ADVI. Goodbye.")
            self.request_exit()
            return "exit"
        if is_sleep_request(text):
            self.sleep()
            return "sleep"
        if is_sound_guide_request(text):
            self.play_sound_guide()
            return "sound_guide"
        if self.other_audio() and not named and not _MEDIA.fullmatch(_plain(command)) and not is_stop_request(text):
            log.info("Ignored while other audio plays (no 'ADVI'): %r", text)
            return "ignored_audio"
        if not command:  # just "ADVI": it is listening already
            self.earcons.play("your_turn")
            return "name_only"
        if is_wake_phrase(text) and _WAKE_ONLY.fullmatch(without_name(text)):
            # "Wake up ADVI" while ADVI runs is not a task for the AI.
            self.say("I'm here. What can I do?")
            return "already_awake"
        if self.is_busy():
            if is_stop_request(text):
                self.request_stop()
                self.stop_speech()
                self.earcons.play("stopped")
                return "stop"
            if not self._busy_told:
                self._busy_told = True
                self.say("I'm still on the last one. Say stop to cancel it.")
            return "busy"
        self.earcons.play("got_it")
        self.submit(command if named else text.strip())
        return "submitted"

    # --- loops -----------------------------------------------------------------------

    def _speaking(self) -> bool:
        return not self.is_quiet()

    def _wait_quiet(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._speaking():
            time.sleep(0.05)

    def _on_speech_start(self) -> None:
        """The user started talking: note who had the floor, and acknowledge softly."""
        self._onset_while_speaking = self._speaking()
        if not self._onset_while_speaking:
            self.earcons.play("heard")
        elif self.echo_cancelled:
            # ADVI's own voice is cancelled, so this is most likely the user: pause now
            # and hear the rest of the sentence without ADVI on top of it. Whether
            # ADVI resumes or stops is decided once the sentence is transcribed.
            self._paused = True
            self.pause_speech()

    _RESUME_AFTER = {None, "echo", "noise", "empty", "ignored_audio"}

    def _settle_pause(self, outcome: str | None) -> None:
        """After a pause at the onset: resume for echo or noise, otherwise the user has the floor."""
        if not self._paused:
            return
        self._paused = False
        if outcome in self._RESUME_AFTER:
            log.info("Resuming ADVI's speech (%s).", outcome or "nothing understood")
            self.resume_speech()
        else:
            self.stop_speech()

    def _transcribe(self, recording: Any) -> str:
        began = time.perf_counter()
        try:
            text = self.transcriber.transcribe(recording.audio)
        except Exception:
            log.exception("Transcription failed.")
            text = ""
        self.last_timing = {"speech_seconds": recording.speech_seconds,
                            "transcribe_seconds": round(time.perf_counter() - began, 2)}
        return text

    def _listen_loop(self) -> None:
        while not self._closed.is_set():
            if not self.awake:
                self._closed.wait(0.5)
                continue
            self._onset_while_speaking = False
            self._interrupt.clear()
            outcome: str | None = None
            try:
                outcome = self._listen_once()
            finally:
                self._settle_pause(outcome)

    def _listen_once(self) -> str | None:
        """Record and handle one utterance; returns the outcome (None if nothing was understood)."""
        try:
            recording = self.recorder.record(
                self._interrupt, cancelled=lambda: not self.awake or self._closed.is_set())
        except Exception:
            log.exception("Voice recording failed.")
            time.sleep(1.0)
            return None
        if recording.reason != "done" or not self.awake:
            return None
        while_speaking = self._onset_while_speaking
        text = self._transcribe(recording)
        if text and looks_unfinished(text) and not while_speaking:
            # "Send a message to ... saying" - the speaker paused; keep listening a moment.
            more = self.recorder.record(self._interrupt, cancelled=lambda: not self.awake or self._closed.is_set(),
                                        no_speech_seconds=self.continuation_seconds)
            if more.reason == "done":
                rest = self._transcribe(more)
                if rest:
                    text = f"{text.rstrip(' .…-')} {rest}"
        if not text:
            if recording.speech_seconds >= 1.2 and not self.other_audio() and not while_speaking:
                self.earcons.play("not_heard")  # real talking that wasn't understood
            return None
        log.info("Voice heard %r (%s)%s", text, self.last_timing, " over ADVI's voice" if while_speaking else "")
        try:
            outcome = self.handle_text(text, while_speaking=while_speaking)
            log.info("Voice outcome: %s", outcome)
            return outcome
        except Exception:
            log.exception("Voice command handling failed.")
            return None

    def _cue_loop(self) -> None:
        """'Your turn' when ADVI goes quiet and idle; a slow, quiet breathing tone while it works."""
        was_active = False
        busy_since: float | None = None
        quiet_since: float | None = None
        last_working = 0.0
        while not self._closed.wait(0.15):
            busy, speaking = self.is_busy(), self._speaking()
            if not busy:
                self._busy_told = False
            if not self.awake:
                was_active, busy_since, quiet_since = False, None, None
                continue
            if busy or speaking:
                was_active = True
                quiet_since = None
            else:
                quiet_since = quiet_since or time.monotonic()
                if was_active and time.monotonic() - quiet_since >= self.quiet_tail:
                    was_active = False
                    self.earcons.play("your_turn")
            if busy and not speaking:
                busy_since = busy_since or time.monotonic()
                now = time.monotonic()
                if now - busy_since >= 1.2 and now - last_working >= 2.6 and not self.earcons.playing:
                    self.earcons.play("working")
                    last_working = now
            else:
                busy_since = None
