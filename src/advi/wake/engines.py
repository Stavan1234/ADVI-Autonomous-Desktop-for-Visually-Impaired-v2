"""Wake-word detection engines.

1. ``WhisperPhraseDetector`` (default, nothing to sign up for): the two-stage
   design used by on-device assistants. A cheap always-on stage (Silero VAD)
   finds short utterances; only those are checked by an accurate second stage
   (local Whisper tiny.en biased to "ADVI", then phrase.is_wake_phrase).
   Measured 2026-10-25 on Windows' voices: 12/12 wake phrases, 1/34 false
   (the adversarial "Look at V"), about 0.4 s per check.

2. ``PorcupineDetector`` (optional): Picovoice Porcupine, a dedicated keyword
   spotter that checks every 32 ms frame with very low CPU. It needs the user's
   free Picovoice AccessKey and an "ADVI" keyword file (.ppn) trained in the
   Picovoice Console. It is used when both are configured, and ADVI falls back to
   (1) if Porcupine can't start.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class PorcupineDetector:
    """Porcupine on 512-sample, 16 kHz frames (the same frames ADVI's microphone gives)."""

    def __init__(self, access_key: str, keyword_path: Path, sensitivity: float = 0.6, factory: Any = None) -> None:
        if factory is None:
            import pvporcupine

            factory = pvporcupine.create
        self._engine = factory(access_key=access_key, keyword_paths=[str(keyword_path)], sensitivities=[sensitivity])
        self.frame_length = int(getattr(self._engine, "frame_length", 512))
        self._pending = np.zeros(0, dtype="float32")

    def process(self, frame: np.ndarray) -> bool:
        self._pending = np.concatenate([self._pending, np.asarray(frame, dtype="float32")])
        found = False
        while self._pending.size >= self.frame_length:
            chunk, self._pending = self._pending[:self.frame_length], self._pending[self.frame_length:]
            pcm = (np.clip(chunk, -1.0, 1.0) * 32767).astype("<i2")
            found = self._engine.process(pcm.tolist()) >= 0 or found
        return found

    def close(self) -> None:
        delete = getattr(self._engine, "delete", None)
        if callable(delete):
            delete()


def keyword_file(asset_root: Path) -> Path | None:
    configured = os.getenv("ADVI_WAKE_KEYWORD_FILE", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    found = sorted((asset_root / "wake").glob("*.ppn")) if (asset_root / "wake").is_dir() else []
    return found[0] if found else None


def porcupine_from_env(asset_root: Path) -> PorcupineDetector | None:
    """Porcupine when PICOVOICE_ACCESS_KEY and a keyword file are configured; None otherwise."""
    key = os.getenv("PICOVOICE_ACCESS_KEY", "").strip()
    path = keyword_file(asset_root)
    if not key or path is None:
        return None
    try:
        sensitivity = float(os.getenv("ADVI_WAKE_SENSITIVITY", "0.6"))
    except ValueError:
        sensitivity = 0.6
    try:
        detector = PorcupineDetector(key, path, sensitivity=min(1.0, max(0.0, sensitivity)))
    except Exception as exc:
        log.warning("Porcupine could not start (%s); using the built-in wake phrase detector.", exc)
        return None
    log.info("Wake engine: Porcupine with %s.", path.name)
    return detector
