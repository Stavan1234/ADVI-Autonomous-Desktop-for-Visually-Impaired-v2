"""Ask a vision model what the screen shows, as words for the text-only brain.

ADVI's brain (Groq gpt-oss-120b) cannot take images. Structure (UIA, the
browser DOM, OCR) stays the primary way ADVI sees; this is the fallback for
what structure cannot tell: custom-drawn apps, a modal covering the page, a
stalled click, the final state of a page. Like Microsoft's UFO2 (UIA first,
vision only for controls UIA cannot see), and browser agents (DOM first,
screenshots when the task needs visual information).

The answer is visual evidence, not task verification, and it never carries
coordinates: actions still go through element IDs, refs, or OCR targets.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

_PROMPT = (
    "You are the eyes of a screen assistant for a blind user. Only state what is visible in this "
    "screenshot. Name the application or website and the visible title or heading. Mention anything "
    "blocking or unusual: dialogs, pop-ups, sign-in pages, CAPTCHAs or robot checks, error messages, "
    "loading spinners, empty pages. Never give pixel coordinates or positions to click. Do not guess "
    "whether the user's task succeeded; report what the screen says. If something is not legible, "
    "say so. Do not mention the screenshot or add an introduction."
)
_DESCRIBE = "Describe the screen in 2 to 5 short sentences: what it is, its main content, and the important controls."
_MAX_SIDE = 1600
_CACHE_SIZE = 16
# Free-tier quota is per model, and gemini-3.8-flash allows 20 requests a day:
# vision on the brain's model used up the brain's Gemini backup (log,
# 2026-10-23). A lite model has its own quota; 3.1-flash-lite answered in 3 s
# where 3.5-flash-lite took 11 s.
DEFAULT_VISION_MODEL = "gemini-3.1-flash-lite"
# After "busy" (503/504) the service usually stays busy for a while.
_BUSY_COOLDOWN = 120.0
_DEFAULT_QUOTA_COOLDOWN = 3600.0


def format_screen_description(text: str) -> str:
    """Normalize the model's description for readable display and speech."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"^(?:here(?:'s| is) what(?:'s| is) on (?:your|the) screen:?\s*)", "", value, flags=re.I)
    return value


def wait_for_stable_frame(
    grab: Callable[[], Any],
    *,
    timeout: float = 1.5,
    interval: float = 0.25,
) -> tuple[Any, bool]:
    """Return (frame, settled): the first frame that matches the one before it.

    Replaces a fixed sleep. A page still loading or a window still animating
    keeps changing; when it never settles the caller is told, because that is
    itself useful evidence (the "timing mismatch" case).
    """
    previous = grab()
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        time.sleep(interval)
        current = grab()
        if _frames_match(previous, current):
            return current, True
        if time.monotonic() >= deadline:
            return current, False
        previous = current


def _frames_match(a: Any, b: Any) -> bool:
    from PIL import ImageChops, ImageStat

    if a.size != b.size:
        return False
    small_a = a.convert("L").resize((96, 54))
    small_b = b.convert("L").resize((96, 54))
    # A blinking caret or a ticking clock changes a handful of pixels only.
    return ImageStat.Stat(ImageChops.difference(small_a, small_b)).mean[0] < 1.5


def _key_specific(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in ("429", "quota", "resource_exhausted", "rate limit", "api key", "401", "403", "permission"))


def _retry_seconds(exc: Exception) -> float:
    """The retry time a quota error names ("'retryDelay': '58140s'"), else an hour."""
    match = re.search(r"retryDelay\W+(\d+)(?:\.\d+)?s", str(exc))
    return float(match.group(1)) if match else _DEFAULT_QUOTA_COOLDOWN


def _plain_reason(exc: Exception | None) -> str:
    """A short reason that can be spoken, not a JSON error body."""
    text = str(exc or "").casefold()
    if any(marker in text for marker in ("429", "quota", "resource_exhausted", "rate limit")):
        return "the vision service's daily quota is used up"
    if any(marker in text for marker in ("503", "504", "unavailable", "deadline", "timed out", "timeout", "overloaded")):
        return "the vision service is busy or too slow right now"
    if any(marker in text for marker in ("api key", "401", "403", "permission")):
        return "the Gemini key was rejected"
    return "the vision service didn't answer"


def _jpeg_bytes(image: Any) -> bytes:
    image = image.convert("RGB")
    longest = max(image.size)
    if longest > _MAX_SIDE:
        scale = _MAX_SIDE / longest
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


class ScreenDescriber:
    """Gemini vision over the configured keys, one attempt per key."""

    def __init__(
        self,
        *,
        api_keys: Sequence[str],
        model: str,
        timeout_seconds: float = 20.0,
        client_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_keys = tuple(key for key in api_keys if key)
        self._clock = clock
        # Circuit breaker: no calls while the service is busy or a key's quota is spent.
        self._busy_until = 0.0
        self._key_off_until: dict[str, float] = {}
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory or self._make_client
        self._clients: dict[str, Any] = {}
        self._cache: OrderedDict[str, str] = OrderedDict()

    @property
    def available(self) -> bool:
        return bool(self.api_keys and self.model)

    def _make_client(self, api_key: str) -> Any:
        from google import genai
        from google.genai import types

        http_options = types.HttpOptions(
            timeout=int(self.timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        return genai.Client(api_key=api_key, http_options=http_options)

    def describe(self, image: Any, question: str | None = None, *, window_title: str | None = None) -> tuple[str, bool]:
        """Return (answer, cached). Raises RuntimeError when every key fails.

        window_title is a fact from Windows, not a guess: the lite model called
        the "Claude" window "Cursor" until it was told the title (2026-10-23).
        """
        if not self.available:
            raise RuntimeError("No Gemini API key is configured for vision.")
        data = _jpeg_bytes(image)
        question = " ".join(str(question or "").split())
        title = " ".join(str(window_title or "").split())[:120]
        key = hashlib.sha256(data + b"\0" + question.encode("utf-8") + b"\0" + title.encode("utf-8")).hexdigest()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key], True

        ask = f"Question: {question}\nAnswer it directly from what is visible, in 1 to 4 sentences." if question else _DESCRIBE
        if title:
            ask = f"Windows reports this window's title as: {title!r}. Use it to name the app.\n{ask}"
        now = self._clock()
        if now < self._busy_until:
            raise RuntimeError("the vision service is busy or too slow right now")
        keys = [k for k in self.api_keys if now >= self._key_off_until.get(k, 0.0)]
        if not keys:
            raise RuntimeError("the vision service's daily quota is used up")
        last_error: Exception | None = None
        for index, api_key in enumerate(keys):
            try:
                answer = self._ask(api_key, data, f"{_PROMPT}\n\n{ask}")
            except Exception as exc:
                last_error = exc
                # Expected service trouble: log it, don't print it on the user's console.
                logger.info("Vision request failed on Gemini key %d: %s", index + 1, str(exc)[:300])
                if _key_specific(exc):
                    self._key_off_until[api_key] = self._clock() + _retry_seconds(exc)
                    continue  # quota or a bad key: the next key may work
                # Busy or timed out (503/504) is the service, not the key; three
                # 20 s waits would leave the user in silence for a minute.
                self._busy_until = self._clock() + _BUSY_COOLDOWN
                break
            answer = format_screen_description(answer)
            if not answer:
                last_error = RuntimeError("The vision model returned an empty answer.")
                continue
            self._cache[key] = answer
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
            return answer, False
        raise RuntimeError(_plain_reason(last_error))

    def _ask(self, api_key: str, data: bytes, prompt: str) -> str:
        from google.genai import types

        client = self._clients.get(api_key)
        if client is None:
            client = self._clients[api_key] = self._client_factory(api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=[prompt, types.Part.from_bytes(data=data, mime_type="image/jpeg")],
            # Describing a screen needs little reasoning; low thinking is faster.
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="low"),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        return str(getattr(response, "text", "") or "").strip()


def describer_from_settings() -> ScreenDescriber | None:
    import os

    from advi.core.config import load_settings

    if os.getenv("ADVI_VISION", "on").strip().casefold() in {"0", "off", "false", "no"}:
        return None
    try:
        settings = load_settings()
    except Exception as exc:
        logger.warning("Vision is unavailable because settings did not load: %s", exc)
        return None
    model = os.getenv("ADVI_VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL
    describer = ScreenDescriber(api_keys=settings.gemini_api_keys, model=model)
    return describer if describer.available else None
