from __future__ import annotations

from types import SimpleNamespace

from advi.agent.contracts import TaskState
from advi.agent.provider_session import ProviderSessionManager
from advi.agent.session import AgentSession
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.providers.errors import LLMRequestError
from advi.tools import ToolGuard, ToolRegistry


class FakeProvider(LLMProvider):
    def __init__(self, name: str, responses=None, errors=None):
        self._name = name
        self.responses = list(responses or [])
        self.errors = list(errors or [])
        self.calls = []

    @property
    def name(self):
        return self._name

    @property
    def model(self):
        return f"{self._name}-model"

    def chat(self, messages, *, purpose="chat"):
        return LLMResponse(text="ok", provider=self.name, model=self.model)

    def create_native_session(self, *, system_instruction, tools, history=None):
        return None

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.calls.append({"messages": messages, "tools": tools})
        if self.errors:
            error = self.errors.pop(0)
            raise error
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(text="done", provider=self.name, model=self.model)


def _registry(*actions: str) -> ToolRegistry:
    registry = CapabilityRegistry()
    registry.register(
        Capability(
            name="browser",
            description="browser",
            supported_actions=set(actions),
            status=CapabilityStatus.AVAILABLE,
            handler=object(),
        )
    )
    return ToolRegistry(registry)


def test_browser_surface_keeps_typing_tool_available_after_navigation():
    tools = _registry("navigate", "inspect_web_page", "search", "click_web_element", "type_web_element", "wait")
    session = object.__new__(AgentSession)
    session.tools = tools
    session.task = TaskState(goal="open youtube and search for Never Say Never", task_id="t1")
    selected = session._select_model_tools(session.task.goal, last_tool="navigate")
    names = {item["function"]["name"] for item in selected}
    assert {"navigate", "inspect_web_page", "click_web_element", "type_web_element", "wait"}.issubset(names)


def test_model_surface_dependency_expansion_is_bounded():
    tools = _registry("navigate", "inspect_web_page", "search", "click_web_element", "type_web_element", "wait", "read_web_page", "reload_page", "go_back", "go_forward")
    specs = tools.expand_model_surface(["navigate"])
    names = [spec.name for spec in specs]
    assert names == ["navigate", "inspect_web_page", "search", "click_web_element", "type_web_element", "wait", "read_web_page", "go_back", "go_forward", "reload_page"][:len(names)]
    assert len(names) <= 10


def test_request_error_does_not_fail_over_to_another_provider():
    first = FakeProvider("first", errors=[LLMRequestError("tool not in request.tools")])
    second = FakeProvider("second", responses=[LLMResponse(text="second", provider="second", model="second-model")])
    manager = ProviderSessionManager([first, second])
    try:
        manager.send_user("do it", system_instruction="sys", tools=[])
    except LLMRequestError:
        pass
    else:
        raise AssertionError("request error should propagate for local repair")
    assert len(first.calls) == 1
    assert len(second.calls) == 0
    assert manager.switch_count == 0


def test_missing_tool_retry_stays_on_same_provider():
    registry = _registry("navigate", "type_web_element")
    guard = ToolGuard(registry)
    provider = FakeProvider(
        "first",
        responses=[LLMResponse(text="", provider="first", model="first-model", tool_calls=(LLMToolCall("c1", "type_web_element", {"selector": "#q", "text": "x"}),))],
        errors=[LLMRequestError("attempted to call tool 'type_web_element' which was not in request.tools")],
    )
    other = FakeProvider("second", responses=[LLMResponse(text="wrong", provider="second", model="second-model")])
    runner = SimpleNamespace()
    session = AgentSession([provider, other], tool_registry=registry, tool_runner=runner, tool_guard=guard)
    session._handoff = lambda: None
    tools = registry._from_action_spec
    # Simulate the actual first provider request having already failed. The
    # repair must then reuse that same active provider rather than fail over.
    try:
        session.provider_manager.send_user("type x", system_instruction=session.system_prompt, tools=[{"function": {"name": "navigate"}}])
    except LLMRequestError:
        pass
    result = session._retry_with_missing_tool(
        LLMRequestError("attempted to call tool 'type_web_element' which was not in request.tools"),
        user_payload="type x",
        tools=[{"function": {"name": "navigate"}}],
        handoff=None,
        observation=None,
    )
    assert result is not None
    assert result[0].tool_calls[0].name == "type_web_element"
    assert len(provider.calls) == 2
    assert len(other.calls) == 0
    assert session._missing_tool_recovery_used is True


def test_tool_alias_normalization_remains_before_execution():
    session = object.__new__(AgentSession)
    session.tools = _registry("navigate")
    call = LLMToolCall("c1", "browser_open", {"url": "https://example.com"})
    normalized = session._normalize_native_call(call)
    assert normalized.name == "navigate"
