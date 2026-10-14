from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from advi.tools.contracts import ToolCall


@dataclass(frozen=True)
class ParameterRule:
    required: bool = False
    aliases: tuple[str, ...] = ()
    kind: str = "string"  # string, number, integer, bool, list, object
    schema: dict[str, Any] | None = None
    description: str | None = None
    model_visible: bool = True


@dataclass(frozen=True)
class ActionContract:
    action: str
    parameters: dict[str, ParameterRule] = field(default_factory=dict)
    allow_target_fallback: bool = False
    constraints: dict[str, Any] = field(default_factory=dict)


ACTION_CONTRACTS: dict[str, ActionContract] = {
    "open_application": ActionContract("open_application", {"application": ParameterRule(required=True, aliases=("app", "name"))}, True),
    "new_document": ActionContract("new_document", {"application": ParameterRule(aliases=("app", "name"))}),
    "save_document": ActionContract("save_document", {}),
    "save_document_as": ActionContract("save_document_as", {"path": ParameterRule(required=True, aliases=("filename",))}),
    "close_document": ActionContract("close_document", {}),
    "focus_window": ActionContract("focus_window", {"title": ParameterRule(required=True, aliases=("window", "name"))}, True),
    "close_window": ActionContract("close_window", {}, True),
    "close_application": ActionContract("close_application", {"application": ParameterRule(required=True, aliases=("app", "name"))}, True),
    "list_applications": ActionContract("list_applications", {
        "query": ParameterRule(description="Optional application-name filter used to locate a specific installed application."),
        "limit": ParameterRule(kind="integer", description="Maximum number of applications to return; defaults to 120."),
    }),
    "find_application": ActionContract("find_application", {
        "name": ParameterRule(required=True, aliases=("app", "query"), description="Application name to locate in Windows Start Apps, AppX/MSIX, registry, PATH, or running processes."),
    }),
    "list_windows": ActionContract("list_windows", {"limit": ParameterRule(kind="integer")}),
    "inspect_window": ActionContract("inspect_window", {"title": ParameterRule(aliases=("window", "name")), "hwnd": ParameterRule(kind="integer")}),
    "read_screen": ActionContract("read_screen", {"application": ParameterRule(aliases=("app", "window"))}),
    "type_text": ActionContract("type_text", {"text": ParameterRule(required=True, aliases=("value", "content"))}),
    "press_key": ActionContract("press_key", {"key": ParameterRule(required=True, aliases=("value",))}, True),
    "hotkey": ActionContract("hotkey", {"keys": ParameterRule(required=True, aliases=("hotkey",), kind="list_or_string")}),
    "click": ActionContract("click", {
        "target": ParameterRule(schema={"type": ["string", "null"]}, description="Accessible UI element name, label, or text to locate."),
        "coordinates": ParameterRule(kind="object", schema={
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        }, description="Screen coordinates to click when no semantic target is available."),
    }, True),
    "double_click": ActionContract("double_click", {
        "target": ParameterRule(schema={"type": ["string", "null"]}, description="Accessible UI element name, label, or text to locate."),
        "coordinates": ParameterRule(kind="object", schema={
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        }, description="Screen coordinates for the double-click."),
    }, True),
    "right_click": ActionContract("right_click", {
        "target": ParameterRule(schema={"type": ["string", "null"]}, description="Accessible UI element name, label, or text to locate."),
        "coordinates": ParameterRule(kind="object", schema={
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        }, description="Screen coordinates for the right-click."),
    }, True),
    "scroll": ActionContract("scroll", {"amount": ParameterRule(kind="integer"), "direction": ParameterRule(kind="string")}),
    "wait": ActionContract("wait", {
        "duration": ParameterRule(kind="number", aliases=("seconds",), description="Seconds to wait when no condition is supplied."),
        "timeout": ParameterRule(kind="number", description="Maximum seconds to wait for a readiness condition."),
        "condition": ParameterRule(description="Optional condition: application_ready, window_ready, dialog_cleared, or ui_stable."),
        "application": ParameterRule(aliases=("app",), description="Application session to observe while waiting."),
    }),
    "navigate": ActionContract("navigate", {"url": ParameterRule(required=True)}, True),
    "search": ActionContract("search", {"query": ParameterRule(required=True, aliases=("value",))}),
    "click_web_element": ActionContract("click_web_element", {"selector": ParameterRule(required=True)}, True),
    "inspect_web_page": ActionContract("inspect_web_page", {"limit": ParameterRule(kind="integer")}),
    "type_web_element": ActionContract("type_web_element", {
        "selector": ParameterRule(required=True, aliases=("target", "field")),
        "text": ParameterRule(required=True, aliases=("value", "content")),
    }, True),
    "read_web_page": ActionContract("read_web_page"),
    "go_back": ActionContract("go_back"),
    "go_forward": ActionContract("go_forward"),
    "reload_page": ActionContract("reload_page"),
    "research_web": ActionContract(
        "research_web",
        {
            "query": ParameterRule(required=True),
            "target": ParameterRule(aliases=("site",)),
            "max_chars": ParameterRule(aliases=("limit",), kind="integer"),
        },
    ),
    "save_file": ActionContract("save_file", {
        "path": ParameterRule(required=True, aliases=("filename",)),
        "content": ParameterRule(required=True, aliases=("value",)),
        "pages": ParameterRule(kind="integer", description="Optional requested page count for generated PDF/DOCX files."),
        "slides": ParameterRule(kind="integer", description="Optional requested slide count for generated PPTX files."),
        "style": ParameterRule(description="Optional visual style for generated documents, e.g. polished, colorful, modern, academic."),
    }, True),
    "create_file": ActionContract("create_file", {
        "path": ParameterRule(required=True, aliases=("filename",)),
        "content": ParameterRule(required=True, aliases=("value",)),
        "pages": ParameterRule(kind="integer", description="Optional requested page count for generated PDF/DOCX files."),
        "slides": ParameterRule(kind="integer", description="Optional requested slide count for generated PPTX files."),
        "style": ParameterRule(description="Optional visual style for generated documents, e.g. polished, colorful, modern, academic."),
    }, True),
    "append_file": ActionContract("append_file", {"path": ParameterRule(required=True, aliases=("filename",)), "content": ParameterRule(required=True, aliases=("value",))}, True),
    "replace_file_text": ActionContract("replace_file_text", {"path": ParameterRule(required=True, aliases=("filename",)), "old_text": ParameterRule(required=True, aliases=("old", "find")), "new_text": ParameterRule(required=True, aliases=("new", "replace", "value"))}, True),
    "read_file": ActionContract("read_file", {
        "path": ParameterRule(required=True, aliases=("filename",)),
        "offset": ParameterRule(kind="integer", description="Character offset to start reading from; defaults to 0."),
        "max_chars": ParameterRule(kind="integer", description="Maximum characters to return; defaults to 8000."),
    }, True),
    "delete_file": ActionContract("delete_file", {"path": ParameterRule(required=True)}, True),
    "copy_file": ActionContract("copy_file", {"source": ParameterRule(required=True, aliases=("from", "path")), "destination": ParameterRule(required=True, aliases=("to", "target"))}, True),
    "move_file": ActionContract("move_file", {"source": ParameterRule(required=True, aliases=("from", "path")), "destination": ParameterRule(required=True, aliases=("to", "target"))}, True),
    "rename_file": ActionContract("rename_file", {"path": ParameterRule(required=True, aliases=("source", "filename")), "new_name": ParameterRule(required=True, aliases=("name", "destination"))}, True),
    "copy_folder": ActionContract("copy_folder", {"source": ParameterRule(required=True, aliases=("from", "path", "folder")), "destination": ParameterRule(required=True, aliases=("to", "target"))}, True),
    "move_folder": ActionContract("move_folder", {"source": ParameterRule(required=True, aliases=("from", "path", "folder")), "destination": ParameterRule(required=True, aliases=("to", "target"))}, True),
    "rename_folder": ActionContract("rename_folder", {"path": ParameterRule(required=True, aliases=("source", "folder", "directory")), "new_name": ParameterRule(required=True, aliases=("name", "destination"))}, True),
    "delete_folder": ActionContract("delete_folder", {"path": ParameterRule(required=True, aliases=("directory", "folder")), "recursive": ParameterRule(kind="bool")}, True),
    "list_files": ActionContract(
        "list_files",
        {
            "directory": ParameterRule(aliases=("path",)),
            "extension": ParameterRule(description="Optional file extension filter, e.g. pdf or .pdf."),
            "recursive": ParameterRule(kind="bool", description="Whether to include nested files/folders; output is still bounded by limit."),
            "scope": ParameterRule(description="Optional scope alias: desktop, documents, downloads, user, or computer (drives)."),
            "limit": ParameterRule(kind="integer", description="Maximum entries to return; defaults to 100."),
            "sort_by": ParameterRule(description="Optional sort field: name, modified_time, created_time, or size."),
            "descending": ParameterRule(kind="bool", description="Sort newest/largest first when true."),
            "include_metadata": ParameterRule(kind="bool", description="Include size and timestamps for each returned entry."),
        },
        True,
    ),
    "find_file": ActionContract("find_file", {"name": ParameterRule(required=True, aliases=("filename", "query")), "scope": ParameterRule(description="Search scope: computer, user, desktop, documents, or downloads."), "limit": ParameterRule(kind="integer")}),
    "search_file_content": ActionContract("search_file_content", {
        "path": ParameterRule(aliases=("file", "directory"), description="Optional file or directory. If omitted, scope is searched."),
        "query": ParameterRule(required=True, aliases=("text", "find")),
        "scope": ParameterRule(description="Search scope when path is omitted: computer, user, desktop, documents, or downloads."),
        "limit": ParameterRule(kind="integer", description="Maximum matching lines to return; defaults to 20."),
    }),
    "create_folder": ActionContract("create_folder", {"path": ParameterRule(required=True, aliases=("directory", "folder"))}, True),
    "open_file": ActionContract("open_file", {"path": ParameterRule(required=True, aliases=("filename",))}),
    "open_folder": ActionContract("open_folder", {"path": ParameterRule(required=True, aliases=("directory", "folder"))}),
    "email_draft_create": ActionContract("email_draft_create", {"to": ParameterRule(required=True, aliases=("recipient",)), "subject": ParameterRule(), "body": ParameterRule(required=True, aliases=("content",))}),
    "email_draft_read": ActionContract("email_draft_read", {"draft_id": ParameterRule(required=True)}, True),
    "email_draft_update": ActionContract("email_draft_update", {"draft_id": ParameterRule(required=True), "to": ParameterRule(aliases=("recipient",)), "subject": ParameterRule(), "body": ParameterRule(aliases=("content",))}, True),
    # Send accepts either an existing draft_id or a complete message.
    # Conditional validation (draft_id OR to+subject+body) is enforced by the
    # Gmail capability rather than pretending the schema can express it directly.
    "email_send": ActionContract(
        "email_send",
        {
            "to": ParameterRule(required=False, aliases=("recipient",)),
            "subject": ParameterRule(required=False),
            "body": ParameterRule(required=False, aliases=("content",)),
            "draft_id": ParameterRule(),
        },
        True,
        constraints={},
    ),
    "email_reply": ActionContract("email_reply", {"message_id": ParameterRule(required=True, aliases=("id",)), "body": ParameterRule(required=True, aliases=("content", "text"))}, True),
    "email_read": ActionContract("email_read", {"query": ParameterRule(kind="string"), "limit": ParameterRule(kind="integer")}),
    "get_system_info": ActionContract("get_system_info", {}),
    "get_system_status": ActionContract("get_system_status", {}),
    "get_battery_status": ActionContract("get_battery_status", {}),
    "memory_retrieval": ActionContract("memory_retrieval", {"query": ParameterRule(required=True)}, True),
    "contact_lookup": ActionContract("contact_lookup", {"name": ParameterRule(required=True, aliases=("query",))}, True),
    "memory_update": ActionContract("memory_update", {
        "fact": ParameterRule(required=True, aliases=("content", "value")),
        "category": ParameterRule(description="Memory category; defaults to user."),
        # Database identity is an implementation detail. The runtime derives it
        # from the durable fact so the model never needs to manufacture a key.
        "key": ParameterRule(model_visible=False),
    }),
    "memory_forget": ActionContract("memory_forget", {"fact": ParameterRule(required=True, aliases=("key",))}, True),
    "david_fallback": ActionContract("david_fallback", {"request": ParameterRule(required=False), "reason": ParameterRule(), "stage": ParameterRule(description="Internal staged fallback operation; normally omitted by the model.", model_visible=False), "operation_intent": ParameterRule(description="Authoritative operation selected by the primary ADVI brain; internal only.", model_visible=False), "protected_parameters": ParameterRule(kind="object", description="Exact parameters captured from the primary task; internal only.", model_visible=False)}),
    "mcp_list_tools": ActionContract("mcp_list_tools", {"server": ParameterRule(description="Optional configured MCP server name.")}),
    "mcp_call": ActionContract("mcp_call", {
        "server": ParameterRule(required=True),
        "tool": ParameterRule(required=True),
        "arguments": ParameterRule(kind="object", description="Arguments for the selected MCP tool."),
    }),
}



