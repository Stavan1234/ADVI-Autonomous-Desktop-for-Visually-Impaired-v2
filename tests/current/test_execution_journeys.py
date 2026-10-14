from __future__ import annotations

from pathlib import Path

from advi.agent import AgentSession
from advi.agent.contracts import TaskState, TaskStatus
from advi.capabilities.files.executor import FileCapability
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.providers.errors import LLMUnavailableError
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner, ToolResult
from advi.core.execution_engine import ExecutionEngine
from advi.core.verification import ToolVerifier


class SequenceProvider(LLMProvider):
    def __init__(self, name: str, responses, fail_on_tool_results: bool = False):
        self._name = name
        self.responses = list(responses)
        self.fail_on_tool_results = fail_on_tool_results
        self.user_calls = []
        self.tool_result_calls = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return f"{self._name}-model"

    def chat(self, messages, *, purpose="chat"):
        self.user_calls.append(messages)
        return self.responses.pop(0)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        if self.fail_on_tool_results and any(m.get("role") == "tool" for m in messages):
            raise LLMUnavailableError(f"{self._name} unavailable after tool execution")
        self.tool_result_calls.append(messages)
        return self.responses.pop(0)


def make_file_runtime(tmp_path: Path):
    file_cap = FileCapability()
    file_cap.desktop_dir = tmp_path
    registry = CapabilityRegistry()
    registry.register(
        Capability(
            name="file_management",
            description="files",
            supported_actions=file_cap.SUPPORTED_ACTIONS,
            status=CapabilityStatus.AVAILABLE,
            handler=file_cap,
        )
    )
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    engine = ExecutionEngine(registry=registry, verifier=ToolVerifier())
    runner = ToolRunner(engine, guard, tools)
    return registry, tools, guard, engine, runner


def test_real_native_filesystem_journey(tmp_path):
    _, tools, guard, _, runner = make_file_runtime(tmp_path)

    create = runner.run(ToolCall("c1", "create_file", {"path": "journey.txt", "content": "hello"}))
    append = runner.run(ToolCall("c2", "append_file", {"path": "journey.txt", "content": " world"}))
    replace = runner.run(ToolCall("c3", "replace_file_text", {
        "path": "journey.txt", "old_text": "world", "new_text": "ADVI",
    }))
    read = runner.run(ToolCall("c4", "read_file", {"path": "journey.txt"}))

    delete_call = ToolCall("c5", "delete_file", {"path": "journey.txt"})
    deleted = runner.run(delete_call)

    assert create.success and create.verified
    assert append.success and append.verified
    assert replace.success and replace.verified
    assert read.success and read.verified and read.output == "hello ADVI"
    assert deleted.success and deleted.verified
    assert not (tmp_path / "journey.txt").exists()


def test_agent_session_completes_a_real_file_task(tmp_path):
    _, tools, guard, engine, runner = make_file_runtime(tmp_path)
    provider = SequenceProvider(
        "primary",
        [
            LLMResponse(
                text="",
                provider="primary",
                model="m",
                tool_calls=(LLMToolCall("c1", "create_file", {"path": "hello.txt", "content": "hello"}),),
            ),
            LLMResponse(text="Created hello.txt and verified it.", provider="primary", model="m"),
        ],
    )
    session = AgentSession(
        [provider],
        tool_registry=tools,
        tool_runner=runner,
        tool_guard=guard,
    )

    response = session.respond("Create hello.txt containing hello")

    assert response.text == "Created hello.txt and verified it."
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert session.task is not None
    assert session.task.status.value == "completed"


def test_provider_switch_preserves_structured_tool_exchange(tmp_path):
    _, tools, guard, _, runner = make_file_runtime(tmp_path)
    first = SequenceProvider(
        "first",
        [
            LLMResponse(
                text="",
                provider="first",
                model="m1",
                tool_calls=(LLMToolCall("c1", "create_file", {"path": "failover.txt", "content": "x"}),),
            ),
        ],
        fail_on_tool_results=True,
    )
    second = SequenceProvider(
        "second",
        [LLMResponse(text="Created and continued after provider failover.", provider="second", model="m2")],
    )
    session = AgentSession(
        [first, second],
        tool_registry=tools,
        tool_runner=runner,
        tool_guard=guard,
    )

    response = session.respond("Create failover.txt containing x")

    assert response.text == "Created and continued after provider failover."
    assert (tmp_path / "failover.txt").read_text(encoding="utf-8") == "x"
    assert session.provider_name == "second"
    assert session.provider_manager.switch_count == 1
    assert session.task is not None and session.task.status.value == "completed"
    assert second.tool_result_calls, "handoff continuation should reach the second provider"
    handoff_messages = second.tool_result_calls[0]
    assert any(
        message.get("role") == "assistant"
        and any(call.get("id") == "c1" for call in message.get("tool_calls", []))
        for message in handoff_messages
    )
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "c1"
        for message in handoff_messages
    )



