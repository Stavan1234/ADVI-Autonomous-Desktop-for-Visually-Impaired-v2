from __future__ import annotations

from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus


class Handler:
    def execute(self, action, *, approval_granted=False):
        return None


def make_registry():
    r = CapabilityRegistry()
    r.register(Capability(
        name="files", description="files", supported_actions={"read_file", "delete_file"},
        status=CapabilityStatus.AVAILABLE, handler=Handler(),
    ))
    return r


def test_registry_exposes_canonical_action_spec():
    spec = make_registry().get_action_spec("delete_file")
    assert spec is not None
    assert spec.action == "delete_file"
    assert spec.capability == "files"
    assert spec.requires_confirmation is False
    assert spec.verification == "file_absence"
    assert "path" in spec.parameters


def test_actions_for_prompt_is_generated_from_specs():
    text = make_registry().actions_for_prompt()
    assert "delete_file:" in text
    assert "confirmation=false" in text
    assert "required_parameters=path" in text


def test_registry_summary_is_authoritative_runtime_state():
    text = make_registry().summary_for_prompt()
    assert "files" in text
    assert "delete_file" not in text  # capability summary intentionally stays capability-level
    assert "unavailable" not in text