def parameter_rule_schema(rule: ParameterRule) -> dict[str, Any]:
    """Return the canonical JSON-schema projection for one parameter rule."""
    if rule.schema is not None:
        schema = deepcopy(rule.schema)
    elif rule.kind == "integer":
        schema = {"type": "integer"}
    elif rule.kind == "number":
        schema = {"type": "number"}
    elif rule.kind == "bool":
        schema = {"type": "boolean"}
    elif rule.kind == "list_or_string":
        schema = {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
    elif rule.kind == "list":
        schema = {"type": "array"}
    elif rule.kind == "object":
        schema = {"type": "object"}
    elif rule.kind == "string":
        schema = {"type": "string"}
    else:
        schema = {}

    if rule.description:
        schema["description"] = rule.description
    return schema


def schema_accepts_null(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null":
        return True
    types = schema.get("type")
    if isinstance(types, list) and "null" in types:
        return True
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key) or []
        if any(isinstance(item, dict) and schema_accepts_null(item) for item in variants):
            return True
    return False


def build_runtime_parameters_schema(contract: ActionContract) -> dict[str, Any]:
    """Build the execution-side schema: only semantically required fields are required."""
    properties = {name: parameter_rule_schema(rule) for name, rule in contract.parameters.items()}
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": [name for name, rule in contract.parameters.items() if rule.required],
        "additionalProperties": False,
    }
    if contract.constraints:
        schema.update(deepcopy(contract.constraints))
    return schema


