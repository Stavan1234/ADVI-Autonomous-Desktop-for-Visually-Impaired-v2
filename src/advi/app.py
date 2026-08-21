from __future__ import annotations

import logging

from .core.runtime import Runtime
from .io.console import print_banner, print_shutdown, read_line
from .io.output import AdviResponse, OutputManager
from .io.tts import PiperTTS


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
                "Input received during foundation phase: %r",
                user_input,
            )

            output.deliver(
                AdviResponse(
                    "Foundation is ready; the conversation engine comes in the next focus area."
                )
            )

    finally:
        runtime.shutdown()
        print_shutdown()


if __name__ == "__main__":
    main()