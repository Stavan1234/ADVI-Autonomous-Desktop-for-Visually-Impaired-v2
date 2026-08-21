from __future__ import annotations

import logging

from .core.conversation import ConversationEngine
from .core.runtime import Runtime
from .io.console import print_banner, print_shutdown, read_line
from .io.output import AdviResponse, OutputManager
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

    conversation = ConversationEngine(provider)

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

            if user_input.lower() in {"exit", "quit", "stop"}:
                break

            logger.info(
                "User input received: %r",
                user_input,
            )

            try:
                response = conversation.respond(user_input)
                output.deliver(response)

            except Exception:
                logger.exception(
                    "Conversation request failed."
                )

                output.deliver(
                    AdviResponse(
                        "I'm sorry, but I couldn't process that request."
                    )
                )

    finally:
        runtime.shutdown()
        print_shutdown()


if __name__ == "__main__":
    main()