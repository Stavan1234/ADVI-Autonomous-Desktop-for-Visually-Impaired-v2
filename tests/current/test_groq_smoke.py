from __future__ import annotations

import os
import pytest

from advi.core.config import load_settings
from advi.capabilities.registry import build_default_registry
from advi.tools import ToolRegistry
from advi.providers.groq import GroqProvider


@pytest.mark.skipif(not os.getenv("GROQ_API_KEY"), reason="GROQ_API_KEY is not set")
def test_groq_tool_calling_smoke_hello():
    settings = load_settings()
    registry = build_default_registry()
    tool_registry = ToolRegistry(registry)
    tools = [tool.to_openai_tool() for tool in tool_registry.model_tools()]

    provider = GroqProvider(api_key=settings.groq_api_key, model=settings.groq_model)
    messages = [{"role": "user", "content": "hello"}]

    response = provider.chat_with_tools(messages, tools)
    assert response is not None
    assert response.text or response.tool_calls
