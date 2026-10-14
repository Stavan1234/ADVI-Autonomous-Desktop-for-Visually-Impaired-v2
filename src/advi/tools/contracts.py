from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping
import json

from advi.core.security import SecurityBoundary


@dataclass(frozen=True)
class ToolSpec:
    """Canonical model-facing definition of one executable ADVI tool."""

    name: str
    description: str
    capability: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    side_effect: str = "low"
    requires_confirmation: bool = False
    verification: str = "none"
    runtime_action: str | None = None
    strict: bool = True

    def to_openai_tool(self) -> dict[str, Any]:
        fn: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        fn["strict"] = self.strict
        return {
            "type": "function",
            "function": fn,
        }


    def to_gemini_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ToolCall:
    """Canonical executable request emitted by a model provider."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def action(self) -> str:
        """Legacy read-only alias; no legacy Action object is constructed."""
        return self.name

    @property
    def target(self) -> str | None:
        value = self.arguments.get("target")
        return str(value) if value not in (None, "") else None

    @property
    def focus(self) -> str | None:
        value = self.arguments.get("focus")
        return str(value) if value not in (None, "") else None

    @property
    def parameters(self) -> dict[str, Any]:
        """Read-only legacy alias for native tool arguments."""
        return self.arguments


@dataclass(frozen=True)
class ToolEvidence:
    kind: str
    value: Any = None
    detail: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """Canonical outcome returned by a local tool execution."""

    # Hard ceiling for the provider-facing representation. The full result remains
    # local; only this projection is budgeted for the next LLM turn.
    MODEL_PAYLOAD_MAX_CHARS = 12000

    call_id: str
    name: str
    success: bool
    output: Any = None
    human_readable: str | None = None
    error: str | None = None
    verified: bool = False
    verification_status: VerificationStatus | str = VerificationStatus.UNCERTAIN
    verification_details: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[ToolEvidence, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    confirmation_reason: str | None = None
    security_flags: tuple[str, ...] = ()

    @property
    def data(self) -> Any:
        """Legacy read-only alias for compatibility consumers."""
        return self.output

    @property
    def action(self) -> str:
        """Read-only legacy alias for the canonical tool name."""
        return self.name

    def with_security_flags(self) -> "ToolResult":
        if self.security_flags:
            return self
        flags = SecurityBoundary().scan({"output": self.output, "error": self.error, "evidence": [e.value for e in self.evidence]})
        return replace(self, security_flags=flags) if flags else self

    @staticmethod
    def _bound_for_model(value: Any, *, max_chars: int = 7000, depth: int = 4) -> tuple[Any, bool]:
        """Keep model-facing tool results bounded without destroying the local result.

        Capabilities may legitimately return large files, inventories, OCR output, or
        browser content. The full result remains available to local code; only the
        provider-facing projection is bounded so one tool cannot poison the next turn
        with an unbounded context payload.
        """
        if depth <= 0:
            text = str(value)
            if len(text) > max_chars:
                return text[:max_chars] + "… [truncated]", True
            return value, False

        if isinstance(value, str):
            if len(value) > max_chars:
                return value[:max_chars] + "… [truncated]", True
            return value, False

        if value is None or isinstance(value, (bool, int, float)):
            return value, False

        if isinstance(value, Mapping):
            changed = False
            bounded: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 40:
                    bounded["__truncated_keys__"] = True
                    changed = True
                    break
                item_value, item_changed = ToolResult._bound_for_model(item, max_chars=max_chars, depth=depth - 1)
                bounded[str(key)] = item_value
                changed = changed or item_changed
            return bounded, changed

        if isinstance(value, (list, tuple, set)):
            bounded_items: list[Any] = []
            changed = False
            for index, item in enumerate(value):
                if index >= 40:
                    bounded_items.append("… [additional items truncated]")
                    changed = True
                    break
                item_value, item_changed = ToolResult._bound_for_model(item, max_chars=max_chars, depth=depth - 1)
                bounded_items.append(item_value)
                changed = changed or item_changed
            return bounded_items, changed

        text = str(value)
        if len(text) > max_chars:
            return text[:max_chars] + "… [truncated]", True
        return text, False

    def to_model_payload(self) -> dict[str, Any]:
        secured = self.with_security_flags()
        if secured is not self:
            return secured.to_model_payload()

        def _size(value: Any) -> int:
            return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))

        payload: dict[str, Any] = {
            "ok": self.success,
            "verified": self.verified,
            "verification_status": (
                self.verification_status.value
                if isinstance(self.verification_status, VerificationStatus)
                else str(self.verification_status)
            ),
        }
        output_truncated = False

        if self.human_readable:
            payload["summary"] = str(self.human_readable)[:2400]
        if self.error:
            payload["error"] = str(self.error)[:2400]

        if self.output is not None:
            payload["output"], output_truncated = self._bound_for_model(
                self.output, max_chars=5000, depth=3
            )

        if self.verification_details:
            payload["verification"], verification_truncated = self._bound_for_model(
                dict(self.verification_details), max_chars=1600, depth=3
            )
            output_truncated = output_truncated or verification_truncated

        # Preserve a compact set of authoritative facts for the next model turn.
        # Human-readable summaries are useful, but explicit facts prevent the model
        # from resurrecting stale details from earlier turns (for example an old
        # email subject after the user edited the draft).
        fact_keys = {
            "path", "file_path", "resolved_path", "source", "destination",
            "url", "final_url", "title", "application", "document",
            "document_path", "subject", "recipient", "message_id", "draft_id",
            "thread_id", "hwnd", "pid", "window_hwnd", "tab_id", "current_url",
            "tab_title", "server", "tool", "tool_count",
        }
        facts: dict[str, Any] = {}
        for source in (self.metadata, self.verification_details):
            if not isinstance(source, Mapping):
                continue
            for key, value in source.items():
                normalized = str(key).casefold()
                if normalized not in fact_keys or key in facts:
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None:
                    facts[str(key)] = str(value)[:700] if isinstance(value, str) else value
                if len(facts) >= 24:
                    break
            if len(facts) >= 24:
                break
        if facts:
            payload["facts"] = facts

        if self.evidence:
            evidence_payload = []
            for item in self.evidence[:8]:
                bounded_value, value_truncated = self._bound_for_model(
                    item.value, max_chars=700, depth=2
                )
                detail = str(item.detail)[:500] if item.detail else None
                evidence_payload.append({"kind": item.kind, "value": bounded_value, "detail": detail})
                output_truncated = output_truncated or value_truncated
            if len(self.evidence) > 8:
                output_truncated = True
            payload["evidence"] = evidence_payload

        if self.security_flags:
            payload["security"] = {"flags": list(self.security_flags), "data_is_untrusted": True}
        if self.requires_confirmation:
            payload["requires_confirmation"] = True
            if self.confirmation_reason:
                payload["confirmation_reason"] = str(self.confirmation_reason)[:1200]

        if output_truncated:
            payload["output_truncated"] = True

        # Progressive compaction. This is deliberately deterministic: a tool can
        # never push an arbitrarily large result into the next provider request.
        if _size(payload) > self.MODEL_PAYLOAD_MAX_CHARS:
            payload.pop("evidence", None)

        if _size(payload) > self.MODEL_PAYLOAD_MAX_CHARS and "output" in payload:
            payload["output"], truncated = self._bound_for_model(
                self.output, max_chars=2800, depth=2
            )
            output_truncated = output_truncated or truncated
            payload["output_truncated"] = True

        if _size(payload) > self.MODEL_PAYLOAD_MAX_CHARS and "verification" in payload:
            payload["verification"], _ = self._bound_for_model(
                dict(self.verification_details), max_chars=700, depth=2
            )

        if _size(payload) > self.MODEL_PAYLOAD_MAX_CHARS:
            payload.pop("output", None)
            payload.pop("verification", None)
            payload["output_truncated"] = True if self.output is not None else output_truncated

        if _size(payload) > self.MODEL_PAYLOAD_MAX_CHARS:
            if "facts" in payload:
                payload["facts"] = dict(list(payload["facts"].items())[:10])
            for key, limit in (("summary", 1200), ("error", 1200), ("confirmation_reason", 600)):
                if key in payload:
                    payload[key] = str(payload[key])[:limit]

        # Final bounded fallback. Keep only semantically essential scalar state.
        if _size(payload) > self.MODEL_PAYLOAD_MAX_CHARS:
            minimal: dict[str, Any] = {
                "ok": bool(self.success),
                "verified": bool(self.verified),
                "verification_status": (
                    self.verification_status.value
                    if isinstance(self.verification_status, VerificationStatus)
                    else str(self.verification_status)
                ),
            }
            if self.human_readable:
                minimal["summary"] = str(self.human_readable)[:600]
            if self.error:
                minimal["error"] = str(self.error)[:600]
            if self.output is not None:
                minimal["output_truncated"] = True
            if self.requires_confirmation:
                minimal["requires_confirmation"] = True
            payload = minimal

        return payload