def build_model_parameters_schema(contract: ActionContract, *, strict: bool = True) -> dict[str, Any]:
    """Build the provider-facing schema from the canonical contract.

    A strict schema requires every declared property to be present. That is only
    correct when every model-visible parameter is semantically required. For tools
    with optional arguments, emit an ordinary JSON schema instead so providers do
    not reject a valid model call simply because an optional field was omitted.
    Runtime validation remains authoritative either way.
    """
    properties: dict[str, Any] = {}
    for name, rule in contract.parameters.items():
        if not rule.model_visible:
            continue
        schema = parameter_rule_schema(rule)
        if strict and not rule.required and not schema_accepts_null(schema):
            schema = {"anyOf": [schema, {"type": "null"}]}
        properties[name] = schema

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if properties:
        schema["required"] = (
            list(properties)
            if strict
            else [name for name, rule in contract.parameters.items() if rule.model_visible and rule.required]
        )
    if contract.constraints:
        schema.update(deepcopy(contract.constraints))
    return schema


def contract_supports_strict_model_schema(contract: ActionContract) -> bool:
    """Return whether every model-visible parameter is semantically required."""
    return all(rule.required for rule in contract.parameters.values() if rule.model_visible)

def get_action_contract(action: str) -> ActionContract | None:
    return ACTION_CONTRACTS.get(action.strip())


