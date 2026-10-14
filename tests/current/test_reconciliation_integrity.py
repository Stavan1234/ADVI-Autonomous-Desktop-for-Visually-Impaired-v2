from __future__ import annotations

from dataclasses import dataclass

from advi.agent.provider_session import ProviderSession
from advi.agent.session_handoff import SessionHandoff
from advi.providers.base import LLMProvider, LLMResponse
from advi.tools import ToolCall, ToolGuard, ToolRegistry
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus


class FakeProvider(LLMProvider):
    def __init__(self):
        self.calls = 0
        self.native_history = None

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def chat(self, messages, *, purpose="chat"):
        self.calls += 1
        return LLMResponse(text="ok", provider=self.name, model=self.model)


class NativeFakeProvider(FakeProvider):
    def create_native_session(self, *, system_instruction, tools, history=None):
        self.native_history = history
        return object()


def test_handoff_is_seeded_without_an_extra_model_call():
    provider = NativeFakeProvider()
    handoff = SessionHandoff(last_user_message="hello", last_assistant_message="hi")
    session = ProviderSession.create(provider, system_instruction="system", tools=[], handoff=handoff)
    assert session.uses_native_state
    assert provider.calls == 0
    assert provider.native_history == [{"role": "user", "content": "hello"}]


def test_tool_guard_rejects_nested_invalid_values():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="dummy",
        description="dummy",
        supported_actions={"hotkey"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    result = guard.check(ToolCall("c1", "hotkey", {"keys": ["ctrl", 7]}))
    assert result is not None
    assert result.success is False
    assert "schema" in (result.error or "").lower() or "must be a string" in (result.error or "").lower()


def test_model_facing_registry_exposes_canonical_browser_research():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="research",
        description="research",
        supported_actions={"research_web"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    tools = ToolRegistry(registry)

    spec = tools.get("research_web")
    assert spec is not None
    assert spec.runtime_action == "research_web"
    assert tools.get("finish") is None
    assert tools.get("research_web_multi") is None


def test_native_runtime_does_not_construct_legacy_services(monkeypatch, tmp_path):
    import advi.core.runtime as runtime_module

    class FakeMemory:
        def __init__(self, path):
            pass

        def start_session(self):
            return 1

        def end_session(self, session_id, summary=""):
            pass

    class FakeWatchtower:
        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(runtime_module, "LongTermMemory", FakeMemory)
    monkeypatch.setattr(runtime_module, "EnvironmentWatchtower", lambda **kwargs: FakeWatchtower())
    monkeypatch.setattr(runtime_module, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime_module, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(runtime_module, "RUNTIME_ROOT", tmp_path / "runtime")

    settings = type("Settings", (), {
        "groq_api_key": None,
        "gemini_api_keys": (),
        "piper_exe": "piper",
        "piper_model": "model",
        "environment_full_inventory": False,
        "environment_process_poll_seconds": 10.0,
    })()
    runtime = runtime_module.Runtime(settings=settings)
    runtime.start()

    assert not hasattr(runtime, "task_persistence")
    assert not hasattr(runtime, "execution_journal")
    runtime.shutdown()


def test_app_import_keeps_legacy_brain_and_router_out_of_primary_startup():
    import os, subprocess, sys
    from pathlib import Path

    code = "import sys; import advi.app; print({k: any(m == k or m.startswith(k + '.') for m in sys.modules) for k in ['advi.brain','advi.providers.router','advi.core.task_persistence','advi.core.execution_journal']})"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    completed = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, env=env)
    assert completed.stdout.strip() == "{'advi.brain': False, 'advi.providers.router': False, 'advi.core.task_persistence': False, 'advi.core.execution_journal': False}"
