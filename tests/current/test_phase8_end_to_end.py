from __future__ import annotations

import json
import re
from pathlib import Path

from advi.agent import AgentSession
from advi.agent.contracts import ExecutionDomain, ExecutionRoute, TaskState, TaskStatus
from advi.core.pipeline_trace import begin_turn, current_turn_metrics
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolGuard, ToolRegistry, ToolResult
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus


class ScriptedProvider(LLMProvider):
    def __init__(self, name: str, responses):
        self._name = name
        self.responses = list(responses)
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return f"{self._name}-model"

    def chat(self, messages, *, purpose="chat"):
        self.calls.append({"kind": "chat", "messages": messages})
        return self.responses.pop(0)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.calls.append({"kind": "tools", "messages": messages, "tools": tools})
        return self.responses.pop(0)


class FakeRunner:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    def run(self, call, *, approved=False):
        self.calls.append((call.name, dict(call.arguments)))
        return self.handler(call)


def _tool_registry(*actions: str) -> ToolRegistry:
    registry = CapabilityRegistry()
    registry.register(
        Capability(
            name="synthetic",
            description="synthetic acceptance-test capability",
            supported_actions=set(actions),
            status=CapabilityStatus.AVAILABLE,
            handler=object(),
        )
    )
    return ToolRegistry(registry)


def _verified(call, *, output=None, metadata=None, verification=None, summary="verified"):
    return ToolResult(
        call_id=call.call_id,
        name=call.name,
        success=True,
        output=output,
        human_readable=summary,
        verified=True,
        verification_status="verified",
        metadata=metadata or {},
        verification_details=verification or {},
    )


def test_open_world_browser_journey_uses_brain_for_semantic_result_and_local_continuation_for_known_step():
    tools = _tool_registry("navigate", "search", "click_web_element", "inspect_web_page", "type_web_element", "wait")
    guard = ToolGuard(tools)
    runner = FakeRunner(
        lambda call: (
            _verified(call, metadata={"current_url": "https://www.amazon.in/"}, summary="Navigated to Amazon")
            if call.name == "navigate"
            else _verified(
                call,
                output="2TB SSD",
                verification={"result_candidates": [{"title": "2TB SSD", "selector": "a#ssd"}]},
                summary="Search completed",
            )
            if call.name == "search"
            else _verified(call, metadata={"selected": "2TB SSD"}, summary="Selected 2TB SSD")
        )
    )
    provider = ScriptedProvider(
        "primary",
        [
            LLMResponse(
                text="",
                provider="primary",
                model="m",
                tool_calls=(LLMToolCall("nav", "navigate", {"url": "https://www.amazon.in/"}),),
            ),
            LLMResponse(text="Search completed: the 2TB SSD results are displayed.", provider="primary", model="m"),
        ],
    )
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=guard)

    response = session.respond('Go to Amazon and search for "2TB SSD"')

    assert response.text == "Search completed"
    assert [name for name, _ in runner.calls] == ["navigate", "search"]
    # The controller performs the known navigate -> search step locally; the model
    # still receives the fresh result and remains responsible for semantic decisions.
    tool_calls = [call for call in provider.calls if call["kind"] == "tools"]
    assert len(tool_calls) == 1
    assert session.task is not None and session.task.status == TaskStatus.COMPLETED


def test_browser_search_is_terminal_only_when_no_followup_action_is_requested():
    from advi.core.execution_strategy import infer_execution_strategy, terminal_evidence_matches

    result = _verified(type("Call", (), {"call_id": "s", "name": "search", "arguments": {}})(), summary="search complete")
    plain = infer_execution_strategy("Go to Amazon and search for 2TB SSD")
    compound = infer_execution_strategy("Go to Amazon, search for 2TB SSD and open the first result")

    assert terminal_evidence_matches(plain, "search", result, "Go to Amazon and search for 2TB SSD") is True
    assert terminal_evidence_matches(compound, "search", result, "Go to Amazon, search for 2TB SSD and open the first result") is False


def test_exploration_keeps_david_as_the_only_terminal_route():
    from advi.core.execution_strategy import infer_execution_strategy, terminal_evidence_matches

    result = _verified(type("Call", (), {"call_id": "d", "name": "david_fallback", "arguments": {}})(), summary="David complete")
    strategy = infer_execution_strategy("do something unusual on my laptop")
    assert terminal_evidence_matches(strategy, "david_fallback", result, "do something unusual on my laptop") is True


