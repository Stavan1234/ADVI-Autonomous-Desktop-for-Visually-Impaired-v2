from types import SimpleNamespace

from advi.agent.session import AgentSession
from advi.capabilities.registry import CapabilityRegistry
from advi.core.execution_engine import ExecutionEngine
from advi.core.execution_watchdog import ExecutionWatchdog
from advi.tools import ToolGuard, ToolRegistry, ToolRunner
from advi.providers.base import LLMProvider, LLMResponse


class _Provider(LLMProvider):
    @property
    def name(self):
        return "test"

    @property
    def model(self):
        return "model"

    def chat(self, messages, *, purpose="chat"):
        return LLMResponse(text="ok", provider=self.name, model=self.model)



def test_watchdog_budget_is_read_only_and_marks_expiry(monkeypatch):
    values = iter([10.0, 16.0, 16.0, 16.0])
    monkeypatch.setattr("advi.core.execution_watchdog.time.monotonic", lambda: next(values))
    watchdog = ExecutionWatchdog(task_timeout_seconds=5)
    watchdog.begin_task()

    decision = watchdog.before_action("read_file", {"path": "x"})
    assert decision.allowed is False
    assert decision.reason == "task_timeout"
    budget = watchdog.budget()
    assert budget["expired"] is True
    assert budget["actions"] == 0