def test_file_content_search_is_executable(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("alpha\nbeta ADVI\ngamma", encoding="utf-8")
    cap = FileCapability()
    cap.desktop_dir = tmp_path
    result = cap.execute(ToolCall("content", "search_file_content", {"path": str(path), "query": "ADVI"}))
    assert result.success
    assert result.verified
    assert any("beta ADVI" in str(row.get("excerpt")) for row in result.data)




def test_find_file_does_not_trust_stale_watchtower_cache(tmp_path: Path) -> None:
    class StaleWatchtower:
        def find_files_by_name(self, name, limit=20, scope="desktop"):
            return [str(tmp_path / "missing.txt")]

    actual = tmp_path / "Passbook.pdf"
    actual.write_bytes(b"pdf placeholder")
    cap = FileCapability(environment_watchtower=StaleWatchtower())
    cap.desktop_dir = tmp_path
    result = cap.execute(ToolCall("find-cache", "find_file", {"name": "Passbook.pdf", "scope": "desktop"}))
    assert result.success
    assert result.verified
    assert str(actual) in result.data

def test_content_search_without_path_uses_scope(tmp_path: Path) -> None:
    path = tmp_path / "scoped.txt"
    path.write_text("before ADVI after", encoding="utf-8")
    cap = FileCapability()
    cap.desktop_dir = tmp_path
    result = cap.execute(ToolCall("content-scope", "search_file_content", {"query": "ADVI", "scope": "desktop"}))
    assert result.success
    assert result.verified
    assert result.metadata.get("scope") == "desktop"
    assert any(row.get("path") == str(path) for row in result.data)


def test_text_reader_supports_utf16_bom(tmp_path: Path) -> None:
    path = tmp_path / "utf16.txt"
    path.write_bytes("hello ADVI".encode("utf-16"))
    cap = FileCapability()
    cap.desktop_dir = tmp_path
    result = cap.execute(ToolCall("utf16", "read_file", {"path": "utf16.txt"}))
    assert result.success
    assert result.verified
    assert "hello ADVI" in result.data
    assert result.metadata.get("parser") == "utf-16"

def test_compound_tool_batch_stops_after_failed_dependency(tmp_path):
    _, tools, guard, _, runner = make_file_runtime(tmp_path)

    class CountingEngine:
        def __init__(self):
            self.calls = []
        def execute_tool_call(self, call, **kwargs):
            self.calls.append(call.name)
            if call.name == "read_file":
                return ToolResult(call_id=call.call_id, name=call.name, success=False, error="missing")
            return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True, output={"path": "x"})

    engine = CountingEngine()
    runner = ToolRunner(engine, guard, tools)
    provider = SequenceProvider("primary", [LLMResponse(text="stopped", provider="primary", model="m")])
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=guard)
    session.prepare()
    response = session._continue_execution(LLMResponse(
        text="", provider="primary", model="m",
        tool_calls=(
            LLMToolCall("r1", "read_file", {"path": "missing.txt"}),
            LLMToolCall("r2", "delete_file", {"path": "missing.txt"}),
        ),
    ), [])
    assert engine.calls == ["read_file"]


def test_verified_artifacts_survive_in_task_state(tmp_path):
    _, tools, guard, _, runner = make_file_runtime(tmp_path)
    provider = SequenceProvider("primary", [
        LLMResponse(text="", provider="primary", model="m", tool_calls=(
            LLMToolCall("c1", "create_file", {"path": "artifact.txt", "content": "x"}),
        )),
        LLMResponse(text="done", provider="primary", model="m"),
    ])
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=guard)
    session.respond("Create artifact.txt containing x")
    assert session.task is not None
    assert str(session.task.artifacts.get("path", "")).endswith("artifact.txt")


def test_uncertain_task_can_be_resumed_with_current_user_command(tmp_path):
    _, tools, guard, _, runner = make_file_runtime(tmp_path)
    provider = SequenceProvider("primary", [
        LLMResponse(text="", provider="primary", model="m", tool_calls=(
            LLMToolCall("c1", "create_file", {"path": "resume.txt", "content": "x"}),
        )),
        LLMResponse(text="done", provider="primary", model="m"),
    ])
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=guard)
    session.last_user_message = "original compound task"
    session.task = TaskState(goal="original compound task", task_id="task-1", status=TaskStatus.UNCERTAIN)
    session.respond("continue")
    assert session.task is not None
    assert session.task.task_id == "task-1"


def test_create_file_does_not_overwrite_existing_file(tmp_path):
    cap = FileCapability()
    cap.desktop_dir = tmp_path
    path = tmp_path / "existing.txt"
    path.write_text("original", encoding="utf-8")
    result = cap.execute(ToolCall("create", "create_file", {"path": "existing.txt", "content": "replacement"}))
    assert not result.success
    assert path.read_text(encoding="utf-8") == "original"


def test_email_side_effect_is_deduplicated_within_task():
    from advi.agent.session import _side_effect_fingerprint
    provider = SequenceProvider("primary", [LLMResponse(text="ok", provider="primary", model="m")])
    session = AgentSession([provider], tool_registry=ToolRegistry(CapabilityRegistry()), tool_runner=None, tool_guard=None)  # type: ignore[arg-type]
    session.task = TaskState(goal="send", task_id="t")
    call = ToolCall("a", "email_send", {"to": "d@example.com", "subject": "Hi", "body": "Hello"})
    fp = _side_effect_fingerprint(call)
    session.task.remember_side_effect(fp, {"message_id": "m123", "summary": "sent"})
    duplicate = session._deduplicate_side_effect(ToolCall("b", "email_send", dict(call.arguments)))
    assert duplicate is not None
    assert duplicate.success and duplicate.verified
    assert duplicate.metadata.get("deduplicated") is True
