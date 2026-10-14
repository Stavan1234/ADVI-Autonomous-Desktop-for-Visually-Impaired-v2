from __future__ import annotations

from typing import Any

from advi.tools.contracts import ToolCall, ToolResult, VerificationStatus

from .david_adapter import DavidAdapter, DavidResult


class DavidFallbackCapability:
    """Native capability boundary for the supported David fallback system."""

    SUPPORTED_ACTIONS = {"david_fallback"}

    def __init__(self, adapter: DavidAdapter) -> None:
        self.adapter = adapter

    def is_available(self) -> bool:
        return self.adapter.available()

    def execute(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        request = str(call.arguments.get("request") or "").strip()
        reason = str(call.arguments.get("reason") or "model_selected_fallback").strip()
        stage = str(call.arguments.get("stage") or "").strip() or None
        operation_intent = str(call.arguments.get("operation_intent") or "").strip() or None
        protected_parameters = call.arguments.get("protected_parameters")
        if not isinstance(protected_parameters, dict):
            protected_parameters = None
        if not request and stage != "send_prepared":
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error="David fallback requires the original user request.",
                metadata={"failure_class": "invalid_input", "fallback": True},
            )

        outcome = self.adapter.execute(
            request,
            reason=reason,
            approved=approval_granted,
            stage=stage,
            operation_intent=operation_intent,
            protected_parameters=protected_parameters,
        )
        return self._to_tool_result(call, outcome)

    @staticmethod
    def _safe_result(item: Any) -> dict[str, Any]:
        def bound(value: Any, limit: int = 4000) -> Any:
            if value is None or isinstance(value, (bool, int, float)):
                return value
            text = str(value)
            return text if len(text) <= limit else text[:limit] + "..."

        return {
            "action": bound(getattr(item, "action", ""), 200),
            "success": bool(getattr(item, "success", False)),
            "data": bound(getattr(item, "data", None)),
            "error": bound(getattr(item, "error", None), 1000),
        }

    @staticmethod
    def _to_tool_result(call: ToolCall, outcome: DavidResult) -> ToolResult:
        metadata = {
            "fallback": True,
            "fallback_system": "david",
            **dict(outcome.metadata or {}),
        }
        # FallbackService reports confirmation through an ordinary result metadata
        # object. Preserve that information rather than treating it as an error.
        raw_results = list(outcome.results or [])
        safe_results = [DavidFallbackCapability._safe_result(item) for item in raw_results[:32]]
        if raw_results:
            metadata["result_count"] = len(raw_results)
            if len(raw_results) > 32:
                metadata["result_results_truncated"] = True
        if outcome.reason:
            metadata["fallback_reason"] = outcome.reason

        # David's ExecutionLoop is the verification authority for its own isolated
        # subsystem. A successful loop is treated as verified-by-fallback rather
        # than pretending the primary verifier inspected every desktop effect.
        if outcome.success:
            awaiting_send = bool(metadata.get("awaiting_send_confirmation"))
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=True,
                output=safe_results,
                human_readable=outcome.text or "David completed the fallback task.",
                verified=True,
                verification_status=VerificationStatus.VERIFIED,
                verification_details={"source": "david_execution_loop", "result_count": len(raw_results)},
                metadata=metadata,
                requires_confirmation=awaiting_send,
                confirmation_reason=("The prepared David Gmail draft is ready; confirmation is required only for the send." if awaiting_send else None),
            )

        if isinstance(outcome.results, list) and getattr(outcome, "text", "") == "" and outcome.reason:
            metadata["fallback_failed"] = True

        if getattr(outcome, "reason", "") and getattr(outcome, "success", False) is False:
            metadata["raw_reason"] = outcome.reason

        awaiting = bool(metadata.get("awaiting_confirmation"))
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            success=False,
            output=safe_results,
            human_readable=outcome.text or None,
            error=(None if awaiting else (outcome.reason or "David fallback did not complete the request.")),
            verified=False,
            verification_status=VerificationStatus.UNCERTAIN if awaiting else VerificationStatus.FAILED,
            metadata=metadata,
            requires_confirmation=awaiting,
            confirmation_reason=("David fallback requires confirmation for the delegated action." if awaiting else None),
        )
