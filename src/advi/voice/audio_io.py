"""One audio stream for everything ADVI says and hears, so it can cancel its own voice.

Why (fourth live session, 2026-10-25): ADVI heard its own sentences and answered
them ("Everything is ready, sir. I'm listening..." came back as a request;
"Backslash, backslash..." and "The protein works solid." were its own voice,
misheard). Matching the transcript against what ADVI said can't work when the
echo is misheard.

Voice assistants solve this with acoustic echo cancellation (AEC): the app knows
exactly what it is playing, and an adaptive filter subtracts that from the
microphone (WebRTC AEC3 in Chrome and Meet, iOS voice processing, Android's
AcousticEchoCanceler, smart speakers' hardware AEC). Phase 12 tried echoff,
whose reference was Windows' loopback; the loopback delivers nothing while the PC
is silent, so listening stalled. Here ADVI owns its playback instead:

- speech and sounds are mixed into one duplex sounddevice stream; the same
  callback receives the microphone block recorded while that output block played,
  so the reference and the microphone stay aligned;
- WebRTC's audio processing module (AEC3, noise suppression and a high-pass
  filter; from the ``livekit`` package) removes ADVI's voice from the microphone;
- the cleaned 16 kHz audio feeds the recorder (Silero VAD, then Whisper).

Measured on the dev laptop (2026-10-25), 29 s of SAPI speech through its
speakers with nobody talking: the raw microphone had 53 speech frames and Whisper
transcribed ADVI's sentences; after AEC, 0 speech frames and 0 utterances. Speech
added on top of ADVI's voice kept 37-59 speech frames, so the user can still
interrupt.

Without ``livekit`` (or if the duplex stream can't open) the microphone is
used raw and the transcript echo check in conversation.py is the only guard.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from advi.voice.recorder import FRAME, MicrophoneUnavailable, SAMPLE_RATE

log = logging.getLogger(__name__)

BLOCK = 480  # 30 ms per callback
APM_FRAME = 160  # WebRTC processes 10 ms frames
TAIL_SECONDS = 0.25  # the room keeps ringing a little after the last sample


def resample(samples: np.ndarray, rate: int, target: int = SAMPLE_RATE) -> np.ndarray:
    samples = np.asarray(samples, dtype="float32").reshape(-1)
    if rate == target or samples.size == 0:
        return samples
    try:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(rate), int(target))
        return resample_poly(samples, target // g, rate // g).astype("float32")
    except Exception:
        positions = np.arange(0, samples.size, rate / target)
        return np.interp(positions, np.arange(samples.size), samples).astype("float32")


@dataclass
class Sound:
    samples: np.ndarray
    kind: str
    position: int = 0
    finished: threading.Event = field(default_factory=threading.Event)

    def wait(self, timeout: float | None = None) -> bool:
        # A paused sound waits too (the user may talk for a while), so the default is
        # generous; stop() and close() always end it.
        if timeout is None:
            timeout = self.samples.size / SAMPLE_RATE + 300.0
        return self.finished.wait(timeout)


class EchoCanceller:
    """WebRTC AEC3 + noise suppression on 10 ms frames, through LiveKit's APM."""

    def __init__(self, delay_ms: int) -> None:
        from livekit import rtc

        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)
        self.delay_ms = int(delay_ms)

    def _frame(self, samples: np.ndarray):
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        return self._rtc.AudioFrame(bytearray(pcm.tobytes()), SAMPLE_RATE, 1, samples.size)

    def process(self, played: np.ndarray, mic: np.ndarray) -> np.ndarray:
        out = np.empty_like(mic)
        for start in range(0, mic.size, APM_FRAME):
            reference = self._frame(played[start:start + APM_FRAME])
            self._apm.process_reverse_stream(reference)
            near = self._frame(mic[start:start + APM_FRAME])
            self._apm.set_stream_delay_ms(self.delay_ms)
            self._apm.process_stream(near)
            out[start:start + APM_FRAME] = np.frombuffer(bytes(near.data), dtype="<i2").astype("float32") / 32768
        return out


