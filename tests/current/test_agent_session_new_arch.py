from __future__ import annotations

import json

from advi.agent import AgentSession
from advi.tools.contracts import ToolResult
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus


class FakeProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "fake-model"

    def chat(self, messages, *, purpose="chat"):
        self.calls.append(messages)
        return self.responses.pop(0)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.calls.append((messages, tools))
        return self.responses.pop(0)


class FakeEngine:
    def __init__(self):
        self.calls = []

    def execute_tool_call(self, call, *, approval_granted=False):
        self.calls.append(call)
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            success=True,
            output={"opened": call.arguments.get("application")},
            verified=True,
            verification_status="verified",
            verification_details={"state": "verified"},
        )


def make_session(responses):
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="desktop_control",
        description="desktop",
        supported_actions={"open_application"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    engine = FakeEngine()
    tools = ToolRegistry(registry)
    runner = ToolRunner(engine, ToolGuard(tools))
    session = AgentSession([FakeProvider(responses)], tool_registry=tools, tool_runner=runner, tool_guard=runner.guard)
    session.local_intents_enabled = False  # exercises the model loop
    return session, engine


def test_agent_session_executes_native_tool_then_speaks():
    session, engine = make_session([
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("c1", "open_application", {"application": "notepad"}),)),
        LLMResponse(text="Notepad is open.", provider="fake", model="m"),
    ])
    response = session.respond("Open Notepad")
    assert response.text == "Notepad is open."
    assert [a.name for a in engine.calls] == ["open_application"]


def test_tool_result_is_returned_to_provider_without_transcript_dump():
    session, _ = make_session([
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("c1", "open_application", {"application": "notepad"}),)),
        LLMResponse(text="Done", provider="fake", model="m"),
    ])
    session.respond("Open Notepad")
    provider = session.provider_manager.providers[0]
    first_messages = provider  # keep reference alive for introspection through manager
    raw = session.provider_manager.session.messages
    assert len(raw) >= 5
    assert raw[1]["role"] == "user"
    assert raw[-1]["role"] == "assistant"
    assert any(item.get("role") == "tool" for item in raw)
    assert not any("ShortTermMemory" in json.dumps(item) for item in raw)







def test_terminal_task_is_not_reused_for_next_request():
    responses = [
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("c1", "open_application", {"application": "notepad"}),)),
        LLMResponse(text="Done one", provider="fake", model="m"),
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("c2", "open_application", {"application": "calculator"}),)),
        LLMResponse(text="Done two", provider="fake", model="m"),
    ]
    session, _ = make_session(responses)

    first = session.respond("Open Notepad")
    first_task_id = session.task.task_id
    assert first.text == "Done one"
    assert session.task.status.value == "completed"

    second = session.respond("Open Calculator")
    second_task_id = session.task.task_id
    assert second.text == "Done two"
    assert second_task_id != first_task_id
    assert session.task.goal == "Open Calculator"
    assert session.task.failed_tools == []








def test_multiple_tool_calls_in_one_model_response_share_one_round():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="desktop_control",
        description="desktop",
        supported_actions={"open_application"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    engine = FakeEngine()
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    provider = FakeProvider([
        LLMResponse(text="", provider="fake", model="m", tool_calls=(
            LLMToolCall("c1", "open_application", {"application": "notepad"}),
            LLMToolCall("c2", "open_application", {"application": "calculator"}),
        )),
        LLMResponse(text="Done", provider="fake", model="m"),
    ])
    session = AgentSession(
        [provider],
        tool_registry=tools,
        tool_runner=ToolRunner(engine, guard),
        tool_guard=guard,
        max_tool_rounds=1,
    )
    session.local_intents_enabled = False  # exercises the model loop

    response = session.respond("Open both apps")
    assert response.text == "Done"
    assert session.task is not None
    assert session.task.tool_rounds == 1
    # Deliberate multi-step desktop work executes one side effect, then asks the
    # provider for a fresh plan based on the resulting environment. A stale second
    # call from the same model response must not be executed blindly.
    assert [call.name for call in engine.calls] == ["open_application"]




def test_confirm_pending_fallback_is_not_recorded_as_failed_outcome():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="david_fallback",
        description="David",
        supported_actions={"david_fallback"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    engine = FakeEngine()
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    session = AgentSession(
        [FakeProvider([LLMResponse(text="done", provider="fake", model="m")])],
        tool_registry=tools,
        tool_runner=ToolRunner(engine, guard),
        tool_guard=guard,
    )
    session.local_intents_enabled = False  # exercises the model loop
    session.task = __import__("advi.agent.contracts", fromlist=["TaskState"]).TaskState(goal="x", task_id="t1")
    result = ToolResult(
        call_id="c1",
        name="david_fallback",
        success=False,
        requires_confirmation=True,
        confirmation_reason="needs approval",
    )
    session.tool_runner = type("Runner", (), {"run": lambda self, call, approved=False: result})()
    out = session._run_tool(LLMToolCall("c1", "david_fallback", {"request": "do it", "reason": "unknown_task"}))
    assert out.requires_confirmation is True
    assert session.task.failed_tools == []
