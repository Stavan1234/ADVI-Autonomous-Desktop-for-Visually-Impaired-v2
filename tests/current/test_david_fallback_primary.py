from __future__ import annotations

from types import SimpleNamespace

from advi.agent import AgentSession
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.fallback.capability import DavidFallbackCapability
from advi.fallback.david_adapter import DavidAdapter
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
from advi.core.execution_engine import ExecutionEngine


class FakeProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "fake-model"

    def chat(self, messages, *, purpose="chat"):
        return self.responses.pop(0)


class FakeDavidService:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def process(self, user_input, *, approved=False):
        self.calls.append((user_input, approved))
        return self.outcomes.pop(0)


def make_session(service, responses):
    registry = CapabilityRegistry()
    david = DavidFallbackCapability(DavidAdapter(service))
    registry.register(
        Capability(
            name="david_fallback",
            description="David fallback",
            supported_actions=david.SUPPORTED_ACTIONS,
            status=CapabilityStatus.AVAILABLE,
            handler=david,
        )
    )
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    engine = ExecutionEngine(registry=registry)
    runner = ToolRunner(engine, guard, tools)
    return AgentSession(
        [FakeProvider(responses)],
        tool_registry=tools,
        tool_runner=runner,
        tool_guard=guard,
    )


def test_david_fallback_is_advertised_as_a_native_tool():
    service = FakeDavidService([])
    session = make_session(service, [LLMResponse(text="done", provider="fake", model="m")])
    spec = session.tools.get("david_fallback")
    assert spec is not None
    assert spec.runtime_action == "david_fallback"


def test_unknown_task_can_delegate_to_david():
    service = FakeDavidService([
        SimpleNamespace(success=True, text="David handled the task.", results=[SimpleNamespace(success=True, action="click", data="ok", error=None)], metadata={})
    ])
    session = make_session(service, [
        LLMResponse(text="", provider="fake", model="m", tool_calls=(
            LLMToolCall("d1", "david_fallback", {"request": "Open the weird legacy application and arrange its windows", "reason": "unknown_task"}),
        )),
        LLMResponse(text="I used David to handle it.", provider="fake", model="m"),
    ])

    response = session.respond("Open the weird legacy application and arrange its windows")
    assert response.text == "I used David to handle it."
    assert service.calls == [("Open the weird legacy application and arrange its windows", False)]
    assert session.task is not None
    assert session.task.status.value == "completed"


def test_gmail_failure_can_be_delegated_to_david_without_replaying_the_native_send():
    service = FakeDavidService([
        SimpleNamespace(success=True, text="Sent through Gmail in Chrome.", results=[SimpleNamespace(success=True, action="submit", data="ok", error=None)], metadata={})
    ])
    request = "Send an email to john@example.com saying the project is ready."
    session = make_session(service, [
        LLMResponse(text="", provider="fake", model="m", tool_calls=(
            LLMToolCall("d1", "david_fallback", {"request": request, "reason": "gmail_capability_unavailable"}),
        )),
        LLMResponse(text="I sent it using the browser fallback.", provider="fake", model="m"),
    ])

    response = session.respond(request)
    assert response.text == "I sent it using the browser fallback."
    assert service.calls == [(request, False)]


def test_david_confirmation_is_preserved_and_approved_once():
    service = FakeDavidService([
        SimpleNamespace(success=False, text="", results=[], metadata={"awaiting_confirmation": True, "goal": "shutdown the computer"}),
        SimpleNamespace(success=True, text="Shutdown completed.", results=[SimpleNamespace(success=True, action="shutdown_system", data="ok", error=None)], metadata={}),
    ])
    session = make_session(service, [
        LLMResponse(text="", provider="fake", model="m", tool_calls=(
            LLMToolCall("d1", "david_fallback", {"request": "Shut down the computer", "reason": "unknown_task"}),
        )),
        LLMResponse(text="The computer has been shut down.", provider="fake", model="m"),
    ])

    first = session.respond("Shut down the computer")
    assert session.pending_confirmation is not None
    assert "confirmation" in first.text.lower()

    second = session.respond("yes")
    assert second.text == "The computer has been shut down."
    assert service.calls == [
        ("Shut down the computer", False),
        ("Shut down the computer", True),
    ]


def test_david_failure_is_not_reported_as_success():
    service = FakeDavidService([
        SimpleNamespace(success=False, text="", results=[], metadata={"reason": "browser unavailable"})
    ])
    session = make_session(service, [
        LLMResponse(text="", provider="fake", model="m", tool_calls=(
            LLMToolCall("d1", "david_fallback", {"request": "Do something impossible", "reason": "unknown_task"}),
        )),
        LLMResponse(text="David could not complete the request.", provider="fake", model="m"),
    ])

    response = session.respond("Do something impossible")
    assert session.task is not None
    assert session.task.status.value == "failed"
    assert "browser unavailable" in (response.text.lower() + str(session.task.context).lower())


