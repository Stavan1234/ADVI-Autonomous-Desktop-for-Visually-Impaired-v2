"""Build the real ADVI agent the way app.py does, without voice.

The runtime (memory, environment, browser registry) is built once per run;
every task gets a fresh AgentSession so no conversation carries over.

The brain is Groq gpt-oss-120b on every key. Gemini is the backup when all
three Groq keys are out (the user's choice, 2026-10-23), exactly as ADVI runs
day to day. Every AI call records its model, and reports count the tasks each
model answered, so a mixed run is never presented as a single-model score.
``--groq-only`` pins the run to Groq.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AdviRuntime:
    runtime: Any
    settings: Any
    providers: list[Any]
    registry: Any
    context: Any
    gmail: Any

    @property
    def brain_model(self) -> str:
        return str(getattr(self.settings, "groq_model", "") or "")


def build_runtime(*, include_gemini: bool = False) -> AdviRuntime:
    from advi import app
    from advi.capabilities.desktop.context import DesktopContext
    from advi.capabilities.registry import build_default_registry
    from advi.core.runtime import Runtime
    from advi.fallback.provider_bridge import DavidProviderBridge
    from advi.fallback.service import FallbackService
    from advi.integrations.gmail import LazyGmailService
    from advi.memory.retriever import MemoryRetriever

    runtime = Runtime.create()
    runtime.start()
    settings = runtime.settings
    providers = list(app._build_groq_providers(settings))
    if include_gemini:
        providers += list(app._build_gemini_providers(settings))
    if not providers:
        raise RuntimeError("No Groq key is configured; benchmarks pin the brain to Groq gpt-oss-120b.")
    retriever = MemoryRetriever(runtime.memory) if runtime.memory is not None else None
    gmail = None
    try:
        candidate = LazyGmailService()
        if candidate.is_available():
            gmail = candidate
    except Exception:
        gmail = None
    prompts = Path(app.__file__).resolve().parent / "fallback" / "prompts"
    david = FallbackService(
        provider=DavidProviderBridge(providers),
        intent_prompt_path=prompts / "intent_prompt.txt",
        planner_prompt_path=prompts / "planner_prompt.txt",
    )
    context = DesktopContext()
    registry = build_default_registry(
        memory=runtime.memory, retriever=retriever, gmail_service=gmail,
        environment_watchtower=runtime.environment_watchtower,
        david_fallback_service=david, desktop_context=context,
    )
    return AdviRuntime(runtime, settings, providers, registry, context, gmail)


def new_agent(advi: AdviRuntime) -> Any:
    """A fresh AgentSession over the shared runtime."""
    from advi.agent import AgentSession
    from advi.core.execution_engine import ExecutionEngine
    from advi.core.execution_watchdog import ExecutionWatchdog
    from advi.core.verification import ToolVerifier
    from advi.memory.retriever import MemoryRetriever
    from advi.memory.service import MemoryService
    from advi.memory.session import SessionBuffer
    from advi.tools import ToolGuard, ToolRegistry, ToolRunner

    runtime = advi.runtime
    retriever = MemoryRetriever(runtime.memory) if runtime.memory is not None else None
    engine = ExecutionEngine(registry=advi.registry, verifier=ToolVerifier(), context=advi.context)
    tools = ToolRegistry(advi.registry)
    guard = ToolGuard(tools)
    agent = AgentSession(
        advi.providers, tool_registry=tools, tool_runner=ToolRunner(engine, guard, tools), tool_guard=guard,
        memory=MemoryService(runtime.memory, retriever), watchdog=ExecutionWatchdog(),
        session_buffer=SessionBuffer(), runtime=runtime,
    )
    agent.prepare()
    return agent
