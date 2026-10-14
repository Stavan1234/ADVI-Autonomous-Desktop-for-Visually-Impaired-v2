from __future__ import annotations

from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.core.capability_readiness import CapabilityReadinessChecker


class Handler:
    def execute(self, action, *, approval_granted=False):
        raise NotImplementedError


def test_default_like_registered_action_is_ready():
    registry = CapabilityRegistry()
    registry.register(
        Capability(
            name="files",
            description="files",
            supported_actions={"read_file"},
            status=CapabilityStatus.AVAILABLE,
            handler=Handler(),
        )
    )
    report = CapabilityReadinessChecker().check(registry)
    assert report.ready
    assert report.checked_actions == 1


def test_readiness_detects_missing_contract():
    registry = CapabilityRegistry()
    registry.register(
        Capability(
            name="custom",
            description="custom",
            supported_actions={"invented_action"},
            status=CapabilityStatus.AVAILABLE,
            handler=Handler(),
        )
    )
    report = CapabilityReadinessChecker().check(registry)
    assert not report.ready
    assert any(issue.code == "missing_contract" for issue in report.issues)


def test_readiness_detects_missing_handler_for_available_capability():
    registry = CapabilityRegistry()
    registry.register(
        Capability(
            name="files",
            description="files",
            supported_actions={"read_file"},
            status=CapabilityStatus.AVAILABLE,
            handler=None,
        )
    )
    report = CapabilityReadinessChecker().check(registry)
    assert not report.ready
    assert any(issue.code == "missing_handler" for issue in report.issues)