def validate_and_normalize_tool_call(call: "ToolCall") -> tuple["ToolCall" | None, list[str]]:
    """Validate and canonicalize a native ToolCall from the authoritative contract."""
    from dataclasses import replace

    contract = get_action_contract(call.name)
    if contract is None:
        return call, []

    params = dict(call.arguments or {})
    errors: list[str] = []
    allowed_names = set(contract.parameters)
    alias_names = {alias for rule in contract.parameters.values() for alias in rule.aliases}

    # Canonicalize argument aliases before schema validation, then remove the alias
    # keys so strict additionalProperties validation sees only canonical names.
    for name, rule in contract.parameters.items():
        if name not in params or params.get(name) in (None, ""):
            for alias in rule.aliases:
                if params.get(alias) not in (None, ""):
                    params[name] = params[alias]
                    break
        for alias in rule.aliases:
            if alias != name:
                params.pop(alias, None)

    unknown = sorted(set(params) - allowed_names)
    errors.extend(f"Unknown parameter '{name}'." for name in unknown if name not in alias_names)

    for name, rule in contract.parameters.items():
        value = params.get(name)
        if call.name in {"click", "double_click", "right_click"} and name == "coordinates" and isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                value = {"x": int(value[0]), "y": int(value[1])}
                params[name] = value
            except (TypeError, ValueError):
                pass
        if rule.required and value in (None, ""):
            errors.append(f"Missing required parameter '{name}'.")
            continue
        if value is not None and value != "":
            # Preserve compatibility with older model/provider adapters that may
            # serialize numeric parameters as strings. The runtime normalizer accepts
            # those values; the model-facing schema remains strictly typed.
            try:
                _validate_kind(name, value, rule.kind)
            except ValueError as exc:
                errors.append(str(exc))

    if call.name in {"click", "double_click", "right_click"}:
        target = params.get("target")
        coordinates = params.get("coordinates")
        if target in (None, "") and coordinates in (None, ""):
            errors.append("Pointer action requires either 'target' or 'coordinates'.")
        if coordinates is not None and coordinates != "":
            if isinstance(coordinates, (list, tuple)) and len(coordinates) == 2:
                try:
                    params["coordinates"] = {"x": int(coordinates[0]), "y": int(coordinates[1])}
                except (TypeError, ValueError):
                    errors.append("Parameter 'coordinates' must contain integer x and y values.")
            elif isinstance(coordinates, dict):
                x, y = coordinates.get("x"), coordinates.get("y")
                if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
                    errors.append("Parameter 'coordinates' must contain integer x and y values.")
            else:
                errors.append("Parameter 'coordinates' must contain integer x and y values.")

    if call.name == "memory_update":
        # The stable key is implementation detail when the user simply asks
        # ADVI to remember a fact. Derive one locally instead of making the
        # provider invent an extra required argument.
        fact = str(params.get("fact") or "").strip()
        if fact and not str(params.get("key") or "").strip():
            import re
            words = re.findall(r"[A-Za-z0-9]+", fact.lower())
            params["key"] = "_".join(words[:8])[:64] or "fact"
        if not str(params.get("category") or "").strip():
            params["category"] = "user"

    if call.name == "email_send":
        has_draft = bool(str(params.get("draft_id") or "").strip())
        has_message = all(bool(str(params.get(key) or "").strip()) for key in ("to", "body"))
        if not (has_draft or has_message):
            errors.append("Email send requires either 'draft_id' or 'to' and 'body'.")

    if errors:
        return None, errors
    from advi.tools.contracts import ToolCall
    return replace(call, arguments=params) if isinstance(call, ToolCall) else None, []


def _validate_kind(name: str, value: Any, kind: str) -> None:
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError(f"Parameter '{name}' must be a string.")
    elif kind == "integer":
        try:
            int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Parameter '{name}' must be an integer.") from exc
    elif kind == "number":
        try:
            float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Parameter '{name}' must be numeric.") from exc
    elif kind == "list_or_string":
        if not isinstance(value, (list, tuple, str)):
            raise ValueError(f"Parameter '{name}' must be a list or string.")
    elif kind == "list":
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"Parameter '{name}' must be a list.")
    elif kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"Parameter '{name}' must be an object.")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"Parameter '{name}' must be a boolean.")
