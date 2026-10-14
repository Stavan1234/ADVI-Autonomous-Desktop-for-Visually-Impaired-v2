from __future__ import annotations

from advi.agent.provider_session import ProviderSessionManager
from advi.providers.base import LLMProvider, LLMResponse, ProviderHealthState
from advi.providers.errors import LLMAuthenticationError, LLMRateLimitError, LLMTimeoutError
from advi.core.config import _load_groq_api_keys, _load_groq_key_groups


class FakeProvider(LLMProvider):
    def __init__(self, name: str, outcomes=None, *, quota_group=None):
        self._name = name
        self._outcomes = list(outcomes or [])
        self._quota_group = quota_group
        self.calls = 0

    @property
    def name(self):
        return self._name

    @property
    def model(self):
        return "test-model"

    @property
    def quota_group(self):
        return self._quota_group

    def chat(self, messages, *, purpose="chat"):
        self.calls += 1
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return LLMResponse(text="ok", provider=self.name, model=self.model)


def test_groq_env_keys_load_in_order_and_deduplicate(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k1")
    monkeypatch.setenv("GROQ_API_KEY_2", "k2")
    monkeypatch.setenv("GROQ_API_KEY_3", "k1")
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    assert _load_groq_api_keys() == ("k1", "k2")


def test_groq_key_groups_default_to_independent_keys():
    assert _load_groq_key_groups(3) == ("groq-1", "groq-2", "groq-3")


def test_groq_key_groups_can_model_shared_quota(monkeypatch):
    monkeypatch.setenv("GROQ_KEY_GROUPS", "shared,shared,project-c")
    assert _load_groq_key_groups(3) == ("shared", "shared", "project-c")


def test_manager_rotates_groq_key_on_rate_limit_and_keeps_handoff(monkeypatch):
    first = FakeProvider("groq-1", [LLMRateLimitError("tokens per minute")], quota_group="groq-1")
    second = FakeProvider("groq-2", [LLMResponse(text="from second", provider="groq-2", model="test-model")], quota_group="groq-2")
    manager = ProviderSessionManager([first, second], health_state=ProviderHealthState())

    result = manager.send_user(
        "hello",
        system_instruction="sys",
        tools=[],
    )

    assert result.provider == "groq-2"
    assert first.calls == 1
    assert second.calls == 1
    assert manager.active_provider.name == "groq-2"
    assert manager.switch_count == 1


def test_manager_rotates_on_auth_failure_and_does_not_touch_invalid_schema_path():
    first = FakeProvider("groq-1", [LLMAuthenticationError("bad key")], quota_group="groq-1")
    second = FakeProvider("groq-2", [LLMResponse(text="ok", provider="groq-2", model="test-model")], quota_group="groq-2")
    manager = ProviderSessionManager([first, second])
    result = manager.send_user("hello", system_instruction="sys", tools=[])
    assert result.provider == "groq-2"
    assert first.calls == 1
    assert second.calls == 1


def test_groq_rate_limit_error_reads_retry_after_header():
    from advi.providers.groq import _build_rate_limit_error

    class Response:
        headers = {
            "retry-after": "2.5",
            "x-ratelimit-reset-tokens": "2.5s",
        }

    class Error(Exception):
        response = Response()

    error = _build_rate_limit_error(Error("429 too many requests"))
    assert error.quota_scope == "rate_limit"
    assert error.retry_after_seconds == 2.5


def test_shared_groq_quota_group_blocks_sibling_keys(monkeypatch):
    first = FakeProvider("groq-1", [LLMRateLimitError("tokens per day")], quota_group="shared")
    err = first._outcomes[0]
    setattr(err, "quota_scope", "project_daily")
    second = FakeProvider("groq-2", quota_group="shared")
    third = FakeProvider("gemini-1", [LLMResponse(text="gemini", provider="gemini-1", model="gemini")], quota_group="gemini")
    monkeypatch.setattr("advi.agent.provider_session.time.monotonic", lambda: 1000.0)
    state = ProviderHealthState()
    manager = ProviderSessionManager([first, second, third], health_state=state)
    result = manager.send_user("hello", system_instruction="sys", tools=[])
    assert result.provider == "gemini-1"
    assert second.calls == 0
    assert third.calls == 1


def test_timeout_rotates_then_sticks_to_healthy_key():
    first = FakeProvider("groq-1", [LLMTimeoutError("timeout")], quota_group="groq-1")
    second = FakeProvider("groq-2", quota_group="groq-2")
    manager = ProviderSessionManager([first, second])
    assert manager.send_user("one", system_instruction="sys", tools=[]).provider == "groq-2"
    assert manager.send_user("two", system_instruction="sys", tools=[]).provider == "groq-2"
    assert first.calls == 1
    assert second.calls == 2



def test_manager_rotates_groq_key_during_tool_result_continuation():
    class ToolProvider(FakeProvider):
        def __init__(self, name, *, fail_tool=False, quota_group=None):
            super().__init__(name, quota_group=quota_group)
            self.fail_tool = fail_tool
            self.chat_calls = 0

        def create_native_session(self, *, system_instruction, tools, history=None):
            return None

        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            self.chat_calls += 1
            if self.fail_tool and self.chat_calls == 1:
                raise LLMRateLimitError("tokens per minute")
            return LLMResponse(text="continued", provider=self.name, model=self.model)

    first = ToolProvider("groq-1", fail_tool=True, quota_group="groq-1")
    second = ToolProvider("groq-2", quota_group="groq-2")
    manager = ProviderSessionManager([first, second])
    manager.start(system_instruction="sys", tools=[])
    from advi.providers.base import LLMToolCall
    from advi.agent.session_handoff import SessionHandoff, ToolExchange
    handoff = SessionHandoff(
        last_user_message="open a file",
        recent_tool_exchanges=(
            ToolExchange.from_calls_and_results(
                [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}],
                [{"call_id": "c1", "name": "read_file", "result": {"ok": True, "output": "hello"}}],
            ),
        ),
    )
    result = manager.send_tool_results(
        [(LLMToolCall(call_id="c1", name="read_file", arguments={"path": "a.txt"}), {"ok": True, "output": "hello"})],
        system_instruction="sys",
        tools=[],
        handoff=handoff,
    )
    assert result.provider == "groq-2"
    assert first.chat_calls == 1
    assert second.chat_calls == 1


