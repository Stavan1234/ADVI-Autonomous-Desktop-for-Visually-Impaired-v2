from __future__ import annotations

import logging
from typing import Any

from advi.capabilities.desktop.context import DesktopContext
from advi.capabilities.registry import CapabilityRegistry, get_default_registry
from advi.tools.contracts import ToolCall, ToolResult
from .action_contracts import validate_and_normalize_tool_call
from .verification import ToolVerifier
from .pipeline_trace import trace, trace_exception

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Native local execution boundary for model-issued ToolCalls.

    The primary runtime accepts only canonical ``ToolCall`` objects. Legacy
    Action/ActionPlan orchestration is outside this native execution boundary.
    """

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        verifier: Any | None = None,
        context: DesktopContext | None = None,
    ) -> None:
        self.registry = registry or get_default_registry()
        self.verifier = verifier or ToolVerifier()
        self.context = context or getattr(self.registry, "desktop_context", None) or DesktopContext()

    def execute_tool_call(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        """Execute one canonical ToolCall and verify the resulting environment state."""
        if not isinstance(call, ToolCall):
            raise TypeError("ExecutionEngine.execute_tool_call() requires a ToolCall.")

        trace(
            "gateway.execution_engine.enter",
            source_component="ExecutionEngine",
            call_id=call.call_id,
            name=call.name,
            arguments=dict(call.arguments),
            approval_granted=approval_granted,
        )

        normalized, errors = validate_and_normalize_tool_call(call)
        if errors:
            result = ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error="Invalid tool arguments: " + " ".join(errors),
                metadata={"validation_errors": errors},
            )
            trace("gateway.execution_engine.exit", source_component="ExecutionEngine", **_tool_result_fields(result))
            return result
        if normalized is not None:
            call = normalized

        self.registry.refresh_for_action(call.name)
        capability = self.registry.get_capability_for_action(call.name)
        if capability is None:
            return _finish_execution(ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error=f"No capability registered for tool '{call.name}'.",
            ))
        if not capability.is_available():
            return _finish_execution(ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error=f"Capability '{capability.name}' is currently unavailable.",
                metadata={"capability": capability.name},
            ))

        handler = capability.handler
        execute = getattr(handler, "execute", None) if handler is not None else None
        if not callable(execute):
            return _finish_execution(ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error=f"Capability handler for '{capability.name}' is missing.",
            ))

        logger.info("Executing tool '%s' via capability '%s'", call.name, capability.name)
        trace(
            "gateway.execution_engine.dispatch",
            source_component="ExecutionEngine",
            tool=call.name,
            capability=capability.name,
            handler=type(handler).__name__ if handler is not None else None,
        )
        try:
            result = execute(call, approval_granted=approval_granted)
        except Exception as exc:
            logger.exception("Exception during execution of tool '%s'", call.name)
            trace_exception("gateway.execution_engine.execute", exc, name=call.name, capability=capability.name)
            return _finish_execution(ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error=f"Execution error: {exc}",
                metadata={"exception_type": type(exc).__name__},
            ))

        if not isinstance(result, ToolResult):
            logger.error("Capability '%s' returned %s instead of ToolResult", capability.name, type(result).__name__)
            return _finish_execution(ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error=f"Capability '{capability.name}' returned an invalid result type.",
            ))

        if result.call_id != call.call_id or result.name != call.name:
            result = ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=result.success,
                output=result.output,
                human_readable=result.human_readable,
                error=result.error,
                verified=result.verified,
                verification_status=result.verification_status,
                verification_details=result.verification_details,
                evidence=result.evidence,
                metadata={**dict(result.metadata), "capability": capability.name},
                requires_confirmation=result.requires_confirmation,
                confirmation_reason=result.confirmation_reason,
                security_flags=result.security_flags,
            )

        verified = self.verifier.verify(call, result)
        return _finish_execution(verified)

    def execute(self, call: ToolCall) -> ToolResult:
        """Explicit alias for the canonical native execution operation."""
        return self.execute_tool_call(call)

def _tool_result_fields(result: ToolResult) -> dict[str, Any]:
    return {
        "call_id": result.call_id,
        "name": result.name,
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "verified": result.verified,
        "verification_status": str(getattr(result.verification_status, "value", result.verification_status)),
        "verification_details": dict(result.verification_details or {}),
        "metadata": dict(result.metadata or {}),
        "human_readable": result.human_readable,
        "requires_confirmation": result.requires_confirmation,
    }


def _finish_execution(result: ToolResult) -> ToolResult:
    trace("gateway.execution_engine.exit", source_component="ExecutionEngine", **_tool_result_fields(result))
    return result
