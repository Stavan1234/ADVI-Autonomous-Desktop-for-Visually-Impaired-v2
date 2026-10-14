from __future__ import annotations

from types import SimpleNamespace

from advi.agent.contracts import TaskState
from advi.agent.session import AgentSession
from advi.agent.session_handoff import SessionHandoffBuilder
from advi.core.action_contracts import validate_and_normalize_tool_call
from advi.fallback.intent_schema import Intent
from advi.fallback.service import FallbackService
from advi.tools.contracts import ToolCall


def _session(task: TaskState) -> AgentSession:
    session = AgentSession.__new__(AgentSession)
    session.task = task
    return session


def test_user_captured_literal_is_task_owned_and_reused() -> None:
    task = TaskState(goal='search for "Never Say Never"', task_id="t1")
    session = _session(task)
    session._capture_task_targets(task.goal)

    assert task.operation_intent == "web_search"
    assert task.protected_parameters["query"] == "Never Say Never"

    later = session._apply_protected_task_parameters(
        ToolCall(call_id="2", name="search", arguments={"query": "video"})
    )
    assert later.arguments["query"] == "Never Say Never"


def test_model_tool_arguments_cannot_poison_protected_values() -> None:
    task = TaskState(goal='create a ppt about "India"', task_id="t1")
    session = _session(task)
    task.protect_parameters({"path": "India.pptx", "slides": 5}, operation_intent="create_file")

    call = session._apply_protected_task_parameters(
        ToolCall(call_id="1", name="create_file", arguments={
            "path": "ADVI.pdf", "content": "wrong", "pages": 10, "style": "beautiful",
        })
    )
    assert call.arguments["path"] == "India.pptx"
    assert call.arguments["slides"] == 5
    # Content/pages/style are not user-protected here, so the model may still
    # choose them for the current document; importantly, it cannot replace the
    # user-owned filename.
    assert task.protected_parameters == {"path": "India.pptx", "slides": 5}



def test_protected_values_are_owned_by_their_operation() -> None:
    task = TaskState(goal="use two applications", task_id="t1")
    session = _session(task)

    session._apply_protected_task_parameters(
        ToolCall(call_id="1", name="open_application", arguments={"application": "Chrome"})
    )
    search = session._apply_protected_task_parameters(
        ToolCall(call_id="2", name="search", arguments={"query": "Never Say Never"})
    )
    assert search.arguments["query"] == "Never Say Never"

    # A later setup step can legitimately open another application; the old Chrome
    # literal must not be injected into an operation that does not own it.
    other = session._apply_protected_task_parameters(
        ToolCall(call_id="3", name="open_application", arguments={"application": "YouTube Music"})
    )
    assert other.arguments["application"] == "YouTube Music"
    assert "application" not in task.protected_parameters


def test_new_task_does_not_share_previous_protected_parameters() -> None:
    old = TaskState(goal="search for Never Say Never", task_id="old")
    old.protect_parameters({"query": "Never Say Never"})
    fresh = TaskState(goal='search for "Python"', task_id="new")
    session = _session(fresh)

    session._capture_task_targets(fresh.goal)
    call = session._apply_protected_task_parameters(
        ToolCall(call_id="1", name="search", arguments={"query": "Python"})
    )
    assert call.arguments["query"] == "Python"
    assert fresh.protected_parameters == {"query": "Python"}
    assert old.protected_parameters == {"query": "Never Say Never"}


def test_email_aliases_are_canonicalized_by_explicit_session_state() -> None:
    task = TaskState(goal="send email", task_id="t1")
    session = _session(task)
    session._record_protected_email_update(
        "stavankalkumbe@gmail.com", "testing david fallback", "exact body text"
    )
    call = session._apply_protected_task_parameters(
        ToolCall(
            call_id="1",
            name="email_send",
            arguments={
                "to": "wrong@example.com",
                "subject": "wrong",
                "body": "wrong body",
            },
        )
    )
    assert call.arguments["to"] == "stavankalkumbe@gmail.com"
    assert task.protected_parameters == {
        "recipient": "stavankalkumbe@gmail.com",
        "subject": "testing david fallback",
        "body": "exact body text",
    }
    assert task.operation_intent == "send_email"


def test_explicit_email_edit_can_replace_locked_body() -> None:
    task = TaskState(goal="send email", task_id="t1")
    task.protect_parameters({
        "recipient": "a@example.com",
        "subject": "old",
        "body": "old body",
    })
    session = _session(task)
    session._record_protected_email_update("a@example.com", "new", "new body")
    assert task.protected_parameters["subject"] == "new"
    assert task.protected_parameters["body"] == "new body"


def test_handoff_preserves_operation_contract() -> None:
    task = TaskState(goal="send email to David", task_id="t1")
    task.operation_intent = "send_email"
    task.protect_parameters({
        "recipient": "05davidalmeida@gmail.com",
        "subject": "Testing",
        "body": "exact body",
    })
    handoff = SessionHandoffBuilder.from_state(
        task,
        last_user_message=task.goal,
        last_assistant_message=None,
        last_tool="email_draft_create",
        last_tool_success=True,
        last_tool_summary="Draft created",
    )
    payload = handoff.to_payload()
    assert payload["operation_intent"] == "send_email"
    assert payload["protected_parameters"] == {
        "recipient": "05davidalmeida@gmail.com",
        "subject": "Testing",
        "body": "exact body",
    }


def test_hidden_david_contract_parameters_pass_runtime_validation() -> None:
    call = ToolCall(
        call_id="d1",
        name="david_fallback",
        arguments={
            "request": "Use the fallback",
            "reason": "native_failure",
            "operation_intent": "send_email",
            "protected_parameters": {"recipient": "a@example.com", "body": "exact"},
        },
    )
    normalized, errors = validate_and_normalize_tool_call(call)
    assert errors == []
    assert normalized is not None
    assert normalized.arguments["protected_parameters"]["body"] == "exact"


def test_fallback_service_uses_primary_operation_instead_of_reclassifying() -> None:
    service = FallbackService.__new__(FallbackService)
    service.intent_service = SimpleNamespace(
        extract_intent=lambda _text: (_ for _ in ()).throw(AssertionError("fallback intent extractor should not run"))
    )

    captured: dict[str, object] = {}

    class FakePlanner:
        def generate_plan(self, intent: Intent, request_text: str | None = None):
            captured["intent"] = intent
            captured["request_text"] = request_text
            return SimpleNamespace(
                actions=[SimpleNamespace(action="navigate")],
                reason="",
                model_dump_json=lambda: "{\"actions\":[{\"action\":\"navigate\"}]}",
                model_dump=lambda: {"actions": [{"action": "navigate"}]},
            )

    class FakeExecutionLoop:
        def run(self, _plan):
            return SimpleNamespace(success=True, results=[], metadata={}, text="done")

    service.planner_service = FakePlanner()
    service.execution_loop = FakeExecutionLoop()
    service._prepared_email = None

    result = service.process(
        "play Never Say Never on YouTube Music",
        approved=False,
        operation_intent="web_search",
        protected_parameters={"query": "Never Say Never", "application": "YouTube Music"},
    )

    intent = captured["intent"]
    assert isinstance(intent, Intent)
    assert intent.goal == "web_search"
    assert intent.entities["query"] == "Never Say Never"
    assert intent.entities["application"] == "YouTube Music"
    assert captured["request_text"] == "play Never Say Never on YouTube Music"
    assert result.success is True
