from __future__ import annotations

from pathlib import Path

from advi.memory.experience import ExperienceMemory
from advi.memory.long_term import LongTermMemory
from advi.memory.service import MemoryService


def make_memory(tmp_path: Path) -> LongTermMemory:
    return LongTermMemory(tmp_path / "memory.sqlite3")


def test_lesson_persists_and_is_reinforced_without_second_database(tmp_path):
    memory = make_memory(tmp_path)
    experience = ExperienceMemory(memory)

    first = experience.remember_recovery(
        recovered_tool="type_text",
        domain="desktop",
        recovery_tool="focus_window",
        error="text effect not observed",
        session_id=7,
    )
    assert first is not None
    assert first.evidence_count == 1
    assert first.confidence == 0.86

    second = experience.remember_recovery(
        recovered_tool="type_text",
        domain="desktop",
        recovery_tool="focus_window",
        error="text effect not observed",
        session_id=8,
    )
    assert second is not None
    assert second.key == first.key
    assert second.evidence_count == 2
    assert second.confidence > first.confidence

    lessons = [item for item in memory.all() if item.kind == "lesson"]
    assert len(lessons) == 1
    assert lessons[0].source_session_id == 8
    assert "focus" in lessons[0].statement.casefold()


def test_experience_is_relevant_to_matching_domain_and_query(tmp_path):
    memory = make_memory(tmp_path)
    experience = ExperienceMemory(memory)
    experience.remember_recovery(
        recovered_tool="type_text", domain="desktop", recovery_tool="focus_window",
        error="text effect not observed", session_id=1,
    )
    experience.warm()

    desktop = experience.relevant("type text in WhatsApp message box", active_domain="desktop", limit=2)
    assert any("focused" in item.statement.casefold() or "editable" in item.statement.casefold() for item in desktop)

    browser = experience.relevant("type text in WhatsApp message box", active_domain="browser", limit=2)
    assert browser == []

    unrelated = experience.relevant("create a PDF about batteries", active_domain="filesystem", limit=2)
    assert unrelated == []


def test_memory_service_keeps_lessons_separate_from_normal_fact_recall(tmp_path):
    memory = make_memory(tmp_path)
    memory.remember_rich(
        kind="user", scope="user", subject="user", key="institution",
        value="FCRIT Vashi", statement="The user studies at FCRIT Vashi.", confidence=0.98,
    )
    experience = ExperienceMemory(memory)
    experience.remember_recovery(
        recovered_tool="type_text", domain="desktop", recovery_tool="focus_window",
        error="text effect not observed", session_id=1,
    )

    service = MemoryService(memory)
    facts = service.recall("FCRIT WhatsApp typing", limit=8)
    lessons = service.experience_lessons("WhatsApp typing", active_domain="desktop", limit=2)

    assert all(source.get("category") != "lesson" for source in facts.sources)
    assert any(item.key == "desktop:type_text:focus-before-type" for item in lessons)


def test_high_confidence_lessons_are_warmable_at_startup(tmp_path):
    memory = make_memory(tmp_path)
    experience = ExperienceMemory(memory)
    for _ in range(2):
        experience.remember_recovery(
            recovered_tool="type_text", domain="desktop", recovery_tool="focus_window",
            error="text effect not observed", session_id=1,
        )
    count = experience.warm(min_confidence=0.88)
    assert count == 1
    assert experience.startup_lessons[0].evidence_count == 2
    assert experience.startup_lessons[0].confidence >= 0.88


def test_agent_session_records_experience_after_verified_recovery(monkeypatch):
    from advi.agent import AgentSession
    from advi.agent.contracts import TaskState
    from advi.providers.base import LLMProvider, LLMResponse
    from advi.tools import ToolGuard, ToolRegistry
    from advi.capabilities.registry import CapabilityRegistry, Capability, CapabilityStatus
    from advi.tools.contracts import ToolResult
    from advi.providers.base import LLMToolCall

    class Provider(LLMProvider):
        @property
        def name(self): return "fake"
        @property
        def model(self): return "fake"
        def chat(self, messages, *, purpose="chat"): return LLMResponse(text="done", provider="fake", model="fake")
        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            return LLMResponse(text="done", provider="fake", model="fake")

    class Runner:
        def __init__(self):
            self.results = [
                ToolResult(call_id="a", name="type_text", success=True, verified=False,
                           verification_status="uncertain", verification_details={"reason": "text_effect_not_observed"}),
                ToolResult(call_id="b", name="type_text", success=True, verified=True,
                           verification_status="verified", verification_details={"state": "verified"}),
            ]
        def run(self, call, approved=False):
            return self.results.pop(0)

    registry = CapabilityRegistry()
    registry.register(Capability(
        name="desktop_control", description="desktop", supported_actions={"type_text"},
        status=CapabilityStatus.AVAILABLE, handler=object(),
    ))
    tools = ToolRegistry(registry)
    runner = Runner()
    guard = ToolGuard(tools)

    class MemoryStub:
        def __init__(self): self.calls = []
        def record_experience_recovery(self, **kwargs):
            self.calls.append(kwargs)
            return type("Lesson", (), {"key": "desktop:type_text:focus-before-type", "evidence_count": 1, "confidence": 0.86})()

    memory = MemoryStub()
    session = AgentSession([Provider()], tool_registry=tools, tool_runner=runner, tool_guard=guard, memory=memory)
    session.task = TaskState(goal="type into WhatsApp", task_id="phase5-recovery")
    session.task.active_domain = "desktop"
    monkeypatch.setattr(session, "_capture_native_recovery_observation", lambda *args, **kwargs: None)

    session._run_tool(LLMToolCall("a", "type_text", {"text": "hello"}))
    session._run_tool(LLMToolCall("b", "type_text", {"text": "hello"}))

    assert len(memory.calls) == 1
    assert memory.calls[0]["recovered_tool"] == "type_text"
    assert memory.calls[0]["recovery_tool"] == "type_text"


def test_agent_memory_context_prioritizes_relevant_lessons_without_dropping_facts():
    from advi.agent import AgentSession
    from advi.agent.contracts import TaskState
    from advi.providers.base import LLMProvider, LLMResponse
    from advi.tools import ToolGuard, ToolRegistry
    from advi.capabilities.registry import CapabilityRegistry
    from advi.memory.service import MemoryRecall

    class Provider(LLMProvider):
        @property
        def name(self): return "fake"
        @property
        def model(self): return "fake"
        def chat(self, messages, *, purpose="chat"): return LLMResponse(text="done", provider="fake", model="fake")
        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            return LLMResponse(text="done", provider="fake", model="fake")

    class MemoryStub:
        def recall(self, query, limit=5):
            return MemoryRecall(("The user studies at FCRIT Vashi.",), ({"category": "user"},))
        def core(self, limit=8):
            return MemoryRecall(("The user prefers practical explanations.",), ({"category": "preference"},))
        def experience_lessons(self, query, *, active_domain=None, limit=2):
            return (type("Lesson", (), {"statement": "Before typing into a desktop field, verify the intended control is focused and editable."})(),)

    tools = ToolRegistry(CapabilityRegistry())
    guard = ToolGuard(tools)
    runner = type("Runner", (), {})()
    session = AgentSession([Provider()], tool_registry=tools, tool_runner=runner, tool_guard=guard, memory=MemoryStub())
    session.task = TaskState(goal="type into WhatsApp", task_id="phase5-context")
    session.task.active_domain = "desktop"
    values = session._memory_context("type a WhatsApp message")
    assert values[0].startswith("[Experience lesson]")
    assert any("FCRIT" in value for value in values)
    assert any("practical explanations" in value for value in values)
