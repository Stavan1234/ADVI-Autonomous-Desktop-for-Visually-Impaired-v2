"""LLM provider adapters.

Provider SDKs are loaded lazily so optional providers do not make the
whole ADVI package unimportable when their SDK is unavailable.
"""

from typing import TYPE_CHECKING

from .base import LLMProvider, LLMResponse, LLMToolCall, ProviderHealthState
from .errors import (
    LLMAuthenticationError,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

if TYPE_CHECKING:
    from .gemini import GeminiProvider
    from .groq import GroqProvider


_LAZY_PROVIDER_MODULES = {
    "GroqProvider": (".groq", "GroqProvider"),
    "GeminiProvider": (".gemini", "GeminiProvider"),
}


def __getattr__(name: str):
    """Load provider implementations only when actually requested."""
    if name not in _LAZY_PROVIDER_MODULES:
        raise AttributeError(name)

    module_name, attr_name = _LAZY_PROVIDER_MODULES[name]
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


__all__ = [
    "LLMProvider",
    "LLMResponse",
    "LLMToolCall",
    "LLMError",
    "LLMAuthenticationError",
    "LLMRateLimitError",
    "LLMRequestError",
    "LLMResponseError",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "GroqProvider",
    "GeminiProvider",
    "ProviderHealthState",
]