def test_david_email_planner_has_deterministic_execution_plan():
    from advi.fallback.service import FallbackService
    from advi.fallback.execution_loop import ExecutionLoopResult

    provider = FakeProvider([
        LLMResponse(
            text='{"goal":"send_email","entities":{"recipient_email":"person@example.com","subject":"Test","content":"Body"},"constraints":[],"confirmation_required":true,"missing_information":[]}',
            provider="fake",
            model="fake-model",
        ),
    ])
    service = FallbackService(
        provider=provider,
        intent_prompt_path="src/advi/fallback/prompts/intent_prompt.txt",
        planner_prompt_path="src/advi/fallback/prompts/planner_prompt.txt",
    )

    def fake_prepare_email_draft(*, recipient, subject, body):
        assert recipient == "person@example.com"
        assert subject == "Test"
        assert body == "Body"
        return ExecutionLoopResult(success=True, results=[], metadata={"stage":"draft_prepared","awaiting_send_confirmation":True}, text="Draft created and ready to send to person@example.com. Should I send it, sir?")

    service.execution_loop.prepare_email_draft = fake_prepare_email_draft
    result = service.process("Send an email to person@example.com with subject Test and body Body", approved=True)
    assert result.success is True
    assert result.metadata["stage"] == "draft_prepared"
    assert result.metadata["awaiting_send_confirmation"] is True
    assert "should i send" in result.text.lower()


def test_gmail_fallback_recovery_marks_native_failure_recovered():
    service = FakeDavidService([
        SimpleNamespace(success=True, text="Sent through Gmail in Chrome.", results=[SimpleNamespace(success=True, action="click", data="Send button", error=None)], metadata={})
    ])
    session = make_session(service, [LLMResponse(text="done", provider="fake", model="fake-model")])

    from advi.agent.contracts import TaskState
    session.task = TaskState(goal="send email", task_id="task-1")
    session.task.record_tool_outcome(
        name="email_send", success=False, verified=False,
        verification_status="failed", summary="Gmail OAuth unavailable",
    )
    session._approved_gmail_fallback = {
        "operation": "email_send",
        "to": "person@example.com",
        "subject": "Test",
        "body": "Body",
        "approved_continuation": True,
    }

    result = session._run_tool(
        LLMToolCall(
            "d1",
            "david_fallback",
            {"request": "Send the email using David", "reason": "gmail_capability_unavailable"},
        ),
        approved=True,
    )
    assert result.success is True
    assert session.task.recovered_tools == ["email_send"]
    session._finalize_task_status()
    assert session.task.status.value == "completed"


def test_david_chrome_launch_polls_for_cdp_instead_of_fixed_sleep(monkeypatch, tmp_path):
    from pathlib import Path
    from advi.fallback.executor import FallbackExecutor
    from advi.fallback.execution_context import ExecutionContext
    from advi.fallback.legacy_action_plan import Action

    localappdata = tmp_path / "localappdata"
    localappdata.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    original_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: True if str(self).lower().endswith("chrome.exe") else original_exists(self),
    )

    context = ExecutionContext()
    executor = FallbackExecutor(context=context)

    class FakeProc:
        pid = 4242

    class FakeCDP:
        def __init__(self):
            self.calls = 0

        def list_tabs(self):
            self.calls += 1
            if self.calls < 3:
                return []
            return [{"id": "owned-tab", "url": "about:blank", "title": ""}]

        def create_new_tab(self, url):
            return {"id": "dedicated-tab", "url": url, "title": ""}

    fake_cdp = FakeCDP()
    executor.cdp_browser = fake_cdp
    monkeypatch.setattr(executor, "_list_chrome_window_hwnds", lambda: set())
    monkeypatch.setattr("advi.fallback.executor.subprocess.Popen", lambda *args, **kwargs: FakeProc())

    result = executor._execute_open_application(
        Action(
            action="open_application",
            focus="Chrome",
            parameters={"application": "Chrome"},
        )
    )

    assert result.success is True
    assert context.chrome_process_id == 4242
    assert context.target_tab_id == "dedicated-tab"
    assert fake_cdp.calls >= 3


