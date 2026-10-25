"""Record one spoken command from the microphone and decide when it has ended.

Follows the usual voice-assistant turn rules:
- a short pre-roll is kept so the first word is not lost;
- speech is detected with Silero VAD (bundled with faster-whisper), with an
  energy check only as a fallback;
- the recording ends after 0.8 s of silence, or 1.5 s once the user has been
  speaking for a while (pauses inside a dictated sentence are not cut off);
- nothing heard within 6 s closes the microphone; 30 s is the hard limit
  (Whisper's window);
- holding the hotkey records until it is released; Escape cancels.

Audio stays in memory; nothing is written to disk.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import queue
import threading
import time
from typing import Callable, Protocol

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME = 512  # 32 ms at 16 kHz: Silero VAD's frame size
FRAME_SECONDS = FRAME / SAMPLE_RATE


class MicrophoneUnavailable(RuntimeError):
    """No usable input device."""


@dataclass(frozen=True)
class Endpointing:
    # Phase 11 used 0.8 s / 1.5 s; the user was cut off mid-sentence in live use
    # (2026-10-24). Conversational assistants wait about 1-2 s; unfinished-sounding
    # sentences also get a short continuation window (voice/conversation.py).
    silence_seconds: float = 1.2
    long_silence_seconds: float = 2.0
    long_speech_after: float = 3.0
    no_speech_seconds: float = 6.0
    max_seconds: float = 30.0
    preroll_seconds: float = 0.3
    min_speech_seconds: float = 0.3  # 0.2 let a cough become "He" (live log 2026-10-24)
    hold_after: float = 0.4  # still holding the key this long after pressing = hold-to-talk
    start_threshold: float = 0.5
    keep_threshold: float = 0.35  # Silero's advice: a lower threshold while speech continues
    # Talking over ADVI stops its voice at once, so one stray frame must not count:
    # "the user started talking" needs about 0.1 s of continuous speech.
    onset_seconds: float = 0.09


@dataclass
class Recording:
    audio: np.ndarray
    reason: str  # "done" | "no_speech" | "cancelled"
    speech_seconds: float = 0.0

    @property
    def heard_speech(self) -> bool:
        return self.reason == "done"


class FrameSource(Protocol):
    def begin(self) -> list[np.ndarray]: ...  # start capturing; returns the pre-roll frames
    def read(self, timeout: float) -> np.ndarray | None: ...
    def end(self) -> None: ...
    def close(self) -> None: ...


class MicStream:
    """An always-open 16 kHz mono input stream with a small rolling pre-roll buffer."""

    def __init__(self, preroll_frames: int) -> None:
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, preroll_frames))
        self._frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=2000)
        self._capturing = False
        self._lock = threading.Lock()
        try:
            import sounddevice as sd

            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=FRAME, callback=self._callback
            )
            self._stream.start()
        except Exception as exc:  # no device, driver error, privacy setting off
            raise MicrophoneUnavailable(str(exc)) from exc

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("Microphone status: %s", status)
        frame = indata[:, 0].copy()
        with self._lock:
            if self._capturing:
                try:
                    self._frames.put_nowait(frame)
                except queue.Full:
                    pass
            else:
                self._preroll.append(frame)

    def begin(self) -> list[np.ndarray]:
        with self._lock:
            preroll = list(self._preroll)
            self._preroll.clear()
            while not self._frames.empty():
                self._frames.get_nowait()
            self._capturing = True
        return preroll

    def read(self, timeout: float) -> np.ndarray | None:
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def end(self) -> None:
        with self._lock:
            self._capturing = False

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            log.debug("Microphone stream close failed.", exc_info=True)


class SileroStream:
    """Silero VAD run frame by frame, keeping its state between frames."""

    def __init__(self) -> None:
        from faster_whisper.vad import get_vad_model

        self._session = get_vad_model().session
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._context = np.zeros((1, 64), dtype="float32")

    def __call__(self, frame: np.ndarray) -> float:
        frame = frame.astype("float32", copy=False)[None, :]
        out, self._h, self._c = self._session.run(
            None, {"input": np.concatenate([self._context, frame], 1), "h": self._h, "c": self._c}
        )
        self._context = frame[:, -64:]
        return float(np.ravel(out)[0])


def energy_vad(frame: np.ndarray) -> float:
    """Fallback when Silero is missing: loud frames count as speech."""
    rms = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0
    return 1.0 if rms >= 0.01 else 0.0


class Recorder:
    def __init__(
        self,
        source: FrameSource,
        *,
        vad: Callable[[np.ndarray], float] | None = None,
        rules: Endpointing = Endpointing(),
        on_speech_start: Callable[[], None] | None = None,
    ) -> None:
        self.source = source
        self.vad = vad or energy_vad
        self.rules = rules
        self.on_speech_start = on_speech_start  # the soft "I hear you" sound

    def record(
        self,
        stop: threading.Event | None = None,
        *,
        key_down: Callable[[], bool] | None = None,
        cancelled: Callable[[], bool] | None = None,
        no_speech_seconds: float | None = None,
    ) -> Recording:
        """Record until the end of speech, a key release, a cancel, or a limit."""
        rules = self.rules
        no_speech_limit = rules.no_speech_seconds if no_speech_seconds is None else no_speech_seconds
        stop = stop or threading.Event()
        reset = getattr(self.vad, "reset", None)
        if callable(reset):
            reset()

        frames = list(self.source.begin())
        for frame in frames:  # warm the VAD on the pre-roll without counting it
            self.vad(frame)
        elapsed = speech = silence = run = 0.0
        in_speech = onset = False
        hold: bool | None = None
        reason = "no_speech"
        try:
            while True:
                frame = self.source.read(timeout=0.5)
                if frame is None:
                    # No audio arrived (device stalled or the source ended): count the
                    # wait as silence, so a turn still ends instead of waiting forever.
                    step, probability = 0.5, 0.0
                else:
                    frames.append(frame)
                    step, probability = FRAME_SECONDS, self.vad(frame)
                elapsed += step
                if probability >= (rules.keep_threshold if in_speech else rules.start_threshold):
                    in_speech = True
                    speech += step
                    run += step
                    silence = 0.0
                else:
                    in_speech = False
                    run = 0.0
                    silence += step
                heard = speech >= rules.min_speech_seconds
                if not onset and run >= rules.onset_seconds:
                    onset = True
                    if self.on_speech_start is not None:
                        self.on_speech_start()

                if cancelled is not None and cancelled():
                    reason = "cancelled"
                    break
                if stop.is_set():  # the hotkey was pressed again: end now
                    reason = self._finished(speech)
                    break
                if hold is None and key_down is not None and elapsed >= rules.hold_after:
                    hold = bool(key_down())
                if hold:
                    if not key_down():
                        reason = self._finished(speech)
                        break
                else:
                    limit = rules.long_silence_seconds if speech > rules.long_speech_after else rules.silence_seconds
                    if heard and silence >= limit:
                        reason = "done"
                        break
                    if not heard and elapsed >= no_speech_limit:
                        reason = "no_speech"
                        break
                if elapsed >= rules.max_seconds:
                    reason = self._finished(speech)
                    break
        finally:
            self.source.end()
        audio = np.concatenate(frames).astype("float32") if frames else np.zeros(0, dtype="float32")
        return Recording(audio=audio, reason=reason, speech_seconds=round(speech, 2))

    def _finished(self, speech: float) -> str:
        return "done" if speech >= self.rules.min_speech_seconds else "no_speech"

    def close(self) -> None:
        self.source.close()


def preroll_frames(rules: Endpointing = Endpointing()) -> int:
    return int(rules.preroll_seconds / FRAME_SECONDS) + 1


def open_microphone(rules: Endpointing = Endpointing(), source: FrameSource | None = None) -> Recorder:
    """A recorder on ``source`` (ADVI's echo-cancelled duplex audio) or the raw default microphone."""
    source = source or MicStream(preroll_frames=preroll_frames(rules))
    try:
        vad: Callable[[np.ndarray], float] = SileroStream()
    except Exception as exc:
        log.info("Silero VAD unavailable (%s); using the energy check.", exc)
        vad = energy_vad
    return Recorder(source, vad=vad, rules=rules)


def wait_until(predicate: Callable[[], bool], timeout: float, poll: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()
