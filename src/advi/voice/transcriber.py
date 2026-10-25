"""Speech-to-text: Whisper Turbo on Groq when online, local Whisper otherwise.

- Online: Groq's free whisper-large-v3-turbo answers in about 0.3-0.5 s, and
  its audio quota is separate from the brain's (it worked on 2026-10-24 while
  the gpt-oss quota was used up).
- Offline or any cloud problem: the local model, which is always loaded at
  startup, so falling back never waits for a model to load.
- ADVI tells the user once when it switches to the local model (it is slower)
  and remembers why, so "why are you slow?" has a real answer.

Whisper invents text on silence or noise ("Thank you.", "Thanks for
watching"), so segments it is not confident about are dropped, online and
offline alike.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

log = logging.getLogger(__name__)

# Measured on the dev laptop (i5-8265U, no GPU), one spoken command, int8 (2026-10-24):
# large-v3-turbo 12-15 s, small.en ~3 s, distil-small.en ~2.8 s, base.en ~1 s.
# Whisper always encodes a 30 s window, so short commands cost the same as long ones.
# distil-small.en was the fastest model that got names right ("Stavan").
DEFAULT_STT_MODEL = "distil-small.en"
DEFAULT_CLOUD_STT_MODEL = "whisper-large-v3-turbo"
# No spelling-hint prompt by default: a comma-separated word list made
# distil-small.en loop ("Open, Note, Open, Note, ...") and drop the end of a long
# command (2026-10-24), while no prompt transcribed all test commands exactly.
# A hint, if ever needed, must be a natural sentence.
VOCABULARY = ""
# Whole transcripts Whisper produces on silence or noise.
_STOCK_PHRASES = {
    "thank you", "thank you very much", "thanks for watching", "thank you for watching",
    "thanks for listening", "please subscribe", "subscribe", "you", "so",
}


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.casefold()).strip()


def _field(segment: Any, name: str) -> Any:
    return segment.get(name) if isinstance(segment, dict) else getattr(segment, name, None)


def _confident(segment: Any) -> bool:
    no_speech = float(_field(segment, "no_speech_prob") or 0.0)
    logprob = float(_field(segment, "avg_logprob") or 0.0)
    # Whisper's own rule pairs both signals; a very low log-probability alone is noise.
    if no_speech > 0.6 and logprob < -0.7:
        return False
    return logprob >= -1.5


def keep_confident_text(segments: Sequence[Any]) -> str:
    """Join the segments Whisper is sure about; '' for silence-style hallucinations."""
    kept = [str(_field(s, "text") or "").strip() for s in segments if _confident(s)]
    text = " ".join(t for t in kept if t).strip()
    if _plain(text) in _STOCK_PHRASES or not _plain(text):
        if text:
            log.info("Dropped a likely Whisper hallucination: %r", text)
        return ""
    return text


def wav_bytes(audio: np.ndarray, sample_rate: int = 16_000) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


class LocalTranscriber:
    def __init__(
        self,
        model_name: str = DEFAULT_STT_MODEL,
        *,
        download_root: Path | None = None,
        language: str | None = "en",
        vocabulary: str = VOCABULARY,
        compute_type: str = "int8",
        model_factory: Callable[..., Any] | None = None,
        hotwords: str | None = None,
    ) -> None:
        self.model_name = model_name
        # Biases recognition towards these words: the wake listener passes "ADVI"
        # (tiny.en found "wake up ADVI" 12/12 with it, 8/12 without; 2026-10-25).
        self.hotwords = hotwords
        self.download_root = download_root
        self.language = language or None
        self.vocabulary = vocabulary
        self.compute_type = compute_type
        self._factory = model_factory
        self._model: Any = None
        self._lock = threading.Lock()

    def preload(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            root = str(self.download_root) if self.download_root else None
            factory = self._factory
            if factory is None:
                from faster_whisper import WhisperModel as factory  # noqa: N813
            # Local files first: offline, a download attempt would only add a long wait.
            for local_only in (True, False):
                try:
                    self._model = factory(
                        self.model_name, device="cpu", compute_type=self.compute_type,
                        download_root=root, local_files_only=local_only,
                    )
                    log.info("Speech model %s loaded.", self.model_name)
                    return True
                except Exception as exc:
                    log.info("Speech model %s not loaded (local_only=%s): %s", self.model_name, local_only, exc)
            return False

    @property
    def ready(self) -> bool:
        return self._model is not None

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0 or not self.preload():
            return ""
        with self._lock:
            segments, _ = self._model.transcribe(
                audio.astype("float32", copy=False),
                language=self.language,
                beam_size=1,
                temperature=0.0,
                condition_on_previous_text=False,
                without_timestamps=True,
                initial_prompt=self.vocabulary or None,
                **({"hotwords": self.hotwords} if self.hotwords else {}),
            )
            segments = list(segments)
        return keep_confident_text(segments)


class CloudUnavailable(RuntimeError):
    """Every key failed; ``reason`` is a plain sentence fragment for the user."""

    def __init__(self, reason: str, retry_after: float = 60.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def _cloud_reason(exc: Exception) -> tuple[str, float]:
    """(plain reason, seconds before trying the cloud again)."""
    name = type(exc).__name__
    if name in {"APITimeoutError", "TimeoutError", "ReadTimeout"}:
        return "the online speech service didn't answer in time", 30.0
    if name in {"APIConnectionError", "ConnectError", "ConnectionError", "OSError"}:
        return "there's no internet connection", 20.0
    if name == "RateLimitError" or getattr(exc, "status_code", None) == 429:
        return "the free online speech limit is used up for now", 300.0
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return "the online speech service rejected the API key", 3600.0
    return "the online speech service had a problem", 60.0


class CloudTranscriber:
    """Groq's hosted Whisper, trying each configured key once per command."""

    def __init__(
        self,
        api_keys: Sequence[str],
        *,
        model: str = DEFAULT_CLOUD_STT_MODEL,
        language: str | None = "en",
        timeout_seconds: float = 5.0,
        client_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_keys = [k for k in api_keys if k]
        self.model = model
        self.language = language or None
        self.timeout_seconds = timeout_seconds
        self._factory = client_factory or self._groq_client
        self._clients: dict[str, Any] = {}
        self._key_off_until: dict[str, float] = {}
        self.clock = clock

    def _groq_client(self, key: str) -> Any:
        from groq import Groq

        return Groq(api_key=key, timeout=self.timeout_seconds, max_retries=0)

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        payload = wav_bytes(audio)
        reasons: list[tuple[str, float]] = []
        for key in self.api_keys:
            if self._key_off_until.get(key, 0.0) > self.clock():
                continue
            try:
                client = self._clients.setdefault(key, self._factory(key))
                response = client.audio.transcriptions.create(
                    file=("speech.wav", payload), model=self.model, language=self.language,
                    response_format="verbose_json", temperature=0.0,
                )
            except Exception as exc:
                reason, wait = _cloud_reason(exc)
                log.info("Cloud speech failed on one key: %s (%s)", reason, type(exc).__name__)
                reasons.append((reason, wait))
                if reason.startswith("there's no internet") or "in time" in reason:
                    break  # another key won't help with the network
                self._key_off_until[key] = self.clock() + wait
                continue
            segments = getattr(response, "segments", None)
            if segments is None:
                segments = [{"text": getattr(response, "text", "")}]
            return keep_confident_text(segments)
        if not reasons:
            raise CloudUnavailable("the free online speech limit is used up for now", 300.0)
        reason, wait = reasons[-1]
        raise CloudUnavailable(reason, min(w for _, w in reasons))


class DualTranscriber:
    """Cloud Turbo first; the always-loaded local model whenever the cloud can't answer."""

    def __init__(
        self,
        local: LocalTranscriber,
        cloud: CloudTranscriber | None = None,
        *,
        notify: Callable[[str], Any] = lambda text: None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.local = local
        self.cloud = cloud
        self.notify = notify
        self.clock = clock
        self.wall_clock = wall_clock
        self.mode = "cloud" if cloud is not None else "local"
        self.reason: str | None = None if cloud is not None else "online speech recognition is turned off"
        self.since: float | None = None
        self.last_seconds: dict[str, float] = {}
        self._cloud_off_until = 0.0
        self._told_user = False

    def preload(self) -> bool:
        # The local model loads even when the cloud works, so a dropped connection never
        # means waiting for a model; that is what keeps offline voice at full quality.
        return self.local.preload()

    @property
    def ready(self) -> bool:
        return self.local.ready or self.cloud is not None

    def transcribe(self, audio: np.ndarray) -> str:
        if self.cloud is not None and self.clock() >= self._cloud_off_until:
            began = self.clock()
            try:
                text = self.cloud.transcribe(audio)
            except CloudUnavailable as exc:
                self._cloud_off_until = self.clock() + exc.retry_after
                self._switch_to_local(exc.reason)
            else:
                self.last_seconds["cloud"] = round(self.clock() - began, 2)
                if self.mode != "cloud":
                    log.info("Online speech recognition is back.")
                self.mode, self.reason, self.since, self._told_user = "cloud", None, None, False
                return text
        if not self.local.ready and not self.local.preload():
            return ""
        began = self.clock()
        text = self.local.transcribe(audio)
        self.last_seconds["local"] = round(self.clock() - began, 2)
        return text

    def _switch_to_local(self, reason: str) -> None:
        if self.mode != "local" or self.reason != reason:
            log.info("Speech recognition switched to the local model: %s", reason)
            self.since = self.since or self.wall_clock()
        self.mode, self.reason = "local", reason
        if not self._told_user:
            self._told_user = True
            self.notify(
                f"I'm understanding your voice offline right now, because {reason}, "
                "so I may be a little slower. Thanks for your patience."
            )

    def status_sentence(self) -> str | None:
        """Why voice may be slow, or None when online recognition is working."""
        if self.mode != "local":
            return None
        took = self.last_seconds.get("local")
        speed = f" Understanding each command takes about {took:.0f} seconds this way." if took else ""
        since = time.strftime(" since %H:%M", time.localtime(self.since)) if self.since else ""
        if self.cloud is None:
            return f"I'm understanding your voice with the offline model on this computer, which is slower than the online one.{speed}"
        return (
            f"I'm understanding your voice with the offline model on this computer{since}, because {self.reason}. "
            f"It's slower than the online one, which takes under a second.{speed}"
        )
