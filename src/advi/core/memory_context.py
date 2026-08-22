from __future__ import annotations

from ..memory.retriever import RetrievedMemory


class MemoryContextBuilder:
    @staticmethod
    def build(
        memories: list[RetrievedMemory],
    ) -> str:
        if not memories:
            return ""

        lines: list[str] = []

        for item in memories:
            if item.score < 0.40:
                continue

            lines.append(
                f"- {item.memory.statement}"
            )

        return "\n".join(lines)