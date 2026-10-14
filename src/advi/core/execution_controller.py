from __future__ import annotations

"""Small goal-directed local continuation layer for the primary agent.

The primary LLM remains the planner. This controller only performs a very small
set of *obvious* continuations when the user's goal and the verified tool result
already determine the next action without further semantic reasoning.
"""

from dataclasses import dataclass
import re
from typing import Any, Collection
import uuid

from advi.agent.contracts import ExecutionRoute, TaskState, TaskStatus
from advi.tools.contracts import ToolCall, ToolResult


@dataclass(frozen=True)
class LocalContinuation:
    """One locally executable continuation whose meaning is already explicit."""

    call: ToolCall
    reason: str


class ExecutionController:
    """Keep the LLM as the brain while handling only unambiguous local steps."""

    MAX_CONTINUATIONS_PER_CHAIN = 3

    _SEARCH_INTENT_RE = re.compile(
        r"\b(?:search|find|look\s+for|look\s+up)\b", re.I
    )
    _OPEN_INTENT_RE = re.compile(
        r"\b(?:open|show|display|launch)\b", re.I
    )
    _READ_INTENT_RE = re.compile(
        r"\b(?:read|inspect|view)\b", re.I
    )
    _FIRST_RESULT_RE = re.compile(
        r"\b(?:first|1st)\s+(?:result|video|song|item|match)\b|"
        r"\bfirst\s+one\b|\bfirst\s+result\b",
        re.I,
    )

    def propose(
        self,
        task: TaskState | None,
        call: ToolCall,
        result: ToolResult,
        *,
        available_tools: Collection[str] = (),
        continuation_count: int = 0,
    ) -> LocalContinuation | None:
        """Return at most one safe local continuation for the current result.

        The method is intentionally conservative. It never invents a target, and
        it never continues a David-routed task through the native path.
        """
        if task is None or task.status is not TaskStatus.IN_PROGRESS:
            return None
        if str(task.execution_route or ExecutionRoute.NATIVE.value).casefold() != ExecutionRoute.NATIVE.value:
            return None
        if continuation_count >= self.MAX_CONTINUATIONS_PER_CHAIN:
            return None
        if not result.success or not result.verified:
            return None

        available = {str(name) for name in available_tools}
        goal = str(task.goal or "")
        call_name = str(call.name or "").casefold()

        if call_name == "navigate":
            return self._after_navigate(task, call, result, goal, available)
        if call_name == "find_file":
            return self._after_find_file(task, result, goal, available)
        if call_name == "search":
            return self._after_search(task, result, goal, available)

        return None

    def _after_navigate(
        self,
        task: TaskState,
        call: ToolCall,
        result: ToolResult,
        goal: str,
        available: set[str],
    ) -> LocalContinuation | None:
        """Continue navigation directly into an explicit user-requested search."""
        if "search" not in available:
            return None
        if not self._SEARCH_INTENT_RE.search(goal):
            return None

        query = str(
            task.protected_parameters.get("query")
            or task.context.get("explicit_search_query")
            or ""
        ).strip()
        if not query:
            return None

        current_url = str(
            result.metadata.get("current_url")
            or result.verification_details.get("current_url")
            or ""
        ).casefold()
        if "/results" in current_url or "search?" in current_url:
            return None

        return LocalContinuation(
            call=ToolCall(
                call_id=f"controller-{uuid.uuid4().hex[:12]}",
                name="search",
                arguments={"query": query},
            ),
            reason="the user explicitly requested a search after the navigation and the exact query is protected in task state",
        )

    def _after_find_file(
        self,
        task: TaskState,
        result: ToolResult,
        goal: str,
        available: set[str],
    ) -> LocalContinuation | None:
        """Open a uniquely resolved file when the user explicitly asked to open it."""
        output = result.output
        if not isinstance(output, (list, tuple)):
            return None
        matches = [str(item).strip() for item in output if str(item).strip()]
        if len(matches) != 1:
            return None

        target = matches[0]
        wants_folder = bool(re.search(r"\b(?:folder|directory)\b", goal, re.I))
        if self._OPEN_INTENT_RE.search(goal) and wants_folder and "open_folder" in available:
            return LocalContinuation(
                call=ToolCall(
                    call_id=f"controller-{uuid.uuid4().hex[:12]}",
                    name="open_folder",
                    arguments={"path": target},
                ),
                reason="the file search returned exactly one verified folder match for an explicit open request",
            )
        if self._OPEN_INTENT_RE.search(goal) and not wants_folder and "open_file" in available:
            return LocalContinuation(
                call=ToolCall(
                    call_id=f"controller-{uuid.uuid4().hex[:12]}",
                    name="open_file",
                    arguments={"path": target},
                ),
                reason="the file search returned exactly one verified match for an explicit open request",
            )
        if self._READ_INTENT_RE.search(goal) and "read_file" in available:
            return LocalContinuation(
                call=ToolCall(
                    call_id=f"controller-{uuid.uuid4().hex[:12]}",
                    name="read_file",
                    arguments={"path": target},
                ),
                reason="the file search returned exactly one verified match for an explicit read request",
            )
        return None

    def _after_search(
        self,
        task: TaskState,
        result: ToolResult,
        goal: str,
        available: set[str],
    ) -> LocalContinuation | None:
        """Select an explicitly requested first result when the browser already resolved it."""
        if "click_web_element" not in available:
            return None
        if not self._FIRST_RESULT_RE.search(goal):
            return None

        details = dict(result.verification_details or {})
        candidates = details.get("result_candidates")
        if not isinstance(candidates, list) or not candidates:
            return None

        first: dict[str, Any] | None = None
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("selector"):
                first = candidate
                break
        if first is None:
            return None

        selector = str(first.get("selector") or "").strip()
        if not selector:
            return None

        return LocalContinuation(
            call=ToolCall(
                call_id=f"controller-{uuid.uuid4().hex[:12]}",
                name="click_web_element",
                arguments={"selector": selector},
            ),
            reason="the user explicitly requested the first result and the browser returned a verified first-result selector",
        )
