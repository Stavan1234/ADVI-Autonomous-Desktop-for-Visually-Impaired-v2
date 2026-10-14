from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from advi.core.action_contracts import build_runtime_parameters_schema, get_action_contract, validate_and_normalize_tool_call

from .contracts import ToolCall, ToolResult
from .registry import ToolRegistry


@dataclass(frozen=True)
class GuardDecision:
    allow: bool
    requires_confirmation: bool = False
    reason: str = ""


class ToolGuard:
    MAX_STRING_CHARS = 16000
    MAX_COLLECTION_ITEMS = 128
    MAX_VALIDATION_DEPTH = 8

    """Deterministic safety/validation boundary before any side effect."""

    def __init__(self, registry: ToolRegistry, *, confirmation_ttl_seconds: float = 300.0) -> None:
        self.registry = registry
        self.confirmation_ttl_seconds = max(1.0, float(confirmation_ttl_seconds))
        self._approvals: dict[str, float] = {}

    def fingerprint(self, call: ToolCall) -> str:
        canonical = json.dumps({"name": call.name, "arguments": call.arguments}, sort_keys=True, separators=(",", ":"), default=str)
        if len(canonical) > 2 * 1024 * 1024:
            raise ValueError("Tool call arguments are too large to fingerprint safely.")
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def approve(self, call: ToolCall) -> None:
        self._approvals[self.fingerprint(call)] = time.monotonic() + self.confirmation_ttl_seconds

    def decision(self, call: ToolCall) -> GuardDecision:
        spec = self.registry.get(call.name)
        if spec is None:
            return GuardDecision(False, reason="unknown_tool")

        runtime_name = self.registry.runtime_name(call.name) or call.name
        canonical_call = ToolCall(call_id=call.call_id, name=runtime_name, arguments=dict(call.arguments or {}))
        contract = get_action_contract(runtime_name)

        if contract is not None:
            normalized, contract_errors = validate_and_normalize_tool_call(canonical_call)
            if contract_errors or normalized is None:
                return GuardDecision(False, reason="; ".join(contract_errors) or "invalid_tool_arguments")
            schema = build_runtime_parameters_schema(contract)
            errors = self._validate_arguments(schema, normalized.arguments)
        else:
            errors = self._validate_arguments(spec.parameters, call.arguments)

        if errors:
            return GuardDecision(False, reason="; ".join(errors))
        fingerprint = self.fingerprint(call)
        expiry = self._approvals.get(fingerprint, 0.0)
        if spec.requires_confirmation and expiry <= time.monotonic():
            return GuardDecision(False, requires_confirmation=True, reason="user_confirmation_required")
        return GuardDecision(True, reason="allowed")

    def consume_approval(self, call: ToolCall) -> None:
        self._approvals.pop(self.fingerprint(call), None)

    def check(self, call: ToolCall) -> ToolResult | None:
        decision = self.decision(call)
        if decision.allow:
            return None
        if decision.requires_confirmation:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                requires_confirmation=True,
                confirmation_reason=decision.reason,
                error="This action requires user confirmation before execution.",
            )
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            success=False,
            error=f"Tool call rejected: {decision.reason}",
        )

    @classmethod
    def _validate_arguments(cls, schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
        if not isinstance(arguments, dict):
            return ["Tool arguments must be an object."]
        return cls._validate_schema(schema, arguments, path="argument")

    @classmethod
    def _validate_schema(cls, schema: dict[str, Any], value: Any, *, path: str, depth: int = 0) -> list[str]:
        """Validate the JSON-schema subset used by ADVI's executable tool contracts."""
        if depth > cls.MAX_VALIDATION_DEPTH:
            return [f"{path} is nested too deeply."]
        if not isinstance(schema, dict) or not schema:
            return []

        # Composition keywords are evaluated before type-specific validation.
        if "anyOf" in schema:
            branches = schema.get("anyOf") or []
            if any(not cls._validate_schema(branch, value, path=path, depth=depth + 1) for branch in branches if isinstance(branch, dict)):
                return []
            return [f"{path} does not match any allowed schema."]

        if "oneOf" in schema:
            branches = [
                branch for branch in (schema.get("oneOf") or [])
                if isinstance(branch, dict)
            ]
            matches = sum(1 for branch in branches if not cls._validate_schema(branch, value, path=path, depth=depth + 1))
            return [] if matches == 1 else [f"{path} does not match exactly one allowed schema."]

        if "allOf" in schema:
            errors: list[str] = []
            for branch in (schema.get("allOf") or []):
                if isinstance(branch, dict):
                    errors.extend(cls._validate_schema(branch, value, path=path, depth=depth + 1))
            return errors

        if "enum" in schema and value not in schema.get("enum", []):
            return [f"{path} must be one of the allowed values."]
        if "const" in schema and value != schema["const"]:
            return [f"{path} must equal the declared constant."]

        expected = schema.get("type")
        expected_types = expected if isinstance(expected, list) else [expected]
        if expected is not None:
            if not any(cls._matches_json_type(value, item) for item in expected_types):
                return [f"{path} has the wrong type."]

        errors: list[str] = []
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                errors.extend(f"Unknown argument '{key}'." for key in value if key not in properties)
            for key in schema.get("required", []):
                if key not in value:
                    errors.append(f"Missing required argument '{key}'.")
            for key, item in value.items():
                if key in properties and isinstance(properties[key], dict):
                    errors.extend(cls._validate_schema(properties[key], item, path=f"{path}.{key}", depth=depth + 1))
            return errors

        if isinstance(value, list):
            if len(value) > cls.MAX_COLLECTION_ITEMS:
                errors.append(f"{path} contains too many items.")
            min_items = schema.get("minItems")
            max_items = schema.get("maxItems")
            if isinstance(min_items, int) and len(value) < min_items:
                errors.append(f"{path} contains too few items.")
            if isinstance(max_items, int) and len(value) > max_items:
                errors.append(f"{path} contains too many items.")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    errors.extend(cls._validate_schema(item_schema, item, path=f"{path}[{index}]", depth=depth + 1))
            return errors

        if isinstance(value, str):
            if len(value) > cls.MAX_STRING_CHARS:
                errors.append(f"{path} is too long.")
            min_length = schema.get("minLength")
            max_length = schema.get("maxLength")
            if isinstance(min_length, int) and len(value) < min_length:
                errors.append(f"{path} is too short.")
            if isinstance(max_length, int) and len(value) > max_length:
                errors.append(f"{path} is too long.")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, int) and not isinstance(schema.get("minimum"), bool) and schema.get("minimum") is not None and value < schema["minimum"]:
                errors.append(f"{path} is below the minimum value.")
            if isinstance(value, (int, float)) and not isinstance(schema.get("maximum"), bool) and schema.get("maximum") is not None and value > schema["maximum"]:
                errors.append(f"{path} exceeds the maximum value.")

        return errors

    @staticmethod
    def _matches_json_type(value: Any, expected: Any) -> bool:
        if expected is None:
            return True
        if expected == "null":
            return value is None
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            if isinstance(value, bool):
                return False
            if isinstance(value, int):
                return True
            if isinstance(value, str):
                try:
                    int(value.strip())
                    return True
                except ValueError:
                    return False
            return False
        if expected == "number":
            if isinstance(value, bool):
                return False
            if isinstance(value, (int, float)):
                return True
            if isinstance(value, str):
                try:
                    float(value.strip())
                    return True
                except ValueError:
                    return False
            return False
        if expected == "boolean":
            return isinstance(value, bool)
        return True

