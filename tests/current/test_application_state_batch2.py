from __future__ import annotations

from types import SimpleNamespace
import ctypes

from advi.capabilities.desktop.context import ApplicationSession, ApplicationSessionState, DesktopContext
from advi.capabilities.desktop.executor import DesktopCapability
import advi.capabilities.desktop.executor as executor_module
from advi.capabilities.desktop.perception import PerceptionResult, ScreenPerception
from advi.capabilities.desktop.vision import VisionPerception
from advi.core.action_contracts import get_action_contract
from advi.core.context_router import ContextRouter
from advi.tools import ToolCall


def test_application_session_tracks_transition_and_identity():
    session = ApplicationSession.create(
        "YouTube Music",
        record={
            "source": "windows_start_apps",
            "app_id": "Brave._crx_music",
            "runtime_type": "pwa",
            "command": r"shell:AppsFolder\Brave._crx_music",
        },
    )
    assert session.state is ApplicationSessionState.DISCOVERING
    session.bind_window(
        hwnd=123,
        pid=456,
        title="YouTube Music",
        process_name="brave.exe",
        state=ApplicationSessionState.STABILIZING,
    )
    session.transition(ApplicationSessionState.READY, reason="window stable")
    snap = session.snapshot()
    assert snap["identity"]["runtime_type"] == "pwa"
    assert snap["window"]["hwnd"] == 123
    assert snap["state"] == "ready"


def test_wait_condition_uses_application_readiness(monkeypatch):
    ctx = DesktopContext(current_application="Telegram")
    session = ctx.start_application_session("Telegram")
    session.bind_window(hwnd=100, pid=200, title="Telegram", process_name="Telegram.exe")
    cap = DesktopCapability(context=ctx)
    monkeypatch.setattr(cap, "_window_state", lambda hwnd: {
        "hwnd": hwnd, "pid": 200, "title": "Telegram", "process": "Telegram.exe",
        "visible": True, "minimized": False, "foreground": True, "is_dialog": False,
    })
    monkeypatch.setattr(cap, "_is_window_visible", lambda hwnd: True)
    monkeypatch.setattr(cap, "_is_foreground_window", lambda hwnd: True)
    monkeypatch.setattr(cap, "_find_blocking_dialog", lambda hwnd, pid: None)
    monkeypatch.setattr(cap, "_readiness_signature", lambda hwnd: ("Telegram", (("Button", "Chats"),)))
    monkeypatch.setattr("advi.capabilities.desktop.executor.time.sleep", lambda *_: None)
    result = cap.execute(ToolCall("w1", "wait", {"condition": "application_ready", "timeout": 1}))
    assert result.success is True
    assert result.verified is True
    assert result.output["ready"] is True
    assert ctx.application_session.state is ApplicationSessionState.READY


def test_dialog_classification_is_specific_for_blind_user():
    assert DesktopCapability._classify_dialog("Microsoft Word (Product Activation Failed)", "Activate Office") == "activation_required"
    assert DesktopCapability._classify_dialog("Sign in", "Please sign in to continue") == "auth_required"
    assert DesktopCapability._classify_dialog("Permission required", "Allow access") == "permission_required"
    assert DesktopCapability._classify_dialog("Document Recovery", "Restore unsaved document") == "recovery_screen"
    assert DesktopCapability._classify_dialog("Confirm", "Choose an option") == "blocked_dialog"


def test_dialog_classification_maps_to_application_session_state():
    ctx = DesktopContext(current_application="Word")
    cap = DesktopCapability(context=ctx)
    assert cap._application_state_for_dialog({"classification": "activation_required"}) is ApplicationSessionState.AUTH_REQUIRED
    assert cap._application_state_for_dialog({"classification": "permission_required"}) is ApplicationSessionState.PERMISSION_REQUIRED
    assert cap._application_state_for_dialog({"classification": "recovery_screen"}) is ApplicationSessionState.RECOVERY_SCREEN
    assert cap._application_state_for_dialog({"classification": "blocked_dialog"}) is ApplicationSessionState.BLOCKED_DIALOG


def test_inspect_window_refreshes_session_with_blocker(monkeypatch):
    ctx = DesktopContext(current_application="Word")
    session = ctx.start_application_session("Word")
    session.bind_window(hwnd=10, pid=20, title="Document1 - Word", process_name="WINWORD.EXE")
    cap = DesktopCapability(context=ctx)

    monkeypatch.setattr(cap, "_window_state", lambda hwnd: {
        "hwnd": hwnd, "pid": 20, "title": "Document1 - Word", "process": "WINWORD.EXE",
        "visible": True, "minimized": False, "foreground": True, "is_dialog": False,
    })
    monkeypatch.setattr(cap, "_window_process_id", lambda hwnd: 20)
    monkeypatch.setattr(cap, "_find_blocking_dialog", lambda hwnd, pid: None)
    monkeypatch.setattr(cap, "_control_text", lambda control: None)
    monkeypatch.setattr(executor_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(IsWindow=lambda hwnd: 1)), raising=False)
    class FakeWindow:
        def exists(self, timeout=0.2): return True
        def descendants(self): return []
    class FakeDesktop:
        def window(self, handle=None): return FakeWindow()
    monkeypatch.setattr(executor_module, "_get_desktop_class", lambda: FakeDesktop, raising=False)
    result = cap.execute(ToolCall("i1", "inspect_window", {"hwnd": 10}))
    assert result.success is True
    assert result.output["application_session"]["state"] == "ready"
    assert result.output["blocking_dialog"] is None


def test_screen_perception_prefers_tracked_window_for_same_application(monkeypatch):
    ctx = DesktopContext(current_application="WhatsApp", target_hwnd=900)
    perception = ScreenPerception(desktop_context=ctx)
    fake_window = object()

    class FakeDesktop:
        def window(self, handle):
            assert handle == 900
            return SimpleNamespace(exists=lambda timeout=0.2: True, is_visible=lambda: True)

    monkeypatch.setattr("advi.capabilities.desktop.perception._get_desktop_class", lambda: lambda backend="uia": FakeDesktop())
    result = perception._find_window("WhatsApp application window")
    assert result.found is True
    assert result.method == "uia_tracked_window"


def test_vision_prefers_exact_tracked_hwnd(monkeypatch):
    ctx = DesktopContext(current_application="YouTube Music", target_hwnd=901)
    vision = VisionPerception(desktop_context=ctx)
    tracked = {"title": "YouTube Music", "left": 10, "top": 20, "right": 1010, "bottom": 700}
    monkeypatch.setattr(vision, "_tracked_window_info", lambda hwnd: tracked if hwnd == 901 else None)
    assert vision._find_relevant_window("YouTube Music") == tracked


def test_context_router_preserves_application_session():
    router = ContextRouter(max_context_chars=6000)
    ctx = DesktopContext(current_application="Telegram", target_hwnd=12, target_pid=34)
    session = ctx.start_application_session("Telegram")
    session.bind_window(hwnd=12, pid=34, title="Telegram", process_name="Telegram.exe", state=ApplicationSessionState.READY)
    packet = router.build(observation=ctx.observation_snapshot())
    payload = packet.sections
    assert payload["observation"]["application_session"]["state"] == "ready"


def test_wait_contract_exposes_condition_fields():
    contract = get_action_contract("wait")
    names = set(contract.parameters)
    assert {"condition", "timeout", "application", "duration"}.issubset(names)