def test_desktop_uncertain_typing_aborts_stale_followup_side_effect():
    tools = _tool_registry("type_text", "press_key", "read_screen", "inspect_window", "click", "focus_window")
    guard = ToolGuard(tools)
    executed: list[str] = []

    def handler(call):
        executed.append(call.name)
        if call.name == "type_text":
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=True,
                output="Hello from advi",
                human_readable="Typed text",
                verified=False,
                verification_status="uncertain",
                verification_details={"reason": "text_effect_not_observed"},
            )
        if call.name == "press_key":
            return _verified(call, summary="Pressed Enter")
        return _verified(call, summary="observed")

    runner = FakeRunner(handler)
    provider = ScriptedProvider(
        "primary",
        [
            LLMResponse(
                text="",
                provider="primary",
                model="m",
                tool_calls=(
                    LLMToolCall("t", "type_text", {"text": "Hello from advi"}),
                    LLMToolCall("enter", "press_key", {"key": "ENTER"}),
                ),
            ),
            LLMResponse(text="The message text could not be verified.", provider="primary", model="m"),
        ],
    )
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=guard)
    response = session.respond("Type Hello from advi into the WhatsApp message box and send it")

    assert executed[0] == "type_text"
    assert "press_key" not in executed
    assert executed[1:] == ["read_screen", "inspect_window"]
    assert "couldn't verify the final state" in response.text
    assert session.task is not None
    assert session.task.status == TaskStatus.UNCERTAIN


def test_explicit_filename_beats_recent_artifact_reference(tmp_path: Path):
    from advi.capabilities.files.executor import FileCapability

    old = tmp_path / "advi.pdf"
    new = tmp_path / "Music Day Speech.pdf"
    old.write_bytes(b"old")
    new.write_bytes(b"new")

    cap = FileCapability()
    cap.desktop_dir = tmp_path
    from advi.tools.contracts import ToolCall
    result = cap.execute(ToolCall("f", "find_file", {"name": "Music Day Speech.pdf", "scope": "desktop", "limit": 10}))
    assert result.success and result.verified
    assert result.output == [str(new)]
    assert str(old) not in result.output


def test_provider_schema_repair_does_not_rotate_provider_and_preserves_same_task():
    from advi.agent.provider_session import ProviderSessionManager
    from advi.providers.errors import LLMRequestError

    class RejectOnce(ScriptedProvider):
        def __init__(self):
            super().__init__("first", [])
            self.calls_count = 0

        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            self.calls_count += 1
            if self.calls_count == 1:
                raise LLMRequestError("attempted to call tool 'type_web_element' which was not in request.tools")
            return LLMResponse(
                text="repaired",
                provider="first",
                model="m",
                tool_calls=(),
            )

    first = RejectOnce()
    second = ScriptedProvider("second", [LLMResponse(text="wrong provider", provider="second", model="m")])
    manager = ProviderSessionManager([first, second])
    tools = _tool_registry("type_web_element")
    # Simulate AgentSession's one-shot repair path directly through its helper.
    session = AgentSession([first, second], tool_registry=tools, tool_runner=FakeRunner(lambda c: _verified(c)), tool_guard=ToolGuard(tools))
    session._handoff = lambda: None
    try:
        manager.send_user(
            "type it",
            system_instruction="sys",
            tools=[{"type": "function", "function": {"name": "navigate"}}],
        )
    except LLMRequestError as exc:
        repaired = session._retry_with_missing_tool(
            exc,
            user_payload="type it",
            tools=[{"type": "function", "function": {"name": "navigate"}}],
            handoff=None,
            observation=None,
        )
    else:
        raise AssertionError("first request should expose the contract defect")

    assert repaired is not None
    assert first.calls_count == 2
    assert second.calls == []


