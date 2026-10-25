from __future__ import annotations

import queue
from dataclasses import dataclass, field
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall, ProviderHealthState
from advi.providers.errors import (
    LLMAuthenticationError,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from advi.core.pipeline_trace import record_metric, trace

from .session_handoff import SessionHandoff


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    success: bool
    error_type: str | None = None


@dataclass
class ProviderSession:
    """One conversational session owned by one concrete provider."""

    provider: LLMProvider
    system_instruction: str
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]] = field(default_factory=list)
    native: Any = None
    closed: bool = False
    # Keep enough conversational continuity while leaving room for the system
    # prompt, tool declarations, current turn, and model completion.
    max_history_tokens: int = 1400
    native_turns: int = 0
    max_native_turns: int = 8

    @classmethod
    def create(
        cls,
        provider: LLMProvider,
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None = None,
    ) -> "ProviderSession":
        instance = cls(provider=provider, system_instruction=system_instruction, tools=list(tools))
        history = instance._handoff_history(handoff)
        try:
            instance.native = provider.create_native_session(
                system_instruction=system_instruction,
                tools=list(tools),
                history=history,
            )
        except (AttributeError, NotImplementedError):
            instance.native = None
        if instance.native is None:
            instance.messages.append({"role": "system", "content": system_instruction})
            if handoff is not None:
                instance.messages.extend(history)
        return instance

    @staticmethod
    def _handoff_history(handoff: SessionHandoff | None) -> list[dict[str, Any]]:
        if handoff is None:
            return []

        messages: list[dict[str, Any]] = []
        # Provider switches are recovery events, not transcript replay. Keep the
        # latest conversational anchors and at most two execution exchanges.
        conversation = handoff.recent_conversation[-4:] if handoff.recent_conversation else ()
        if conversation:
            for message in conversation:
                role = str(message.get("role") or "").strip().lower()
                content = message.get("content")
                if role in {"user", "assistant"} and content not in (None, ""):
                    messages.append({"role": role, "content": str(content)[:1400]})
        elif handoff.last_user_message:
            messages.append({"role": "user", "content": str(handoff.last_user_message)[:1400]})

        for exchange in handoff.recent_tool_exchanges[-2:]:
            if exchange.calls:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(call.get("call_id") or "handoff-call"),
                            "type": "function",
                            "function": {
                                "name": str(call.get("name") or ""),
                                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False, separators=(",", ":"), default=str)[:900],
                            },
                        }
                        for call in exchange.calls[:6]
                    ],
                })
            for result in exchange.results[:6]:
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(result.get("call_id") or result.get("name") or "handoff-call"),
                    "name": str(result.get("name") or ""),
                    "content": json.dumps(result.get("result") or {}, ensure_ascii=False, separators=(",", ":"), default=str)[:1800],
                })

        # Final provider-neutral size gate. Never send a giant recovery packet.
        for _ in range(6):
            size = sum(ProviderSession._estimate_tokens(message) for message in messages) if messages else 0
            if size <= 1500:
                return messages
            messages = [ProviderSession._truncate_message(m, content_chars=700, tool_arg_chars=350) for m in messages[-6:]]
        return messages[-4:]

    turn_deadline_seconds: float = 20.0
    _call_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _active_call_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _close_when_idle: bool = field(default=False, init=False, repr=False)

    def _run_with_deadline(self, func, *args, **kwargs) -> LLMResponse:
        """Run a provider call with a hard interactive deadline without killing a live SDK thread.

        Python cannot safely terminate a running provider thread. A timed-out call therefore
        poisons this session and defers native-session teardown until the worker actually exits.
        This prevents the classic race: timeout -> close SDK session -> still-running SDK call
        touches closed state. The worker is daemonized so a pathological SDK cannot hold process
        shutdown hostage.
        """
        deadline = max(0.01, float(getattr(self, "turn_deadline_seconds", 35.0)))
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        current_thread = threading.current_thread()

        with self._call_lock:
            if self.closed:
                raise RuntimeError("Provider session is closed.")
            if self._active_call_thread is not None and self._active_call_thread.is_alive():
                raise RuntimeError("Provider session already has an active call.")

        def worker() -> None:
            native_to_close = None
            try:
                try:
                    result = func(*args, **kwargs)
                except BaseException as exc:
                    result_queue.put(("error", exc))
                else:
                    result_queue.put(("result", result))
            finally:
                with self._call_lock:
                    if self._active_call_thread is threading.current_thread():
                        self._active_call_thread = None
                    if self._close_when_idle:
                        native_to_close = self.native
                        self.native = None
                        self._close_when_idle = False
                if native_to_close is not None:
                    close = getattr(native_to_close, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass

        thread = threading.Thread(
            target=worker,
            name=f"advi-provider-{self.provider.name}",
            daemon=True,
        )
        with self._call_lock:
            self._active_call_thread = thread
        thread.start()

        try:
            kind, payload = result_queue.get(timeout=deadline)
        except queue.Empty as exc:
            with self._call_lock:
                self.closed = True
                self._close_when_idle = True
            raise LLMTimeoutError(
                f"Provider '{self.provider.name}' interactive turn timed out after {deadline:.0f}s."
            ) from exc

        if kind == "error":
            assert isinstance(payload, BaseException)
            raise payload
        return payload  # type: ignore[return-value]

    def continue_from_handoff(self) -> LLMResponse:
        """Resume from seeded handoff state without injecting a fake user request."""
        if self.closed:
            raise RuntimeError("Provider session is closed.")
        if self.native is not None:
            continuation = getattr(self.native, "continue_from_handoff", None)
            if callable(continuation):
                return self._run_with_deadline(continuation)
            return self._run_with_deadline(
                self.native.send_user,
                "Continue the existing task using the supplied handoff state.",
            )
        return self._run_with_deadline(self._send_stateless)

    @property
    def uses_native_state(self) -> bool:
        return self.native is not None

    @staticmethod
    def _tool_surface_signature(tools: list[dict[str, Any]]) -> str:
        return json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def _refresh_tool_surface_if_needed(
        self,
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None,
    ) -> None:
        """Keep the provider's model-visible tool surface synchronized with runtime readiness."""
        current_signature = self._tool_surface_signature(tools)
        existing_signature = self._tool_surface_signature(self.tools)
        if current_signature == existing_signature:
            return

        # Stateless providers can switch tool declarations immediately. Native
        # provider sessions generally bind tools at construction time, so rebuild
        # only when the actual tool surface changes and seed from the bounded
        # handoff rather than leaking a full transcript.
        if self.native is None:
            self.tools = list(tools)
            return

        self.closed = True
        native_close = getattr(self.native, "close", None)
        if callable(native_close):
            try:
                native_close()
            except Exception:
                pass
        self.native = None
        self.closed = False
        history = self._handoff_history(handoff)
        try:
            self.native = self.provider.create_native_session(
                system_instruction=system_instruction,
                tools=list(tools),
                history=history,
            )
        except (AttributeError, NotImplementedError):
            self.native = None
        self.tools = list(tools)
        if self.native is None:
            self.messages = [{"role": "system", "content": system_instruction}, *history]

    def _recycle_native_session_if_needed(
        self,
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None,
    ) -> None:
        """Rebuild long-lived native sessions from bounded handoff state.

        Some provider SDKs retain their own transcript internally, outside ADVI's
        Python `messages` list. Periodic recycling prevents that hidden history from
        growing without bound while preserving the recent task state we explicitly
        hand off.
        """
        if self.native is None or self.native_turns < max(1, int(self.max_native_turns)):
            return
        native_close = getattr(self.native, "close", None)
        if callable(native_close):
            try:
                native_close()
            except Exception:
                pass
        self.native = None
        history = self._handoff_history(handoff)
        try:
            self.native = self.provider.create_native_session(
                system_instruction=system_instruction,
                tools=list(tools),
                history=history,
            )
        except (AttributeError, NotImplementedError):
            self.native = None
        if self.native is None:
            self.messages = [{"role": "system", "content": system_instruction}, *history]
        self.tools = list(tools)
        self.native_turns = 0

    def send_user(
        self,
        content: str,
        *,
        system_instruction: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        handoff: SessionHandoff | None = None,
    ) -> LLMResponse:
        if self.closed:
            raise RuntimeError("Provider session is closed.")
        if system_instruction is not None and tools is not None:
            self._refresh_tool_surface_if_needed(
                system_instruction=system_instruction,
                tools=tools,
                handoff=handoff,
            )
        if system_instruction is not None and tools is not None:
            self._recycle_native_session_if_needed(
                system_instruction=system_instruction,
                tools=tools,
                handoff=handoff,
            )
        trace(
            "gateway.provider_session.send_user",
            source_component="ProviderSession",
            provider=self.provider.name,
            native=self.native is not None,
            content=content[:1200],
            content_truncated=len(content) > 1200,
            tool_count=len(self.tools),
            tool_names=[str(item.get("function", {}).get("name") or item.get("name") or "") for item in self.tools],
            history_count=len(self.messages),
            history_tokens=sum(self._estimate_tokens(m) for m in self.messages),
        )
        if self.native is not None:
            response = self._run_with_deadline(self.native.send_user, content)
            self.native_turns += 1
            return response
        self.messages.append({"role": "user", "content": content})
        return self._run_with_deadline(self._send_stateless)

    def seed_turn(self, content: str, response: LLMResponse) -> bool:
        """Record a user turn and its assistant reply without calling the provider.

        Used when a provider rejected a request but returned the model's intended
        tool call: the agent runs that call locally, and the history must show it
        as the assistant's reply so the tool results that follow are valid.
        """
        if self.closed or self.native is not None:
            return False
        self.messages.append({"role": "user", "content": content})
        self.record_assistant(response)
        return True

    def send_tool_results(
        self,
        results: list[tuple[LLMToolCall, dict[str, Any]]],
        *,
        system_instruction: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        handoff: SessionHandoff | None = None,
    ) -> LLMResponse:
        if self.closed:
            raise RuntimeError("Provider session is closed.")
        if system_instruction is not None and tools is not None:
            self._refresh_tool_surface_if_needed(
                system_instruction=system_instruction,
                tools=tools,
                handoff=handoff,
            )
            self._recycle_native_session_if_needed(
                system_instruction=system_instruction,
                tools=tools,
                handoff=handoff,
            )
        trace(
            "gateway.provider_session.send_tool_results",
            source_component="ProviderSession",
            provider=self.provider.name,
            native=self.native is not None,
            result_summaries=[
                {
                    "call_id": getattr(call, "call_id", None),
                    "name": getattr(call, "name", None),
                    "ok": bool(payload.get("ok")) if isinstance(payload, dict) else None,
                    "verified": payload.get("verified") if isinstance(payload, dict) else None,
                    "payload_chars": len(json.dumps(payload, ensure_ascii=False, default=str)),
                }
                for call, payload in results
            ],
        )
        if self.native is not None:
            response = self._run_with_deadline(self.native.send_tool_results, results)
            self.native_turns += 1
            return response
        for call, result in results:
            self.messages.append({
                "role": "tool",
                "tool_call_id": call.call_id,
                "name": call.name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
        return self._run_with_deadline(self._send_stateless)

    @staticmethod
    def _estimate_tokens(message: dict[str, Any]) -> int:
        """Conservatively estimate tokens without adding a tokenizer dependency."""
        try:
            payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            payload = str(message)
        return max(1, (len(payload) + 3) // 4)

    @staticmethod
    def _truncate_message(message: dict[str, Any], *, content_chars: int = 1800, tool_arg_chars: int = 900) -> dict[str, Any]:
        """Lossily compact one history message while preserving its protocol shape."""
        compact = dict(message)
        content = compact.get("content")
        if isinstance(content, str) and len(content) > content_chars:
            compact["content"] = content[:content_chars] + "… [history truncated]"

        tool_calls = compact.get("tool_calls")
        if isinstance(tool_calls, list):
            compact_calls = []
            for call in tool_calls[:8]:
                item = dict(call) if isinstance(call, dict) else {"value": str(call)}
                function = item.get("function")
                if isinstance(function, dict):
                    function = dict(function)
                    arguments = function.get("arguments")
                    if isinstance(arguments, str) and len(arguments) > tool_arg_chars:
                        function["arguments"] = arguments[:tool_arg_chars] + "… [history truncated]"
                    item["function"] = function
                compact_calls.append(item)
            compact["tool_calls"] = compact_calls
            if len(tool_calls) > 8:
                compact["tool_calls_truncated"] = True
        return compact

    def _compact_oversized_block(self, block: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        """Keep the semantic anchors of one huge turn without allowing it to blow the budget."""
        if not block:
            return []

        if len(block) <= 4:
            candidates = list(block)
        else:
            # Preserve the user's request plus the most recent execution exchange.
            candidates = [block[0], *block[-3:]]

        # Remove accidental duplicates while preserving order.
        seen_ids: set[int] = set()
        unique: list[dict[str, Any]] = []
        for message in candidates:
            marker = id(message)
            if marker not in seen_ids:
                unique.append(message)
                seen_ids.add(marker)

        compacted = [self._truncate_message(message) for message in unique]
        # Tighten progressively if even the compacted anchors are still too large.
        for _ in range(6):
            used = sum(self._estimate_tokens(message) for message in compacted)
            if used <= budget:
                break
            ratio = max(0.18, min(0.85, (budget / max(1, used)) * 0.88))
            content_cap = max(160, int(1800 * ratio))
            arg_cap = max(120, int(900 * ratio))
            compacted = [self._truncate_message(message, content_chars=content_cap, tool_arg_chars=arg_cap) for message in compacted]
        return compacted

    def _trim_stateless_history(self) -> None:
        """Trim complete conversational blocks and compact an oversized newest block.

        The provider-facing history is intentionally lossy: the local task/evidence
        state remains authoritative, while the model receives only enough recent
        context to continue the conversation. This prevents large tool results or
        transcripts from growing a request beyond the provider's interactive budget.
        """
        if self.native is not None or not self.messages:
            return

        system = [m for m in self.messages if m.get("role") == "system"][:1]
        non_system = [m for m in self.messages if m.get("role") != "system"]
        if not non_system:
            self.messages = system
            return

        blocks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for message in non_system:
            if message.get("role") == "user" and current:
                blocks.append(current)
                current = []
            current.append(message)
        if current:
            blocks.append(current)

        budget = max(768, int(self.max_history_tokens))
        selected: list[list[dict[str, Any]]] = []
        used = 0
        for block in reversed(blocks):
            block_cost = sum(self._estimate_tokens(message) for message in block)
            if not selected and block_cost > budget:
                compacted = self._compact_oversized_block(block, budget)
                selected.append(compacted)
                used = sum(self._estimate_tokens(message) for message in compacted)
                continue
            if selected and used + block_cost > budget:
                break
            selected.append(block)
            used += block_cost

        selected.reverse()
        self.messages = system + [message for block in selected for message in block]

    def history_token_estimate(self) -> int:
        return sum(self._estimate_tokens(message) for message in self.messages)

    def _send_stateless(self) -> LLMResponse:
        self._trim_stateless_history()
        return self.provider.chat_with_tools(
            list(self.messages),
            list(self.tools),
            tool_choice="auto",
            purpose="agent",
        )

    def record_assistant(self, response: LLMResponse) -> None:
        if self.native is not None:
            return
        message: dict[str, Any] = {"role": "assistant", "content": response.text or None}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")),
                    },
                }
                for call in response.tool_calls
            ]
        self.messages.append(message)

    def close(self) -> None:
        with self._call_lock:
            active = self._active_call_thread
            self.closed = True
            if active is not None and active.is_alive():
                self._close_when_idle = True
                return
            native = self.native
            self.native = None
        native_close = getattr(native, "close", None)
        if callable(native_close):
            try:
                native_close()
            except Exception:
                pass


class ProviderSessionManager:
    """Keeps one active provider session and performs explicit state handoff on failure."""

    def __init__(
        self,
        providers: Iterable[LLMProvider],
        *,
        cooldown_seconds: float = 60.0,
        health_state: ProviderHealthState | None = None,
    ) -> None:
        self.providers = tuple(p for p in providers if p is not None)
        if not self.providers:
            raise ValueError("At least one provider is required.")
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.health_state = health_state or ProviderHealthState()
        self.active_index = 0
        self.session: ProviderSession | None = None
        self.switch_count = 0
        self.last_attempts: tuple[ProviderAttempt, ...] = ()
        # Told how long ADVI is about to wait, so it can say so out loud.
        self.wait_notice: Callable[[float], None] | None = None
        self._sleep: Callable[[float], None] = time.sleep

    # A per-minute limit on every key clears within seconds. Telling a blind
    # user "try again in about 20 seconds" made them repeat themselves; ADVI
    # now waits that out once (Desktop Suite dev run, 2026-10-23). A daily
    # quota is never waited for.
    SHORT_WAIT_MAX_SECONDS = 25.0

    def _short_wait(self, exc: Exception) -> float | None:
        if not isinstance(exc, LLMRateLimitError):
            return None
        message = str(exc).casefold()
        if getattr(exc, "quota_scope", None) == "project_daily" or "quota-exhausted" in message or "daily" in message or "per day" in message:
            return None
        now = time.monotonic()
        if all(self.health_state.quota_exhausted_until.get(self._group_for(i), 0.0) > now
               for i in range(len(self.providers))):
            return None  # every key is out for the day
        # Wait until the first key is free again, not for the slowest one.
        cooling = [until - now for until in self.health_state.cooldown_until.values() if until > now]
        try:
            wait = min(cooling) if cooling else float(getattr(exc, "retry_after_seconds", None))
        except (TypeError, ValueError):
            return None
        return wait + 0.5 if 0 < wait <= self.SHORT_WAIT_MAX_SECONDS else None

    def _waiting_out_short_limits(self, send: Callable[[], LLMResponse]) -> LLMResponse:
        try:
            return send()
        except LLMRateLimitError as exc:
            wait = self._short_wait(exc)
            if wait is None:
                raise
            trace("gateway.provider_session.short_limit_wait", seconds=round(wait, 1), error=str(exc))
            if self.wait_notice is not None:
                try:
                    self.wait_notice(wait)
                except Exception:
                    pass
            self._sleep(wait)
            return send()

    def send_user(self, content: str, **kwargs: Any) -> LLMResponse:
        """Send a user turn; one short per-minute limit is waited out, not reported."""
        return self._waiting_out_short_limits(lambda: self._send_user_once(content, **kwargs))

    def send_tool_results(self, results: list[tuple[LLMToolCall, dict[str, Any]]], **kwargs: Any) -> LLMResponse:
        """Continue a tool round; one short per-minute limit is waited out, not reported."""
        return self._waiting_out_short_limits(lambda: self._send_tool_results_once(results, **kwargs))

    @property
    def active_provider(self) -> LLMProvider:
        return self.providers[self.active_index]

    @property
    def active_provider_blocked(self) -> bool:
        """Whether the active provider is currently unavailable for background work."""
        return self._is_blocked(self.active_index)

    def _group_for(self, index: int) -> str:
        provider = self.providers[index]
        return provider.quota_group or f"provider:{provider.name}:{index}"

    def _mark_failure(self, index: int, exc: Exception) -> None:
        group = self._group_for(index)
        now = time.monotonic()
        if isinstance(exc, LLMRateLimitError):
            retry_after = getattr(exc, "retry_after_seconds", None)
            try:
                retry_after = float(retry_after) if retry_after is not None else None
            except (TypeError, ValueError):
                retry_after = None
            scope = getattr(exc, "quota_scope", None)
            if scope == "project_daily":
                until = (now + max(5.0, retry_after)) if retry_after is not None else self._next_pacific_midnight_epoch()
                self.health_state.quota_exhausted_until[group] = until
                return
            if retry_after is not None:
                self.health_state.cooldown_until[group] = now + max(5.0, retry_after)
                return
        self.health_state.cooldown_until[group] = now + self.cooldown_seconds

    @staticmethod
    def _next_pacific_midnight_epoch() -> float:
        now_pacific = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
        next_midnight = now_pacific.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return time.monotonic() + max(0.0, (next_midnight - now_pacific).total_seconds())

    def _is_blocked(self, index: int) -> bool:
        group = self._group_for(index)
        now = time.monotonic()
        return (
            self.health_state.quota_exhausted_until.get(group, 0.0) > now
            or self.health_state.cooldown_until.get(group, 0.0) > now
        )

    def _all_blocked_error(self) -> LLMRateLimitError | LLMUnavailableError:
        now = time.monotonic()
        future_quota = [until for until in self.health_state.quota_exhausted_until.values() if until > now]
        future_cooldown = [until for until in self.health_state.cooldown_until.values() if until > now]
        next_quota = min(future_quota, default=0.0)
        next_any = min([*future_quota, *future_cooldown], default=0.0)
        if next_any > now:
            remaining = max(1, int(round(next_any - now)))
            minutes, seconds = divmod(remaining, 60)
            if minutes:
                wait_text = f"about {minutes} minute{'s' if minutes != 1 else ''}"
                if seconds >= 30:
                    wait_text += " and about 30 seconds"
            else:
                wait_text = f"about {seconds} second{'s' if seconds != 1 else ''}"
            scope = "quota-exhausted" if next_quota == next_any else "rate-limited"
            error = LLMRateLimitError(f"All configured language-model providers are temporarily {scope}. Try again in {wait_text}.")
            setattr(error, "quota_scope", "all_providers")
            setattr(error, "retry_after_seconds", remaining)
            return error
        return LLMUnavailableError("All language-model providers are temporarily unavailable.")

    def _candidate_indices(self, *, start: int) -> list[int]:
        n = len(self.providers)
        ordered = [(start + offset) % n for offset in range(n)]
        return [i for i in ordered if not self._is_blocked(i)]

    def _close_session(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def start(
        self,
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None = None,
    ) -> ProviderSession:
        candidates = self._candidate_indices(start=self.active_index)
        last_error: Exception | None = None
        attempts: list[ProviderAttempt] = []
        if not candidates:
            raise self._all_blocked_error()
        for index in candidates:
            self._close_session()
            self.active_index = index
            try:
                self.session = ProviderSession.create(
                    self.active_provider,
                    system_instruction=system_instruction,
                    tools=tools,
                    handoff=handoff,
                )
                attempts.append(ProviderAttempt(self.active_provider.name, True))
                self.last_attempts = tuple(attempts)
                return self.session
            except LLMError as exc:
                last_error = exc
                attempts.append(ProviderAttempt(self.active_provider.name, False, type(exc).__name__))
                if not isinstance(exc, LLMRequestError):
                    self._mark_failure(index, exc)
                if not self._should_failover(exc):
                    self.last_attempts = tuple(attempts)
                    raise
            except Exception as exc:
                last_error = exc
                attempts.append(ProviderAttempt(self.active_provider.name, False, type(exc).__name__))
                self._mark_failure(index, exc)
                self.last_attempts = tuple(attempts)
                raise
        self.last_attempts = tuple(attempts)
        raise last_error or RuntimeError("No provider session could be created.")

    def prepare(
        self,
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None = None,
    ) -> ProviderSession:
        """Prepare the active provider session before interactive mode.

        This creates only provider-local/session state. It deliberately does not
        send a user/model request, so startup preparation cannot consume an LLM
        turn or generate a user-visible response.
        """
        if self.session is not None:
            return self.session
        return self.start(
            system_instruction=system_instruction,
            tools=tools,
            handoff=handoff,
        )

    @staticmethod
    def _is_contract_error(exc: Exception) -> bool:
        """Return True for deterministic provider request/schema defects."""
        return isinstance(exc, LLMRequestError)

    @classmethod
    def _should_failover(cls, exc: Exception) -> bool:
        """Fail over only for conditions another provider can plausibly recover from."""
        # Request/schema defects are deterministic. Switching providers merely
        # repeats the same malformed contract and turns a local surface bug into
        # a provider-rotation storm. AgentSession owns the one-shot repair.
        if cls._is_contract_error(exc):
            return False
        return isinstance(exc, (LLMAuthenticationError, LLMRateLimitError, LLMTimeoutError, LLMUnavailableError))

    def _recycle_native_session_if_needed(
        self,
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None,
    ) -> None:
        """Rebuild long-lived native sessions from bounded handoff state.

        Some provider SDKs retain their own transcript internally, outside ADVI's
        Python `messages` list. Periodic recycling prevents that hidden history from
        growing without bound while preserving the recent task state we explicitly
        hand off.
        """
        if self.native is None or self.native_turns < max(1, int(self.max_native_turns)):
            return
        native_close = getattr(self.native, "close", None)
        if callable(native_close):
            try:
                native_close()
            except Exception:
                pass
        self.native = None
        history = self._handoff_history(handoff)
        try:
            self.native = self.provider.create_native_session(
                system_instruction=system_instruction,
                tools=list(tools),
                history=history,
            )
        except (AttributeError, NotImplementedError):
            self.native = None
        if self.native is None:
            self.messages = [{"role": "system", "content": system_instruction}, *history]
        self.tools = list(tools)
        self.native_turns = 0

    def _send_user_once(
        self,
        content: str,
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None = None,
    ) -> LLMResponse:
        """Send a user turn and walk the configured provider chain on recoverable failure."""
        attempted: set[int] = set()
        failures: list[ProviderAttempt] = []
        last_error: Exception | None = None
        next_index = self.active_index

        while len(attempted) < len(self.providers):
            candidates = self._candidate_indices(start=next_index)
            index = next((candidate for candidate in candidates if candidate not in attempted), None)
            if index is None:
                break
            attempted.add(index)

            switched = index != self.active_index
            if switched:
                self._close_session()
                self.active_index = index
                self.switch_count += 1
                record_metric("provider_switches")

            provider_name = self.providers[index].name
            record_metric("provider_attempts")
            record_metric("llm_calls")
            try:
                if self.session is None:
                    self.session = ProviderSession.create(
                        self.providers[index],
                        system_instruction=system_instruction,
                        tools=tools,
                        handoff=handoff if switched else None,
                    )

                if switched:
                    # A failed-over provider receives the bounded structured
                    # handoff rather than a duplicate user message.
                    response = self.session.continue_from_handoff() if handoff is not None else self.session.send_user(
                        content, system_instruction=system_instruction, tools=tools, handoff=None
                    )
                else:
                    # The current prepared session has not yet seen this user turn.
                    response = self.session.send_user(
                        content,
                        system_instruction=system_instruction,
                        tools=tools,
                        handoff=handoff,
                    )
                self.session.record_assistant(response)
                self.last_attempts = tuple([*failures, ProviderAttempt(provider_name, True)])
                return response
            except LLMError as exc:
                last_error = exc
                failures.append(ProviderAttempt(provider_name, False, type(exc).__name__))
                record_metric("provider_failures")
                # A deterministic request/schema defect is not a provider health
                # failure. Do not cool down a perfectly healthy key while the
                # AgentSession repairs the request surface.
                if not isinstance(exc, LLMRequestError):
                    self._mark_failure(index, exc)
                trace(
                    "gateway.provider_session.failover" if self._should_failover(exc) else "gateway.provider_session.request_error",
                    failed_provider=provider_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    next_provider=self._next_healthy_provider_name(index),
                )
                self._close_session()
                if not self._should_failover(exc):
                    self.last_attempts = tuple(failures)
                    raise
                next_index = (index + 1) % len(self.providers)
                continue
            except Exception as exc:
                last_error = exc
                failures.append(ProviderAttempt(provider_name, False, type(exc).__name__))
                self._mark_failure(index, exc)
                self.last_attempts = tuple(failures)
                raise

        self.last_attempts = tuple(failures)
        raise last_error or self._all_blocked_error()

    def adopt_rejected_call(
        self,
        content: str,
        response: LLMResponse,
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None = None,
    ) -> bool:
        """Continue on the active provider as if ``response`` had been returned for ``content``."""
        try:
            if self.session is None:
                self.session = ProviderSession.create(
                    self.providers[self.active_index],
                    system_instruction=system_instruction,
                    tools=tools,
                    handoff=handoff,
                )
            return self.session.seed_turn(content, response)
        except Exception:
            self._close_session()
            return False

    def _send_tool_results_once(
        self,
        results: list[tuple[LLMToolCall, dict[str, Any]]],
        *,
        system_instruction: str,
        tools: list[dict[str, Any]],
        handoff: SessionHandoff | None = None,
    ) -> LLMResponse:
        """Continue a tool round and walk the configured provider chain on recoverable failure."""
        attempted: set[int] = set()
        failures: list[ProviderAttempt] = []
        last_error: Exception | None = None
        next_index = (self.active_index + 1) % len(self.providers) if self._is_blocked(self.active_index) else self.active_index

        while len(attempted) < len(self.providers):
            candidates = self._candidate_indices(start=next_index)
            index = next((candidate for candidate in candidates if candidate not in attempted), None)
            if index is None:
                break
            attempted.add(index)

            switched = index != self.active_index
            if switched:
                self._close_session()
                self.active_index = index
                self.switch_count += 1
                record_metric("provider_switches")

            provider_name = self.providers[index].name
            record_metric("provider_attempts")
            record_metric("llm_calls")
            try:
                if self.session is None:
                    self.session = ProviderSession.create(
                        self.providers[index],
                        system_instruction=system_instruction,
                        tools=tools,
                        handoff=handoff if switched else handoff,
                    )

                if switched and handoff is not None:
                    response = self.session.continue_from_handoff()
                else:
                    response = self.session.send_tool_results(
                        results,
                        system_instruction=system_instruction,
                        tools=tools,
                        handoff=handoff,
                    )
                self.session.record_assistant(response)
                self.last_attempts = tuple([*failures, ProviderAttempt(provider_name, True)])
                return response
            except LLMError as exc:
                last_error = exc
                failures.append(ProviderAttempt(provider_name, False, type(exc).__name__))
                record_metric("provider_failures")
                # A deterministic request/schema defect is not a provider health
                # failure. Do not cool down a perfectly healthy key while the
                # AgentSession repairs the request surface.
                if not isinstance(exc, LLMRequestError):
                    self._mark_failure(index, exc)
                trace(
                    "gateway.provider_session.failover" if self._should_failover(exc) else "gateway.provider_session.request_error",
                    failed_provider=provider_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    next_provider=self._next_healthy_provider_name(index),
                )
                self._close_session()
                if not self._should_failover(exc):
                    self.last_attempts = tuple(failures)
                    raise
                next_index = (index + 1) % len(self.providers)
                continue
            except Exception as exc:
                last_error = exc
                failures.append(ProviderAttempt(provider_name, False, type(exc).__name__))
                self._mark_failure(index, exc)
                self.last_attempts = tuple(failures)
                raise

        self.last_attempts = tuple(failures)
        raise last_error or self._all_blocked_error()

    def _next_healthy_index(self, failed_index: int) -> int | None:
        for index in self._candidate_indices(start=(failed_index + 1) % len(self.providers)):
            if index != failed_index:
                return index
        return None

    def _next_healthy_provider_name(self, failed_index: int) -> str | None:
        index = self._next_healthy_index(failed_index)
        return self.providers[index].name if index is not None else None

    def close(self) -> None:
        self._close_session()
