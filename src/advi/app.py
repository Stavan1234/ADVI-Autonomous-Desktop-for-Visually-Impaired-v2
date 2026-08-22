from __future__ import annotations

import logging

from .memory.short_term import ShortTermMemory
from .memory.consolidator import SessionConsolidator
from .memory.retriever import MemoryRetriever

from .core.conversation import ConversationEngine
from .core.runtime import Runtime

from .io.console import (
    print_banner,
    print_shutdown,
    read_line,
)

from .io.output import (
    AdviResponse,
    OutputManager,
)

from .io.tts import PiperTTS
from .providers import GroqProvider


logger = logging.getLogger(__name__)


def main() -> None:
    runtime = Runtime.create()
    runtime.start()

    print_banner()

    settings = runtime.settings

    tts = PiperTTS(
        executable=settings.piper_exe,
        model=settings.piper_model,
        output_wav=settings.tts_output,
    )

    output = OutputManager(
        tts=tts if tts.available() else None
    )

    if not settings.groq_api_key:
        output.deliver(
            AdviResponse(
                "Groq API key is not configured. "
                "Conversation is currently unavailable."
            )
        )

        runtime.shutdown()
        print_shutdown()
        return

    provider = GroqProvider(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
    )

    retriever = MemoryRetriever(
        runtime.memory
    )

    previous_session_context = ""

    if runtime.memory is not None:
        sessions = runtime.memory.recent_sessions(
            limit=3
        )

        if sessions:
            previous_session_context = "\n".join(
                f"- {session['summary']}"
                for session in sessions
                if session.get("summary")
            )

    conversation = ConversationEngine(
        provider=provider,
        memory=ShortTermMemory(),
        retriever=retriever,
        previous_session_context=(
            previous_session_context
        ),
    )

    consolidator = SessionConsolidator(
        provider
    )

    try:
        while True:
            try:
                user_input = read_line()

            except EOFError:
                print()
                break

            except KeyboardInterrupt:
                print()
                break

            if not user_input:
                continue

            if user_input.lower() in {
                "exit",
                "quit",
                "stop",
                "sleep",
            }:
                break

            logger.info(
                "User input received: %r",
                user_input,
            )

            try:
                response = conversation.respond(
                    user_input
                )

                output.deliver(response)

            except Exception:
                logger.exception(
                    "Conversation request failed."
                )

                output.deliver(
                    AdviResponse(
                        "I'm sorry, but I couldn't "
                        "process that request."
                    )
                )

    finally:
        _consolidate_session(
            conversation=conversation,
            consolidator=consolidator,
            runtime=runtime,
        )

        runtime.shutdown(
    session_summary=runtime.session_summary
)

        print_shutdown()


def _consolidate_session(
    conversation: ConversationEngine,
    consolidator: SessionConsolidator,
    runtime: Runtime,
) -> None:
    messages = conversation.get_session_buffer()

    if not messages:
        return

    try:
        result = consolidator.consolidate(
            messages
        )

        runtime.session_summary = result.summary

        if runtime.memory is not None:
            runtime.memory.save_consolidation(
                result
            )

    except Exception:
        logger.exception(
            "Session consolidation failed."
        )


def _get_session_summary(
    runtime: Runtime,
) -> str | None:
    return getattr(
        runtime,
        "_pending_session_summary",
        None,
    )


if __name__ == "__main__":
    main()