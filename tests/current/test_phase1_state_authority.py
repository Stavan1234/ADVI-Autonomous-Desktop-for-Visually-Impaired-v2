from __future__ import annotations

from advi.agent.contracts import ExecutionDomain, ExecutionRoute, TaskState
from advi.agent.session import AgentSession
from advi.capabilities.desktop.context import ApplicationSessionState, DesktopContext
from advi.capabilities.registry import CapabilityRegistry
from advi.providers.base import LLMProvider, LLMResponse


class Phase1Provider(LLMProvider):
    @property
    def name(self):
        return "phase1"

    @property
    def model(self):
        return "phase1-model"

    def chat(self, messages, *, purpose="chat"):
        return LLMResponse(text="ok", provider=self.name, model=self.model)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        return LLMResponse(text="ok", provider=self.name, model=self.model)


def test_task_state_records_authoritative_domain_route_and_target():
    task = TaskState(goal="open Music Day Speech.pdf", task_id="t1")
    task.set_active_domain(ExecutionDomain.FILESYSTEM, source="user_request")
    task.set_current_target(kind="file", value="Music Day Speech.pdf", source="user", explicit=True)
    assert task.active_domain == "filesystem"
    assert task.execution_route == ExecutionRoute.NATIVE.value
    assert task.current_target["value"] == "Music Day Speech.pdf"


def test_explicit_david_route_cannot_be_silently_replaced():
    task = TaskState(goal="send email with David", task_id="t2")
    assert task.set_execution_route(ExecutionRoute.DAVID, explicit=True) is True
    assert task.set_execution_route(ExecutionRoute.NATIVE, explicit=False) is False
    assert task.execution_route == ExecutionRoute.DAVID.value
    assert task.set_execution_route(ExecutionRoute.NATIVE, explicit=True) is True
    assert task.execution_route == ExecutionRoute.NATIVE.value


def test_artifact_aliases_track_typed_recent_outputs():
    task = TaskState(goal="create test files", task_id="t3")
    task.record_artifacts({"path": r"C:\Users\HP\Desktop\Battery.pdf", "folder": r"C:\Users\HP\Desktop\stavan_test"})
    assert task.artifact_aliases["last_file"].endswith("Battery.pdf")
    assert task.artifact_aliases["last_pdf"].endswith("Battery.pdf")
    assert task.artifact_aliases["last_folder"].endswith("stavan_test")


def test_desktop_observation_filters_stale_browser_state_for_desktop_domain():
    ctx = DesktopContext(
        current_application="WhatsApp",
        target_hwnd=100,
        target_pid=200,
        target_title="WhatsApp",
        target_process_name="WhatsApp.Root.exe",
        target_tab_id="stale-youtube-tab",
        target_tab_url="https://music.youtube.com/search?q=old",
        target_tab_title="YouTube Music",
    )
    session = ctx.start_application_session("WhatsApp")
    session.bind_window(
        hwnd=100,
        pid=200,
        title="WhatsApp",
        process_name="WhatsApp.Root.exe",
        state=ApplicationSessionState.READY,
    )
    observed = ctx.observation_snapshot("desktop")
    assert observed["active_application"] == "WhatsApp"
    assert observed["target"]["hwnd"] == 100
    assert "browser" not in observed
    assert observed["observation_domain"] == "desktop"


def test_desktop_observation_can_expose_browser_only_state():
    ctx = DesktopContext(
        current_application="WhatsApp",
        target_hwnd=100,
        target_tab_id="tab-1",
        target_tab_url="https://music.youtube.com",
        target_tab_title="YouTube Music",
    )
    observed = ctx.observation_snapshot("browser")
    assert observed["browser"]["tab_id"] == "tab-1"
    assert "active_application" not in observed
    assert "target" not in observed
    assert observed["observation_domain"] == "browser"


def test_agent_session_captures_explicit_file_target_before_model(monkeypatch):
    registry = CapabilityRegistry()
    session = AgentSession([Phase1Provider()], tool_registry=registry, tool_runner=type("Runner", (), {})(), tool_guard=type("Guard", (), {})())
    session.last_user_message = 'open Music Day Speech.pdf'
    task = session._ensure_active_task()
    assert task.active_domain == ExecutionDomain.FILESYSTEM.value
    assert task.current_target["value"] == "Music Day Speech.pdf"
    assert task.protected_parameters["path"] == "Music Day Speech.pdf"
    assert task.protected_parameters["name"] == "Music Day Speech.pdf"


def test_agent_session_resolves_recent_pdf_reference_without_reusing_wrong_artifact(monkeypatch):
    registry = CapabilityRegistry()
    session = AgentSession([Phase1Provider()], tool_registry=registry, tool_runner=type("Runner", (), {})(), tool_guard=type("Guard", (), {})())
    session._last_completed_artifact_aliases = {"last_pdf": r"C:\Users\HP\Desktop\Battery.pdf"}
    session.last_user_message = "open that pdf"
    task = session._ensure_active_task()
    assert task.current_target["value"].endswith("Battery.pdf")
    assert task.context["resolved_reference"]["alias"] == "last_pdf"
    assert task.protected_parameters["path"].endswith("Battery.pdf")


