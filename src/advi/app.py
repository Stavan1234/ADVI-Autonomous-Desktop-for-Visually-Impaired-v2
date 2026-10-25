from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
from pathlib import Path
import threading
import time

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
from advi.core.config import ASSET_ROOT, RUNTIME_ROOT
from advi.io.console import InputHub, StartupAnnouncer, print_banner, print_line, print_shutdown
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
from advi.voice import start_voice, transcriber_from_env, voice_enabled
from advi.voice.listener import open_duplex_audio
from advi.wake import autostart as wake_autostart
from advi.wake.presence import APP_MUTEX, hold as hold_mutex

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
    # Tells the wake listener ADVI is running (it lets go of the microphone);
    # Windows drops it when this process ends, even after a crash.
    app_mutex, _ = hold_mutex(APP_MUTEX)

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
        # With voice on, every sound ADVI makes goes through one duplex stream with
        # echo cancellation, from the first startup sentence on: the microphone
        # must never hear ADVI's own voice as a request (live log 2026-10-25).
        audio = open_duplex_audio() if voice_enabled() else None
        if audio is not None and voice_ready:
            tts.attach_player(audio)
        announcer = StartupAnnouncer(
            speaker=tts.speak if voice_ready else None,
            interval_seconds=7.0,
        )
        announcer.start()

        runtime.start()
        # The wake listener must already be listening when "sleep" closes ADVI.
        wake_word = False
        try:
            wake_word = wake_autostart.ensure_running()
        except Exception as exc:
            logger.warning("Wake listener could not be started: %s", exc)

        if audio is not None:
            runtime.register_resource(audio)  # closed after the output below (reverse order)
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
        agent.progress_callback = output.narrate

        # Everything expensive and deterministic happens BEFORE interactive chat:
        # provider session construction, semantic model load, saved Gmail token
        # preparation, MCP connection/tool discovery, and capability readiness.
        prep_jobs = {"provider session": agent.prepare}
        # The semantic memory model takes ~15 s to load (PyTorch); memory search is
        # lexical until it is ready, so it loads in the background after startup
        # instead of delaying "ready" (which matters when the wake word starts ADVI).
        if memory_service is not None:
            prep_jobs["experience memory"] = memory_service.warm_experience
        if gmail_service is not None:
            prep_jobs["Gmail session"] = gmail_service.prepare
        if mcp_manager is not None:
            prep_jobs["MCP servers"] = mcp_manager.prepare
        # The local speech model loads once here, alongside the other preparation.
        transcriber = (
            transcriber_from_env(ASSET_ROOT, settings.groq_api_keys, notify=output.say) if voice_enabled() else None
        )
        if transcriber is not None:
            prep_jobs["speech recognition"] = transcriber.preload
            agent.speech_status = transcriber.status_sentence

        if prep_jobs:
            with ThreadPoolExecutor(max_workers=min(4, len(prep_jobs))) as executor:
                future_map = {executor.submit(job): name for name, job in prep_jobs.items()}
                for future in as_completed(future_map):
                    name = future_map[future]
                    try:
                        result = future.result()
                        if name == "experience memory":
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

        def exit_now() -> None:
            # Stop the running task; if it doesn't hand control back soon (a tool stuck
            # in a long call), leave anyway: the user asked to close ADVI.
            agent.request_stop()

            def force() -> None:
                if left_loop.wait(8.0):
                    return  # the normal shutdown is under way
                logger.warning("ADVI was still busy 8 s after exit was requested; closing now.")
                print_shutdown()
                os._exit(0)

            threading.Thread(target=force, name="advi-exit-watchdog", daemon=True).start()

        left_loop = threading.Event()
        hub = InputHub(on_exit=exit_now)
        voice = None
        voice_message = ""
        if transcriber is not None:
            voice, voice_message = start_voice(
                transcriber,
                submit=hub.put_voice,
                is_busy=lambda: hub.busy,
                say=output.say,
                is_quiet=output.is_quiet,
                stop_speech=output.silence,
                request_stop=agent.request_stop,
                request_exit=hub.request_exit,
                spoken_recently=output.spoken_recently,
                pause_speech=output.pause_speech,
                resume_speech=output.resume_speech,
                audio=audio,
                wake_word=wake_word,
            )
            if voice is not None:
                runtime.register_resource(voice)

        if retriever is not None:
            def warm_semantic_memory() -> None:
                if not retriever.preload():
                    logger.warning("Semantic memory could not be warmed; lexical memory remains available.")

            threading.Thread(target=warm_semantic_memory, name="advi-semantic-memory", daemon=True).start()

        ready = "Everything is ready, sir." + (f" {voice_message}" if voice_message else "")
        if announcer is not None:
            announcer.stop()
            announcer = None
        # Spoken through the output queue: the main loop starts listening for requests
        # while this long sentence is still being said.
        print_line(f"\nADVI › {ready}")
        output.deliver(AdviResponse(ready), display=False)
        print_banner()
        hub.start()
        if voice is not None:
            # The first time voice is used, teach the sounds once: earcons only help
            # when people know what they mean.
            marker = RUNTIME_ROOT / "voice_sound_guide_played"
            if not marker.exists():
                try:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text("1", encoding="utf-8")
                    voice.play_sound_guide()
                except OSError:
                    logger.debug("Sound guide marker could not be written", exc_info=True)

        while True:
            try:
                source, user_input = hub.get()
            except KeyboardInterrupt:
                print()
                break
            if source in {"eof", "exit"}:
                # Typed exit / quit / sleep, or a spoken "exit" / "sleep" / "close ADVI", closes
                # ADVI completely (2026-10-25); the wake listener starts it again.
                left_loop.set()
                if source == "exit" and voice is not None:
                    output.wait_quiet(6.0)  # let "Going to sleep..." / "Goodbye." finish
                print()
                break
            user_input = user_input.strip()
            if not user_input:
                hub.busy = False
                continue
            logger.info("User input received (%s): %r", source, user_input)
            hub.busy = True
            response = None
            try:
                output.start_progress()
                response = agent.respond(user_input)
                output.deliver(response, display=True)
            except Exception as exc:
                logger.exception("Agent request failed: %s", exc)
                output.deliver(AdviResponse("I ran into a problem processing that request."))
            finally:
                hub.busy = False

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
        from advi.wake.presence import release as release_mutex

        release_mutex(app_mutex)


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
