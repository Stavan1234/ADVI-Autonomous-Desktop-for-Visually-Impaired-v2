from __future__ import annotations

from typing import Any

from advi.capabilities.registry import CapabilityRegistry
from advi.core.action_contracts import (
    build_model_parameters_schema,
    contract_supports_strict_model_schema,
    get_action_contract,
)

from .contracts import ToolSpec



TOOL_ALIASES = {
    "memory_retrieval": "memory_search",
    "memory_update": "memory_remember",
}

# Provider/model aliases seen in older prompts, cached transcripts, or provider
# error payloads. These are intentionally small and map only to actions that are
# already real, registered ADVI tools.
RUNTIME_TOOL_ALIASES = {
    "browser_open": "navigate",
    "browser_navigate": "navigate",
    "browser_search": "search",
    "browser_click": "click_web_element",
    "browser_type": "type_web_element",
    "browser_read": "inspect_web_page",
    "browser_inspect": "inspect_web_page",
    "repo_browser.open_file": "open_file",
    "create_document": "create_file",
    "keyboard_type": "type_text",
    "keyboard_hotkey": "hotkey",
    "mouse_click": "click",
    "focus": "focus_window",
}

EXCLUDED_ACTIONS: set[str] = set()

# Small, canonical continuation clusters. These are not plans: they only keep
# the next immediately-relevant interaction tools available after the model
# receives a page/window/file observation. The goal is to prevent provider-side
# "tool not in request.tools" rejections without exposing the entire registry.
MODEL_TOOL_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "navigate": ("inspect_web_page", "search", "click_web_element", "type_web_element", "wait"),
    "search": ("inspect_web_page", "click_web_element", "type_web_element", "navigate", "wait"),
    "inspect_web_page": ("click_web_element", "type_web_element", "navigate", "search", "wait"),
    "read_web_page": ("inspect_web_page", "click_web_element", "type_web_element", "navigate", "search", "wait"),
    "click_web_element": ("inspect_web_page", "type_web_element", "navigate", "search", "wait"),
    "type_web_element": ("inspect_web_page", "click_web_element", "navigate", "search", "wait"),
    "go_back": ("inspect_web_page", "click_web_element", "type_web_element", "navigate", "wait"),
    "go_forward": ("inspect_web_page", "click_web_element", "type_web_element", "navigate", "wait"),
    "reload_page": ("inspect_web_page", "click_web_element", "type_web_element", "navigate", "wait"),
}


class ToolRegistry:
    """Canonical model-facing tool registry backed by real capabilities."""

    def __init__(self, capability_registry: CapabilityRegistry) -> None:
        self.capability_registry = capability_registry

    def runtime_name(self, name: str) -> str | None:
        """Resolve a model-facing tool name to an executable runtime operation.

        Some provider tool-calling surfaces may qualify a declared function name
        with a namespace (for example ``default_api:memory_search``). ADVI still
        resolves only the suffix when that suffix is an actually registered
        canonical action; unknown namespaces remain rejected.
        """
        candidates = [str(name or "").strip()]
        if ":" in candidates[0]:
            candidates.append(candidates[0].rsplit(":", 1)[-1].strip())

        for candidate in candidates:
            if candidate in EXCLUDED_ACTIONS:
                continue
            alias = RUNTIME_TOOL_ALIASES.get(candidate)
            if alias:
                candidate = alias
            runtime_name = next(
                (runtime for runtime, canonical in TOOL_ALIASES.items() if canonical == candidate),
                candidate,
            )
            if runtime_name in EXCLUDED_ACTIONS:
                continue
            action_spec = self.capability_registry.get_action_spec(runtime_name)
            if action_spec is None:
                # Keep known tools resolvable even during a transient capability
                # readiness failure. ExecutionEngine will perform the authoritative
                # availability check before touching the real environment.
                action_spec = self.capability_registry.get_registered_action_spec(runtime_name)
            if action_spec is not None:
                return action_spec.action
        return None

    def get(self, name: str, *, include_unavailable: bool = True) -> ToolSpec | None:
        runtime_name = self.runtime_name(name)
        if runtime_name is None:
            return None
        action_spec = self.capability_registry.get_action_spec(runtime_name)
        if action_spec is None and include_unavailable:
            action_spec = self.capability_registry.get_registered_action_spec(runtime_name)
        if action_spec is None:
            return None
        return self._from_action_spec(action_spec)


    def list(self, *, available_only: bool = True) -> list[ToolSpec]:
        specs = self.capability_registry.list_action_specs(available_only=available_only)
        return [self._from_action_spec(spec) for spec in specs if spec.action not in EXCLUDED_ACTIONS]

    def model_tools(self) -> list[ToolSpec]:
        # Availability is short-lived runtime state. Refresh stale probes before
        # deciding that ADVI has no executable tools for the turn.
        try:
            self.capability_registry.refresh_stale()
        except Exception:
            pass
        return self.list(available_only=True)

    def available_action_names(self) -> set[str]:
        """Return canonical model-facing names currently backed by live actions."""
        return {spec.name for spec in self.model_tools()}

    def expand_model_surface(self, names: list[str] | set[str] | tuple[str, ...]) -> list[ToolSpec]:
        """Return a small canonical surface plus safe one-step dependencies.

        Dependencies are intentionally limited to tools needed to continue from
        the current operation. Selected tools that are temporarily unavailable
        remain representable so the execution boundary can return the authoritative
        unavailable result instead of silently deleting the operation from the
        model's vocabulary.
        """
        live = {spec.name: spec for spec in self.model_tools()}
        known: dict[str, ToolSpec] = dict(live)
        for name in names:
            canonical = self.runtime_name(str(name)) or str(name)
            if canonical not in known:
                spec = self.get(canonical, include_unavailable=True)
                if spec is not None:
                    known[canonical] = spec

        ordered: list[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            canonical = self.runtime_name(name) or name
            if canonical in seen:
                return
            spec = known.get(canonical)
            if spec is None:
                spec = self.get(canonical, include_unavailable=True)
                if spec is not None:
                    known[canonical] = spec
            if spec is None:
                return
            seen.add(canonical)
            ordered.append(canonical)

        for name in names:
            add(str(name))
        for name in tuple(ordered):
            for dependency in MODEL_TOOL_DEPENDENCIES.get(name, ()):
                add(dependency)

        return [known[name] for name in ordered]

    @staticmethod
    def _from_action_spec(spec: Any) -> ToolSpec:
        contract = get_action_contract(spec.action)
        is_strict = contract_supports_strict_model_schema(contract) if contract is not None else True
        parameters = (
            build_model_parameters_schema(contract, strict=is_strict) if contract is not None
            else {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        )
        canonical_name = TOOL_ALIASES.get(spec.action, spec.action)
        return ToolSpec(
            name=canonical_name,
            description=spec.description,
            capability=spec.capability,
            parameters=parameters,
            side_effect=spec.side_effect,
            requires_confirmation=spec.requires_confirmation,
            verification=spec.verification,
            runtime_action=spec.action,
            strict=is_strict,
        )

