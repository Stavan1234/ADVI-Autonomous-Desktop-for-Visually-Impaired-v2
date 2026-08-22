from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ShortTermMemory:
    """Bounded conversation memory for the current ADVI session."""

    max_messages: int = 20
    messages: list[dict[str, str]] = field(default_factory=list)

    def add(
        self,
        role: str,
        content: str,
    ) -> list[dict[str, str]]:
        content = content.strip()

        if not content:
            return []

        self.messages.append(
            {
                "role": role,
                "content": content,
            }
        )

        return self._trim()

    def _trim(self) -> list[dict[str, str]]:
        if len(self.messages) <= self.max_messages:
            return []

        excess = len(self.messages) - self.max_messages

        evicted = [
            message.copy()
            for message in self.messages[:excess]
        ]

        del self.messages[:excess]

        return evicted

    def get_messages(self) -> list[dict[str, str]]:
        return [
            message.copy()
            for message in self.messages
        ]

    def remove_last(self) -> None:
        if self.messages:
            self.messages.pop()

    def clear(self) -> None:
        self.messages.clear()

    @property
    def count(self) -> int:
        return len(self.messages)