def test_manager_walks_all_three_groq_keys_before_gemini():
    first = FakeProvider("groq-1", [LLMTimeoutError("timeout")], quota_group="groq-1")
    second = FakeProvider("groq-2", [LLMRateLimitError("tokens per minute")], quota_group="groq-2")
    third = FakeProvider("groq-3", [LLMResponse(text="from third", provider="groq-3", model="test-model")], quota_group="groq-3")
    manager = ProviderSessionManager([first, second, third])

    result = manager.send_user("hello", system_instruction="sys", tools=[])

    assert result.provider == "groq-3"
    assert first.calls == 1
    assert second.calls == 1
    assert third.calls == 1
    assert manager.active_provider.name == "groq-3"
    assert manager.switch_count == 2
    assert len(manager.last_attempts) == 3
    assert manager.last_attempts[0].success is False
    assert manager.last_attempts[1].success is False
    assert manager.last_attempts[2].success is True


def test_tool_result_continuation_walks_all_three_keys():
    class ToolProvider(FakeProvider):
        def __init__(self, name, failures=0, quota_group=None):
            super().__init__(name, quota_group=quota_group)
            self.failures = failures

        def create_native_session(self, *, system_instruction, tools, history=None):
            return None

        def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
            self.calls += 1
            if self.calls <= self.failures:
                raise LLMRateLimitError("tokens per minute")
            return LLMResponse(text=f"from {self.name}", provider=self.name, model=self.model)

    first = ToolProvider("groq-1", failures=1, quota_group="g1")
    second = ToolProvider("groq-2", failures=1, quota_group="g2")
    third = ToolProvider("groq-3", failures=0, quota_group="g3")
    manager = ProviderSessionManager([first, second, third])
    manager.start(system_instruction="sys", tools=[])

    from advi.providers.base import LLMToolCall
    from advi.agent.session_handoff import SessionHandoff, ToolExchange
    handoff = SessionHandoff(
        last_user_message="open a file",
        recent_tool_exchanges=(
            ToolExchange.from_calls_and_results(
                [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}],
                [{"call_id": "c1", "name": "read_file", "result": {"ok": True, "output": "hello"}}],
            ),
        ),
    )

    result = manager.send_tool_results(
        [(LLMToolCall(call_id="c1", name="read_file", arguments={"path": "a.txt"}), {"ok": True, "output": "hello"})],
        system_instruction="sys",
        tools=[],
        handoff=handoff,
    )

    assert result.provider == "groq-3"
    assert first.calls == 1
    assert second.calls == 1
    assert third.calls == 1

