# from aiohttp import client_middleware_digest_auth
# from aiohttp import client_middleware_digest_auth
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from .long_term import LongTermMemory, Memory

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


@dataclass(frozen=True)
class RetrievedMemory:
    memory: Memory
    score: float
    semantic_score: float
    lexical_score: float


class MemoryRetriever:
    """Hybrid local semantic + lexical memory retrieval."""

    def __init__(
        self,
        memory: LongTermMemory,
        model_name: str = "BAAI/bge-small-en-v1.5",
    ) -> None:
        self.memory = memory
        self.model_name = model_name

        self._model: SentenceTransformer | None = None
        self._memories: list[Memory] = []
        self._embeddings: np.ndarray | None = None

        self.refresh()

    @property
    def model(self) -> SentenceTransformer:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.model_name
            )

        return self._model

    def _memory_text(
        self,
        memory: Memory,
    ) -> str:
        return memory.statement

    def refresh(self) -> None:
        """
        Refresh the memory list without loading the
        semantic model.

        Embeddings are generated lazily on the first
        semantic search.
        """
        self._memories = self.memory.all()
        self._embeddings = None

    def _ensure_embeddings(self) -> None:
        if self._embeddings is not None:
            return

        if not self._memories:
            return

        texts = [
            self._memory_text(memory)
            for memory in self._memories
        ]

        self._embeddings = self.model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )

    def search(
        self,
        query: str,
        limit: int = 15,
    ) -> list[RetrievedMemory]:
        query = query.strip()

        if not query or not self._memories:
            return []

        self._ensure_embeddings()

        if self._embeddings is None:
            return []

        query_embedding = self.model.encode(
            "Represent this sentence for searching "
            "relevant personal memories: "
            + query,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )

        semantic_scores = (
            self._embeddings @ query_embedding
        )

        lexical_memories = self.memory.search(
            query,
            limit=limit,
        )

        lexical_scores = {
            (
                memory.category,
                memory.key,
            ): 1.0
            for memory in lexical_memories
        }

        results: list[RetrievedMemory] = []

        for index, memory in enumerate(
            self._memories
        ):
            semantic_score = float(
                semantic_scores[index]
            )

            lexical_score = lexical_scores.get(
                (
                    memory.category,
                    memory.key,
                ),
                0.0,
            )

            score = (
                0.85 * semantic_score
                + 0.15 * lexical_score
            )

            results.append(
                RetrievedMemory(
                    memory=memory,
                    score=score,
                    semantic_score=semantic_score,
                    lexical_score=lexical_score,
                )
            )

        results.sort(
            key=lambda item: item.score,
            reverse=True,
        )

        return results[:limit]