from __future__ import annotations

import json

import pytest

from advi.agent.provider_session import ProviderSession, ProviderSessionManager
from advi.agent.session_handoff import SessionHandoff, ToolExchange
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall, ProviderHealthState
from advi.providers.errors import LLMRateLimitError, LLMTimeoutError
from advi.providers.gemini import GeminiProvider


class NativeStub:
    def __init__(self, provider_name: str = "stub") -> None:
        self.provider_name = provider_name
        self.calls: list[tuple[str, object]] = []
        self.closed = False
        self.counter = 0

    def send_user(self, content: str) -> LLMResponse:
        self.calls.append(("user", content))
        self.counter += 1
        return LLMResponse(text=f"reply-{self.counter}", provider=self.provider_name, model="model")

    def send_tool_results(self, results):
        self.calls.append(("tool", results))
        self.counter += 1
        return LLMResponse(text=f"tool-reply-{self.counter}", provider=self.provider_name, model="model")

    def close(self) -> None:
        self.closed = True


class NativeProvider(LLMProvider):
    def __init__(self, name: str, *, quota_group: str | None = None, native: NativeStub | None = None):
        self._name = name
        self._quota_group = quota_group
        self.native = native or NativeStub(name)
        self.create_calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._name + "-model"

    @property
    def quota_group(self) -> str | None:
        return self._quota_group

    def chat(self, messages, *, purpose="chat") -> LLMResponse:
        return LLMResponse(text="stateless", provider=self.name, model=self.model)

    def create_native_session(self, *, system_instruction, tools, history=None):
        self.create_calls += 1
        return self.native


class FailingProvider(NativeProvider):
    def __init__(self, name: str, outcome: Exception, **kwargs):
        super().__init__(name, **kwargs)
        self.outcome = outcome

    def create_native_session(self, *, system_instruction, tools, history=None):
        self.create_calls += 1
        return self.native


class FailingNative(NativeStub):
    def __init__(self, provider_name: str, outcome: Exception):
        super().__init__(provider_name)
        self.outcome = outcome
        self.failures = 0

    def send_user(self, content: str) -> LLMResponse:
        self.failures += 1
        raise self.outcome


def test_provider_session_uses_native_and_closes_it():
    native = NativeStub("native")
    provider = NativeProvider("native", native=native)
    session = ProviderSession.create(provider, system_instruction="sys", tools=[])

    assert session.uses_native_state is True
    assert session.send_user("hello").text == "reply-1"
    session.close()
    assert native.closed is True


def test_provider_session_keeps_stateless_fallback_without_native():
    class StatelessOnly(NativeProvider):
        def create_native_session(self, *, system_instruction, tools, history=None):
            return None

    provider = StatelessOnly("groq")
    session = ProviderSession.create(provider, system_instruction="sys", tools=[])
    assert session.uses_native_state is False
    session.send_user("hello")
    assert session.messages[0]["role"] == "system"
    assert session.messages[-1]["role"] == "user"


def test_unexpected_native_creation_error_is_not_silently_hidden():
    class BrokenProvider(NativeProvider):
        def create_native_session(self, *, system_instruction, tools, history=None):
            raise RuntimeError("SDK programming bug")

    with pytest.raises(RuntimeError, match="SDK programming bug"):
        ProviderSession.create(BrokenProvider("broken"), system_instruction="sys", tools=[])


def test_gemini_interactions_session_uses_server_state_and_replays_tool_result_structured():
    class FakeInteractions:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                return type("Interaction", (), {
                    "id": "i-1",
                    "output_text": "",
                    "steps": [type("Step", (), {
                        "type": "function_call",
                        "id": "fc-1",
                        "name": "read_file",
                        "arguments": {"path": "x.txt"},
                    })()],
                })()
            return type("Interaction", (), {
                "id": "i-2",
                "output_text": "done",
                "steps": [],
                "usage": None,
            })()

    class FakeClient:
        def __init__(self):
            self.interactions = FakeInteractions()

    provider = GeminiProvider(api_key="not-used", model="gemini-test", client=FakeClient())
    session = ProviderSession.create(
        provider,
        system_instruction="You are ADVI.",
        tools=[{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }],
    )

    first = session.send_user("read x.txt")
    assert first.tool_calls[0].call_id == "fc-1"
    assert session.send_tool_results([
        (first.tool_calls[0], {"ok": True, "content": "hello"})
    ]).text == "done"

    requests = provider._client.interactions.requests
    assert requests[0]["input"] == "read x.txt"
    assert requests[1]["previous_interaction_id"] == "i-1"
    assert requests[1]["input"][0]["type"] == "function_result"
    assert requests[1]["input"][0]["call_id"] == "fc-1"
    assert json.loads(requests[1]["input"][0]["result"][0]["text"])["content"] == "hello"
    assert requests[0]["tools"][0]["name"] == "read_file"
    assert "function" not in requests[0]["tools"][0]


