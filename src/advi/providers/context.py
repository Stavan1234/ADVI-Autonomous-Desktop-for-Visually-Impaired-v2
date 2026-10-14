from __future__ import annotations
from contextvars import ContextVar

_purpose: ContextVar[str] = ContextVar("advi_llm_purpose", default="unspecified")

def set_llm_purpose(purpose: str):
    return _purpose.set(purpose)

def reset_llm_purpose(token) -> None:
    _purpose.reset(token)

def current_llm_purpose(default: str = "unspecified") -> str:
    value = _purpose.get()
    return value if value else default
