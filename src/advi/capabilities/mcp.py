from __future__ import annotations


from advi.tools.contracts import ToolCall, ToolResult
from advi.integrations.mcp import MCPManager


class MCPCapability:
    """Expose configured MCP servers through two deterministic ADVI actions."""

    SUPPORTED_ACTIONS = {"mcp_list_tools", "mcp_call"}

    def __init__(self, manager: MCPManager | None) -> None:
        self.manager = manager

    def is_available(self) -> bool:
        return self.manager is not None and self.manager.is_available()

    def execute(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        handler = getattr(self, f"_execute_{action.name}", None)
        if not callable(handler):
            return ToolResult(action.call_id, action.name, False, error=f"Unsupported MCP action: {action.name}")
        try:
            return handler(action)
        except Exception as exc:
            return ToolResult(
                action.call_id, action.name, False,
                error=str(exc),
                metadata={"failure_class": "mcp_unavailable_or_failed"},
            )

    def _execute_mcp_list_tools(self, action: ToolCall) -> ToolResult:
        if self.manager is None:
            return ToolResult(action.call_id, action.name, False, error="MCP is not configured.")
        server = str(action.parameters.get("server") or "").strip() or None
        rows = self.manager.list_tools(server=server)
        return ToolResult(
            action.call_id, action.name, True, output=rows,
            human_readable=f"Found {len(rows)} MCP tool{'s' if len(rows) != 1 else ''}.",
            verified=True,
            verification_status="verified",
            verification_details={"tool_count": len(rows), "server": server},
            metadata={"server": server, "tool_count": len(rows)},
        )

    def _execute_mcp_call(self, action: ToolCall) -> ToolResult:
        if self.manager is None:
            return ToolResult(action.call_id, action.name, False, error="MCP is not configured.")
        server = str(action.parameters.get("server") or "").strip()
        tool = str(action.parameters.get("tool") or "").strip()
        arguments = action.parameters.get("arguments") or {}
        if not server or not tool:
            return ToolResult(action.call_id, action.name, False, error="MCP call requires server and tool.")
        if not isinstance(arguments, dict):
            return ToolResult(action.call_id, action.name, False, error="MCP arguments must be an object.")
        result = self.manager.call_tool(server, tool, arguments)
        is_error = bool(result.get("is_error"))
        return ToolResult(
            action.call_id, action.name, not is_error,
            output=result.get("output"),
            error="The MCP server reported a tool error." if is_error else None,
            human_readable=f"MCP tool '{tool}' completed." if not is_error else f"MCP tool '{tool}' reported an error.",
            verified=not is_error,
            verification_status="verified" if not is_error else "failed",
            verification_details={"server": server, "tool": tool, "server_reported_error": is_error},
            metadata={"server": server, "tool": tool},
        )


    def close(self) -> None:
        if self.manager is not None:
            self.manager.close()
