from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .capability_readiness import CapabilityReadinessChecker
from advi.capabilities.registry import CapabilityRegistry


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: HealthStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class HealthReport:
    status: HealthStatus
    checks: list[HealthCheck]

    @property
    def healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "healthy": self.healthy,
            "checks": [check.to_dict() for check in self.checks],
        }


class ADVIHealthChecker:
    """Read-only health checks for the current native AgentSession runtime."""

    def __init__(self, readiness_checker: CapabilityReadinessChecker | None = None) -> None:
        self.readiness_checker = readiness_checker or CapabilityReadinessChecker()

    def check(self, agent: Any, *, refresh_capabilities: bool = False) -> HealthReport:
        checks = [
            self._runtime_check(agent),
            self._provider_check(agent),
            self._task_state_check(agent),
            self._capability_check(agent, refresh_capabilities=refresh_capabilities),
            self._fallback_check(agent),
        ]
        statuses = [check.status for check in checks]
        if any(status == HealthStatus.UNHEALTHY for status in statuses):
            overall = HealthStatus.UNHEALTHY
        elif any(status == HealthStatus.DEGRADED for status in statuses):
            overall = HealthStatus.DEGRADED
        elif any(status == HealthStatus.UNKNOWN for status in statuses):
            overall = HealthStatus.UNKNOWN
        else:
            overall = HealthStatus.HEALTHY
        return HealthReport(overall, checks)

    @staticmethod
    def _runtime_check(agent: Any) -> HealthCheck:
        runtime = getattr(agent, "runtime", None)
        if runtime is None:
            return HealthCheck(
                "runtime",
                HealthStatus.DEGRADED,
                "Runtime lifecycle state is not attached to this session.",
            )
        if not bool(getattr(runtime, "started", False)):
            details = {}
            error = getattr(runtime, "last_start_error", None)
            if error:
                details["last_start_error"] = error
            return HealthCheck("runtime", HealthStatus.UNHEALTHY, "Runtime is not active.", details)
        return HealthCheck("runtime", HealthStatus.HEALTHY, "Runtime is active.", {"started": True})

    @staticmethod
    def _provider_check(agent: Any) -> HealthCheck:
        manager = getattr(agent, "provider_manager", None)
        if manager is None:
            return HealthCheck("provider", HealthStatus.UNHEALTHY, "Provider session manager is unavailable.")
        providers = list(getattr(manager, "providers", ()) or ())
        if not providers:
            return HealthCheck("provider", HealthStatus.UNHEALTHY, "No LLM provider is configured.")
        attempts = list(getattr(manager, "last_attempts", ()) or ())
        failed = sum(1 for item in attempts if not getattr(item, "success", False))
        if attempts and failed == len(attempts):
            status = HealthStatus.UNHEALTHY
            message = "The last provider attempt failed."
        elif failed:
            status = HealthStatus.DEGRADED
            message = "Provider chain recovered after a failure."
        else:
            status = HealthStatus.HEALTHY
            message = "LLM provider is configured."
        session = getattr(manager, "session", None)
        return HealthCheck(
            "provider", status, message,
            {
                "active": getattr(getattr(manager, "active_provider", None), "name", None),
                "provider_count": len(providers),
                "session_active": session is not None and not bool(getattr(session, "closed", False)),
                "switch_count": int(getattr(manager, "switch_count", 0)),
            },
        )

    @staticmethod
    def _task_state_check(agent: Any) -> HealthCheck:
        task = getattr(agent, "task", None)
        if task is None:
            return HealthCheck("task_state", HealthStatus.HEALTHY, "Agent session is idle; no active task.")
        context = getattr(task, "context", {}) or {}
        status = getattr(getattr(task, "status", None), "value", getattr(task, "status", None))
        if status in {"awaiting_confirmation", "AWAITING_CONFIRMATION"} and not getattr(task, "confirmation_fingerprint", None):
            return HealthCheck("task_state", HealthStatus.UNHEALTHY, "Task is awaiting confirmation without an approval fingerprint.")
        if context.get("_interrupted_step") and status not in {"awaiting_input", "AWAITING_INPUT"}:
            return HealthCheck("task_state", HealthStatus.UNHEALTHY, "Interrupted execution is recorded without an input state.")
        return HealthCheck("task_state", HealthStatus.HEALTHY, "Active task state is internally consistent.", {"status": status, "task_id": getattr(task, "task_id", None)})

    def _capability_check(self, agent: Any, *, refresh_capabilities: bool) -> HealthCheck:
        tools = getattr(agent, "tools", None)
        registry: CapabilityRegistry | None = getattr(tools, "capability_registry", None) if tools is not None else None
        if registry is None:
            return HealthCheck("capabilities", HealthStatus.UNHEALTHY, "Capability registry is missing.")
        if refresh_capabilities:
            try:
                registry.refresh_stale()
            except Exception as exc:
                return HealthCheck("capabilities", HealthStatus.DEGRADED, "Capability refresh failed; using cached state.", {"error": str(exc)})
        report = self.readiness_checker.check(registry)
        unavailable = [c.name for c in registry.list_all() if not c.is_available()]
        optional_unavailable = [
            c.name for c in registry.list_all()
            if not c.is_available() and bool(getattr(c, "metadata", {}).get("optional"))
        ]
        required_unavailable = [name for name in unavailable if name not in optional_unavailable]
        if not report.ready:
            return HealthCheck(
                "capabilities", HealthStatus.UNHEALTHY,
                "Capability readiness audit found issues.",
                {"checked_actions": report.checked_actions, "issues": [issue.__dict__ for issue in report.issues], "unavailable": unavailable},
            )
        if required_unavailable:
            return HealthCheck("capabilities", HealthStatus.DEGRADED, "Some required capabilities are unavailable.", {"unavailable": unavailable, "optional_unavailable": optional_unavailable})
        if optional_unavailable:
            return HealthCheck("capabilities", HealthStatus.HEALTHY, "Core capabilities are ready; some optional integrations are unavailable.", {"unavailable": unavailable, "optional_unavailable": optional_unavailable})
        return HealthCheck("capabilities", HealthStatus.HEALTHY, "Registered capabilities are ready.", {"checked_actions": report.checked_actions})

    @staticmethod
    def _fallback_check(agent: Any) -> HealthCheck:
        tools = getattr(agent, "tools", None)
        registry = getattr(tools, "capability_registry", None) if tools is not None else None
        if registry is None:
            return HealthCheck("fallback", HealthStatus.DEGRADED, "David fallback capability registry is unavailable.", {"available": False, "optional": True})
        try:
            capability = registry.get_capability_for_action("david_fallback")
            if capability is None:
                return HealthCheck("fallback", HealthStatus.HEALTHY, "David fallback is not configured; native capabilities remain available.", {"available": False, "optional": True})
            available = bool(capability.is_available())
            return HealthCheck(
                "fallback",
                HealthStatus.HEALTHY,
                "David fallback is available." if available else "David fallback is unavailable; native capabilities remain available.",
                {"available": available, "optional": True, "capability": capability.name},
            )
        except Exception as exc:
            return HealthCheck("fallback", HealthStatus.UNKNOWN, "David fallback availability could not be determined.", {"error": str(exc), "optional": True})


__all__ = ["HealthStatus", "HealthCheck", "HealthReport", "ADVIHealthChecker"]
