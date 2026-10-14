from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionBuffer:
    """Small temporary conversation buffer used for optional persistence."""

    max_messages: int = 40
    messages: list[dict[str, str]] = field(default_factory=list)

    def add(self, messages: list[dict[str, str]]) -> None:
        self.messages.extend(
            message.copy()
            for message in messages
        )
        limit = max(2, int(self.max_messages))
        if len(self.messages) > limit:
            self.messages = self.messages[-limit:]

    def drain(self) -> list[dict[str, str]]:
        messages = self.messages
        self.messages = []
        return messages

    @property
    def count(self) -> int:
        return len(self.messages)

    def clear(self) -> None:
        self.messages.clear()
