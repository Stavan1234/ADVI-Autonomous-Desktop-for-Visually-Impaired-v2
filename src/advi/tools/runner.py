from __future__ import annotations

from dataclasses import replace

from advi.core.execution_engine import ExecutionEngine
from advi.core.pipeline_trace import trace, trace_exception

from .contracts import ToolCall, ToolResult
from .guard import ToolGuard
from .registry import ToolRegistry


class ToolRunner:
    """Canonical model-facing execution boundary."""

    def __init__(
        self,
        execution_engine: ExecutionEngine,
        guard: ToolGuard | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.execution_engine = execution_engine
        self.guard = guard
        self.registry = registry or (getattr(guard, "registry", None) if guard is not None else None)

    def run(self, call: ToolCall, *, approved: bool = False) -> ToolResult:
        trace(
            "gateway.tool_runner.run.enter",
            source_component="ToolRunner",
            call_id=call.call_id,
            name=call.name,
            arguments=dict(call.arguments),
            approved=approved,
        )
        try:
            result = self._run(call, approved=approved)
        except Exception as exc:
            trace_exception("gateway.tool_runner.run", exc, name=call.name, arguments=dict(call.arguments))
            raise
        trace(
            "gateway.tool_runner.run.exit",
            source_component="ToolRunner",
            call_id=result.call_id,
            name=result.name,
            success=result.success,
            output=result.output,
            error=result.error,
            verified=result.verified,
            verification_status=str(getattr(result.verification_status, "value", result.verification_status)),
            metadata=dict(result.metadata or {}),
            requires_confirmation=result.requires_confirmation,
        )
        return result

    def _run(self, call: ToolCall, *, approved: bool = False) -> ToolResult:
        if self.guard is not None:
            if approved:
                self.guard.approve(call)
            blocked = self.guard.check(call)
            if blocked is not None:
                return blocked
            if approved:
                self.guard.consume_approval(call)

        runtime_call = call
        if self.registry is not None:
            runtime_name = self.registry.runtime_name(call.name)
            if runtime_name is None:
                return ToolResult(
                    call_id=call.call_id,
                    name=call.name,
                    success=False,
                    error=f"Tool '{call.name}' is not executable.",
                )
            runtime_call = replace(call, name=runtime_name)

        execute = self.execution_engine.execute_tool_call
        result = execute(runtime_call, approval_granted=approved)
        if result.name != call.name:
            result = replace(
                result,
                name=call.name,
                metadata={**dict(result.metadata), "runtime_tool": runtime_call.name},
            )
        return result
