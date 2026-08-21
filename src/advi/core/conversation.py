from __future__ import annotations

from dataclasses import dataclass

from ..providers import LLMProvider
from ..providers.errors import LLMError
from ..io.output import AdviResponse


@dataclass
class ConversationEngine:
    provider: LLMProvider

    def respond(self, user_input: str) -> AdviResponse:
        text = user_input.strip()

        if not text:
            return AdviResponse("")

        messages = [
            {
                "role": "user",
                "content": text,
            }
        ]

        try:
            result = self.provider.chat(messages)

        except LLMError:
            raise

        return AdviResponse(result.text)