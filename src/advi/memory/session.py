from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionBuffer:
    """Temporary staging area for conversation history."""

    messages: list[dict[str, str]] = field(default_factory=list)

    def add(self, messages: list[dict[str, str]]) -> None:
        self.messages.extend(
            message.copy()
            for message in messages
        )

    def drain(self) -> list[dict[str, str]]:
        messages = self.messages
        self.messages = []
        return messages

    @property
    def count(self) -> int:
        return len(self.messages)

    def clear(self) -> None:
        self.messages.clear()