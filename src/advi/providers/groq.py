from __future__ import annotations

import time

from groq import Groq
from groq import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from .base import LLMProvider, LLMResponse
from .errors import (
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)


class GroqProvider(LLMProvider):
    """Groq-backed LLM provider."""

    def __init__(
        self,
        api_key: str,
        model: str,
    ) -> None:
        self._client = Groq(
            api_key=api_key,
            timeout=30.0,
            max_retries=0,
        )
        self._model = model

    @property
    def name(self) -> str:
        return "groq"

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        messages: list[dict[str, str]],
    ) -> LLMResponse:
        started = time.perf_counter()

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
            )

        except AuthenticationError as exc:
            raise LLMAuthenticationError(
                "Groq authentication failed."
            ) from exc

        except RateLimitError as exc:
            raise LLMRateLimitError(
                "Groq rate limit or quota was exceeded."
            ) from exc

        except APITimeoutError as exc:
            raise LLMTimeoutError(
                "Groq request timed out."
            ) from exc

        except APIConnectionError as exc:
            raise LLMUnavailableError(
                "Could not connect to Groq."
            ) from exc

        latency_ms = (
            time.perf_counter() - started
        ) * 1000

        if not response.choices:
            raise LLMUnavailableError(
                "Groq returned no choices."
            )

        message = response.choices[0].message
        text = message.content or ""

        if not text.strip():
            raise LLMUnavailableError(
                "Groq returned an empty response."
            )

        usage = response.usage

        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=(
                usage.prompt_tokens
                if usage is not None
                else None
            ),
            output_tokens=(
                usage.completion_tokens
                if usage is not None
                else None
            ),
            latency_ms=latency_ms,
            finish_reason=(
                response.choices[0].finish_reason
            ),
            request_id=getattr(
                response,
                "id",
                None,
            ),
        )