def test_provider_switch_resumes_handoff_without_sending_the_current_user_twice():
    old_native = FailingNative("first", LLMTimeoutError("timeout"))
    first = NativeProvider("first", native=old_native)
    second_native = NativeStub("second")
    second = NativeProvider("second", native=second_native)
    manager = ProviderSessionManager([first, second], cooldown_seconds=60)
    handoff = SessionHandoff(
        last_user_message="hello",
        recent_conversation=(
            {"role": "user", "content": "hello"},
        ),
    )

    result = manager.send_user("hello", system_instruction="sys", tools=[], handoff=handoff)

    assert result.provider == "second"
    assert old_native.closed is True
    assert [kind for kind, _ in second_native.calls] == ["user"]
    assert second_native.calls[0][1] != "hello"
    assert "Continue the existing task" in str(second_native.calls[0][1])
    assert manager.switch_count == 1


def test_provider_daily_quota_blocks_sibling_in_same_group(monkeypatch):
    first_native = FailingNative("g1", LLMRateLimitError("daily"))
    err = first_native.outcome
    setattr(err, "quota_scope", "project_daily")
    first = NativeProvider("gemini-1", quota_group="project-a", native=first_native)
    sibling = NativeProvider("gemini-2", quota_group="project-a")
    different = NativeProvider("groq", quota_group="groq")

    monkeypatch.setattr("advi.agent.provider_session.time.monotonic", lambda: 1000.0)
    manager = ProviderSessionManager([first, sibling, different], cooldown_seconds=60, health_state=ProviderHealthState())
    monkeypatch.setattr("advi.agent.provider_session.datetime", __import__("datetime").datetime)

    result = manager.send_user("hello", system_instruction="sys", tools=[])
    assert result.provider == "groq"
    assert sibling.create_calls == 0
    assert different.create_calls == 1


def test_manager_does_not_retry_when_every_provider_is_blocked():
    first = NativeProvider("first", quota_group="project-a")
    second = NativeProvider("second", quota_group="project-a")
    state = ProviderHealthState()
    state.quota_exhausted_until["project-a"] = __import__("time").monotonic() + 3600.0
    manager = ProviderSessionManager([first, second], health_state=state)

    with pytest.raises(LLMRateLimitError, match="quota-exhausted"):
        manager.start(system_instruction="sys", tools=[])

    assert first.create_calls == 0
    assert second.create_calls == 0


def test_stateless_handoff_rebuilds_native_tool_messages_without_text_blob():
    class StatelessProvider(NativeProvider):
        def create_native_session(self, *, system_instruction, tools, history=None):
            self.handoff_history = history
            return None

    provider = StatelessProvider("groq")
    handoff = SessionHandoff(
        last_user_message="open the file",
        recent_tool_exchanges=(
            ToolExchange.from_calls_and_results(
                [{"call_id": "call-1", "name": "read_file", "arguments": {"path": "a.txt"}}],
                [{"call_id": "call-1", "name": "read_file", "result": {"ok": True, "output": "hello"}}],
            ),
        ),
    )
    session = ProviderSession.create(provider, system_instruction="sys", tools=[], handoff=handoff)
    assert session.messages[0] == {"role": "system", "content": "sys"}
    assert session.messages[1] == {"role": "user", "content": "open the file"}
    assert session.messages[2]["role"] == "assistant"
    assert session.messages[2]["tool_calls"][0]["id"] == "call-1"
    assert session.messages[3]["role"] == "tool"
    assert session.messages[3]["tool_call_id"] == "call-1"
    assert "TOOL RESULTS FROM PRIOR PROVIDER" not in json.dumps(session.messages)


