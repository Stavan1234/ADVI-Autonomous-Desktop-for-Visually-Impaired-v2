from __future__ import annotations

from collections import defaultdict
import threading
import time
from typing import Iterable

from advi.providers import LLMProvider, LLMResponse
from advi.providers.errors import LLMAuthenticationError, LLMRateLimitError, LLMTimeoutError, LLMUnavailableError


class DavidProviderBridge(LLMProvider):
    """Transport-only bridge with health-aware provider rotation for David.

    David remains an isolated fallback subsystem. This bridge deliberately does
    not add planning or reasoning; it only avoids retrying providers that the
    main runtime has already observed as rate-limited or unavailable.
    """

    def __init__(self, providers: Iterable[LLMProvider]) -> None:
        self.providers = tuple(p for p in providers if p is not None)
        if not self.providers:
            raise ValueError("David fallback requires at least one configured provider.")
        self._lock = threading.Lock()
        self._blocked_until: dict[str, float] = defaultdict(float)
        self._cursor = 0

    @property
    def name(self) -> str:
        return "david-provider-bridge"

    @property
    def model(self) -> str:
        return self.providers[0].model

    @property
    def quota_group(self) -> str | None:
        return None

    def _group(self, provider: LLMProvider) -> str:
        return provider.quota_group or provider.name

    def _mark_blocked(self, provider: LLMProvider, exc: Exception) -> None:
        wait = getattr(exc, "retry_after_seconds", None)
        try:
            seconds = float(wait) if wait is not None else 60.0
        except (TypeError, ValueError):
            seconds = 60.0
        seconds = max(5.0, min(seconds, 3600.0))
        if isinstance(exc, LLMRateLimitError) and getattr(exc, "quota_scope", None) == "project_daily":
            seconds = max(seconds, 300.0)
        self._blocked_until[self._group(provider)] = time.monotonic() + seconds

    def _candidate_indices(self) -> list[int]:
        now = time.monotonic()
        with self._lock:
            n = len(self.providers)
            ordered = [(self._cursor + offset) % n for offset in range(n)]
            return [i for i in ordered if self._blocked_until.get(self._group(self.providers[i]), 0.0) <= now]

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "fallback",
    ) -> LLMResponse:
        last_error: Exception | None = None
        candidates = self._candidate_indices()
        if not candidates:
            # All providers are currently blocked. Raising the last concrete
            # provider error is more truthful than generating a synthetic success.
            raise LLMRateLimitError("No configured provider is currently available for David fallback.")

        for index in candidates:
            provider = self.providers[index]
            try:
                result = provider.chat(messages, purpose=purpose)
                with self._lock:
                    self._cursor = (index + 1) % len(self.providers)
                return result
            except (LLMAuthenticationError, LLMRateLimitError, LLMTimeoutError, LLMUnavailableError) as exc:
                last_error = exc
                with self._lock:
                    self._mark_blocked(provider, exc)
                continue
            except Exception as exc:
                # David should not swallow unexpected provider defects, but trying
                # the next configured provider is still useful for a fallback path.
                last_error = exc
                with self._lock:
                    self._mark_blocked(provider, exc)

        raise last_error or RuntimeError("No configured provider could serve David fallback.")
