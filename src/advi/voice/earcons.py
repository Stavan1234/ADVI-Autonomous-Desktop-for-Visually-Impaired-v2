"""ADVI's sounds: one soft family, so a blind user always knows whose turn it is.

Design (Google's earcon guidance: few sounds, brief, distinct, consistent, and
taught; plus what screen-reader users are used to):
- one timbre for everything: a soft marimba/glass tone (a sine with gently
  decaying overtones and a short room-like tail), never a beep;
- one musical scale (C major pentatonic), so no two sounds clash;
- rising = "over to you", falling = "got it / done / stopped", low and slow =
  "I'm working";
- quiet: these sit under speech, they never compete with it.

The sounds, in the order of a conversation:
  your_turn  rising "ti-ting" (C6 -> G6): I'm ready, talk now;
  heard      a tiny soft tick: I can hear you (when you start talking);
  got_it     falling two-note (G5 -> E5): got it, working on it;
  working    a slow, quiet "breath" (low C, fades in and out): still working;
  stopped    short falling minor third (E5 -> C5): stopped / interrupted;
  not_heard  two soft low notes (D5, D5): I didn't catch that;
  wake       rising arpeggio (C -> E -> G -> C): I'm awake and listening;
  sleep      falling arpeggio (C -> G -> E -> C): I've stopped listening.

"What do the sounds mean?" plays each one with its meaning (GUIDE).

Sounds are made in memory and played through sounddevice, not winsound:
winsound has one global player, so a chime would cut off ADVI's own speech.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

RATE = 24_000
C4, D4, E4, G4, A4 = 261.63, 293.66, 329.63, 392.00, 440.00
C5, D5, E5, G5, A5 = 523.25, 587.33, 659.25, 783.99, 880.00
C6, E6, G6 = 1046.50, 1318.51, 1567.98

# name: ((frequency, start_ms, length_ms), ...), volume, kind
_SOUNDS: dict[str, tuple[tuple[tuple[float, int, int], ...], float, str]] = {
    "your_turn": (((C6, 0, 220), (G6, 90, 420)), 0.16, "bell"),
    "heard": (((A5, 0, 70),), 0.06, "tick"),
    "got_it": (((G5, 0, 160), (E5, 85, 300)), 0.13, "bell"),
    "working": (((C4, 0, 1100), (G4, 0, 1100)), 0.05, "breath"),
    "stopped": (((E5, 0, 150), (C5, 80, 320)), 0.13, "bell"),
    "not_heard": (((D5, 0, 150), (D5, 210, 260)), 0.12, "bell"),
    "wake": (((C5, 0, 260), (E5, 90, 260), (G5, 180, 260), (C6, 270, 520)), 0.15, "bell"),
    "sleep": (((C6, 0, 260), (G5, 110, 260), (E5, 220, 260), (C5, 330, 620)), 0.14, "bell"),
}

GUIDE = (
    ("your_turn", "This sound means I'm ready. It's your turn to talk."),
    ("heard", "This soft tick means I can hear you speaking."),
    ("got_it", "This means I got what you said and I'm working on it."),
    ("working", "This slow breathing sound means I'm still working."),
    ("stopped", "This means I stopped, because you interrupted me or asked me to."),
    ("not_heard", "This means I heard something but couldn't understand it. Please say it again."),
    ("wake", "This means I'm awake and listening."),
    ("sleep", "And this means I've gone to sleep and stopped listening."),
)


def _voice(frequency: float, length_ms: int, kind: str) -> np.ndarray:
    n = int(RATE * length_ms / 1000)
    t = np.arange(n) / RATE
    if kind == "breath":
        # Fades in and out over its whole length, like a slow breath.
        envelope = np.sin(np.pi * t / max(t[-1], 1e-6)) ** 2
        wave = np.sin(2 * np.pi * frequency * t) + 0.15 * np.sin(4 * np.pi * frequency * t)
        return (wave * envelope).astype("float32")
    attack = np.minimum(1.0, t / (0.003 if kind == "tick" else 0.006))
    decay = np.exp(-t / (0.025 if kind == "tick" else max(0.08, length_ms / 1000 / 3.0)))
    # Marimba/glass: overtones that fade faster than the fundamental, so it is round, not shrill.
    wave = (np.sin(2 * np.pi * frequency * t)
            + 0.30 * np.exp(-t / 0.06) * np.sin(2 * np.pi * 2 * frequency * t)
            + 0.12 * np.exp(-t / 0.03) * np.sin(2 * np.pi * 3 * frequency * t))
    return (wave * attack * decay).astype("float32")


def _room(sound: np.ndarray) -> np.ndarray:
    """A short, soft echo tail so the sound feels warm instead of dry."""
    out = np.concatenate([sound, np.zeros(int(RATE * 0.25), dtype="float32")])
    for delay_ms, gain in ((37, 0.22), (71, 0.14), (113, 0.08)):
        d = int(RATE * delay_ms / 1000)
        out[d:] += gain * out[:-d].copy() if d < out.size else 0
    return out


def synthesize(name: str) -> np.ndarray:
    notes, volume, kind = _SOUNDS[name]
    total = max(start + length for _, start, length in notes)
    mix = np.zeros(int(RATE * total / 1000) + 1, dtype="float32")
    for frequency, start, length in notes:
        tone = _voice(frequency, length, kind)
        offset = int(RATE * start / 1000)
        mix[offset:offset + tone.size] += tone
    if kind != "breath":
        mix = _room(mix)
    peak = float(np.max(np.abs(mix))) or 1.0
    sound = mix / peak * volume
    # Fade the last 10 ms so it never ends with a click.
    fade = min(sound.size, int(RATE * 0.01))
    sound[-fade:] *= np.linspace(1.0, 0.0, fade, dtype="float32")
    return sound.astype("float32")


class Earcons:
    def __init__(self, enabled: bool = True, player=None) -> None:
        self.enabled = enabled
        self._player = player
        self._cache: dict[str, np.ndarray] = {}
        self._playing_until = 0.0
        self._lock = threading.Lock()

    @property
    def playing(self) -> bool:
        return time.monotonic() < self._playing_until

    def play(self, name: str, *, wait: bool = False) -> None:
        if not self.enabled or name not in _SOUNDS:
            return
        sound = self._cache.get(name)
        if sound is None:
            sound = self._cache[name] = synthesize(name)
        seconds = sound.size / RATE
        with self._lock:
            self._playing_until = max(self._playing_until, time.monotonic()) + seconds
        try:
            if self._player is not None:
                self._player(sound, RATE)
            else:
                import sounddevice as sd

                sd.play(sound, RATE)
            if wait:
                time.sleep(seconds)
        except Exception:
            log.debug("Earcon %s could not play.", name, exc_info=True)