def test_manager_tool_result_failover_continues_from_structured_handoff():
    class SequencedStatelessProvider(NativeProvider):
        def __init__(self, name, *, fail_first_call=False):
            super().__init__(name)
            self.calls = 0
            self.fail_first_call = fail_first_call

        def create_native_session(self, *, system_instruction, tools, history=None):
            return None

        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            self.calls += 1
            if self.fail_first_call and self.calls == 1:
                raise LLMTimeoutError("timeout")
            return LLMResponse(text="continued", provider=self.name, model=self.model)

    first = SequencedStatelessProvider("first", fail_first_call=True)
    second = SequencedStatelessProvider("second")
    manager = ProviderSessionManager([first, second], cooldown_seconds=60)
    # Start both through the stateless path, then fail the first tool-result submission.
    manager.start(system_instruction="sys", tools=[])
    handoff = SessionHandoff(
        last_user_message="open a file",
        recent_tool_exchanges=(
            ToolExchange.from_calls_and_results(
                [{"call_id": "call-9", "name": "read_file", "arguments": {"path": "a.txt"}}],
                [{"call_id": "call-9", "name": "read_file", "result": {"ok": True, "output": "hello"}}],
            ),
        ),
    )
    result = manager.send_tool_results(
        [
            (
                LLMToolCall(call_id="call-9", name="read_file", arguments={"path": "a.txt"}),
                {"ok": True, "output": "hello"},
            )
        ],
        system_instruction="sys",
        tools=[],
        handoff=handoff,
    )
    assert result.provider == "second"
    assert result.text == "continued"
    assert second.calls == 1
    assert manager.session is not None
    assert manager.session.messages[1]["role"] == "user"
    assert manager.session.messages[2]["role"] == "assistant"
    assert manager.session.messages[3]["role"] == "tool"


def test_gemini_handoff_preserves_structured_tool_exchange_data():
    class FakeInteractions:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            return type("Interaction", (), {
                "id": "i-handoff",
                "output_text": "continued",
                "steps": [],
            })()

    class FakeClient:
        def __init__(self):
            self.interactions = FakeInteractions()

    provider = GeminiProvider(api_key="not-used", model="gemini-test", client=FakeClient())
    handoff = SessionHandoff(
        last_user_message="open the file",
        recent_tool_exchanges=(
            ToolExchange.from_calls_and_results(
                [{"call_id": "call-2", "name": "read_file", "arguments": {"path": "a.txt"}}],
                [{"call_id": "call-2", "name": "read_file", "result": {"ok": True, "output": "hello"}}],
            ),
        ),
    )
    session = ProviderSession.create(provider, system_instruction="sys", tools=[], handoff=handoff)
    result = session.continue_from_handoff()
    assert result.text == "continued"
    request = provider._client.interactions.requests[0]
    assert isinstance(request["input"], list)
    handoff_input = request["input"][0]
    assert handoff_input["type"] == "user_input"
    assert "recent_tool_exchanges" not in handoff_input["content"][0]["text"]
    assert '"tool_call_id":"call-2"' in handoff_input["content"][0]["text"]
    assert '"name":"read_file"' in handoff_input["content"][0]["text"]


def test_provider_handoff_preserves_bounded_recent_conversation_for_new_turn_failover():
    class StatelessProvider(NativeProvider):
        def create_native_session(self, *, system_instruction, tools, history=None):
            self.handoff_history = history
            return None

    provider = StatelessProvider("groq")
    handoff = SessionHandoff(
        last_user_message="current request",
        last_assistant_message="previous answer",
        recent_conversation=(
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "current request"},
        ),
    )

    session = ProviderSession.create(provider, system_instruction="sys", tools=[], handoff=handoff)

    assert session.messages[1:] == [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "previous answer"},
        {"role": "user", "content": "current request"},
    ]


def test_public_provider_package_does_not_eagerly_load_legacy_router():
    import subprocess, sys

    code = "import sys; import advi.providers; print('advi.providers.router' in sys.modules)"
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "src")
    completed = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, env=env)
    assert completed.stdout.strip() == "False"


def test_stateless_provider_history_is_bounded_by_complete_conversation_blocks():
    from advi.agent.provider_session import ProviderSession

    session = ProviderSession(
        provider=object(),
        system_instruction="system",
        tools=[],
        max_history_tokens=1024,
    )
    session.messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request " + "x" * 5000},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "old", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old", "name": "read_file", "content": "old result " + "x" * 5000},
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "current answer"},
    ]

    session._trim_stateless_history()
    payload = session.messages
    assert payload[0]["role"] == "system"
    assert payload[-2]["role"] == "user"
    assert payload[-1]["role"] == "assistant"
    assert not any(m.get("content", "").startswith("old request") for m in payload if isinstance(m.get("content"), str))


def test_stateless_history_keeps_tool_call_and_tool_result_together_when_budget_allows():
    from advi.agent.provider_session import ProviderSession

    session = ProviderSession(provider=object(), system_instruction="system", tools=[], max_history_tokens=1000)
    session.messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]

    session._trim_stateless_history()
    roles = [item["role"] for item in session.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]


def test_gemini_provider_configures_bounded_timeout_and_no_retries():
    pytest = __import__("pytest")
    pytest.importorskip("google.genai")
    from advi.providers.gemini import GeminiProvider
    from google import genai

    provider = GeminiProvider(api_key="fake-key", model="gemini-test", request_timeout_seconds=14.0)
    assert isinstance(provider._client, genai.Client)
    http_opts = provider._client._api_client._http_options
    assert http_opts.timeout == 14000
    assert http_opts.retry_options is not None
    assert http_opts.retry_options.attempts == 1


