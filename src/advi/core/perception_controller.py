from __future__ import annotations

"""Small policy layer for targeted, domain-correct perception.

The primary LLM remains responsible for semantic reasoning. This module decides
whether a *local* observation is justified and, when it is, which observer should
be used. It deliberately does not inspect arbitrary surfaces or invent recovery
steps.
"""

from dataclasses import dataclass
import hashlib
import time
from typing import Any, Collection

from advi.agent.contracts import ExecutionDomain, TaskState
from advi.core.execution_strategy import ExecutionStrategy, is_observation_tool
from advi.tools.contracts import ToolCall, ToolResult


@dataclass(frozen=True)
class PerceptionDecision:
    """A bounded decision about model-visible perception."""

    needed: bool
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    reason: str = ""
    domain: str | None = None


class PerceptionController:
    """Choose the smallest useful observer for the current execution surface."""

    OBSERVATION_REUSE_SECONDS = 2.0
    BROWSER_OBSERVER = "inspect_web_page"
    DESKTOP_OBSERVER = "inspect_window"

    _BROWSER_ACTIONS = frozenset({
        "navigate", "search", "click_web_element", "type_web_element",
        "read_web_page", "go_back", "go_forward", "reload_page",
    })
    _DESKTOP_ACTIONS = frozenset({
        "open_application", "new_document", "save_document", "save_document_as",
        "close_document", "close_window", "close_application", "focus_window",
        "type_text", "press_key", "hotkey", "click", "double_click", "right_click",
        "scroll", "read_screen",
    })
    _FILES_ACTIONS = frozenset({
        "find_file", "list_files", "read_file", "open_file", "open_folder",
        "create_file", "save_file", "append_file", "replace_file_text", "delete_file",
        "copy_file", "move_file", "rename_file", "copy_folder", "move_folder",
        "rename_folder", "delete_folder",
    })
    _GMAIL_ACTIONS = frozenset({
        "email_read", "email_draft_create", "email_draft_read", "email_draft_update",
        "email_send", "email_reply", "contact_lookup",
    })

    @staticmethod
    def _domain(task: TaskState | None, call: ToolCall) -> str | None:
        if task is not None and task.active_domain:
            return str(task.active_domain).casefold()
        name = str(call.name or "").casefold()
        if name in PerceptionController._BROWSER_ACTIONS:
            return ExecutionDomain.BROWSER.value
        if name in PerceptionController._DESKTOP_ACTIONS:
            return ExecutionDomain.DESKTOP.value
        if name in PerceptionController._FILES_ACTIONS:
            return ExecutionDomain.FILESYSTEM.value
        if name in PerceptionController._GMAIL_ACTIONS:
            return ExecutionDomain.GMAIL.value
        return None

    @staticmethod
    def _fingerprint(call: ToolCall, result: ToolResult, domain: str | None) -> str:
        payload = "|".join((
            str(domain or ""),
            str(call.name or ""),
            repr(sorted((call.arguments or {}).items())),
            str(result.success),
            str(result.verified),
            str(result.verification_status),
            str(result.error or ""),
        ))
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:20]

    def decide(
        self,
        task: TaskState | None,
        call: ToolCall,
        result: ToolResult,
        strategy: ExecutionStrategy,
        *,
        local_sync: dict[str, Any] | None = None,
        available_tools: Collection[str] = (),
        now: float | None = None,
    ) -> PerceptionDecision:
        """Return one targeted observation decision, or no observation.

        Observation is an escalation path. It is requested only when the action
        is state-changing, the result is not confidently verified, or local
        synchronization reports an unexpected/unready surface.
        """
        if task is None or not result.success:
            return PerceptionDecision(False, reason="no successful action to observe")
        if is_observation_tool(call.name):
            return PerceptionDecision(False, reason="the action already produced observation")
        if strategy.mode.value == "simple":
            return PerceptionDecision(False, reason="simple operation needs no adaptive perception")

        sync = dict(local_sync or {})
        needed = (not result.verified) or bool(sync.get("unexpected")) or not bool(sync.get("ready", True))
        if not needed:
            return PerceptionDecision(False, reason="verified action has healthy local execution surface")

        domain = self._domain(task, call)
        if domain is None:
            return PerceptionDecision(False, reason="no safe observation domain")

        available = {str(name) for name in available_tools}
        tool_name: str | None = None
        arguments: dict[str, Any] = {}

        if domain == ExecutionDomain.BROWSER.value and self.BROWSER_OBSERVER in available:
            tool_name = self.BROWSER_OBSERVER
            arguments = {"limit": 24}
        elif domain == ExecutionDomain.DESKTOP.value and self.DESKTOP_OBSERVER in available:
            tool_name = self.DESKTOP_OBSERVER
        elif domain in {ExecutionDomain.FILESYSTEM.value, ExecutionDomain.GMAIL.value}:
            # Do not manufacture a generic desktop observation for API/stateful
            # operations. Their operation result is the authoritative evidence; a
            # later brain turn can explicitly request a domain-specific read tool.
            return PerceptionDecision(False, reason=f"no generic observer for {domain}", domain=domain)
        else:
            return PerceptionDecision(False, reason=f"observer unavailable for {domain}", domain=domain)

        fingerprint = self._fingerprint(call, result, domain)
        runtime = task.context
        last_fp = str(runtime.get("perception_fingerprint") or "")
        last_at_raw = runtime.get("perception_at")
        try:
            last_at = float(last_at_raw) if last_at_raw is not None else None
        except (TypeError, ValueError):
            last_at = None
        current = time.monotonic() if now is None else float(now)
        if last_fp == fingerprint and last_at is not None and current - last_at <= self.OBSERVATION_REUSE_SECONDS:
            return PerceptionDecision(False, reason="recent equivalent observation is still fresh", domain=domain)

        return PerceptionDecision(
            True,
            tool_name=tool_name,
            arguments=arguments,
            reason=(
                "action result is unverified" if not result.verified
                else "local synchronization reports unexpected or unready execution surface"
            ),
            domain=domain,
        )

    def record(self, task: TaskState | None, call: ToolCall, result: ToolResult, domain: str | None) -> None:
        if task is None:
            return
        task.set_context("perception_fingerprint", self._fingerprint(call, result, domain))
        task.set_context("perception_at", time.monotonic())
        task.set_context("perception_domain", domain or "")
        task.set_context("perception_tool", "inspect_web_page" if domain == ExecutionDomain.BROWSER.value else "inspect_window")