def test_david_submit_uses_owned_chrome_target():
    from advi.fallback.executor import FallbackExecutor
    from advi.fallback.execution_context import ExecutionContext
    from advi.fallback.legacy_action_plan import Action

    context = ExecutionContext()
    context.set_target_tab("tab-1", url="https://mail.google.com", title="Gmail")
    executor = FallbackExecutor(context=context)

    class FakeCDP:
        def click_relevant_result(self, tab_id, target):
            assert tab_id == "tab-1"
            assert target == "Send button"
            return True

    executor.cdp_browser = FakeCDP()
    result = executor._execute_submit(
        Action(action="submit", focus="Chrome", parameters={"target": "Send button"})
    )

    assert result.success is True
    assert result.data == "Send button"


def test_exact_gmail_fallback_confirmation_continuation_completes_task():
    """Regression for the Windows log: native Gmail failure -> David -> confirm -> success."""
    class RecordingDavidService:
        def __init__(self):
            self.calls = []

        def process(self, user_input, *, approved=False):
            self.calls.append((user_input, approved))
            if not approved:
                return SimpleNamespace(
                    success=False,
                    text="",
                    results=[],
                    metadata={
                        "awaiting_confirmation": True,
                        "goal": "send_email",
                        "entities": {
                            "recipient_email": "05davidalmeida@gmail.com",
                            "subject": "Test",
                            "content": "This is a testing email.",
                        },
                    },
                )
            return SimpleNamespace(
                success=True,
                text="Sent through Gmail in Chrome.",
                results=[SimpleNamespace(success=True, action="click", data="Send button", error=None)],
                metadata={"verified": True},
            )

    service = RecordingDavidService()
    registry = CapabilityRegistry()
    david = DavidFallbackCapability(DavidAdapter(service))
    registry.register(Capability("david_fallback", "David", {"david_fallback"}, CapabilityStatus.AVAILABLE, handler=david))
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    runner = ToolRunner(ExecutionEngine(registry=registry), guard, tools)
    provider = FakeProvider([
        LLMResponse(
            text="", provider="fake", model="fake-model",
            tool_calls=(LLMToolCall("d1", "david_fallback", {
                "request": "Send an email to 05davidalmeida@gmail.com saying This is a testing email.",
                "reason": "gmail_native_failure",
            }),),
        ),
        LLMResponse(text="The email was sent through David.", provider="fake", model="fake-model"),
    ])
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=guard)

    first = session.respond("05davidalmeida@gmail.com send using david fallback")
    assert "confirmation" in first.text.lower()

    second = session.respond("yes do it")
    assert second.text == "The email was sent through David."
    assert service.calls[0][1] is False
    assert service.calls[1][1] is True
    assert session.task is not None
    assert session.task.status.value == "completed"


class FakeEmailExecutionLoop:
    def __init__(self):
        self.prepare_calls = []
        self.send_calls = []

    def prepare_email_draft(self, *, recipient, subject, body):
        from advi.fallback.execution_loop import ExecutionLoopResult
        self.prepare_calls.append((recipient, subject, body))
        return ExecutionLoopResult(
            success=True,
            results=[],
            metadata={
                "stage": "draft_prepared",
                "awaiting_send_confirmation": True,
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "request": "Send the email through David",
            },
            text=f"Draft created and ready to send to {recipient}.",
        )

    def send_prepared_email(self, *, recipient, subject, body):
        from advi.fallback.execution_loop import ExecutionLoopResult
        self.send_calls.append((recipient, subject, body))
        return ExecutionLoopResult(
            success=True,
            results=[],
            metadata={"stage": "sent", "send_verified": True},
            text="The email was sent successfully through the Gmail interface.",
        )


def test_david_email_requires_only_one_confirmation_after_draft_preparation():
    from advi.fallback.service import FallbackService

    service = FallbackService(
        provider=FakeProvider([]),
        intent_prompt_path="src/advi/fallback/prompts/intent_prompt.txt",
        planner_prompt_path="src/advi/fallback/prompts/planner_prompt.txt",
    )
    loop = FakeEmailExecutionLoop()
    service.execution_loop = loop
    result = service.process(
        "Send an email to person@example.com with subject Test and body Body using David fallback",
        approved=False,
        operation_intent="send_email",
        protected_parameters={"recipient": "person@example.com", "subject": "Test", "body": "Body"},
    )
    assert result.success is True
    assert result.metadata["awaiting_send_confirmation"] is True
    assert loop.prepare_calls == [("person@example.com", "Test", "Body")]