def test_experience_memory_is_relevant_after_realistic_failure_recovery(tmp_path: Path):
    from advi.memory.experience import ExperienceMemory
    from advi.memory.long_term import LongTermMemory
    from advi.memory.service import MemoryService

    memory = LongTermMemory(tmp_path / "memory.db")
    service = MemoryService(memory)
    lesson = service.memory.experience.record_experience_recovery(
        recovered_tool="type_text",
        domain=ExecutionDomain.DESKTOP.value,
        recovery_tool="focus_window",
        error="text effect not observed",
        session_id=1,
    ) if hasattr(service.memory, "experience") else ExperienceMemory(memory).remember_recovery(
        recovered_tool="type_text",
        domain=ExecutionDomain.DESKTOP.value,
        recovery_tool="focus_window",
        error="text effect not observed",
        session_id=1,
    )
    assert lesson is not None
    retrieved = service.experience_lessons("type a WhatsApp message", active_domain=ExecutionDomain.DESKTOP.value, limit=2)
    assert retrieved and retrieved[0].key == lesson.key


def test_turn_metrics_capture_brain_tools_and_observation_without_exposing_raw_secrets():
    begin_turn("open WhatsApp")
    from advi.core.pipeline_trace import record_metric
    record_metric("llm_calls")
    record_metric("tool_calls", 3)
    record_metric("observation_calls")
    record_metric("local_continuations", 2)
    record_metric("provider_switches")
    metrics = current_turn_metrics()
    assert metrics == {
        "llm_calls": 1,
        "tool_calls": 3,
        "observation_calls": 1,
        "local_continuations": 2,
        "provider_switches": 1,
        "provider_failures": 0,
    }


def test_runtime_log_analyzer_detects_the_known_failure_classes(tmp_path: Path):
    import importlib.util

    module_path = Path(__file__).resolve().parents[2] / "scripts" / "analyze_runtime_log.py"
    spec = importlib.util.spec_from_file_location("advi_log_analyzer", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    log = tmp_path / "advi.log"
    log.write_text(
        "\n".join(
            [
                "attempted to call tool 'type_web_element' which was not in request.tools",
                "text_effect_not_observed",
                "Unknown parameter 'tab_id'",
                "safe tool-call limit",
                "trace() got multiple values for argument 'stage'",
                "active_application='Chrome' target='WhatsApp' browser='https://music.youtube.com'",
                "'advi.pdf' is already open; reused its existing window.",
            ]
        ),
        encoding="utf-8",
    )
    result = module.analyze(log)
    counts = result["failure_pattern_counts"]
    assert counts["missing_tool_contract"] == 1
    assert counts["unverified_typing"] == 1
    assert counts["unknown_parameter"] == 1
    assert counts["unsafe_tool_limit"] == 1
    assert counts["david_trace_error"] == 1
    assert counts["cross_domain_observation"] == 1
    assert counts["wrong_artifact_reuse"] == 1



def test_last_request_query_is_classified_as_state_query():
    from advi.agent.turns import TurnKind, classify_turn
    assert classify_turn("what was the last thing I asked you to do?").kind is TurnKind.QUERY_STATE


def test_reference_folder_phrase_does_not_capture_relational_word():
    from advi.agent.contracts import TaskState
    from advi.agent.session import AgentSession
    task = TaskState(goal="open this folder which you created", task_id="t1")
    session = AgentSession.__new__(AgentSession)
    session.task = task
    session._capture_task_targets(task.goal)
    assert task.current_target == {}
    assert task.protected_parameters == {}

    task.record_artifacts({"folder": r"C:\\Users\\HP\\Downloads\\doremon"})
    session._resolve_recent_reference(task.goal)
    assert task.current_target["value"] == r"C:\\Users\\HP\\Downloads\\doremon"
    assert task.protected_parameters["path"] == r"C:\\Users\\HP\\Downloads\\doremon"


def test_find_file_unique_result_can_promote_absolute_path_for_open():
    from advi.agent.contracts import TaskState
    from advi.agent.session import AgentSession
    task = TaskState(goal="open 1023146_Stavan.pdf", task_id="t1")
    task.operation_intent = "open_file"
    task.protect_parameters({"path": "1023146_Stavan.pdf"}, operation_intent="open_file")
    task.protect_parameters({"name": "1023146_Stavan.pdf"}, operation_intent="find_file")
    resolved = r"C:\\Users\\HP\\Downloads\\1023146_Stavan.pdf"
    task.protect_parameters({"path": resolved}, overwrite=True, operation_intent="open_file")
    session = AgentSession.__new__(AgentSession)
    session.task = task
    from advi.tools.contracts import ToolCall
    call = session._apply_protected_task_parameters(ToolCall("1", "open_file", {"path": "1023146_Stavan.pdf"}))
    assert call.arguments["path"] == resolved


def test_last_request_query_returns_local_session_truth_without_llm():
    from advi.agent.contracts import TaskState
    session = AgentSession.__new__(AgentSession)
    session.task = None
    class Buffer:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hello! How can I help?"},
            {"role": "user", "content": "what can you do?"},
        ]
    session.session_buffer = Buffer()
    result = session._handle_local_last_request_query("what was the last thing I asked you to do?")
    assert result is not None
    assert "haven't asked me to perform an action yet" in result.text.lower()


