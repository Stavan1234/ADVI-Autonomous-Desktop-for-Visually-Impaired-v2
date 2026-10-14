"""Deferred tool loading for the model-facing tool surface.

The per-turn tool surface is deliberately small because full schemas for every
capability cost ~6k tokens per provider request, which exhausts free-tier
token-per-minute limits. A small surface must never become a capability
boundary, though: when intent routing omits a tool, the model would otherwise
tell the user "I can't do that" for something ADVI can do.

``request_tools`` closes that gap. It is always declared on actionable turns,
its description lists every executable tool that is not already exposed, and
calling it loads the named schemas for the remainder of the task.
"""
from __future__ import annotations

from typing import Any, Iterable

from .contracts import ToolSpec

REQUEST_TOOLS_NAME = "request_tools"

# Stable, speech-friendly group labels keyed by capability name.
_CAPABILITY_LABELS: dict[str, str] = {
    "file_management": "Files",
    "email": "Email",
    "browser_control": "Browser",
    "desktop_control": "Desktop apps",
    "messaging": "Messaging (WhatsApp)",
    "desktop_ui": "Desktop UI (see and act on app elements)",
    "system": "System",
    "memory": "Memory",
    "mcp": "MCP",
    "david_fallback": "Fallback",
}

MAX_TOOLS_PER_REQUEST = 8


def tool_catalog(specs: Iterable[ToolSpec], *, exclude: Iterable[str] = ()) -> dict[str, list[str]]:
    """Group executable tool names by capability label, omitting ``exclude``."""
    excluded = {str(name) for name in exclude}
    grouped: dict[str, list[str]] = {}
    for spec in specs:
        if spec.name in excluded or spec.name == REQUEST_TOOLS_NAME:
            continue
        label = _CAPABILITY_LABELS.get(spec.capability, spec.capability.replace("_", " ").title())
        grouped.setdefault(label, []).append(spec.name)
    return {label: sorted(names) for label, names in sorted(grouped.items())}


def build_request_tools_spec(specs: Iterable[ToolSpec], *, exposed: Iterable[str]) -> ToolSpec | None:
    """Build the meta-tool declaration, or None when nothing remains to load."""
    catalog = tool_catalog(specs, exclude=exposed)
    if not catalog:
        return None
    listing = "; ".join(f"{label}: {', '.join(names)}" for label, names in catalog.items())
    return ToolSpec(
        name=REQUEST_TOOLS_NAME,
        description=(
            "Load additional ADVI tools for this task. The tools below exist and work but are not "
            "declared yet. If the request needs one, call this first, then call the loaded tool. "
            "Never tell the user you lack a capability listed here. Available to load: "
            f"{listing}."
        ),
        capability="agent",
        parameters={
            "type": "object",
            "properties": {
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Exact tool names from the list, at most {MAX_TOOLS_PER_REQUEST}.",
                },
            },
            "required": ["tools"],
            "additionalProperties": False,
        },
        side_effect="none",
    )


def parse_requested_tools(arguments: dict[str, Any] | None) -> list[str]:
    """Normalize the ``tools`` argument; tolerate a string or comma list."""
    arguments = arguments or {}
    # A model that calls this while it is undeclared has no schema to follow;
    # the log shows {"name": "open_file"}. Accept the obvious variants.
    raw = next(
        (arguments[key] for key in ("tools", "names", "tool_names", "name", "tool") if arguments.get(key)),
        None,
    )
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    names: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names[:MAX_TOOLS_PER_REQUEST]