def test_task_snapshot_contains_state_authority_fields():
    task = TaskState(goal="send a message", task_id="t4")
    task.set_active_domain("desktop", source="user_request")
    task.set_execution_route("native", explicit=False)
    task.set_current_target(kind="chat", value="Anurodh", source="user", explicit=True)
    snapshot = task.snapshot()
    assert snapshot["active_domain"] == "desktop"
    assert snapshot["execution_route"] == "native"
    assert snapshot["current_target"]["value"] == "Anurodh"


def test_handoff_preserves_domain_route_and_target():
    from advi.agent.session_handoff import SessionHandoffBuilder

    task = TaskState(goal="send a WhatsApp message", task_id="t5")
    task.set_active_domain(ExecutionDomain.DESKTOP, source="user_request")
    task.set_execution_route(ExecutionRoute.DAVID, explicit=True)
    task.set_current_target(kind="chat", value="Anurodh", source="user", explicit=True)
    handoff = SessionHandoffBuilder.from_state(
        task,
        last_user_message=task.goal,
        last_assistant_message=None,
        last_tool="inspect_window",
        last_tool_success=True,
        last_tool_summary="ready",
    )
    payload = handoff.to_payload()
    assert payload["active_domain"] == "desktop"
    assert payload["execution_route"] == "david"
    assert payload["current_target"]["value"] == "Anurodh"


def test_explicit_david_route_rejects_native_side_effect_without_running_it():
    from advi.agent.session import AgentSession
    from advi.capabilities.registry import Capability, CapabilityStatus
    from advi.providers.base import LLMToolCall
    from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
    from advi.tools.contracts import ToolResult

    registry = CapabilityRegistry()
    registry.register(Capability(
        name="email",
        description="email",
        supported_actions={"email_send"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    calls = []
    class Runner:
        def run(self, call, approved=False):
            calls.append(call)
            return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True, verification_status="verified")

    session = AgentSession([Phase1Provider()], tool_registry=__import__('advi.tools', fromlist=['ToolRegistry']).ToolRegistry(registry), tool_runner=Runner(), tool_guard=ToolGuard(__import__('advi.tools', fromlist=['ToolRegistry']).ToolRegistry(registry)))
    session.task = TaskState(goal="use David to send email", task_id="t6")
    session.task.set_execution_route(ExecutionRoute.DAVID, explicit=True)
    result = session._run_tool(LLMToolCall("c1", "email_send", {"to":"a@example.com", "subject":"x", "body":"y"}))
    assert result.success is False
    assert result.verification_details["reason"] == "execution_route_conflict"
    assert calls == []


def test_explicit_file_target_overrides_stale_model_argument():
    from advi.tools import ToolCall

    registry = CapabilityRegistry()
    session = AgentSession([Phase1Provider()], tool_registry=registry, tool_runner=type("Runner", (), {})(), tool_guard=type("Guard", (), {})())
    session.last_user_message = "open Music Day Speech.pdf"
    task = session._ensure_active_task()
    normalized = session._apply_protected_task_parameters(
        ToolCall("c1", "open_file", {"path": r"C:\Users\HP\Desktop\stale\advi.pdf"})
    )
    assert normalized.arguments["path"] == "Music Day Speech.pdf"


def test_wait_does_not_change_browser_domain():
    from advi.tools import ToolCall
    task = TaskState(goal="search YouTube", task_id="t7", active_domain=ExecutionDomain.BROWSER.value)
    registry = CapabilityRegistry()
    session = AgentSession([Phase1Provider()], tool_registry=registry, tool_runner=type("Runner", (), {})(), tool_guard=type("Guard", (), {})())
    session.task = task
    session._set_domain_from_tool("wait")
    assert session.task.active_domain == ExecutionDomain.BROWSER.value


def test_agent_session_model_observation_cannot_mix_desktop_and_browser_state():
    from advi.agent.contracts import TaskState

    ctx = DesktopContext(
        current_application="WhatsApp",
        target_hwnd=101,
        target_tab_id="stale-tab",
        target_tab_url="https://music.youtube.com/search?q=praise",
        target_tab_title="YouTube Music",
    )
    registry = CapabilityRegistry(desktop_context=ctx)
    session = AgentSession([Phase1Provider()], tool_registry=__import__('advi.tools', fromlist=['ToolRegistry']).ToolRegistry(registry), tool_runner=type("Runner", (), {})(), tool_guard=type("Guard", (), {})())
    session.task = TaskState(goal="on WhatsApp find Anurodh", task_id="t8", active_domain="desktop")
    observed = session._task_observation()
    assert observed is not None
    assert observed["active_domain"] == "desktop"
    assert observed["active_application"] == "WhatsApp"
    assert "browser" not in observed