def test_david_send_prepared_trace_stage_does_not_collide_with_trace_stage():
    from advi.fallback.service import FallbackService
    from advi.fallback.execution_loop import ExecutionLoopResult

    service = FallbackService(
        provider=FakeProvider([]),
        intent_prompt_path="src/advi/fallback/prompts/intent_prompt.txt",
        planner_prompt_path="src/advi/fallback/prompts/planner_prompt.txt",
    )

    class Loop:
        def send_prepared_email(self, **kwargs):
            return ExecutionLoopResult(True, [], {"stage": "sent"}, text="sent")

    service.execution_loop = Loop()
    result = service.process(
        "send prepared",
        approved=True,
        stage="send_prepared",
        protected_parameters={"recipient": "person@example.com", "subject": "Test", "body": "Body"},
    )
    assert result.success is True


def test_david_confirmation_result_is_intercepted_before_llm_continuation():
    service = FakeDavidService([
        SimpleNamespace(success=False, text="", results=[], metadata={"awaiting_confirmation": True, "goal": "shutdown the computer"}),
        SimpleNamespace(success=True, text="Shutdown completed.", results=[SimpleNamespace(success=True, action="shutdown_system", data="ok", error=None)], metadata={}),
    ])
    session = make_session(service, [])
    first_call = LLMToolCall("d1", "david_fallback", {"request": "Shut down the computer", "reason": "unknown_task"})
    response = session._continue_execution(
        LLMResponse(text="", provider="fake", model="m", tool_calls=(first_call,)),
        [],
    )
    assert "confirmation" in response.text.lower()
    assert session.pending_confirmation is not None
    assert session.task.status.value == "awaiting_confirmation"


def test_explicit_david_email_is_not_switched_to_native_after_failure():
    from advi.agent.contracts import ExecutionRoute
    service = FakeDavidService([
        SimpleNamespace(success=False, text="", results=[], metadata={"reason": "David unavailable"}),
    ])
    session = make_session(service, [LLMResponse(text="", provider="fake", model="m", tool_calls=())])
    session.task = __import__('advi.agent.contracts', fromlist=['TaskState']).TaskState(goal="use David to send an email", task_id="t7")
    session.task.set_execution_route(ExecutionRoute.DAVID, explicit=True)
    result = session._run_tool(
        LLMToolCall("d1", "david_fallback", {"request": "use David to send the email", "reason": "explicit_user_requested_fallback"}),
        approved=True,
    )
    assert result.success is False
    assert session.task.execution_route == ExecutionRoute.DAVID.value
    native = session._run_tool(
        LLMToolCall("e1", "email_send", {"to": "person@example.com", "subject": "Test", "body": "Body"}),
        approved=True,
    )
    assert native.success is False
    assert native.verification_details["reason"] == "execution_route_conflict"


def test_explicit_david_email_flow_uses_one_user_confirmation():
    from advi.fallback.execution_loop import ExecutionLoopResult

    class DavidEmailService:
        def __init__(self):
            self.calls = []

        def process(self, user_input, *, approved=False, stage=None, operation_intent=None, protected_parameters=None):
            self.calls.append({"approved": approved, "stage": stage, "operation_intent": operation_intent})
            if stage == "send_prepared":
                return SimpleNamespace(
                    success=True,
                    text="The email was sent successfully through the Gmail interface.",
                    results=[],
                    metadata={"stage": "sent", "send_verified": True},
                )
            return SimpleNamespace(
                success=True,
                text="Draft created and ready to send to person@example.com.",
                results=[],
                metadata={
                    "stage": "draft_prepared",
                    "awaiting_send_confirmation": True,
                    "recipient": "person@example.com",
                    "subject": "Test",
                    "body": "Body",
                    "request": "Send the email using David",
                },
            )

    service = DavidEmailService()
    session = make_session(
        service,
        [
            LLMResponse(
                text="",
                provider="fake",
                model="m",
                tool_calls=(
                    LLMToolCall(
                        "d1",
                        "david_fallback",
                        {
                            "request": "Send the email using David",
                            "reason": "explicit_user_requested_fallback",
                            "operation_intent": "send_email",
                            "protected_parameters": {
                                "recipient": "person@example.com",
                                "subject": "Test",
                                "body": "Body",
                            },
                        },
                    ),
                ),
            ),
        ],
    )

    first = session.respond("send email to person@example.com saying Body using David fallback")
    assert "ready to send" in first.text.lower()
    assert session._pending_david_email_send is not None
    assert session.pending_confirmation is None

    second = session.respond("yes")
    assert "sent successfully" in second.text.lower()
    assert session._pending_david_email_send is None
    assert session.pending_confirmation is None
    assert [item["stage"] for item in service.calls] == [None, "send_prepared"]
    assert session.task is not None
    assert session.task.execution_route == "david"
