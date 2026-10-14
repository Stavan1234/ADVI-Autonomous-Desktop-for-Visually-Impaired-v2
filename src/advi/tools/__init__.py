"""Canonical model-facing tools with lazy optional runner import."""

from .contracts import ToolCall, ToolEvidence, ToolResult, ToolSpec, VerificationStatus
from .guard import GuardDecision, ToolGuard
from .registry import ToolRegistry

_LAZY = {"ToolRunner": (".runner", "ToolRunner")}

def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(name)
    from importlib import import_module
    module_name, attr_name = _LAZY[name]
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value

__all__ = [
    "ToolSpec", "ToolCall", "ToolEvidence", "ToolResult", "VerificationStatus",
    "ToolRegistry", "ToolGuard", "GuardDecision", "ToolRunner",
]
