from __future__ import annotations

from advi.agent.session import AgentSession
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.core.verification import ToolVerifier
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolResult, ToolRunner


def _registry(*actions: str) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(
        Capability(
            name="test_capability",
            description="test",
            supported_actions=set(actions),
            status=CapabilityStatus.AVAILABLE,
            handler=object(),
        )
    )
    return registry


def test_guard_accepts_nullable_strict_pointer_schema() -> None:
    tools = ToolRegistry(_registry("click"))
    guard = ToolGuard(tools)

    assert guard.decision(ToolCall("1", "click", {"target": "Save", "coordinates": None})).allow
    assert guard.decision(ToolCall("2", "click", {"target": None, "coordinates": {"x": 10, "y": 20}})).allow
    assert guard.decision(ToolCall("3", "click", {"target": "Save"})).allow


def test_guard_rejects_invalid_nested_pointer_schema() -> None:
    tools = ToolRegistry(_registry("click"))
    guard = ToolGuard(tools)

    malformed = guard.check(
        ToolCall("1", "click", {"target": "Save", "coordinates": {"x": "10", "y": 20}})
    )
    assert malformed is not None
    assert malformed.success is False

    extra = guard.check(
        ToolCall("2", "click", {"target": "Save", "coordinates": {"x": 10, "y": 20, "z": 30}})
    )
    assert extra is not None
    assert extra.success is False


def test_guard_normalizes_argument_aliases_before_schema_validation() -> None:
    tools = ToolRegistry(_registry("open_application"))
    guard = ToolGuard(tools)

    assert guard.decision(ToolCall("1", "open_application", {"app": "notepad"})).allow


def test_engine_contract_rejects_unknown_arguments_without_guard() -> None:
    from advi.core.execution_engine import ExecutionEngine

    class Handler:
        def execute(self, call, *, approval_granted=False):
            raise AssertionError("handler must not run")

    registry = _registry("open_application")
    registry.get("test_capability").handler = Handler()
    engine = ExecutionEngine(registry=registry)

    result = engine.execute_tool_call(
        ToolCall("1", "open_application", {"application": "notepad", "unexpected": True})
    )
    assert result.success is False
    assert "unknown parameter" in (result.error or "").lower()


def test_email_send_contract_rejects_incomplete_message_before_confirmation() -> None:
    tools = ToolRegistry(_registry("email_send"))
    guard = ToolGuard(tools)

    decision = guard.decision(ToolCall("1", "email_send", {}))
    assert decision.allow is False
    assert decision.requires_confirmation is False
    assert "email send requires" in decision.reason.lower()


def test_email_send_contract_accepts_draft_or_complete_message() -> None:
    tools = ToolRegistry(_registry("email_send"))
    guard = ToolGuard(tools)

    draft = guard.decision(ToolCall("1", "email_send", {"draft_id": "d1"}))
    message = guard.decision(ToolCall("2", "email_send", {"to": "a@b.com", "subject": "S", "body": "B"}))
    assert draft.requires_confirmation is True
    assert message.requires_confirmation is True


def test_confirmation_detection_does_not_misclassify_invalid_confirmation_tool() -> None:
    class FakeProvider(LLMProvider):
        @property
        def name(self):
            return "fake"

        @property
        def model(self):
            return "fake-model"

        def chat(self, messages, *, purpose="chat"):
            return LLMResponse(text="Invalid", provider=self.name, model=self.model)

        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            return LLMResponse(
                text="",
                provider=self.name,
                model=self.model,
                tool_calls=(LLMToolCall("1", "email_send", {}),),
            )

    class Engine:
        def execute_tool_call(self, call, **kwargs):
            raise AssertionError("invalid call must not execute")

    tools = ToolRegistry(_registry("email_send"))
    guard = ToolGuard(tools)
    runner = ToolRunner(Engine(), guard, tools)
    session = AgentSession([FakeProvider()], tool_registry=tools, tool_runner=runner, tool_guard=guard)

    response = session.respond("Send an email")
    assert session.pending_confirmation is None
    assert "confirmation" not in response.text.lower()
    assert "couldn't" not in response.text.lower() or "invalid" in response.text.lower()


