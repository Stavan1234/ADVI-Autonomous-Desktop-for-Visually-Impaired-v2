from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone


from ..core.pipeline_trace import (
    llm_response_to_dict,
    next_llm_call_id,
    sanitize_string,
    trace,
    trace_exception,
)
from .base import LLMProvider, LLMResponse, LLMToolCall
from .errors import (
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)


logger = logging.getLogger(__name__)


def _coerce_headers(obj: object | None) -> dict[str, str]:
    headers = getattr(obj, "headers", None)
    if headers is None and isinstance(obj, dict):
        headers = obj.get("headers")
    if headers is None:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in dict(headers).items()}
    except Exception:
        return {}


def _parse_duration(value: object) -> float | None:
    """Parse common Groq reset/retry durations such as `7.66s` or `2m59.56s`."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        return max(0.0, numeric)

    match = re.fullmatch(r"\s*(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?\s*", text)
    if not match or not any(match.groups()):
        return None
    hours = float(match.group(1) or 0.0)
    minutes = float(match.group(2) or 0.0)
    seconds = float(match.group(3) or 0.0)
    return max(0.0, hours * 3600.0 + minutes * 60.0 + seconds)


def _error_headers(exc: Exception) -> dict[str, str]:
    """Extract response headers without retaining or logging secret values."""
    response = getattr(exc, "response", None)
    headers = _coerce_headers(response)
    if headers:
        return headers
    return _coerce_headers(exc)


def _rate_limit_details(exc: Exception) -> tuple[str, float | None, str]:
    """Classify a Groq quota error and extract the best available reset hint."""
    message = str(exc or "")
    lower = message.lower()
    headers = _error_headers(exc)

    daily = any(token in lower for token in (
        "tokens per day", "token per day", "tpd", "requests per day", "request per day", "rpd",
        "per-day", "per day",
    ))
    if daily:
        scope = "project_daily"
        detail = "Groq daily token quota is exhausted."
        retry_after = _parse_duration(headers.get("retry-after"))
        if retry_after is None:
            retry_after = _parse_duration(headers.get("x-ratelimit-reset-requests"))
    else:
        scope = "rate_limit"
        detail = "Groq rate limit or quota was exceeded."
        retry_after = _parse_duration(headers.get("retry-after"))
        if retry_after is None:
            retry_after = _parse_duration(headers.get("x-ratelimit-reset-tokens"))
        if retry_after is None:
            retry_after = _parse_duration(headers.get("x-ratelimit-reset-requests"))
        if "tokens per minute" in lower or "token per minute" in lower or "tpm" in lower:
            detail = "Groq token-per-minute rate limit was reached."
        elif "requests per minute" in lower or "request per minute" in lower or "rpm" in lower:
            detail = "Groq request-per-minute rate limit was reached."

    # Some SDK/error strings carry the useful retry hint even when headers are
    # not exposed on the exception object.
    if retry_after is None:
        match = re.search(r"try again in\s+(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*([\d.]+)s", lower)
        if match:
            retry_after = (
                float(match.group(1) or 0.0) * 3600.0
                + float(match.group(2) or 0.0) * 60.0
                + float(match.group(3) or 0.0)
            )

    return scope, retry_after, detail


def _build_rate_limit_error(exc: Exception) -> LLMRateLimitError:
    scope, retry_after, detail = _rate_limit_details(exc)
    if retry_after is not None:
        total = max(1, int(round(retry_after)))
        minutes, seconds = divmod(total, 60)
        if minutes:
            wait_text = f"about {minutes} minute{'s' if minutes != 1 else ''}"
            if seconds >= 30:
                wait_text += " and about 30 seconds"
        else:
            wait_text = f"about {seconds} second{'s' if seconds != 1 else ''}"
        detail = f"{detail} Try again in {wait_text}."
    error = LLMRateLimitError(detail)
    setattr(error, "quota_scope", scope)
    setattr(error, "retry_after_seconds", retry_after)
    setattr(error, "provider_name", "groq")
    return error

class GroqProvider(LLMProvider):
    """Groq-backed LLM provider."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        quota_group: str | None = None,
        label: str = "groq",
        request_timeout_seconds: float = 14.0,
    ) -> None:
        try:
            from groq import Groq
        except ImportError as exc:
            raise LLMUnavailableError(
                "Groq SDK is not installed."
            ) from exc

        self._request_timeout_seconds = max(1.0, float(request_timeout_seconds))
        self._client = Groq(
            api_key=api_key,
            timeout=self._request_timeout_seconds,
            max_retries=0,
        )
        self._model = model
        self._quota_group = quota_group
        self._label = label

    def close(self) -> None:
        client = getattr(self, "_client", None)
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()

    @property
    def name(self) -> str:
        return self._label

    @property
    def model(self) -> str:
        return self._model

    @property
    def quota_group(self) -> str | None:
        return self._quota_group

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "chat",
    ) -> LLMResponse:
        started = time.perf_counter()
        llm_id = next_llm_call_id()
        request_started = datetime.now(timezone.utc).isoformat()

        system_prompt = None
        for message in messages:
            if message.get("role") == "system":
                system_prompt = message.get("content")
                break

        trace(
            "groq.request",
            provider=self.name,
            model=self._model,
            purpose=purpose,
            llm_call_id=llm_id,
            request_started_at=request_started,
            message_count=len(messages),
            message_chars=sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict)),
            system_prompt_chars=len(str(system_prompt or "")),
            structured_output=False,
        )

        from groq import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            RateLimitError,
        )

        try:
            request_kwargs = {
                "model": self._model,
                "messages": messages,
                "reasoning_effort": "low",
                "include_reasoning": False,
                "max_completion_tokens": 768 if purpose != "consolidation" else 512,
            }
            response = self._client.chat.completions.create(**request_kwargs)

        except AuthenticationError as exc:
            normalized = LLMAuthenticationError("Groq authentication failed.")
            trace(
                "groq.error.authentication",
                provider=self.name,
                model=self._model,
                llm_call_id=llm_id,
                error=str(normalized),
            )
            raise normalized from exc

        except RateLimitError as exc:
            normalized = _build_rate_limit_error(exc)
            trace(
                "groq.error.rate_limit",
                provider=self.name,
                model=self._model,
                llm_call_id=llm_id,
                quota_scope=getattr(normalized, "quota_scope", None),
                retry_after_seconds=getattr(normalized, "retry_after_seconds", None),
                error=str(normalized),
            )
            raise normalized from exc

        except APITimeoutError as exc:
            normalized = LLMTimeoutError("Groq request timed out.")
            trace(
                "groq.error.timeout",
                provider=self.name,
                model=self._model,
                llm_call_id=llm_id,
                error=str(normalized),
            )
            raise normalized from exc

        except APIConnectionError as exc:
            normalized = LLMUnavailableError("Could not connect to Groq.")
            trace(
                "groq.error.connection",
                provider=self.name,
                model=self._model,
                llm_call_id=llm_id,
                error=str(normalized),
            )
            raise normalized from exc

        except APIStatusError as exc:
            trace_exception(
                "groq.error",
                exc,
                provider=self.name,
                model=self._model,
                llm_call_id=llm_id,
            )
            status = getattr(exc, "status_code", None)
            safe_msg = sanitize_string(str(getattr(exc, "message", exc) or exc))
            if status == 429:
                raise _build_rate_limit_error(exc) from exc
            if status in (401, 403):
                raise LLMAuthenticationError(f"Groq authentication/permission failed ({status}).") from exc
            if status is not None and (status == 498 or status >= 500):
                raise LLMUnavailableError(f"Groq service error ({status}).") from exc
            if status in (400, 413, 422):
                raise LLMRequestError(f"Groq rejected the request ({status}): {safe_msg}") from exc
            raise LLMRequestError(f"Groq API error ({status}): {safe_msg}") from exc

        latency_ms = (
            time.perf_counter() - started
        ) * 1000

        if not response.choices:
            trace(
                "groq.response.raw",
                provider=self.name,
                model=self._model,
                llm_call_id=llm_id,
                status="error",
                latency_ms=round(latency_ms, 2),
                error="no_choices",
                raw_response_id=getattr(response, "id", None),
            )
            raise LLMUnavailableError(
                "Groq returned no choices."
            )

        message = response.choices[0].message
        text = message.content or ""

        usage = response.usage
        finish_reason = response.choices[0].finish_reason

        trace(
            "groq.response.raw",
            provider=self.name,
            model=self._model,
            llm_call_id=llm_id,
            status="success",
            latency_ms=round(latency_ms, 2),
            raw_text=text,
            raw_text_empty=not bool(text.strip()),
            finish_reason=finish_reason,
            request_id=getattr(response, "id", None),
            usage={
                "prompt_tokens": (
                    usage.prompt_tokens
                    if usage is not None
                    else None
                ),
                "completion_tokens": (
                    usage.completion_tokens
                    if usage is not None
                    else None
                ),
                "total_tokens": (
                    usage.total_tokens
                    if usage is not None
                    else None
                ),
            },
            response_type=type(response).__name__,
        )

        if not text.strip():
            raise LLMUnavailableError(
                "Groq returned an empty response."
            )

        llm_response = LLMResponse(
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
            finish_reason=finish_reason,
            request_id=getattr(
                response,
                "id",
                None,
            ),
        )

        trace(
            "groq.response.normalized",
            provider=self.name,
            model=self.model,
            llm_call_id=llm_id,
            normalized=llm_response_to_dict(llm_response),
        )

        return llm_response

    def chat_with_tools(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        *,
        tool_choice: str = "auto",
        purpose: str = "agent",
    ) -> LLMResponse:
        """Use Groq's native local tool-calling API."""
        import json

        started = time.perf_counter()
        llm_id = next_llm_call_id()

        from groq import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            RateLimitError,
        )

        # A tool-bearing ADVI request must never be sent with tool_choice=none.
        # Some provider/model combinations have returned a contradictory 400
        # claiming "tool choice is none, but model called a tool". Force the
        # canonical automatic mode here and below retry only that exact provider
        # contract glitch once.
        effective_tool_choice = "auto" if tools and str(tool_choice).casefold() == "none" else tool_choice
        request_kwargs = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "tool_choice": effective_tool_choice,
            "temperature": 0,
            "max_completion_tokens": 1536,
            "reasoning_effort": "low",
            "include_reasoning": False,
        }
        tool_choice_recovery_attempted = False
        try:
            response = self._client.chat.completions.create(**request_kwargs)
        except AuthenticationError as exc:
            normalized = LLMAuthenticationError("Groq authentication failed.")
            trace("groq.tool_calling.authentication", provider=self.name, model=self._model, llm_call_id=llm_id, error=str(normalized))
            raise normalized from exc
        except RateLimitError as exc:
            normalized = _build_rate_limit_error(exc)
            trace("groq.tool_calling.rate_limit", provider=self.name, model=self._model, llm_call_id=llm_id, quota_scope=getattr(normalized, "quota_scope", None), retry_after_seconds=getattr(normalized, "retry_after_seconds", None), error=str(normalized))
            raise normalized from exc
        except APITimeoutError as exc:
            normalized = LLMTimeoutError("Groq request timed out.")
            trace("groq.tool_calling.timeout", provider=self.name, model=self._model, llm_call_id=llm_id, error=str(normalized))
            raise normalized from exc
        except APIConnectionError as exc:
            normalized = LLMUnavailableError("Could not connect to Groq.")
            trace("groq.tool_calling.connection", provider=self.name, model=self._model, llm_call_id=llm_id, error=str(normalized))
            raise normalized from exc
        except APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            safe_msg = sanitize_string(str(getattr(exc, "message", exc) or exc))
            # Groq has returned a contradictory 400 in which the request is
            # interpreted as tool_choice=none even though ADVI supplied tools.
            # Retry that exact protocol glitch once with explicit automatic tool
            # choice; do not retry arbitrary 400s.
            if status == 400 and "tool choice is none, but model called a tool" in safe_msg.casefold() and tools and not tool_choice_recovery_attempted:
                tool_choice_recovery_attempted = True
                trace("groq.tool_calling.recover_tool_choice", provider=self.name, model=self._model, llm_call_id=llm_id)
                request_kwargs["tool_choice"] = "auto"
                try:
                    response = self._client.chat.completions.create(**request_kwargs)
                except Exception as retry_exc:
                    trace("groq.tool_calling.error", provider=self.name, model=self._model, llm_call_id=llm_id, error=sanitize_string(str(retry_exc)))
                    raise LLMRequestError(f"Groq rejected tool-calling request (400): {sanitize_string(str(retry_exc))}") from retry_exc
            else:
                trace("groq.tool_calling.error", provider=self.name, model=self._model, llm_call_id=llm_id, error=safe_msg)
                if status == 429:
                    raise _build_rate_limit_error(exc) from exc
                if status in (401, 403):
                    raise LLMAuthenticationError(f"Groq authentication/permission failed ({status}).") from exc
                if status == 498 or (status is not None and status >= 500):
                    raise LLMUnavailableError(f"Groq service error ({status}): {safe_msg}") from exc
                if status in (400, 413, 422):
                    raise LLMRequestError(f"Groq rejected tool-calling request ({status}): {safe_msg}") from exc
                raise LLMRequestError(f"Groq API error ({status}): {safe_msg}") from exc
            if status == 429:
                raise _build_rate_limit_error(exc) from exc
            if status in (401, 403):
                raise LLMAuthenticationError(f"Groq authentication/permission failed ({status}).") from exc
            if status == 498 or (status is not None and status >= 500):
                raise LLMUnavailableError(f"Groq service error ({status}): {safe_msg}") from exc
            if status in (400, 413, 422):
                raise LLMRequestError(f"Groq rejected tool-calling request ({status}): {safe_msg}") from exc
            raise LLMRequestError(f"Groq API error ({status}): {safe_msg}") from exc
        

        latency_ms = (time.perf_counter() - started) * 1000
        if not response.choices:
            raise LLMResponseError("Groq returned no choices.")

        message = response.choices[0].message
        calls: list[LLMToolCall] = []
        for item in (getattr(message, "tool_calls", None) or []):
            function = getattr(item, "function", None)
            name = str(getattr(function, "name", "") or "").strip()
            raw_arguments = getattr(function, "arguments", "{}")
            if not name:
                continue
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments or {})
            except (TypeError, ValueError) as exc:
                raise LLMResponseError(f"Groq returned invalid arguments for tool '{name}'.") from exc
            if not isinstance(arguments, dict):
                raise LLMResponseError(f"Groq returned non-object arguments for tool '{name}'.")
            calls.append(LLMToolCall(call_id=str(getattr(item, "id", name)), name=name, arguments=arguments))

        usage = response.usage
        return LLMResponse(
            text=str(getattr(message, "content", None) or ""),
            provider=self.name,
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage is not None else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage is not None else None,
            latency_ms=latency_ms,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
            request_id=getattr(response, "id", None),
            tool_calls=tuple(calls),
        )

    def structured(
        self,
        messages: list[dict[str, str]],
        schema: dict,
        *,
        purpose: str = "structured",
    ) -> LLMResponse:
        started = time.perf_counter()
        llm_id = next_llm_call_id()
        request_started = datetime.now(timezone.utc).isoformat()

        trace(
            "groq.structured.request",
            provider=self.name,
            model=self._model,
            purpose=purpose,
            llm_call_id=llm_id,
            request_started_at=request_started,
            message_count=len(messages),
            message_chars=sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict)),
            schema_keys=sorted(schema) if isinstance(schema, dict) else [],
            structured_output=True,
        )

        from groq import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            RateLimitError,
        )

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0,
                max_tokens=1024,
                reasoning_effort="low",
                include_reasoning=False,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": f"advi_{purpose.replace(' ', '_')[:40]}",
                        "strict": False,
                        "schema": schema,
                    },
                },
            )
        except AuthenticationError as exc:
            trace("groq.structured.error", provider=self.name, model=self._model, llm_call_id=llm_id, status=getattr(exc, "status_code", None), error=sanitize_string(str(exc)))
            raise LLMAuthenticationError("Groq authentication failed.") from exc
        except RateLimitError as exc:
            trace("groq.structured.error", provider=self.name, model=self._model, llm_call_id=llm_id, status=getattr(exc, "status_code", None), error=sanitize_string(str(exc)))
            raise _build_rate_limit_error(exc) from exc
        except APITimeoutError as exc:
            trace("groq.structured.error", provider=self.name, model=self._model, llm_call_id=llm_id, status=getattr(exc, "status_code", None), error=sanitize_string(str(exc)))
            raise LLMTimeoutError("Groq request timed out.") from exc
        except APIConnectionError as exc:
            trace("groq.structured.error", provider=self.name, model=self._model, llm_call_id=llm_id, status=getattr(exc, "status_code", None), error=sanitize_string(str(exc)))
            raise LLMUnavailableError("Could not connect to Groq.") from exc
        except APIStatusError as exc:
            trace("groq.structured.error", provider=self.name, model=self._model, llm_call_id=llm_id, status=getattr(exc, "status_code", None), error=sanitize_string(str(exc)))
            status = getattr(exc, "status_code", None)
            safe_msg = sanitize_string(str(getattr(exc, "message", exc) or exc))
            if status == 429:
                raise _build_rate_limit_error(exc) from exc
            if status in (401, 403):
                raise LLMAuthenticationError(f"Groq authentication/permission failed ({status}).") from exc
            if status == 498 or (status is not None and status >= 500):
                raise LLMUnavailableError(f"Groq service error ({status}).") from exc
            if status in (400, 413, 422):
                raise LLMRequestError(f"Groq rejected structured request ({status}): {safe_msg}") from exc
            raise LLMRequestError(f"Groq API error ({status}): {safe_msg}") from exc
        except Exception as exc:
            trace("groq.structured.error", provider=self.name, model=self._model, llm_call_id=llm_id, status=getattr(exc, "status_code", None), error=sanitize_string(str(exc)))
            raise LLMUnavailableError("Groq structured request failed.") from exc

        latency_ms = (time.perf_counter() - started) * 1000
        if not response.choices:
            raise LLMResponseError("Groq returned no choices.")
        message = response.choices[0].message
        text = message.content or ""
        if not text.strip():
            raise LLMResponseError("Groq returned an empty structured response.")
        usage = response.usage
        llm_response = LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage is not None else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage is not None else None,
            latency_ms=latency_ms,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
            request_id=getattr(response, "id", None),
        )
        trace("groq.structured.response.normalized", provider=self.name, model=self.model, llm_call_id=llm_id, normalized=llm_response_to_dict(llm_response))
        return llm_response
