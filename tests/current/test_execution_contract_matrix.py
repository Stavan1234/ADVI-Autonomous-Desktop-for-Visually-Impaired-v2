from __future__ import annotations

import inspect

from advi.capabilities.registry import build_default_registry
from advi.tools.registry import EXCLUDED_ACTIONS, ToolRegistry


def test_registry_build_is_side_effect_free_by_default(monkeypatch):
    from advi.capabilities.registry import CapabilityRegistry

    def fail_if_called(*args, **kwargs):
        raise AssertionError("registry construction must not probe the machine")

    monkeypatch.setattr(CapabilityRegistry, "refresh_all", fail_if_called)
    registry = build_default_registry()
    assert registry.list_all()


def test_every_registered_action_has_a_contract_and_handler():
    registry = build_default_registry(refresh=False)

    for capability in registry.list_all():
        handler = capability.handler
        if handler is None:
            continue
        for action in capability.supported_actions:
            if action in EXCLUDED_ACTIONS:
                continue
            assert capability.action_spec(action) is not None, action
            assert callable(getattr(handler, f"_execute_{action}", None)), (
                capability.name,
                action,
            )


def test_model_schemas_match_strict_tool_contract():
    registry = build_default_registry(refresh=False)
    tools = ToolRegistry(registry)

    for spec in tools.list(available_only=False):
        schema = spec.parameters
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False
        properties = schema.get("properties", {})
        tool = spec.to_openai_tool()["function"]
        if tool["strict"]:
            assert set(schema.get("required", [])) == set(properties)
        else:
            assert set(schema.get("required", [])) <= set(properties)


def test_excluded_actions_are_not_model_resolvable():
    registry = build_default_registry(refresh=False)
    tools = ToolRegistry(registry)
    for name in EXCLUDED_ACTIONS:
        assert tools.get(name) is None


def test_registry_refresh_default_is_false():
    signature = inspect.signature(build_default_registry)
    assert signature.parameters["refresh"].default is False


def test_model_schema_strictness_matches_semantic_optionality():
    registry = build_default_registry(refresh=False)
    tools = ToolRegistry(registry)
    for spec in tools.list(available_only=False):
        contract = __import__("advi.core.action_contracts", fromlist=["get_action_contract"]).get_action_contract(spec.runtime_action or spec.name)
        if contract is None:
            continue
        visible = [rule for rule in contract.parameters.values() if rule.model_visible]
        assert spec.strict is all(rule.required for rule in visible)

def test_new_feature_actions_are_registered_and_executable():
    registry = build_default_registry(refresh=False)
    for action in ("search_file_content", "email_reply"):
        capability = registry.get_capability_for_action(action)
        assert capability is not None
        spec = capability.action_spec(action)
        assert spec is not None
        assert callable(getattr(capability.handler, f"_execute_{action}"))
