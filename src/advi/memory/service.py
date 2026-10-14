from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .long_term import LongTermMemory
from .experience import ExperienceLesson, ExperienceMemory


@dataclass(frozen=True)
class MemoryRecall:
    statements: tuple[str, ...]
    sources: tuple[dict[str, Any], ...]


class MemoryService:
    """High-level memory interface used by the new agent loop."""

    def __init__(self, memory: LongTermMemory | None, retriever: Any | None = None) -> None:
        self.memory = memory
        self.retriever = retriever
        self.experience = ExperienceMemory(memory)

    def core(self, limit: int = 8) -> MemoryRecall:
        if self.memory is None:
            return MemoryRecall((), ())
        try:
            rows = self.memory.all()[: max(0, int(limit))]
        except Exception:
            return MemoryRecall((), ())
        rows = [item for item in rows if item.kind != "lesson" and item.status == "active"]
        return MemoryRecall(
            tuple(item.statement for item in rows if item.statement),
            tuple({"category": item.category, "key": item.key, "confidence": item.confidence} for item in rows),
        )

    def recall(self, query: str, limit: int = 5) -> MemoryRecall:
        if self.memory is None or not query.strip():
            return MemoryRecall((), ())
        results = []
        if self.retriever is not None:
            try:
                results = self.retriever.search(query, limit=limit)
            except Exception:
                results = []
        if not results:
            try:
                lexical = self.memory.search(query, limit=limit)
                lexical = [item for item in lexical if item.kind != "lesson" and item.status == "active"]
                return MemoryRecall(
                    tuple(item.statement for item in lexical if item.statement),
                    tuple({"category": item.category, "key": item.key, "confidence": item.confidence} for item in lexical),
                )
            except Exception:
                return MemoryRecall((), ())
        filtered = [item for item in results if item.memory.kind != "lesson" and item.memory.status == "active"]
        return MemoryRecall(
            tuple(item.memory.statement for item in filtered if item.memory.statement),
            tuple({"category": item.memory.category, "key": item.memory.key, "score": round(item.score, 4)} for item in filtered),
        )


    def warm_experience(self, limit: int = 8, *, min_confidence: float = 0.88) -> int:
        """Preload only high-confidence experience lessons for fast startup context."""
        return self.experience.warm(limit=limit, min_confidence=min_confidence)

    def experience_lessons(self, query: str, *, active_domain: str | None = None, limit: int = 2) -> tuple[ExperienceLesson, ...]:
        """Return a tiny set of relevant experience lessons for one model turn."""
        return tuple(self.experience.relevant(query, active_domain=active_domain, limit=limit))

    @property
    def startup_experience(self) -> tuple[ExperienceLesson, ...]:
        return self.experience.startup_lessons

    def record_experience_recovery(
        self,
        *,
        recovered_tool: str,
        domain: str | None = None,
        recovery_tool: str | None = None,
        error: str | None = None,
        session_id: int | None = None,
    ) -> ExperienceLesson | None:
        """Record one explainable recovery pattern; transient noise is ignored."""
        return self.experience.remember_recovery(
            recovered_tool=recovered_tool,
            domain=domain,
            recovery_tool=recovery_tool,
            error=error,
            session_id=session_id,
        )

    def remember(self, *, kind: str, scope: str, subject: str, key: str, value: str,
                 statement: str, confidence: float = 0.8, importance: int = 5,
                 session_id: int | None = None) -> None:
        if self.memory is None:
            raise RuntimeError("Durable memory is unavailable.")
        if hasattr(self.memory, "remember_rich"):
            self.memory.remember_rich(
                kind=kind, scope=scope, subject=subject, key=key, value=value,
                statement=statement, confidence=confidence, importance=importance,
                source_session_id=session_id,
            )
        else:
            self.memory.remember(category=kind, key=key, value=value, statement=statement, confidence=confidence)

    def forget(self, *, category: str, key: str) -> None:
        if self.memory is None:
            raise RuntimeError("Durable memory is unavailable.")
        self.memory.forget(category=category, key=key)