def test_dynamic_confirmation_preserves_pre_confirmation_results_in_handoff() -> None:
    class FakeProvider(LLMProvider):
        @property
        def name(self):
            return "fake"

        @property
        def model(self):
            return "fake-model"

        def chat(self, messages, *, purpose="chat"):
            return LLMResponse(text="done", provider=self.name, model=self.model)

        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            return LLMResponse(text="", provider=self.name, model=self.model)

    class Engine:
        def __init__(self):
            self.calls = []

        def execute_tool_call(self, call, **kwargs):
            self.calls.append(call.name)
            if len(self.calls) == 1:
                return ToolResult(call_id=call.call_id, name=call.name, success=True, output="first", verified=True)
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                requires_confirmation=True,
                confirmation_reason="dynamic_gate",
                error="confirmation required",
            )

    registry = _registry("open_application")
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    engine = Engine()
    runner = ToolRunner(engine, guard, tools)
    provider = FakeProvider()
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=guard)

    # Bypass the pre-scan only to exercise the executor-generated confirmation path.
    session._requires_confirmation = lambda _call: False
    session._continue_execution(
        LLMResponse(
            text="",
            provider="fake",
            model="fake-model",
            tool_calls=(
                LLMToolCall("1", "open_application", {"application": "a"}),
                LLMToolCall("2", "open_application", {"application": "b"}),
            ),
        ),
        [],
    )

    assert session.pending_confirmation is not None
    exchange = session._recent_tool_exchanges[-1]
    assert [item["call_id"] for item in exchange.calls] == ["1", "2"]
    assert [item["call_id"] for item in exchange.results] == ["1", "2"]


def test_save_file_verification_checks_intended_content(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("wrong", encoding="utf-8")
    result = ToolResult(
        call_id="1",
        name="save_file",
        success=True,
        output=str(path),
        metadata={"resolved_path": str(path)},
    )
    verified = ToolVerifier().verify(
        ToolCall("1", "save_file", {"path": str(path), "content": "expected"}),
        result,
    )
    assert verified.success is False
    assert verified.verified is False
    assert verified.verification_status.value == "failed"


def test_save_file_verification_accepts_exact_content(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("expected", encoding="utf-8")
    result = ToolResult(
        call_id="1",
        name="save_file",
        success=True,
        output=str(path),
        metadata={"resolved_path": str(path)},
    )
    verified = ToolVerifier().verify(
        ToolCall("1", "save_file", {"path": str(path), "content": "expected"}),
        result,
    )
    assert verified.success is True
    assert verified.verified is True
    assert verified.verification_status.value == "verified"


def test_replace_file_verification_allows_remaining_duplicates_after_one_replacement(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("old old", encoding="utf-8")
    result = ToolResult(
        call_id="1",
        name="replace_file_text",
        success=True,
        output=str(path),
        metadata={
            "resolved_path": str(path),
            "occurrences_before": 2,
        },
    )
    path.write_text("new old", encoding="utf-8")
    verified = ToolVerifier().verify(
        ToolCall("1", "replace_file_text", {"path": str(path), "old_text": "old", "new_text": "new"}),
        result,
    )
    assert verified.success is True
    assert verified.verified is True
    assert verified.verification_status.value == "verified"


def test_namespaced_provider_tool_name_resolves_to_registered_action():
    from advi.tools import ToolRegistry
    from advi.capabilities.registry import CapabilityRegistry, Capability, CapabilityStatus

    registry = CapabilityRegistry()
    registry.register(
        Capability(
            "memory",
            "memory",
            {"memory_retrieval"},
            CapabilityStatus.AVAILABLE,
            handler=object(),
        )
    )
    tools = ToolRegistry(registry)

    assert tools.runtime_name("default_api:memory_search") == "memory_retrieval"
    assert tools.get("default_api:memory_search") is not None



def test_filename_search_no_match_is_a_verified_empty_search(tmp_path):
    from advi.capabilities.files.executor import FileCapability
    cap = FileCapability()
    result = cap._file_search_result(
        ToolCall("1", "find_file", {"name": "does-not-exist.txt", "scope": "user"}),
        "does-not-exist.txt", "user", [], 0.0, timed_out=False,
    )
    assert result.success is True
    assert result.verified is True
    assert result.output == []

def test_routine_read_and_wait_results_are_verified():
    verifier = ToolVerifier()
    read = verifier.verify(ToolCall("1", "email_read", {"query": "is:inbox"}), ToolResult("1", "email_read", True, output=[]))
    wait = verifier.verify(ToolCall("2", "wait", {"duration": 0.1}), ToolResult("2", "wait", True))
    assert read.verified is True
    assert wait.verified is True
