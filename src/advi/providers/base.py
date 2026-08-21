from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMResponse:
    """Normalized response returned by every LLM provider."""

    text: str
    provider: str
    model: str

    input_tokens: int | None = None
    output_tokens: int | None = None

    latency_ms: float | None = None
    finish_reason: str | None = None

    request_id: str | None = None

    rate_limit_requests_remaining: int | None = None
    rate_limit_requests_reset: str | None = None

    rate_limit_tokens_remaining: int | None = None
    rate_limit_tokens_reset: str | None = None


class LLMProvider(ABC):
    """Common interface for all ADVI language-model providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider identifier."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Active model identifier."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
    ) -> LLMResponse:
        """Send a conversation and return a normalized response."""