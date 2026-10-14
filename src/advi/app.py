from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from pathlib import Path

from advi.agent import AgentSession
from advi.capabilities.registry import build_default_registry
from advi.core.execution_engine import ExecutionEngine
from advi.core.execution_watchdog import ExecutionWatchdog
from advi.core.build_info import RELEASE_ID, source_fingerprint
from advi.core.runtime import Runtime
from advi.core.startup_health import StartupHealthValidator
from advi.core.verification import ToolVerifier
from advi.capabilities.desktop.context import DesktopContext
from advi.integrations.gmail import LazyGmailService
from advi.integrations.mcp import MCPManager
from advi.io.console import print_banner, print_shutdown, read_line, StartupAnnouncer
from advi.io.output import AdviResponse, OutputManager
from advi.io.tts import PiperTTS
from advi.memory.consolidator import SessionConsolidator
from advi.memory.retriever import MemoryRetriever
from advi.memory.session import SessionBuffer
from advi.providers import GeminiProvider, GroqProvider, LLMRateLimitError
from advi.fallback.provider_bridge import DavidProviderBridge
from advi.fallback.service import FallbackService
from advi.memory.service import MemoryService
from advi.tools import ToolGuard, ToolRegistry, ToolRunner

logger = logging.getLogger(__name__)


def _build_groq_providers(settings):
    providers = []
    keys = tuple(getattr(settings, "groq_api_keys", ()) or ())
    groups = tuple(getattr(settings, "groq_key_groups", ()) or ())
    for index, key in enumerate(keys, start=1):
        try:
            group = groups[index - 1] if index - 1 < len(groups) else f"groq-{index}"
            providers.append(
                GroqProvider(
                    api_key=key,
                    model=settings.groq_model,
                    quota_group=group,
                    label=f"groq-{index}",
                )
            )
            logger.info("Groq provider configured: key=%s quota_group=%s", index, group)
        except Exception as exc:
            logger.warning("Groq provider %s could not be initialized: %s", index, exc)
    return providers


def _build_gemini_providers(settings):
    providers = []
    keys = tuple(getattr(settings, "gemini_api_keys", ()) or ())
    groups = tuple(getattr(settings, "gemini_key_groups", ()) or ())
    for index, (key, group) in enumerate(zip(keys, groups), start=1):
        try:
            providers.append(
                GeminiProvider(
                    api_key=key,
                    model=settings.gemini_model,
                    quota_group=group,
                    label=f"gemini-{index}",
                )
            )
        except Exception as exc:
            logger.warning("Gemini provider %s could not be initialized: %s", index, exc)
    return providers


