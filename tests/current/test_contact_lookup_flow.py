from __future__ import annotations

import pytest
from pathlib import Path

from advi.memory.long_term import LongTermMemory
from advi.capabilities.memory.executor import MemoryCapability
from advi.capabilities.registry import CapabilityRegistry, Capability, CapabilityStatus
from advi.tools.contracts import ToolCall
from advi.tools import ToolRunner, ToolGuard, ToolRegistry
from advi.agent import AgentSession
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall


def test_contact_lookup_in_memory_capability(tmp_path: Path):
    db_path = tmp_path / "test_memory.db"
    mem = LongTermMemory(db_path)
    mem.save_contact("David Smith", "david@example.com")
    mem.save_contact("Alice Jones", "alice@example.com")
    mem.save_contact("David Miller", "dmiller@example.com")

    cap = MemoryCapability(memory=mem)

    # 1. Exact match
    res_exact = cap.execute(ToolCall(call_id="c1", name="contact_lookup", arguments={"name": "David Smith"}))
    assert res_exact.success is True
    assert res_exact.verified is True
    assert len(res_exact.output) == 1
    assert res_exact.output[0]["name"] == "David Smith"
    assert res_exact.output[0]["email"] == "david@example.com"
    assert "david@example.com" in res_exact.human_readable

    # 2. Ambiguous match
    res_ambig = cap.execute(ToolCall(call_id="c2", name="contact_lookup", arguments={"name": "David"}))
    assert res_ambig.success is True
    assert len(res_ambig.output) == 2
    assert res_ambig.metadata.get("ambiguous") is True
    assert "Clarification required" in res_ambig.human_readable

    # 3. Not found
    res_none = cap.execute(ToolCall(call_id="c3", name="contact_lookup", arguments={"name": "Bob"}))
    assert res_none.success is True
    assert len(res_none.output) == 0
    assert "No contact found" in res_none.human_readable

    # 4. Empty query
    res_empty = cap.execute(ToolCall(call_id="c4", name="contact_lookup", arguments={"name": ""}))
    assert res_empty.success is False
    assert "No contact name provided" in res_empty.error


def test_contact_lookup_tool_registry_and_contracts(tmp_path: Path):
    from advi.capabilities.registry import build_default_registry

    db_path = tmp_path / "test_memory.db"
    mem = LongTermMemory(db_path)
    registry = build_default_registry(memory=mem)

    spec = registry.get_action_spec("contact_lookup")
    assert spec is not None
    assert spec.action == "contact_lookup"
    assert spec.capability == "memory"
    assert "name" in spec.parameters
    assert spec.requires_confirmation is False

    tool_reg = ToolRegistry(registry)
    tool_spec = tool_reg.get("contact_lookup")
    assert tool_spec is not None
    openai_tool = tool_spec.to_openai_tool()
    assert openai_tool["function"]["name"] == "contact_lookup"
    assert "name" in openai_tool["function"]["parameters"]["properties"]


class FakeFlowProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls = []

    @property
    def name(self):
        return "fake_flow"

    @property
    def model(self):
        return "fake-model"

    def chat(self, messages, *, purpose="chat"):
        self.calls.append(messages)
        return self.responses.pop(0)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.calls.append((messages, tools))
        return self.responses.pop(0)


class MockExecutionEngine:
    def __init__(self, memory_cap):
        self.memory_cap = memory_cap
        self.executed = []

    def execute_tool_call(self, call, *, approval_granted=False):
        self.executed.append(call)
        if call.name == "contact_lookup":
            return self.memory_cap.execute(call)
        from advi.tools.contracts import ToolResult
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            success=True,
            output={"sent": True},
            verified=True,
            verification_status="verified",
            metadata={"message_id": "msg_123"},
        )


def test_agent_session_contact_resolution_flow(tmp_path: Path):
    db_path = tmp_path / "test_memory.db"
    mem = LongTermMemory(db_path)
    mem.save_contact("David", "david.test@example.com")
    mem_cap = MemoryCapability(memory=mem)

    cap_registry = CapabilityRegistry()
    cap_registry.register(Capability(
        name="memory",
        description="memory",
        supported_actions=mem_cap.SUPPORTED_ACTIONS,
        status=CapabilityStatus.AVAILABLE,
        handler=mem_cap,
    ))
    cap_registry.register(Capability(
        name="email",
        description="email",
        supported_actions={"email_send"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))

    engine = MockExecutionEngine(mem_cap)
    tools = ToolRegistry(cap_registry)
    guard = ToolGuard(tools)
    tool_runner = ToolRunner(engine, guard)

    # Provider sequence:
    # 1. Model decides to call contact_lookup(name="David")
    # 2. Model receives contact result with email, then asks confirmation or sends email
    responses = [
        LLMResponse(
            text="",
            provider="fake_flow",
            model="fake-model",
            tool_calls=(LLMToolCall(call_id="call_1", name="contact_lookup", arguments={"name": "David"}),),
        ),
        LLMResponse(
            text="I found David's email (david.test@example.com). Would you like me to send the email with content 'this is testing email'?",
            provider="fake_flow",
            model="fake-model",
        ),
    ]

    provider = FakeFlowProvider(responses)
    session = AgentSession(
        providers=[provider],
        tool_registry=tools,
        tool_runner=tool_runner,
        tool_guard=guard,
    )

    result = session.respond("send an email to david and tell him this is testing email")
    assert "david.test@example.com" in result.text
    assert len(engine.executed) == 1
    assert engine.executed[0].name == "contact_lookup"
    assert engine.executed[0].arguments["name"] == "David"