class DuplexAudio:
    """Plays ADVI's speech and sounds, and gives the recorder echo-cancelled microphone frames.

    Implements the recorder's FrameSource (begin / read / end / close).
    """

    def __init__(
        self,
        *,
        preroll_frames: int = 10,
        echo_cancel: bool = True,
        stream_factory: Callable[..., Any] | None = None,
        canceller_factory: Callable[[int], Any] | None = None,
    ) -> None:
        self._sounds: list[Sound] = []
        self._paused: set[str] = set()
        self._sound_lock = threading.Lock()
        self._sounding_until: dict[str, float] = {}
        self._pairs: queue.Queue[tuple[np.ndarray, np.ndarray]] = queue.Queue(maxsize=400)
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, preroll_frames))
        self._frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=2000)
        self._pending = np.zeros(0, dtype="float32")
        self._capturing = False
        self._capture_lock = threading.Lock()
        self._closed = threading.Event()
        try:
            if stream_factory is None:
                import sounddevice as sd

                stream_factory = sd.Stream
            self._stream = stream_factory(
                samplerate=SAMPLE_RATE, blocksize=BLOCK, channels=1, dtype="float32", callback=self._callback
            )
            latency = getattr(self._stream, "latency", (0.03, 0.18))
            self.input_latency, self.output_latency = (float(latency[0]), float(latency[1])) \
                if isinstance(latency, (tuple, list)) else (float(latency), float(latency))
        except Exception as exc:
            raise MicrophoneUnavailable(str(exc)) from exc
        self.canceller: Any = None
        if echo_cancel:
            delay_ms = int(round((self.input_latency + self.output_latency) * 1000))
            try:
                self.canceller = (canceller_factory or EchoCanceller)(delay_ms)
            except Exception as exc:  # livekit missing or its native library failed
                log.warning("Echo cancellation unavailable (%s); the microphone is used as is.", exc)
        self._worker = threading.Thread(target=self._process_loop, name="advi-audio-aec", daemon=True)
        self._worker.start()
        self._stream.start()
        log.info("Duplex audio open: latency in %.0f ms, out %.0f ms, echo cancellation %s.",
                 self.input_latency * 1000, self.output_latency * 1000, "on" if self.echo_cancelling else "off")

    @property
    def echo_cancelling(self) -> bool:
        return self.canceller is not None

    # --- playback ------------------------------------------------------------------

    def play(self, samples: np.ndarray, rate: int, *, kind: str = "speech") -> Sound:
        sound = Sound(resample(samples, rate), kind)
        if self._closed.is_set() or sound.samples.size == 0:
            sound.finished.set()
            return sound
        with self._sound_lock:
            self._sounds.append(sound)
        return sound

    def play_earcon(self, samples: np.ndarray, rate: int) -> None:
        self.play(samples, rate, kind="earcon")

    def pause(self, kind: str = "speech") -> None:
        """Hold sounds of this kind where they are (the user may be talking); resume() continues them."""
        with self._sound_lock:
            self._paused.add(kind)

    def resume(self, kind: str = "speech") -> None:
        with self._sound_lock:
            self._paused.discard(kind)

    def paused(self, kind: str = "speech") -> bool:
        with self._sound_lock:
            return kind in self._paused

    def stop(self, kind: str | None = None) -> None:
        with self._sound_lock:
            if kind is None:
                self._paused.clear()
            else:
                self._paused.discard(kind)
            for sound in self._sounds:
                if kind is None or sound.kind == kind:
                    sound.position = sound.samples.size
                    sound.finished.set()
            self._sounds = [s for s in self._sounds if not s.finished.is_set()]

    def playing(self, kind: str | None = None) -> bool:
        """True while a sound of this kind plays, including the time it takes to leave the speakers."""
        now = time.monotonic()
        with self._sound_lock:
            if any(kind is None or s.kind == kind for s in self._sounds):
                return True
            if kind is None:
                return any(until > now for until in self._sounding_until.values())
            return self._sounding_until.get(kind, 0.0) > now

    def _mix(self, frames: int) -> np.ndarray:
        out = np.zeros(frames, dtype="float32")
        with self._sound_lock:
            if not self._sounds:
                return out
            now = time.monotonic()
            for sound in self._sounds:
                if sound.kind in self._paused:
                    continue
                chunk = sound.samples[sound.position:sound.position + frames]
                out[:chunk.size] += chunk
                sound.position += chunk.size
                self._sounding_until[sound.kind] = now + frames / SAMPLE_RATE + self.output_latency + TAIL_SECONDS
                if sound.position >= sound.samples.size:
                    sound.finished.set()
            self._sounds = [s for s in self._sounds if not s.finished.is_set()]
        return np.clip(out, -1.0, 1.0)

    # --- the stream callback and echo cancellation ----------------------------------------

    def _callback(self, indata, outdata, frames, time_info, status) -> None:
        if status:
            log.debug("Audio stream status: %s", status)
        played = self._mix(frames)
        outdata[:, 0] = played
        try:
            self._pairs.put_nowait((played, indata[:, 0].copy()))
        except queue.Full:
            pass

    def _process_loop(self) -> None:
        while not self._closed.is_set():
            try:
                played, mic = self._pairs.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.canceller is not None:
                try:
                    mic = self.canceller.process(played, mic)
                except Exception:
                    log.exception("Echo cancellation failed; continuing without it.")
                    self.canceller = None
            self._deliver(mic)

    def _deliver(self, audio: np.ndarray) -> None:
        """Re-cut the cleaned audio into Silero's 512-sample frames for the recorder."""
        self._pending = np.concatenate([self._pending, audio])
        while self._pending.size >= FRAME:
            frame, self._pending = self._pending[:FRAME].copy(), self._pending[FRAME:]
            with self._capture_lock:
                if self._capturing:
                    try:
                        self._frames.put_nowait(frame)
                    except queue.Full:
                        pass
                else:
                    self._preroll.append(frame)

    # --- FrameSource for the recorder ---------------------------------------------------

    def capture_source(self) -> "CaptureView":
        """The microphone side for a recorder; closing the recorder leaves speech output running."""
        return CaptureView(self)

    def begin(self) -> list[np.ndarray]:
        with self._capture_lock:
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
        with self._capture_lock:
            self._capturing = False

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.stop()
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            log.debug("Audio stream close failed.", exc_info=True)


class CaptureView:
    """FrameSource over a DuplexAudio whose close() doesn't close the shared stream (the app owns it)."""

    def __init__(self, audio: DuplexAudio) -> None:
        self._audio = audio

    def begin(self) -> list[np.ndarray]:
        return self._audio.begin()

    def read(self, timeout: float) -> np.ndarray | None:
        return self._audio.read(timeout)

    def end(self) -> None:
        self._audio.end()

    def close(self) -> None:
        self._audio.end()