def main() -> None:
    runtime = None
    agent = None
    output = None
    announcer = None

    try:
        # Settings are cheap. Voice is the first real runtime resource so the very
        # first progress message can be spoken before heavy preparation begins.
        runtime = Runtime.create()
        settings = runtime.settings

        tts = PiperTTS(
            executable=settings.piper_exe,
            model=settings.piper_model,
            output_wav=settings.tts_output,
        )
        voice_ready = tts.preflight()
        announcer = StartupAnnouncer(
            speaker=tts.speak if voice_ready else None,
            interval_seconds=7.0,
        )
        announcer.start()

        runtime.start()

        output = OutputManager(tts=tts if voice_ready else None)
        runtime.register_resource(output)

        providers = [*_build_groq_providers(settings), *_build_gemini_providers(settings)]
        if not providers:
            output.deliver(AdviResponse("No language-model provider is configured or could be initialized."))
            return
        for provider in providers:
            runtime.register_resource(provider)

        retriever = MemoryRetriever(runtime.memory) if runtime.memory is not None else None

        mcp_manager = None
        try:
            candidate_mcp = MCPManager(settings.mcp_config)
            if candidate_mcp.configs:
                mcp_manager = candidate_mcp
                runtime.register_resource(candidate_mcp)
                logger.info("MCP configured servers: %s", len(candidate_mcp.configs))
        except Exception as exc:
            logger.warning("MCP configuration could not be initialized: %s", exc)

        gmail_service = None
        try:
            candidate = LazyGmailService()
            if candidate.is_available():
                gmail_service = candidate
                runtime.register_resource(gmail_service)
            else:
                logger.info("Gmail credentials are not configured; Gmail remains unavailable until configured.")
        except Exception as exc:
            logger.info("Gmail integration not available: %s", exc)

        david_service = None
        try:
            fallback_provider = DavidProviderBridge(providers)
            fallback_root = Path(__file__).resolve().parent / "fallback" / "prompts"
            david_service = FallbackService(
                provider=fallback_provider,
                intent_prompt_path=fallback_root / "intent_prompt.txt",
                planner_prompt_path=fallback_root / "planner_prompt.txt",
            )
        except Exception as exc:
            logger.warning("David fallback could not be initialized: %s", exc)

        desktop_context = DesktopContext()
        registry = build_default_registry(
            memory=runtime.memory,
            retriever=retriever,
            gmail_service=gmail_service,
            environment_watchtower=runtime.environment_watchtower,
            david_fallback_service=david_service,
            mcp_manager=mcp_manager,
            desktop_context=desktop_context,
        )
        for capability in registry.list_all():
            if capability.handler is not None:
                runtime.register_resource(capability.handler)

        execution_engine = ExecutionEngine(
            registry=registry,
            verifier=ToolVerifier(),
            context=desktop_context,
        )
        tool_registry = ToolRegistry(registry)
        tool_guard = ToolGuard(tool_registry)
        tool_runner = ToolRunner(execution_engine, tool_guard, tool_registry)
        memory_service = MemoryService(runtime.memory, retriever)

        agent = AgentSession(
            providers,
            tool_registry=tool_registry,
            tool_runner=tool_runner,
            tool_guard=tool_guard,
            memory=memory_service,
            watchdog=ExecutionWatchdog(),
            session_buffer=SessionBuffer(),
            runtime=runtime,
        )

        # Everything expensive and deterministic happens BEFORE interactive chat:
        # provider session construction, semantic model load, saved Gmail token
        # preparation, MCP connection/tool discovery, and capability readiness.
        prep_jobs = {"provider session": agent.prepare}
        if retriever is not None:
            prep_jobs["semantic memory"] = retriever.preload
        if memory_service is not None:
            prep_jobs["experience memory"] = memory_service.warm_experience
        if gmail_service is not None:
            prep_jobs["Gmail session"] = gmail_service.prepare
        if mcp_manager is not None:
            prep_jobs["MCP servers"] = mcp_manager.prepare

        if prep_jobs:
            with ThreadPoolExecutor(max_workers=min(4, len(prep_jobs))) as executor:
                future_map = {executor.submit(job): name for name, job in prep_jobs.items()}
                for future in as_completed(future_map):
                    name = future_map[future]
                    try:
                        result = future.result()
                        if name == "semantic memory" and not result:
                            logger.warning("Semantic memory could not be warmed; lexical memory remains available.")
                        elif name == "experience memory":
                            logger.info("Experience memory warmed: %s high-confidence lesson(s).", result)
                        elif name == "MCP servers" and result is False:
                            logger.warning("No configured MCP server completed startup preparation.")
                        elif name == "Gmail session" and result is False:
                            logger.info("Gmail has no saved OAuth token; first-use authorization remains available on demand.")
                    except Exception as exc:
                        if name == "provider session":
                            raise
                        logger.warning("Startup preparation failed for %s: %s", name, exc)

        if gmail_service is not None:
            gmail_service.freeze_lazy_initialization()

        startup = StartupHealthValidator().validate(agent.health(refresh_capabilities=True))
        if startup.state == "not_ready":
            logger.error("Startup health check blocked interactive mode: %s", startup.blocking_checks)
            output.deliver(AdviResponse(startup.message))
            return
        if startup.state == "degraded":
            logger.warning("ADVI starting with optional degraded capabilities: %s", startup.degraded_checks)

        if announcer is not None:
            announcer.stop(final_message="Everything is ready, sir.")
            announcer = None
        print_banner()

        while True:
            try:
                user_input = read_line()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input.casefold() in {"exit", "quit", "sleep"}:
                break

            logger.info("User input received: %r", user_input)
            try:
                output.start_progress()
                response = agent.respond(user_input)
                output.deliver(response, display=True)
            except Exception as exc:
                logger.exception("Agent request failed: %s", exc)
                output.deliver(AdviResponse("I ran into a problem processing that request."))

    except Exception:
        logger.exception("ADVI startup failed before interactive mode became ready.")
        if output is not None:
            try:
                output.deliver(AdviResponse("ADVI could not finish preparing, so interactive mode was not started."))
            except Exception:
                logger.exception("Could not report startup failure through output manager.")
    finally:
        if announcer is not None:
            announcer.stop()
        if agent is not None and runtime is not None:
            _consolidate_session(agent=agent, runtime=runtime)
        if runtime is not None:
            runtime.shutdown(session_summary=runtime.session_summary)
        print_shutdown()


def _consolidate_session(agent: AgentSession, runtime: Runtime) -> None:
    messages = agent.get_session_buffer()
    if not messages:
        return
    manager = getattr(agent, "provider_manager", None)
    if manager is not None and getattr(manager, "active_provider_blocked", False):
        logger.info("Session consolidation skipped because the active provider is rate-limited or quota-exhausted.")
        return
    try:
        consolidator = SessionConsolidator(agent.provider)
        result = consolidator.consolidate(messages)
        runtime.session_summary = result.summary
        if runtime.memory is not None:
            runtime.memory.save_consolidation(result, session_id=runtime.session_id)
    except LLMRateLimitError as exc:
        logger.info("Session consolidation skipped because provider quota is unavailable: %s", exc)
    except Exception as exc:
        logger.warning("Session consolidation failed; continuing shutdown without consolidation: %s", exc)


if __name__ == "__main__":
    main()
