from __future__ import annotations

from advi.core.readiness import wait_until, wait_for_stable_value
from advi.capabilities.browser.cdp import CDPBrowser
from advi.capabilities.desktop.context import DesktopContext
from advi.providers.base import LLMProvider, LLMResponse


class Phase2Provider(LLMProvider):
    @property
    def name(self):
        return "phase2"

    @property
    def model(self):
        return "phase2-model"

    def chat(self, messages, *, purpose="chat"):
        return LLMResponse(text="ok", provider=self.name, model=self.model)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        return LLMResponse(text="ok", provider=self.name, model=self.model)


def test_wait_until_requires_stable_truth_before_success(monkeypatch):
    samples = iter([False, True, True, True])
    times = iter([0.0, 0.05, 0.12, 0.25, 1.0])
    monkeypatch.setattr("advi.core.readiness.time.monotonic", lambda: next(times))
    monkeypatch.setattr("advi.core.readiness.time.sleep", lambda _s: None)
    result = wait_until(lambda: next(samples), timeout=1.0, poll_interval=0.05, stable_for=0.1)
    assert result.ready
    assert result.attempts == 3


def test_wait_until_times_out_without_truth(monkeypatch):
    clock = [0.0]
    def monotonic():
        clock[0] += 0.2
        return clock[0]
    monkeypatch.setattr("advi.core.readiness.time.monotonic", monotonic)
    monkeypatch.setattr("advi.core.readiness.time.sleep", lambda _s: None)
    result = wait_until(lambda: False, timeout=0.5, poll_interval=0.05)
    assert result.ready is False
    assert result.reason == "timeout"


def test_stable_value_waits_for_same_state(monkeypatch):
    values = iter(["loading", "ready", "ready", "ready"])
    clock = [0.0]
    def monotonic():
        return clock[0]
    def advance(seconds):
        clock[0] += seconds
    monkeypatch.setattr("advi.core.readiness.time.monotonic", monotonic)
    monkeypatch.setattr("advi.core.readiness.time.sleep", advance)
    result = wait_for_stable_value(lambda: next(values), timeout=1.0, poll_interval=0.05, stable_for=0.10)
    assert result.ready
    assert result.value == "ready"


def test_context_observation_freshness_tracks_domain_and_revision():
    ctx = DesktopContext(current_application="WhatsApp")
    assert not ctx.observation_is_fresh("desktop")
    ctx.mark_observed("desktop")
    assert ctx.observation_is_fresh("desktop", max_age=1.0)
    assert not ctx.observation_is_fresh("browser", max_age=1.0)
    revision = ctx.observation_revision
    ctx.invalidate_observation("desktop")
    assert ctx.observation_revision > revision
    assert not ctx.observation_is_fresh("desktop")


def test_cdp_wait_for_url_returns_matching_live_url(monkeypatch):
    browser = CDPBrowser()
    values = iter([
        {"id":"tab-1", "url":"https://example.test/start", "title":"Start"},
        {"id":"tab-1", "url":"https://example.test/results?q=advi", "title":"Results"},
    ])
    monkeypatch.setattr(browser, "get_tab_info", lambda _tab: next(values))
    result = browser.wait_for_url("tab-1", contains="results", timeout=1.0, )
    assert result == "https://example.test/results?q=advi"


def test_adaptive_checkpoint_does_not_escalate_when_local_sync_is_ready(monkeypatch):
    from advi.agent.contracts import TaskState
    from advi.agent.session import AgentSession
    from advi.core.execution_strategy import ExecutionMode, OperationFamily, ExecutionStrategy
    from advi.tools.contracts import ToolCall, ToolResult

    session = AgentSession([Phase2Provider()], tool_registry=None, tool_runner=None, tool_guard=None)
    session.task = TaskState(goal="multi step desktop task", task_id="p2-local")
    strategy = ExecutionStrategy(ExecutionMode.DELIBERATE, OperationFamily.DESKTOP_WORKFLOW, 0.0, True, 1, ("inspect_window",), "test")
    monkeypatch.setattr(session, "_refresh_execution_strategy_state", lambda: strategy)
    monkeypatch.setattr(session, "_local_execution_sync", lambda _call: {"ready": True, "unexpected": False, "domain": "desktop", "reason": "target_ready"})
    called = []
    session.tool_runner = type("Runner", (), {"run": lambda self, call, approved=False: called.append(call)})()

    call = ToolCall("c1", "click", {"target": "Search"})
    result = ToolResult(call_id="c1", name="click", success=True, verified=True, verification_status="verified")
    session._adaptive_checkpoint_after_tool(call, result)

    assert called == []
    assert session.task.context["adaptive_observation_age"] == "local-fresh"


def test_adaptive_checkpoint_escalates_when_action_is_unverified(monkeypatch):
    from advi.agent.contracts import TaskState
    from advi.agent.session import AgentSession
    from advi.core.execution_strategy import ExecutionMode, OperationFamily, ExecutionStrategy
    from advi.tools.contracts import ToolCall, ToolResult

    session = AgentSession([Phase2Provider()], tool_registry=None, tool_runner=None, tool_guard=None)
    session.task = TaskState(goal="multi step desktop task", task_id="p2-escalate")
    strategy = ExecutionStrategy(ExecutionMode.DELIBERATE, OperationFamily.DESKTOP_WORKFLOW, 0.0, True, 1, ("inspect_window",), "test")
    monkeypatch.setattr(session, "_refresh_execution_strategy_state", lambda: strategy)
    monkeypatch.setattr(session, "_local_execution_sync", lambda _call: {"ready": True, "unexpected": False, "domain": "desktop", "reason": "target_ready"})
    observed = []
    def run(call, approved=False):
        observed.append(call)
        return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True, verification_status="verified", output={"controls": []})
    session.tool_runner = type("Runner", (), {"run": staticmethod(run)})()

    call = ToolCall("c2", "type_text", {"text": "hello"})
    result = ToolResult(call_id="c2", name="type_text", success=True, verified=False, verification_status="uncertain")
    session._adaptive_checkpoint_after_tool(call, result)

    assert len(observed) == 1
    assert observed[0].name == "inspect_window"
    assert session.task.context["adaptive_observation_age"] == "fresh"