def test_folder_reference_open_file_is_normalized_to_open_folder():
    from advi.agent.contracts import TaskState
    from advi.tools.contracts import ToolCall
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="open this folder which you created", task_id="t1")
    session.task.operation_intent = "open_folder"
    session.task.set_current_target(kind="folder", value=r"C:\\Users\\HP\\Downloads\\doremon", source="artifact_reference", explicit=False)
    normalized = session._apply_protected_task_parameters(
        ToolCall("1", "open_file", {"path": r"C:\\Users\\HP\\Downloads\\doremon"})
    )
    assert normalized.name == "open_folder"


def test_you_have_to_search_for_application_is_a_new_task():
    from advi.agent.turns import TurnKind, classify_turn
    text = "you have to search for youtube music application on my laptop, then click on first option"
    assert classify_turn(text).kind is TurnKind.NEW_TASK


def test_file_and_memory_discovery_operations_are_terminal_when_goal_is_read_only():
    from advi.core.execution_strategy import terminal_evidence_matches, infer_execution_strategy
    from advi.tools.contracts import ToolResult
    good = _verified(type("Call", (), {"call_id": "x", "name": "list_files", "arguments": {}})(), summary="listed")
    assert terminal_evidence_matches(infer_execution_strategy("list pdfs in downloads"), "list_files", good, "list pdfs in downloads")
    read = _verified(type("Call", (), {"call_id": "r", "name": "read_file", "arguments": {}})(), summary="read")
    assert terminal_evidence_matches(infer_execution_strategy("read this pdf"), "read_file", read, "read this pdf")
    mem = _verified(type("Call", (), {"call_id": "m", "name": "memory_update", "arguments": {}})(), summary="remembered")
    assert terminal_evidence_matches(infer_execution_strategy("remember this"), "memory_update", mem, "remember this")


def test_read_file_content_is_available_to_a_follow_up_about_the_same_pdf(tmp_path: Path):
    from advi.agent.contracts import TaskState
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="open example.pdf", task_id="t1", status=TaskStatus.COMPLETED)
    session.pending_confirmation = None
    session._pending_david_email_send = None
    session._approved_gmail_fallback = None
    session.last_tool = "read_file"
    session.last_tool_success = True
    session.last_tool_summary = "read example.pdf"
    session._recent_tool_exchanges = []
    session._last_completed_goal = "open example.pdf"
    session._last_completed_artifacts = {"path": str(tmp_path / "example.pdf")}
    session._last_completed_artifact_aliases = {"last_pdf": str(tmp_path / "example.pdf")}
    session._last_read_path = str(tmp_path / "example.pdf")
    session._last_read_content = "This PDF is a school certificate and marksheet."
    # A completed task has just crossed a turn boundary, so the read content must
    # still be available even when the new turn starts with no active task.
    session._prepare_user_turn("tell me what that pdf is about")
    ctx = session._reference_context("tell me what that pdf is about")
    assert ctx is not None
    assert "school certificate and marksheet" in ctx


def test_you_have_to_request_retires_previous_uncertain_task_boundary():
    from types import SimpleNamespace
    session = AgentSession.__new__(AgentSession)
    session.pending_confirmation = None
    session._pending_david_email_send = None
    session._approved_gmail_fallback = None
    session.last_tool = None
    session.last_tool_success = None
    session.last_tool_summary = None
    session._recent_tool_exchanges = []
    session._last_completed_goal = None
    session._last_completed_artifacts = {}
    session._last_completed_artifact_aliases = {}
    session.task = TaskState(goal="old task", task_id="old", status=TaskStatus.UNCERTAIN)
    session._prepare_user_turn("you have to search for youtube music application on my laptop, then click on first option")
    assert session.task is None