def test_provider_session_turn_deadline_enforced():
    import time
    from advi.agent.provider_session import ProviderSession
    from advi.providers.base import LLMProvider, LLMResponse
    from advi.providers.errors import LLMTimeoutError
    import pytest

    class SlowProvider(LLMProvider):
        @property
        def name(self): return "slow"
        @property
        def model(self): return "slow-model"
        def chat(self, messages, *, purpose="chat"): return LLMResponse(text="hi", provider="slow", model="slow-model")
        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            time.sleep(0.3)
            return LLMResponse(text="late", provider="slow", model="slow-model")

    session = ProviderSession(
        provider=SlowProvider(),
        system_instruction="system",
        tools=[],
        turn_deadline_seconds=0.05,
    )
    with pytest.raises(LLMTimeoutError) as exc_info:
        session.send_user("hello")
    assert "interactive turn timed out" in str(exc_info.value)


def test_task_lifecycle_keeps_terminal_truth_on_provider_error():
    from advi.agent import AgentSession
    from advi.agent.contracts import TaskStatus
    from advi.tools.contracts import ToolResult
    from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
    from advi.providers.errors import LLMUnavailableError
    from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
    from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus

    class FailingProvider(LLMProvider):
        def __init__(self):
            self.turn = 0
        @property
        def name(self): return "fail"
        @property
        def model(self): return "fail-model"
        def chat(self, messages, *, purpose="chat"): return LLMResponse(text="hi", provider="fail", model="fail-model")
        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    text="",
                    provider="fail",
                    model="fail-model",
                    tool_calls=(LLMToolCall(call_id="c1", name="open_application", arguments={"application": "Notepad"}),),
                )
            raise LLMUnavailableError("Network broke")

    class FakeEngine:
        def execute_tool_call(self, call, *, approval_granted=False):
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=True,
                verified=True,
                verification_status="verified",
            )

    registry = CapabilityRegistry()
    registry.register(Capability(
        name="desktop_control",
        description="desktop",
        supported_actions={"open_application"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    tool_registry = ToolRegistry(registry)
    session = AgentSession(
        [FailingProvider()],
        tool_registry=tool_registry,
        tool_runner=ToolRunner(FakeEngine(), ToolGuard(tool_registry), tool_registry),
        tool_guard=ToolGuard(tool_registry),
    )
    session.local_intents_enabled = False  # exercises the model loop

    resp = session.respond("Open Notepad")
    assert "completed and verified" in resp.text.lower()
    # The requested operation itself is terminal and was verified before the
    # provider became unavailable, so the provider failure cannot invalidate it.
    assert session.task is not None
    assert session.task.status == TaskStatus.COMPLETED

    # Next user turn must clear the terminal task
    session.respond("Hello")
    assert session.task is None


def test_task_lifecycle_transitions_to_uncertain_when_external_effect_unconfirmed():
    from advi.agent import AgentSession
    from advi.agent.contracts import TaskStatus
    from advi.tools.contracts import ToolResult
    from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
    from advi.providers.errors import LLMUnavailableError
    from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
    from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus

    class FailingProvider(LLMProvider):
        def __init__(self):
            self.turn = 0
        @property
        def name(self): return "fail"
        @property
        def model(self): return "fail-model"
        def chat(self, messages, *, purpose="chat"): return LLMResponse(text="hi", provider="fail", model="fail-model")
        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(
                    text="",
                    provider="fail",
                    model="fail-model",
                    tool_calls=(LLMToolCall(call_id="c1", name="open_application", arguments={"application": "Notepad"}),),
                )
            raise LLMUnavailableError("Network broke")

    class FakeEngine:
        def execute_tool_call(self, call, *, approval_granted=False):
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=True,
                verified=False,
                verification_status="uncertain",
            )

    registry = CapabilityRegistry()
    registry.register(Capability(
        name="desktop_control",
        description="desktop",
        supported_actions={"open_application"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    tool_registry = ToolRegistry(registry)
    session = AgentSession(
        [FailingProvider()],
        tool_registry=tool_registry,
        tool_runner=ToolRunner(FakeEngine(), ToolGuard(tool_registry), tool_registry),
        tool_guard=ToolGuard(tool_registry),
    )
    session.local_intents_enabled = False  # exercises the model loop

    resp = session.respond("Open Notepad")
    assert "I couldn't continue that task just now" in resp.text
    # Task should be UNCERTAIN because verification was unconfirmed
    assert session.task is not None
    assert session.task.status == TaskStatus.UNCERTAIN

    # Next user turn must clear the terminal uncertain task
    session.respond("Hello")
    assert session.task is None

