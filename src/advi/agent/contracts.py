from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutionDomain(str, Enum):
    """Authoritative interaction domain for the current task state."""

    DESKTOP = "desktop"
    BROWSER = "browser"
    FILESYSTEM = "filesystem"
    GMAIL = "gmail"
    SYSTEM = "system"
    MEMORY = "memory"
    GENERIC = "generic"


class ExecutionRoute(str, Enum):
    """Execution path requested/committed for the current task."""

    NATIVE = "native"
    DAVID = "david"


class TaskStatus(str, Enum):
    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


@dataclass
class TaskState:
    """Bounded task execution state owned by one AgentSession."""

    goal: str
    task_id: str
    status: TaskStatus = TaskStatus.IN_PROGRESS
    waiting_for: str | None = None
    operation_intent: str | None = None
    active_domain: str | None = None
    execution_route: str = ExecutionRoute.NATIVE.value
    current_target: dict[str, Any] = field(default_factory=dict)
    protected_parameters: dict[str, Any] = field(default_factory=dict)
    protected_parameter_operations: dict[str, str] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    failed_tools: list[str] = field(default_factory=list)
    uncertain_tools: list[str] = field(default_factory=list)
    recovered_tools: list[str] = field(default_factory=list)
    tool_outcomes: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    artifact_aliases: dict[str, Any] = field(default_factory=dict)
    completed_side_effects: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_rounds: int = 0

    MAX_CONTEXT_ITEMS = 32
    MAX_CONTEXT_VALUE_CHARS = 4000
    MAX_TOOL_OUTCOMES = 8
    MAX_ARTIFACTS = 24
    MAX_ARTIFACT_VALUE_CHARS = 1200
    MAX_SIDE_EFFECTS = 8
    MAX_SUMMARY_CHARS = 500
    MAX_PROTECTED_PARAMETER_CHARS = 16000
    MAX_PROTECTED_PARAMETERS = 24
    MAX_TARGET_VALUE_CHARS = 2000
    MAX_ARTIFACT_ALIASES = 12

    def set_active_domain(self, domain: str | ExecutionDomain | None, *, source: str = "inferred") -> None:
        """Bind the current task to one authoritative execution domain."""
        value = str(getattr(domain, "value", domain) or "").strip().casefold()
        if not value:
            return
        self.active_domain = value
        self.context["active_domain_source"] = str(source)[:120]

    def set_execution_route(self, route: str | ExecutionRoute, *, explicit: bool = False) -> bool:
        """Set the task execution route without silently overriding an explicit route."""
        value = str(getattr(route, "value", route) or "").strip().casefold()
        if value not in {ExecutionRoute.NATIVE.value, ExecutionRoute.DAVID.value}:
            return False
        current = str(self.execution_route or ExecutionRoute.NATIVE.value).casefold()
        if current == ExecutionRoute.DAVID.value and value == ExecutionRoute.NATIVE.value and not explicit:
            return False
        self.execution_route = value
        self.context["execution_route_explicit"] = bool(explicit)
        return True

    def set_current_target(self, *, kind: str, value: Any, source: str = "user", explicit: bool = True) -> None:
        """Store the currently authoritative target and its provenance."""
        if value in (None, ""):
            return
        safe = str(value)[: self.MAX_TARGET_VALUE_CHARS]
        self.current_target = {
            "kind": str(kind)[:120],
            "value": safe,
            "source": str(source)[:120],
            "explicit": bool(explicit),
        }

    def remember_artifact_alias(self, alias: str, value: Any) -> None:
        """Keep a few typed aliases for natural follow-ups such as 'that PDF'."""
        if value in (None, ""):
            return
        key = str(alias).strip().casefold()
        if not key:
            return
        self.artifact_aliases[key] = str(value)[: self.MAX_ARTIFACT_VALUE_CHARS] if isinstance(value, str) else value
        while len(self.artifact_aliases) > self.MAX_ARTIFACT_ALIASES:
            self.artifact_aliases.pop(next(iter(self.artifact_aliases)), None)

    def protect_parameters(
        self,
        values: dict[str, Any] | None,
        *,
        overwrite: bool = False,
        operation_intent: str | None = None,
    ) -> None:
        """Store exact, execution-relevant parameter values without semantic rewriting."""
        if not isinstance(values, dict):
            return
        for key, value in values.items():
            normalized_key = str(key).strip()
            if not normalized_key or value in (None, ""):
                continue
            if normalized_key in self.protected_parameters and not overwrite:
                continue
            if isinstance(value, str):
                safe_value: Any = value[: self.MAX_PROTECTED_PARAMETER_CHARS]
            elif isinstance(value, (bool, int, float)):
                safe_value = value
            elif isinstance(value, (list, tuple)):
                safe_value = list(value)[:64]
            elif isinstance(value, dict):
                safe_value = dict(list(value.items())[:64])
            else:
                safe_value = str(value)[: self.MAX_PROTECTED_PARAMETER_CHARS]
            self.protected_parameters[normalized_key] = safe_value
            if operation_intent:
                self.protected_parameter_operations[normalized_key] = str(operation_intent)
        if len(self.protected_parameters) > self.MAX_PROTECTED_PARAMETERS:
            keys = list(self.protected_parameters)
            for key in keys[:-self.MAX_PROTECTED_PARAMETERS]:
                self.protected_parameters.pop(key, None)

    def replace_protected_parameter(self, key: str, value: Any) -> None:
        """Explicitly amend one protected parameter after the user changes it."""
        self.protect_parameters({str(key): value}, overwrite=True)
        self.protected_parameter_operations[str(key)] = self.operation_intent or ""

    def consume_tool_round(self, limit: int) -> bool:
        """Consume one model tool-calling round within the task lifetime."""
        maximum = max(1, int(limit))
        if self.tool_rounds >= maximum:
            return False
        self.tool_rounds += 1
        return True

    def record_tool_outcome(self, *, name: str, success: bool, verified: bool, verification_status: str, summary: str) -> None:
        if not success:
            if name not in self.failed_tools:
                self.failed_tools.append(name)
        elif not verified:
            if name not in self.uncertain_tools:
                self.uncertain_tools.append(name)
        self.tool_outcomes.append({
            "tool": str(name),
            "success": bool(success),
            "verified": bool(verified),
            "verification_status": str(verification_status),
            "summary": str(summary)[: self.MAX_SUMMARY_CHARS],
        })
        del self.tool_outcomes[:-self.MAX_TOOL_OUTCOMES]
        del self.failed_tools[:-8]
        del self.uncertain_tools[:-8]
        del self.recovered_tools[:-8]

    def mark_recovered(self, name: str) -> None:
        """Clear a previously failed/uncertain operation after verified recovery."""
        name = str(name)
        if name in self.failed_tools:
            self.failed_tools = [item for item in self.failed_tools if item != name]
        if name in self.uncertain_tools:
            self.uncertain_tools = [item for item in self.uncertain_tools if item != name]
        if name not in self.recovered_tools:
            self.recovered_tools.append(name)
        if len(self.recovered_tools) > 8:
            del self.recovered_tools[:-8]


    def remember_side_effect(self, fingerprint: str, details: dict[str, Any]) -> None:
        """Remember a verified consequential side effect for duplicate suppression."""
        key = str(fingerprint).strip()
        if not key:
            return
        bounded = {str(k): (str(v)[:self.MAX_ARTIFACT_VALUE_CHARS] if isinstance(v, str) else v) for k, v in list(details.items())[:12]}
        self.completed_side_effects[key] = bounded
        if len(self.completed_side_effects) > self.MAX_SIDE_EFFECTS:
            keys = list(self.completed_side_effects)
            for old in keys[:-self.MAX_SIDE_EFFECTS]:
                self.completed_side_effects.pop(old, None)

    def prior_side_effect(self, fingerprint: str) -> dict[str, Any] | None:
        return self.completed_side_effects.get(str(fingerprint).strip())

    def record_artifacts(self, values: dict[str, Any]) -> None:
        """Retain a small set of reusable outputs from completed tool work.

        Artifacts are local task state, not a transcript. They let later steps
        (or provider failover) recover concrete paths/IDs/URLs without replaying
        a giant tool history.
        """
        for key, value in values.items():
            key = str(key).strip()
            if not key or value in (None, ""):
                continue
            if isinstance(value, (str, int, float, bool)):
                normalized = str(value)[: self.MAX_ARTIFACT_VALUE_CHARS] if isinstance(value, str) else value
            elif isinstance(value, (list, tuple)):
                normalized = [str(item)[:300] for item in list(value)[:8]]
            else:
                normalized = str(value)[: self.MAX_ARTIFACT_VALUE_CHARS]
            self.artifacts[key] = normalized

            lowered = key.casefold()
            if lowered in {"folder", "directory"}:
                self.remember_artifact_alias("last_folder", normalized)
            elif lowered in {"path", "file_path", "document_path"}:
                self.remember_artifact_alias("last_file", normalized)
                suffix = str(normalized).casefold().rsplit(".", 1)[-1] if "." in str(normalized) else ""
                if suffix in {"pdf", "docx", "pptx", "xlsx", "txt", "csv"}:
                    self.remember_artifact_alias(f"last_{suffix}", normalized)
            elif lowered == "application":
                self.remember_artifact_alias("last_application", normalized)
            elif lowered in {"url", "final_url"}:
                self.remember_artifact_alias("last_url", normalized)
            elif lowered in {"recipient", "to"}:
                self.remember_artifact_alias("last_recipient", normalized)
            elif lowered in {"subject"}:
                self.remember_artifact_alias("last_subject", normalized)
        if len(self.artifacts) > self.MAX_ARTIFACTS:
            keys = list(self.artifacts)
            for key in keys[:-self.MAX_ARTIFACTS]:
                self.artifacts.pop(key, None)

    def set_context(self, key: str, value: Any) -> None:
        if len(self.context) >= self.MAX_CONTEXT_ITEMS and key not in self.context:
            oldest = next(iter(self.context))
            self.context.pop(oldest, None)
        if isinstance(value, str):
            value = value[: self.MAX_CONTEXT_VALUE_CHARS]
        elif isinstance(value, (list, tuple)):
            value = list(value)[:32]
        elif isinstance(value, dict):
            value = {str(k): str(v)[: self.MAX_CONTEXT_VALUE_CHARS] for k, v in list(value.items())[:32]}
        self.context[str(key)] = value

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal[: self.MAX_CONTEXT_VALUE_CHARS],
            "operation_intent": self.operation_intent,
            "active_domain": self.active_domain,
            "execution_route": self.execution_route,
            "current_target": dict(self.current_target),
            "protected_parameters": dict(self.protected_parameters),
            "protected_parameter_operations": dict(self.protected_parameter_operations),
            "status": self.status.value,
            "waiting_for": self.waiting_for,
            "tool_rounds": self.tool_rounds,
            "context": dict(list(self.context.items())[: self.MAX_CONTEXT_ITEMS]),
            "failed_tools": list(self.failed_tools[-8:]),
            "uncertain_tools": list(self.uncertain_tools[-8:]),
            "recovered_tools": list(self.recovered_tools[-8:]),
            "recent_tool_outcomes": list(self.tool_outcomes[-self.MAX_TOOL_OUTCOMES:]),
            "artifacts": dict(list(self.artifacts.items())[-self.MAX_ARTIFACTS:]),
            "artifact_aliases": dict(self.artifact_aliases),
            "completed_side_effects": dict(list(self.completed_side_effects.items())[-self.MAX_SIDE_EFFECTS:]),
        }
