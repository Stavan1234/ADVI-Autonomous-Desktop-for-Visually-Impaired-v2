from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .health import HealthReport, HealthStatus


@dataclass(frozen=True)
class StartupHealth:
    """Read-only startup readiness decision derived from runtime health."""

    state: str
    message: str
    blocking_checks: list[str] = field(default_factory=list)
    degraded_checks: list[str] = field(default_factory=list)
    report: HealthReport | None = None

    @property
    def ready(self) -> bool:
        return self.state in {"ready", "degraded"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ready": self.ready,
            "message": self.message,
            "blocking_checks": list(self.blocking_checks),
            "degraded_checks": list(self.degraded_checks),
            "health": self.report.to_dict() if self.report else None,
        }


class StartupHealthValidator:
    """Validate whether ADVI may safely enter its interactive loop.

    This class only inspects state. It never repairs, executes user actions,
    retries providers, or automatically changes capability configuration.
    """

    _blocking = {
        "runtime": HealthStatus.UNHEALTHY,
        "provider": HealthStatus.UNHEALTHY,
        "task_state": HealthStatus.UNHEALTHY,
    }

    def validate(self, report: HealthReport) -> StartupHealth:
        blocking: list[str] = []
        degraded: list[str] = []
        checks_list = report.get('checks', []) if isinstance(report, dict) else getattr(report, 'checks', [])
        
        for check in checks_list:
            status = check.get('status') if isinstance(check, dict) else getattr(check, 'status', None)
            name = check.get('name') if isinstance(check, dict) else getattr(check, 'name', None)
            
            if status == HealthStatus.UNHEALTHY and name in self._blocking:
                blocking.append(name)
            elif status in {HealthStatus.DEGRADED, HealthStatus.UNKNOWN, HealthStatus.UNHEALTHY}:
                degraded.append(name)

        if blocking:
            return StartupHealth(
                state="not_ready",
                message="ADVI cannot safely enter interactive mode yet.",
                blocking_checks=blocking,
                degraded_checks=[name for name in degraded if name not in blocking],
                report=report,
            )

        report_status = report.get('status') if isinstance(report, dict) else getattr(report, 'status', None)
        if report_status in {HealthStatus.DEGRADED, HealthStatus.UNKNOWN} or degraded:  
            return StartupHealth(
                state="degraded",
                message="ADVI can start, but some capabilities or services are degraded.",
                degraded_checks=degraded,
                report=report,
            )

        return StartupHealth(
            state="ready",
            message="ADVI is ready for interactive use.",
            report=report,
        )


__all__ = ["StartupHealth", "StartupHealthValidator"]
