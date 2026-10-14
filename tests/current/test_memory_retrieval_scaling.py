from dataclasses import replace
from pathlib import Path

from advi.memory.long_term import Memory
from advi.memory.retriever import MemoryRetriever


class FakeMemoryStore:
    def __init__(self, memories):
        self.memories = memories
        self.search_calls = []
        self.recent_calls = []

    def search(self, query, limit=15):
        self.search_calls.append((query, limit))
        return self.memories[: min(3, len(self.memories))]

    def recent(self, limit=32):
        self.recent_calls.append(limit)
        return self.memories[-min(limit, len(self.memories)):]


class FakeEncoder:
    def __init__(self):
        self.document_batches = []
        self.query_batches = []

    def encode_document(self, texts, **kwargs):
        self.document_batches.append(list(texts))
        import numpy as np
        dimension = 4
        return np.ones((len(texts), dimension), dtype=float)

    def encode_query(self, query, **kwargs):
        import numpy as np
        self.query_batches.append(query)
        return np.ones(4, dtype=float)


def make_memories(count):
    return [
        Memory(
            id=i,
            category="user",
            key=f"key-{i}",
            value=f"value-{i}",
            statement=f"memory statement {i}",
            confidence=0.9,
            created_at="2026-01-01",
            updated_at=f"2026-01-01T00:00:{i % 60:02d}",
        )
        for i in range(count)
    ]


def test_retriever_does_not_encode_entire_memory_store(monkeypatch):
    memories = make_memories(1000)
    store = FakeMemoryStore(memories)
    retriever = MemoryRetriever(store, candidate_limit=32)
    encoder = FakeEncoder()
    monkeypatch.setattr(retriever, "_model", encoder)

    results = retriever.search("memory statement", limit=5)

    assert results
    assert store.search_calls == [("memory statement", 32)]
    assert store.recent_calls == [32]
    assert len(encoder.document_batches[0]) == 32
    assert len(encoder.document_batches[0]) < len(memories)


def test_refresh_does_not_load_all_memories():
    class ExplodingStore:
        def all(self):
            raise AssertionError("refresh must not load the entire memory table")

    retriever = MemoryRetriever(ExplodingStore())
    assert retriever.refresh() is None


def test_retriever_uses_query_document_encoders_when_available(monkeypatch):
    memories = make_memories(4)
    store = FakeMemoryStore(memories)
    retriever = MemoryRetriever(store, candidate_limit=4)
    encoder = FakeEncoder()
    monkeypatch.setattr(retriever, "_model", encoder)

    retriever.search("my editor", limit=2)

    assert encoder.query_batches == ["my editor"]
    assert encoder.document_batches


def test_candidate_limit_remains_bounded_for_large_requested_limits():
    memories = make_memories(1000)
    store = FakeMemoryStore(memories)
    retriever = MemoryRetriever(store, candidate_limit=32)
    encoder = FakeEncoder()
    retriever._model = encoder

    retriever.search("memory", limit=1000)

    assert store.search_calls == [("memory", 256)]
    assert len(encoder.document_batches[0]) <= 256
