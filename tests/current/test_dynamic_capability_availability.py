from advi.capabilities.registry import (
    AvailabilitySnapshot,
    Capability,
    CapabilityRegistry,
    CapabilityStatus,
)


def test_runtime_probe_updates_status_and_reason():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="browser",
        description="browser",
        supported_actions={"navigate"},
        status=CapabilityStatus.AVAILABLE,
        availability_probe=lambda _: (CapabilityStatus.PARTIAL, "Chrome not connected"),
    ))

    snapshot = registry.refresh_capability("browser")

    assert isinstance(snapshot, AvailabilitySnapshot)
    assert snapshot.status == CapabilityStatus.PARTIAL
    assert snapshot.reason == "Chrome not connected"
    assert registry.get("browser").status == CapabilityStatus.PARTIAL
    assert registry.is_action_supported("navigate")


def test_probe_failure_degrades_capability_safely():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="external",
        description="external",
        supported_actions={"do_external"},
        status=CapabilityStatus.AVAILABLE,
        availability_probe=lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
    ))

    snapshot = registry.refresh_capability("external")

    assert snapshot is not None
    assert snapshot.status == CapabilityStatus.UNAVAILABLE
    assert "probe failed" in snapshot.reason
    assert not registry.is_action_supported("do_external")


def test_runtime_status_change_removes_action_from_available_specs():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="files",
        description="files",
        supported_actions={"read_file"},
        status=CapabilityStatus.AVAILABLE,
    ))
    assert registry.get_action_spec("read_file") is not None

    registry.set_status("files", CapabilityStatus.UNAVAILABLE, reason="filesystem unavailable")

    assert registry.get_action_spec("read_file") is None
    assert registry.actions_for_prompt() == "- (no executable actions currently available)"
    assert "filesystem unavailable" in registry.availability_summary()


def test_refresh_all_publishes_current_state_for_each_capability():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="one",
        description="one",
        supported_actions=set(),
        availability_probe=lambda _: (CapabilityStatus.AVAILABLE, "ok"),
    ))
    registry.register(Capability(
        name="two",
        description="two",
        supported_actions=set(),
        availability_probe=lambda _: (CapabilityStatus.UNAVAILABLE, "blocked"),
    ))

    snapshots = registry.refresh_all()
    statuses = {s.capability: s.status for s in snapshots}

    assert statuses == {"one": CapabilityStatus.AVAILABLE, "two": CapabilityStatus.UNAVAILABLE}
    assert "two: unavailable (blocked)" in registry.availability_summary()
