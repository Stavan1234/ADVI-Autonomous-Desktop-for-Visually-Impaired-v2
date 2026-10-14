from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from advi.capabilities.registry import CapabilityRegistry
from .action_contracts import get_action_contract


@dataclass(frozen=True)
class CapabilityReadinessIssue:
    action: str
    capability: str
    code: str
    message: str


@dataclass(frozen=True)
class CapabilityReadinessReport:
    ready: bool
    checked_actions: int
    issues: list[CapabilityReadinessIssue] = field(default_factory=list)


class CapabilityReadinessChecker:
    """Audit the registry before actions are offered to the planner/executor.

    This is deliberately deterministic. It catches drift between the capability
    registry, action contracts, semantic metadata, and concrete handlers.
    """

    def check(self, registry: CapabilityRegistry) -> CapabilityReadinessReport:
        issues: list[CapabilityReadinessIssue] = []
        checked = 0

        for capability in registry.list_all():
            for action in sorted(capability.supported_actions):
                checked += 1
                spec = capability.action_spec(action)
                if spec is None:
                    issues.append(
                        CapabilityReadinessIssue(
                            action, capability.name, "missing_metadata",
                            "Supported action has no semantic metadata.",
                        )
                    )
                    continue

                if get_action_contract(action) is None:
                    issues.append(
                        CapabilityReadinessIssue(
                            action, capability.name, "missing_contract",
                            "Supported action has no canonical action contract.",
                        )
                    )

                if not spec.verification:
                    issues.append(
                        CapabilityReadinessIssue(
                            action, capability.name, "missing_verification",
                            "Supported action has no declared verification strategy.",
                        )
                    )

                if capability.is_available():
                    handler = capability.handler
                    if handler is None or not hasattr(handler, "execute"):
                        issues.append(
                            CapabilityReadinessIssue(
                                action, capability.name, "missing_handler",
                                "Available capability has no executable handler.",
                            )
                        )

        return CapabilityReadinessReport(
            ready=not issues,
            checked_actions=checked,
            issues=issues,
        )


__all__ = ["CapabilityReadinessIssue", "CapabilityReadinessReport", "CapabilityReadinessChecker"]
