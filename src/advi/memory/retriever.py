from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import logging
import os
from dataclasses import dataclass
from io import StringIO
from threading import Lock
from typing import TYPE_CHECKING

from .long_term import LongTermMemory, Memory

if TYPE_CHECKING:
    import numpy as np
    from sentence_transformers import SentenceTransformer


@dataclass(frozen=True)
class RetrievedMemory:
    memory: Memory
    score: float
    semantic_score: float
    lexical_score: float


class MemoryRetriever:
    """Bounded hybrid lexical + semantic memory retrieval."""

    MIN_CANDIDATES = 16
    MAX_CANDIDATES = 256
    DEFAULT_CANDIDATES = 128

    def __init__(
        self,
        memory: LongTermMemory,
        model_name: str = "BAAI/bge-small-en-v1.5",
        candidate_limit: int = DEFAULT_CANDIDATES,
    ) -> None:
        self.memory = memory
        self.model_name = model_name

        self.candidate_limit = max(
            self.MIN_CANDIDATES,
            min(candidate_limit, self.MAX_CANDIDATES),
        )

        self._model: SentenceTransformer | None = None
        self._model_error: Exception | None = None
        self._preload_lock = Lock()

    @property
    def semantic_ready(self) -> bool:
        """Whether the semantic encoder has already been prepared for use."""
        return self._model is not None

    @property
    def semantic_error(self) -> str | None:
        """Return the cached semantic initialization error, if any."""
        return str(self._model_error) if self._model_error is not None else None

    @property
    def model(self) -> SentenceTransformer:
        """Return the already-prepared semantic model.

        Interactive retrieval never starts model loading. Startup owns preload();
        this accessor only provides a clear failure if callers use semantic
        encoding before startup preparation has completed.
        """
        if self._model is None:
            if self._model_error is not None:
                raise RuntimeError(
                    f"Semantic memory is unavailable: {self._model_error}"
                )
            raise RuntimeError("Semantic memory has not been preloaded yet.")
        return self._model

    @staticmethod
    def _memory_text(memory: Memory) -> str:
        return memory.statement

    @staticmethod
    def _memory_key(memory: Memory) -> tuple[str, str]:
        return (memory.category, memory.key)

    def _encode_query(self, query: str) -> np.ndarray:
        import numpy as np

        model = self.model
        encode_query = getattr(model, "encode_query", None)
        if callable(encode_query):
            return np.asarray(
                encode_query(
                    query,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            )
        return np.asarray(
            model.encode(
                query,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        )

    def _encode_documents(self, texts: list[str]) -> np.ndarray:
        import numpy as np

        model = self.model
        encode_document = getattr(model, "encode_document", None)
        if callable(encode_document):
            return np.asarray(
                encode_document(
                    texts,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            )
        return np.asarray(
            model.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        )

    @contextmanager
    def _quiet_model_load(self):
        """Suppress third-party model-loader chatter from ADVI's user console."""
        sink = StringIO()
        logger_names = (
            "sentence_transformers",
            "huggingface_hub",
            "transformers",
            "torch",
        )
        saved_levels: dict[str, int] = {}
        try:
            for name in logger_names:
                logger_obj = logging.getLogger(name)
                saved_levels[name] = logger_obj.level
                logger_obj.setLevel(logging.ERROR)
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
        finally:
            for name, level in saved_levels.items():
                logging.getLogger(name).setLevel(level)

    def preload(self) -> bool:
        """Load semantic memory completely during startup or an explicit warm-up."""
        if self._model is not None:
            return True
        if self._model_error is not None:
            return False

        with self._preload_lock:
            if self._model is not None:
                return True
            if self._model_error is not None:
                return False

            try:
                # Keep third-party model libraries quiet in the user's console.
                # ADVI owns the startup UX; dependency warnings/telemetry are not
                # interactive feedback.
                os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
                os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
                from sentence_transformers import SentenceTransformer

                with self._quiet_model_load():
                    self._model = SentenceTransformer(self.model_name)
                return True
            except Exception as exc:
                self._model_error = exc
                return False

    def _candidate_memories(
        self,
        query: str,
        candidate_limit: int | None = None,
    ) -> tuple[list[Memory], set[tuple[str, str]]]:
        limit = self.candidate_limit if candidate_limit is None else candidate_limit

        lexical = self.memory.search(
            query,
            limit=limit,
        )

        recent = self.memory.recent(
            limit=limit,
        )

        candidates: list[Memory] = []
        seen: set[tuple[str, str]] = set()

        for memory in [*lexical, *recent]:
            key = self._memory_key(memory)

            if key in seen:
                continue

            seen.add(key)
            candidates.append(memory)

            if len(candidates) >= limit:
                break

        lexical_keys = {
            self._memory_key(memory)
            for memory in lexical
        }

        return candidates, lexical_keys

    def search(
        self,
        query: str,
        limit: int = 15,
    ) -> list[RetrievedMemory]:
        query = query.strip()

        if not query or limit <= 0:
            return []

        candidate_limit = min(
            self.MAX_CANDIDATES,
            max(self.candidate_limit, limit),
        )

        candidates, lexical_keys = (
            self._candidate_memories(query, candidate_limit=candidate_limit)
        )

        if not candidates:
            return []

        # Startup is responsible for semantic warm-up. If the model could not be
        # prepared, lexical retrieval remains immediately available without starting
        # another background loader during an interactive turn.
        if self._model is None:
            return [
                RetrievedMemory(
                    memory=memory,
                    score=1.0 if self._memory_key(memory) in lexical_keys else 0.0,
                    semantic_score=0.0,
                    lexical_score=(
                        1.0
                        if self._memory_key(memory) in lexical_keys
                        else 0.0
                    ),
                )
                for memory in candidates[:limit]
            ]

        texts = [
            self._memory_text(memory)
            for memory in candidates
        ]

        try:
            query_embedding = self._encode_query(
                query
            )

            document_embeddings = (
                self._encode_documents(texts)
            )

        except (
            ImportError,
            ModuleNotFoundError,
            RuntimeError,
        ):
            return [
                RetrievedMemory(
                    memory=memory,
                    score=1.0 if self._memory_key(memory) in lexical_keys else 0.0,
                    semantic_score=0.0,
                    lexical_score=(
                        1.0
                        if self._memory_key(memory) in lexical_keys
                        else 0.0
                    ),
                )
                for memory in candidates[:limit]
            ]

        semantic_scores = (
            document_embeddings @ query_embedding
        )

        results: list[RetrievedMemory] = []

        for index, memory in enumerate(candidates):
            key = self._memory_key(memory)

            semantic_score = float(
                semantic_scores[index]
            )

            lexical_score = (
                1.0
                if key in lexical_keys
                else 0.0
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

    def retrieve(
        self,
        query: str,
        limit: int = 5,
    ) -> list[str]:
        return [
            result.memory.statement
            for result in self.search(
                query,
                limit=limit,
            )
        ]

    def refresh(self) -> None:
        """
        Kept for compatibility with code that refreshes
        retrieval state after memory writes.

        The retriever intentionally does not preload
        or embed the entire memory database.
        """
        return None