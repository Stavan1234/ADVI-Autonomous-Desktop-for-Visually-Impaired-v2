from __future__ import annotations

from datetime import datetime, timedelta, timezone

from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus


def test_refresh_stale_does_not_probe_fresh_capability():
    calls = []

    def probe(_):
        calls.append(1)
        return CapabilityStatus.AVAILABLE, "reachable"

    registry = CapabilityRegistry()
    registry.register(Capability(
        name="browser",
        description="browser",
        supported_actions={"navigate"},
        availability_probe=probe,
    ))

    registry.refresh_capability("browser")
    assert calls == [1]

    registry.refresh_stale("browser")
    assert calls == [1]


def test_refresh_stale_reprobes_expired_capability():
    calls = []

    def probe(_):
        calls.append(1)
        return CapabilityStatus.AVAILABLE, f"check-{len(calls)}"

    registry = CapabilityRegistry()
    registry.register(Capability(
        name="external",
        description="external",
        supported_actions={"do_external"},
        availability_probe=probe,
        availability_ttl_seconds=0.0,
    ))

    first = registry.refresh_capability("external")
    second = registry.refresh_stale("external")

    assert first is not None and second is not None
    assert calls == [1, 1]
    assert second.reason == "check-2"


def test_refresh_stale_handles_malformed_timestamp():
    calls = []

    def probe(_):
        calls.append(1)
        return CapabilityStatus.PARTIAL, "degraded"

    registry = CapabilityRegistry()
    cap = Capability(
        name="browser",
        description="browser",
        supported_actions={"navigate"},
        availability_probe=probe,
    )
    registry.register(cap)
    cap.last_checked_at = "not-a-timestamp"

    snapshot = registry.refresh_stale("browser")

    assert snapshot is not None
    assert snapshot.status == CapabilityStatus.PARTIAL
    assert calls == [1]
