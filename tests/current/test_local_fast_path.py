from __future__ import annotations

from types import SimpleNamespace

from advi.agent.contracts import TaskState, TaskStatus
from advi.agent.session import AgentSession
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
from advi.tools.contracts import ToolResult
from advi.providers.base import LLMProvider, LLMResponse


class NeverProvider(LLMProvider):
    @property
    def name(self):
        return "never"

    @property
    def model(self):
        return "never"

    def chat(self, messages, *, purpose="chat"):
        raise AssertionError("fast path must not call the provider")

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        raise AssertionError("fast path must not call the provider")


class FakeEngine:
    def execute_tool_call(self, call, *, approval_granted=False):
        if call.name == "get_battery_status":
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=True,
                verified=True,
                verification_status="verified",
                output={"percentage": 87},
                human_readable="Battery is at 87%.",
            )
        raise AssertionError(f"unexpected tool: {call.name}")


def make_session(*, memory=None):
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="system_control",
        description="system",
        supported_actions={"get_battery_status"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    tools = ToolRegistry(registry)
    runner = ToolRunner(FakeEngine(), ToolGuard(tools))
    return AgentSession(
        [NeverProvider()],
        tool_registry=tools,
        tool_runner=runner,
        tool_guard=runner.guard,
        memory=memory,
    )


def test_greeting_never_calls_provider_or_creates_task():
    session = make_session()
    response = session.respond("hello")
    assert response.text == "Hello! How can I help?"
    assert session.task is None


def test_task_status_query_is_task_free_when_idle():
    session = make_session()
    response = session.respond("what task are active")
    assert response.text == "There are no active tasks right now."
    assert session.task is None


def test_greeting_retires_uncertain_task_without_overwriting_previous_task_message():
    session = make_session()
    task = TaskState(goal="send an email", task_id="t1", status=TaskStatus.UNCERTAIN)
    session.task = task
    session.last_user_message = task.goal
    session.last_assistant_message = "I could not verify the final state."

    response = session.respond("hello")

    assert response.text == "Hello! How can I help?"
    assert session.last_user_message == task.goal
    assert session.last_assistant_message == "I could not verify the final state."
    assert session.task is None


def test_task_status_query_reports_existing_active_task_without_mutating_it():
    session = make_session()
    task = TaskState(goal="send an email", task_id="t1", status=TaskStatus.UNCERTAIN)
    task.waiting_for = "recipient"
    session.task = task
    response = session.respond("what task are active")
    assert "one active task" in response.text.lower()
    assert "send an email" in response.text
    assert "waiting for recipient" in response.text.lower()
    assert session.task is task
    assert task.status == TaskStatus.UNCERTAIN


def test_greeting_retires_completed_task_without_touching_conversation_history():
    session = make_session()
    task = TaskState(goal="open Notepad", task_id="t1", status=TaskStatus.COMPLETED)
    session.task = task
    session.last_user_message = task.goal
    session.last_assistant_message = "The requested action was completed and verified."

    response = session.respond("hello")

    assert response.text == "Hello! How can I help?"
    assert session.task is None
    assert session.last_user_message == "hello"
    assert session.last_assistant_message == "Hello! How can I help?"

def test_battery_query_uses_local_tool_without_creating_task():
    session = make_session()
    response = session.respond("what is my battery status")
    assert response.text == "Battery is at 87%."
    assert session.task is None


def test_capability_query_does_not_create_task():
    session = make_session()
    response = session.respond("what are your capabilities")
    # Spoken sentences, never raw tool identifiers.
    assert response.text.startswith("I can ")
    assert "battery" in response.text and "get_battery_status" not in response.text
    assert session.task is None


def test_user_email_query_is_local_and_task_free():
    class FakeStore:
        def recall(self, category, key):
            assert (category, key) == ("user_profile", "email")
            return SimpleNamespace(value="stavankalkumbe@gmail.com")

        def find_contacts(self, query, limit=5):
            return []

    session = make_session(memory=SimpleNamespace(memory=FakeStore()))
    response = session.respond("what is my email")
    assert response.text == "Your email address is stavankalkumbe@gmail.com."
    assert session.task is None
