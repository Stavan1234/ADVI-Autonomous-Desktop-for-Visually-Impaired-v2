from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderHealthState:
    """Shared in-process provider health and quota state."""

    def __init__(self) -> None:
        self.cooldown_until: dict[str, float] = {}
        self.quota_exhausted_until: dict[str, float] = {}



@dataclass(frozen=True)
class LLMToolCall:
    """Provider-neutral normalized function/tool call."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


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

    tool_calls: tuple[LLMToolCall, ...] = ()

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
        *,
        purpose: str = "chat",
    ) -> LLMResponse:
        """Send a conversation and return a normalized response."""

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        purpose: str = "agent",
    ) -> LLMResponse:
        """Send a tool-aware request. Providers should override when supported."""
        return self.chat(messages, purpose=purpose)

    def create_native_session(
        self,
        *,
        system_instruction: str,
        tools: list[dict[str, object]],
        history: list[dict[str, object]] | None = None,
    ):
        """Return a provider-owned stateful session when supported; otherwise None.

        ``history`` is intentionally optional and bounded. It is used only for
        explicit provider handoff/initialization, not as a transcript replay path.
        """
        return None

    @property
    def quota_group(self) -> str | None:
        """Optional provider quota/rate-limit grouping identifier."""
        return None

    def close(self) -> None:
        """Release provider resources. Providers may override this."""
        return None

    def structured(
        self,
        messages: list[dict[str, str]],
        schema: dict,
        *,
        purpose: str = "structured",
    ) -> LLMResponse:
        """
        Send a structured-output request.

        Providers with native structured-output support should
        override this method. The default falls back to normal
        chat so lightweight test doubles and providers that do not
        yet support native schemas remain compatible.
        """
        return self.chat(messages)

    def generate(
        self,
        prompt: str,
    ) -> LLMResponse:
        """Send a single prompt string and return a normalized response."""
        return self.chat([{"role": "user", "content": prompt}], purpose="generate")
