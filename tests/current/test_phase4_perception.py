from __future__ import annotations

from advi.agent.contracts import ExecutionDomain, TaskState
from advi.core.execution_controller import ExecutionController
from advi.core.execution_strategy import ExecutionMode, ExecutionStrategy, OperationFamily
from advi.core.perception_controller import PerceptionController
from advi.tools.contracts import ToolCall, ToolResult


def result(name: str, *, verified: bool, error: str | None = None) -> ToolResult:
    return ToolResult(
        call_id=f"r-{name}", name=name, success=True, verified=verified,
        verification_status="verified" if verified else "uncertain", error=error,
    )


def strategy(family: OperationFamily) -> ExecutionStrategy:
    return ExecutionStrategy(
        ExecutionMode.DELIBERATE, family, 0.1, True, 1, (), "test"
    )


def test_browser_unverified_action_requests_browser_observer_only():
    task = TaskState(goal="search Amazon for 2TB SSD", task_id="p4-browser")
    task.active_domain = ExecutionDomain.BROWSER.value
    decision = PerceptionController().decide(
        task,
        ToolCall("c1", "click_web_element", {"selector": "#search"}),
        result("click_web_element", verified=False),
        strategy(OperationFamily.BROWSER_WORKFLOW),
        local_sync={"ready": True, "unexpected": False},
        available_tools={"inspect_web_page", "inspect_window"},
    )
    assert decision.needed
    assert decision.tool_name == "inspect_web_page"
    assert decision.arguments == {"limit": 24}
    assert decision.domain == "browser"


def test_desktop_unverified_action_never_uses_browser_observer():
    task = TaskState(goal="type into WhatsApp", task_id="p4-desktop")
    task.active_domain = ExecutionDomain.DESKTOP.value
    decision = PerceptionController().decide(
        task,
        ToolCall("c2", "type_text", {"text": "hello"}),
        result("type_text", verified=False),
        strategy(OperationFamily.DESKTOP_WORKFLOW),
        local_sync={"ready": False, "unexpected": True},
        available_tools={"inspect_web_page", "inspect_window"},
    )
    assert decision.needed
    assert decision.tool_name == "inspect_window"
    assert decision.domain == "desktop"


def test_filesystem_uncertainty_does_not_trigger_unrelated_window_inspection():
    task = TaskState(goal="open Music Day Speech.pdf", task_id="p4-files")
    task.active_domain = ExecutionDomain.FILESYSTEM.value
    decision = PerceptionController().decide(
        task,
        ToolCall("c3", "open_file", {"path": "Music Day Speech.pdf"}),
        result("open_file", verified=False),
        strategy(OperationFamily.FILE_WORKFLOW),
        local_sync={"ready": False, "unexpected": True},
        available_tools={"inspect_window", "inspect_web_page"},
    )
    assert not decision.needed
    assert decision.domain == "filesystem"


def test_verified_healthy_action_skips_perception():
    task = TaskState(goal="browser workflow", task_id="p4-local")
    task.active_domain = ExecutionDomain.BROWSER.value
    decision = PerceptionController().decide(
        task,
        ToolCall("c4", "click_web_element", {"selector": "#x"}),
        result("click_web_element", verified=True),
        strategy(OperationFamily.BROWSER_WORKFLOW),
        local_sync={"ready": True, "unexpected": False},
        available_tools={"inspect_web_page"},
    )
    assert not decision.needed


def test_equivalent_recent_observation_is_reused():
    task = TaskState(goal="browser workflow", task_id="p4-reuse")
    task.active_domain = ExecutionDomain.BROWSER.value
    controller = PerceptionController()
    call = ToolCall("c5", "type_web_element", {"selector": "#q", "text": "x"})
    failed = result("type_web_element", verified=False)
    first = controller.decide(
        task, call, failed, strategy(OperationFamily.BROWSER_WORKFLOW),
        local_sync={"ready": False, "unexpected": True},
        available_tools={"inspect_web_page"}, now=10.0,
    )
    assert first.needed
    controller.record(task, call, failed, "browser")
    second = controller.decide(
        task, call, failed, strategy(OperationFamily.BROWSER_WORKFLOW),
        local_sync={"ready": False, "unexpected": True},
        available_tools={"inspect_web_page"}, now=11.0,
    )
    assert not second.needed


def test_explicit_observation_is_not_observed_again():
    task = TaskState(goal="explore website", task_id="p4-observe")
    task.active_domain = ExecutionDomain.BROWSER.value
    decision = PerceptionController().decide(
        task,
        ToolCall("c6", "inspect_web_page", {}),
        result("inspect_web_page", verified=True),
        strategy(OperationFamily.EXPLORATION),
        local_sync={"ready": True},
        available_tools={"inspect_web_page"},
    )
    assert not decision.needed
