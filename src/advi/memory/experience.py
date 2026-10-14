from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .long_term import LongTermMemory, Memory


@dataclass(frozen=True)
class ExperienceLesson:
    """A compact, reusable lesson learned from concrete execution evidence."""

    key: str
    scope: str
    subject: str
    trigger: str
    rule: str
    avoid: str
    preferred: str
    confidence: float
    importance: int
    evidence_count: int

    @property
    def statement(self) -> str:
        return self.rule


class ExperienceMemory:
    """Thin experience layer built on the existing durable SQLite memory.

    Lessons are structured memories (kind='lesson'), not a second database and
    not raw transcripts. Only high-value, explainable recovery patterns are
    recorded automatically.
    """

    KIND = "lesson"
    MAX_CACHE = 8

    _RULES: dict[str, dict[str, Any]] = {
        "desktop:type_text:focus-before-type": {
            "scope": "desktop",
            "subject": "type_text",
            "trigger": "desktop text entry is attempted but the text effect cannot be observed",
            "rule": "Before typing into a desktop field, verify the intended control is focused, editable, and actually ready to receive text.",
            "avoid": "Do not treat keypresses as proof that the intended field received the text.",
            "preferred": "Focus the intended control, verify it, type, then verify the resulting text when possible.",
            "importance": 8,
        },
        "browser:target-loss:reacquire": {
            "scope": "browser",
            "subject": "browser target lifecycle",
            "trigger": "a browser action reports a lost or missing target/tab",
            "rule": "When a browser target disappears, reacquire the current target before reusing selectors or tab identifiers.",
            "avoid": "Do not continue operating on stale CDP target identifiers or stale DOM assumptions.",
            "preferred": "Reacquire the live browser target, inspect only the current page state, then continue.",
            "importance": 8,
        },
        "desktop:open_application:verify-window": {
            "scope": "desktop",
            "subject": "application launch",
            "trigger": "an application launch needs later recovery or window verification",
            "rule": "After launching a desktop application, verify the real visible target window before acting on it.",
            "avoid": "Do not assume launcher success means the intended application window is ready.",
            "preferred": "Confirm the application identity and visible window, then perform dependent actions.",
            "importance": 7,
        },
        "browser:uncertain-action:reobserve": {
            "scope": "browser",
            "subject": "browser action recovery",
            "trigger": "a browser action is uncertain and later recovery succeeds",
            "rule": "When a browser action is uncertain, re-observe the current page state before retrying a state-changing action.",
            "avoid": "Do not blindly repeat the same selector or action against a changed page.",
            "preferred": "Use a fresh targeted observation, retarget, then retry once the page is ready.",
            "importance": 7,
        },
    }

    def __init__(self, memory: LongTermMemory | None) -> None:
        self.memory = memory
        self._startup_cache: tuple[ExperienceLesson, ...] = ()

    @property
    def startup_lessons(self) -> tuple[ExperienceLesson, ...]:
        return self._startup_cache

    def warm(self, limit: int = MAX_CACHE, *, min_confidence: float = 0.88) -> int:
        if self.memory is None:
            self._startup_cache = ()
            return 0
        lessons = [
            item for item in self.memory.all()
            if item.kind == self.KIND
            and item.status == "active"
            and item.confidence >= float(min_confidence)
        ]
        lessons.sort(key=lambda item: (-item.confidence, -item.importance, item.id))
        self._startup_cache = tuple(self._parse(item) for item in lessons[: max(0, min(int(limit), self.MAX_CACHE))])
        return len(self._startup_cache)

    def relevant(self, query: str, *, active_domain: str | None = None, limit: int = 2) -> list[ExperienceLesson]:
        if self.memory is None or not str(query or "").strip() or limit <= 0:
            return []

        candidates: list[Memory] = []
        try:
            candidates = self.memory.search(query, limit=min(20, max(6, limit * 5)))
        except Exception:
            candidates = []

        lessons: list[Memory] = []
        seen: set[tuple[str, str]] = set()
        domain = str(active_domain or "").casefold().strip()
        query_terms = {
            token for token in __import__("re").findall(r"[a-z0-9_]+", str(query).casefold())
            if len(token) >= 3
        }

        def add(item: Memory, *, require_match: bool) -> None:
            if len(lessons) >= limit:
                return
            if item.kind != self.KIND or item.status != "active":
                return
            if domain and item.scope not in {domain, "global"}:
                return
            if require_match:
                lesson_text = f"{item.subject} {item.statement} {item.value}".casefold()
                if query_terms and not any(term in lesson_text for term in query_terms):
                    return
            key = (item.category, item.key)
            if key in seen:
                return
            seen.add(key)
            lessons.append(item)

        for item in candidates:
            add(item, require_match=False)
        if len(lessons) < limit:
            for item in self._cache_memory_items():
                add(item, require_match=True)

        return [self._parse(item) for item in lessons]

    def remember_recovery(self, *, recovered_tool: str, domain: str | None = None,
                          recovery_tool: str | None = None, error: str | None = None,
                          session_id: int | None = None) -> ExperienceLesson | None:
        if self.memory is None:
            return None

        key = self._rule_key(str(recovered_tool), domain, error)
        rule = self._RULES.get(key)
        if rule is None:
            return None

        existing = self.memory.recall(self.KIND, key)
        evidence_count = 1
        confidence = 0.86
        if existing is not None and existing.kind == self.KIND:
            metadata = self._metadata(existing)
            evidence_count = max(1, int(metadata.get("evidence_count", 1))) + 1
            confidence = min(0.99, max(existing.confidence, 0.86) + 0.03)
        else:
            confidence = 0.86

        if existing is not None:
            try:
                previous = self._metadata(existing)
            except Exception:
                previous = {}
        else:
            previous = {}
        value = {
            "trigger": rule["trigger"],
            "rule": rule["rule"],
            "avoid": rule["avoid"],
            "preferred": rule["preferred"],
            "evidence_count": evidence_count,
            "recovery_tool": str(recovery_tool or "")[:120],
        }
        statement = str(rule["rule"])
        self.memory.remember_rich(
            kind=self.KIND,
            scope=str(rule["scope"]),
            subject=str(rule["subject"]),
            key=key,
            value=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            statement=statement,
            confidence=confidence,
            importance=int(rule["importance"]),
            source_session_id=session_id,
        )
        if existing is not None and previous.get("evidence_count"):
            # remember_rich already updates the lesson. This branch exists only
            # to keep the intent explicit: repeated evidence strengthens the same
            # lesson instead of creating another record.
            pass
        refreshed = self.memory.recall(self.KIND, key)
        return self._parse(refreshed) if refreshed is not None else None

    def _cache_memory_items(self) -> list[Memory]:
        if self.memory is None:
            return []
        result: list[Memory] = []
        for lesson in self._startup_cache:
            memory = self.memory.recall(self.KIND, lesson.key)
            if memory is not None:
                result.append(memory)
        return result

    @staticmethod
    def _rule_key(tool: str, domain: str | None, error: str | None = None) -> str | None:
        tool = tool.casefold().strip()
        domain = (domain or "").casefold().strip()
        error_text = str(error or "").casefold()
        if tool == "type_text" and domain == "desktop":
            return "desktop:type_text:focus-before-type"
        if domain == "desktop" and tool == "open_application":
            return "desktop:open_application:verify-window"
        if domain == "browser" and any(
            marker in error_text for marker in ("no such target", "target not found", "tab not found", "connection refused")
        ):
            return "browser:target-loss:reacquire"
        if domain == "browser" and tool in {
            "navigate", "search", "click_web_element", "type_web_element", "read_web_page"
        }:
            return "browser:uncertain-action:reobserve"
        return None

    @staticmethod
    def _metadata(memory: Memory) -> dict[str, Any]:
        try:
            value = json.loads(memory.value)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _parse(cls, memory: Memory) -> ExperienceLesson:
        metadata = cls._metadata(memory)
        return ExperienceLesson(
            key=memory.key,
            scope=memory.scope,
            subject=memory.subject,
            trigger=str(metadata.get("trigger") or "").strip(),
            rule=str(metadata.get("rule") or memory.statement).strip(),
            avoid=str(metadata.get("avoid") or "").strip(),
            preferred=str(metadata.get("preferred") or "").strip(),
            confidence=float(memory.confidence),
            importance=int(memory.importance),
            evidence_count=max(1, int(metadata.get("evidence_count", 1))),
        )
