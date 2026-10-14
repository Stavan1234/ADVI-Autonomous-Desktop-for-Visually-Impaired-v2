from __future__ import annotations

from pathlib import Path

from advi.agent import AgentSession
from advi.agent.contracts import TaskState, TaskStatus
from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.desktop.context import DesktopContext
from advi.capabilities.desktop.executor import DesktopCapability
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.core.action_contracts import validate_and_normalize_tool_call
from advi.core.execution_watchdog import WatchdogDecision
from advi.fallback.intent_service import IntentService
from advi.memory.long_term import LongTermMemory
from advi.memory.service import MemoryService
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
from advi.tools.contracts import ToolResult


class SequenceProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "fake-model"

    def chat(self, messages, *, purpose="chat"):
        self.calls.append(("chat", messages))
        return self.responses.pop(0)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.calls.append(("tools", messages, tools))
        return self.responses.pop(0)


class FakeEngine:
    def __init__(self):
        self.calls = []

    def execute_tool_call(self, call, *, approval_granted=False):
        self.calls.append(call)
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            success=True,
            output={"ok": True},
            human_readable=f"Completed {call.name}.",
            verified=True,
            verification_status="verified",
            verification_details={"state": "verified"},
        )


def make_open_app_session(responses, memory=None):
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="desktop_control",
        description="desktop",
        supported_actions={"open_application", "find_application", "list_applications", "list_windows", "inspect_window", "focus_window", "close_window", "close_application", "new_document", "type_text", "press_key", "hotkey", "click", "double_click", "right_click", "scroll", "wait"},
        status=CapabilityStatus.AVAILABLE,
        handler=object(),
    ))
    engine = FakeEngine()
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    session = AgentSession(
        [SequenceProvider(responses)],
        tool_registry=tools,
        tool_runner=ToolRunner(engine, guard),
        tool_guard=guard,
        memory=memory,
    )
    session.local_intents_enabled = False  # exercises the model loop
    return session, engine


class RecordingWatchdog:
    def __init__(self):
        self.begin_calls = []
    def begin_task(self, task_id=None):
        self.begin_calls.append(task_id)
    def before_action(self, action_name, arguments=None):
        return WatchdogDecision(True)
    def after_action(self, *args, **kwargs):
        return None


def test_empty_model_response_closes_task_and_next_new_request_gets_fresh_watchdog():
    session, engine = make_open_app_session([
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("c1", "open_application", {"application": "Notepad"}),)),
        LLMResponse(text="", provider="fake", model="m"),
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("c2", "open_application", {"application": "Calculator"}),)),
        LLMResponse(text="Calculator is open.", provider="fake", model="m"),
    ])
    watchdog = RecordingWatchdog()
    session.watchdog = watchdog

    # First request executes its tool, then the model emits an empty final answer.
    # ADVI should synthesize a truthful response from the verified execution
    # evidence instead of returning a generic failure.
    first = session.respond("Open Notepad")
    assert "usable response" not in first.text
    assert first.text == "Completed open_application."
    assert session.task is not None
    assert session.task.status == TaskStatus.COMPLETED
    assert "terminal_reason" not in session.task.context

    second = session.respond("Open Calculator")
    assert second.text == "Calculator is open."
    assert len(watchdog.begin_calls) == 2
    opened_apps = [
        call.arguments["application"]
        for call in engine.calls
        if call.name == "open_application"
    ]
    assert opened_apps == ["Notepad", "Calculator"]


def test_explicit_desktop_application_compound_request_exposes_open_application():
    session, _ = make_open_app_session([])
    tools = session._select_model_tools("open youtube music application from my laptop, then play Never Say Never")
    names = {tool["function"]["name"] for tool in tools}
    assert "open_application" in names
    assert "find_application" in names


def test_contact_facts_are_persisted_immediately(tmp_path: Path):
    memory = MemoryService(LongTermMemory(tmp_path / "memory.db"))
    session, _ = make_open_app_session([], memory=memory)
    session._persist_explicit_contact_facts(
        "I am Stavan Kalkumbe. My email address is stavan@example.com. "
        "David Almeida (david@example.com) is also on the team."
    )
    assert memory.memory.get_contact("Stavan Kalkumbe").email == "stavan@example.com"
    assert memory.memory.get_contact("David Almeida").email == "david@example.com"
    recall = memory.recall("my email address")
    assert any("stavan@example.com" in statement for statement in recall.statements)


def test_youtube_music_search_discovers_dynamic_play_result():
    cap = BrowserCapability(context=DesktopContext())
    cap.context.set_target_tab("music-tab", url="https://music.youtube.com/", title="YouTube Music")

    class FakeCDP:
        def list_tabs(self):
            return [{"id": "music-tab", "type": "page", "url": self.url, "title": "YouTube Music"}]

        def get_tab_info(self, tab_id):
            return {"id": tab_id, "type": "page", "url": self.url, "title": "YouTube Music"}

        def execute_in_site_search(self, tab_id, query):
            self.url = "https://music.youtube.com/search?q=Never+Say+Never"
            return True

        def evaluate_js(self, tab_id, script):
            if "ytmusic-responsive-list-item-renderer" in script:
                return {"value": [{
                    "result_index": 1,
                    "title": "Never Say Never (feat. Jaden)",
                    "href": "https://music.youtube.com/watch?v=_Z5-P9v3F8w",
                    "selector": 'ytmusic-play-button-renderer[aria-label="Play Never Say Never - Justin Bieber"]',
                    "play_label": "Play Never Say Never - Justin Bieber",
                    "source": "youtube_music_shadow_dom",
                }]}
            return {"value": True}

    cdp = FakeCDP()
    cdp.url = "https://music.youtube.com/"
    cap.cdp = cdp
    result = cap._execute_search(ToolCall("s1", "search", {"query": "Never Say Never"}))
    assert result.success
    candidates = result.verification_details["result_candidates"]
    assert candidates[0]["href"].endswith("_Z5-P9v3F8w")
    assert "ytmusic-play-button-renderer" in candidates[0]["selector"]
    assert "Play Never Say Never - Justin Bieber" in candidates[0]["selector"]


def test_youtube_music_playback_verifier_uses_media_state():
    cap = BrowserCapability(context=DesktopContext())
    cap.context.set_target_tab("music-tab", url="https://music.youtube.com/search?q=Never", title="YouTube Music")

    class FakeCDP:
        def list_tabs(self):
            return [{"id": "music-tab", "type": "page", "url": "https://music.youtube.com/search?q=Never", "title": "YouTube Music"}]

        def evaluate_js(self, tab_id, script):
            if "media=queryDeep" in script:
                return {"value": {"music": True, "playing": True, "paused": False, "currentTime": 2.1, "mediaCount": 1}}
            return {"value": {"clicked": False}}

    cap.cdp = FakeCDP()
    state = cap._ensure_youtube_music_playing("music-tab", timeout=0.2)
    assert state and state["playing"] is True



def test_contact_lookup_backfills_legacy_generic_memory(tmp_path: Path):
    store = LongTermMemory(tmp_path / "memory.db")
    store.remember(
        category="contact",
        key="email",
        value="legacy@example.com",
        statement="Anurodh's email address is legacy@example.com.",
        confidence=0.99,
    )
    from advi.capabilities.memory.executor import MemoryCapability

    cap = MemoryCapability(store)
    result = cap._execute_contact_lookup(ToolCall("c1", "contact_lookup", {"name": "Anurodh"}))
    assert result.success is True
    assert result.metadata["match"] == "memory_backfill"
    assert store.get_contact("Anurodh").email == "legacy@example.com"


def test_failed_native_browser_action_exposes_bounded_recovery_observation(monkeypatch):
    session, _ = make_open_app_session([])
    session.task = TaskState(goal="play Never Say Never on YouTube Music", task_id="task-1")
    observed = ToolResult(
        call_id="obs", name="inspect_web_page", success=True, output=[{"label": "Play Never Say Never"}],
        verified=True, verification_status="verified", verification_details={"method": "dom_interactive_inventory"},
    )
    monkeypatch.setattr(session.tool_runner, "run", lambda *args, **kwargs: observed)
    failed = ToolResult(
        call_id="bad", name="click_web_element", success=False, error="element_not_found",
        verified=False, verification_status="uncertain",
    )
    session._capture_native_recovery_observation(
        ToolCall("bad", "click_web_element", {"selector": "ytmusic-play-button-renderer"}), failed
    )
    assert session.task.context["native_recovery_domain"] == "browser"
    assert session.task.context["native_recovery_observed"] is True
    assert "Play Never Say Never" in session.task.context["native_recovery_observation"]


def test_windows_search_launch_uses_keyboard_activation_and_never_ocr_click(monkeypatch):
    cap = DesktopCapability(context=DesktopContext())
    calls = []

    class FakePyAutoGUI:
        def hotkey(self, *keys): calls.append(("hotkey", keys))
        def write(self, text, interval=0.0): calls.append(("write", text, interval))
        def press(self, key): calls.append(("press", key))

    class Record:
        runtime_type = "pwa"
        command = r"shell:AppsFolder\Brave._crx_music"
        executable = None

    monkeypatch.setattr("advi.capabilities.desktop.executor.os.name", "nt")
    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: FakePyAutoGUI())
    monkeypatch.setattr(cap, "_find_window_candidates", lambda target, exact_only=False: [456])
    monkeypatch.setattr(cap, "_window_process_id", lambda hwnd: 789)
    monkeypatch.setattr(cap, "_window_title", lambda hwnd: "YouTube Music")
    monkeypatch.setattr(cap, "_process_name", lambda pid: "brave.exe")
    monkeypatch.setattr(cap, "_application_window_matches_record", lambda *args, **kwargs: True)
    monkeypatch.setattr(cap, "_remember_window_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(cap, "_activate_window", lambda hwnd: True)
    monkeypatch.setattr(cap, "_is_window_visible", lambda hwnd: True)

    result = cap._windows_search_and_launch("YouTube Music", ToolCall("o1", "open_application", {"application": "YouTube Music"}), record=Record())
    assert result is not None
    assert result.success is True
    assert result.metadata["recovery"] == "windows_search_keyboard"
    assert calls[0][0] == "hotkey"
    assert calls[1][0] == "write"
    assert calls[1][1] == "YouTube Music"
    assert calls[2] == ("press", "enter")


def test_email_draft_create_allows_blank_subject():
    normalized, errors = validate_and_normalize_tool_call(
        ToolCall("e1", "email_draft_create", {"to": "person@example.com", "subject": "", "body": "hello"})
    )
    assert errors == []
    assert normalized is not None
    assert normalized.parameters["subject"] == ""


def test_david_explicit_email_intent_does_not_call_provider():
    provider = SequenceProvider([])
    service = IntentService(provider, "src/advi/fallback/prompts/intent_prompt.txt")
    intent = service.extract_intent(
        'Send an email to person@example.com with subject "Test" and body "Hello there".'
    )
    assert intent.goal == "send_email"
    assert intent.entities["recipient_email"] == "person@example.com"
    assert intent.entities["subject"] == "Test"
    assert intent.entities["content"] == "Hello there"
    assert intent.missing_information == []
    assert provider.calls == []


def test_david_explicit_email_intent_allows_blank_subject():
    provider = SequenceProvider([])
    service = IntentService(provider, "src/advi/fallback/prompts/intent_prompt.txt")
    intent = service.extract_intent(
        'Send an email to person@example.com with subject "" and body "Hello there".'
    )
    assert intent.goal == "send_email"
    assert intent.entities["subject"] == ""
    assert intent.entities["content"] == "Hello there"
    assert intent.missing_information == []
    assert provider.calls == []


def test_email_draft_state_supports_body_edit_and_send_followup():
    from dataclasses import dataclass
    from advi.capabilities.gmail.executor import GmailCapability
    from advi.core.execution_engine import ExecutionEngine

    @dataclass
    class Draft:
        draft_id: str
        recipient: str
        subject: str
        body: str

    class FakeGmail:
        SUPPORTED_ACTIONS = {"email_draft_create", "email_draft_read", "email_draft_update", "email_send"}
        def __init__(self):
            self.draft = Draft("d1", "person@example.com", "Test", "old body")
        def is_available(self): return True
        def ensure_ready(self): return None
        def create_draft(self, *, to, subject, body):
            self.draft = Draft("d1", to, subject, body)
            return self.draft
        def get_draft(self, draft_id): return self.draft if draft_id == self.draft.draft_id else None
        def update_draft(self, *, draft_id, to, subject, body):
            self.draft = Draft(draft_id, to, subject, body)
            return self.draft
        def send_draft(self, draft_id):
            return {"id": "m1"}
        def get_message(self, message_id):
            return {"id": message_id}
        def get_parsed_message(self, message_id):
            return type("Parsed", (), {"recipients": self.draft.recipient, "subject": self.draft.subject})()

    registry = CapabilityRegistry()
    gmail = FakeGmail()
    registry.register(Capability("email", "gmail", gmail.SUPPORTED_ACTIONS, CapabilityStatus.AVAILABLE, handler=GmailCapability(gmail)))
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    runner = ToolRunner(ExecutionEngine(registry=registry), guard, tools)
    session = AgentSession([SequenceProvider([])], tool_registry=tools, tool_runner=runner, tool_guard=guard)
    session.local_intents_enabled = False  # exercises the model loop

    created = session._run_tool(ToolCall("c1", "email_draft_create", {"to": "person@example.com", "subject": "Test", "body": "old body"}))
    assert created.success and created.verified
    assert session._last_email_draft["draft_id"] == "d1"
    assert session._last_email_draft["body"] == "old body"

    edited = session._handle_local_email_draft_followup('change the body to "new body"')
    assert edited is not None
    assert "body" in edited.text.lower()
    assert gmail.draft.body == "new body"
    assert session._last_email_draft["body"] == "new body"

    send_prompt = session._handle_local_email_draft_followup("send the email")
    assert send_prompt is not None
    assert session.pending_confirmation is not None
    assert session.pending_confirmation.name == "email_send"
    assert session.pending_confirmation.arguments["draft_id"] == "d1"
    assert session.task.status == TaskStatus.AWAITING_CONFIRMATION


def test_email_draft_read_followup_is_local_and_speech_friendly():
    from dataclasses import dataclass
    from advi.capabilities.gmail.executor import GmailCapability
    from advi.core.execution_engine import ExecutionEngine

    @dataclass
    class Draft:
        draft_id: str
        recipient: str
        subject: str
        body: str

    class FakeGmail:
        SUPPORTED_ACTIONS = {"email_draft_read"}
        def __init__(self):
            self.draft = Draft("d2", "person@example.com", "Hello", "Please read this aloud.")
        def is_available(self): return True
        def ensure_ready(self): return None
        def get_draft(self, draft_id): return self.draft

    registry = CapabilityRegistry()
    gmail = FakeGmail()
    registry.register(Capability("email", "gmail", gmail.SUPPORTED_ACTIONS, CapabilityStatus.AVAILABLE, handler=GmailCapability(gmail)))
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    runner = ToolRunner(ExecutionEngine(registry=registry), guard, tools)
    session = AgentSession([SequenceProvider([])], tool_registry=tools, tool_runner=runner, tool_guard=guard)
    session.local_intents_enabled = False  # exercises the model loop
    session._last_email_draft = {"draft_id": "d2", "to": "person@example.com", "subject": "Hello", "body": "old"}

    response = session._handle_local_email_draft_followup("read out the email")
    assert response is not None
    assert "person@example.com" in response.text
    assert "Please read this aloud." in response.text
    assert session.task.status == TaskStatus.COMPLETED


def test_verified_intermediate_execution_does_not_complete_compound_task():
    session = AgentSession.__new__(AgentSession)
    from advi.agent.contracts import TaskState, TaskStatus
    session.task = TaskState(
        goal="open Chrome and search for youtube",
        task_id="t-intermediate",
        status=TaskStatus.IN_PROGRESS,
    )
    session.last_tool = "open_application"
    session.last_tool_success = True
    session.last_tool_summary = "Opened Chrome."

    session._finalize_task_status()

    assert session.task.status == TaskStatus.UNCERTAIN
    assert session.task.context["completion_evidence"] == "verified_progress_without_terminal_evidence"


def test_verified_terminal_execution_can_complete_single_step_task():
    session = AgentSession.__new__(AgentSession)
    from advi.agent.contracts import TaskState, TaskStatus
    session.task = TaskState(goal="open Notepad", task_id="t-terminal", status=TaskStatus.IN_PROGRESS)
    session.last_tool = "open_application"
    session.last_tool_success = True
    session.last_tool_summary = "Opened Notepad."

    session._finalize_task_status()

    assert session.task.status == TaskStatus.COMPLETED
    assert session.task.context["completion_evidence"] == "verified_terminal_execution"


def test_empty_verified_terminal_response_uses_execution_evidence():
    session, _ = make_open_app_session([
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("c1", "open_application", {"application": "Notepad"}),)),
        LLMResponse(text="", provider="fake", model="m"),
    ])
    response = session.respond("Open Notepad")
    assert response.text == "Completed open_application."
    assert session.task is not None
    assert session.task.status == TaskStatus.COMPLETED


def test_open_application_verifier_accepts_windows_wrapper_pid_when_identity_matches(monkeypatch):
    import ctypes
    import psutil
    from advi.core.verification import ToolVerifier

    class FakeUser32:
        def IsWindow(self, hwnd): return True
        def IsWindowVisible(self, hwnd): return True
        def GetWindowThreadProcessId(self, hwnd, ptr):
            ptr._obj.value = 12324
            return 1
        def GetWindowTextW(self, hwnd, buffer, size):
            buffer.value = "Untitled - Notepad"
            return len(buffer.value)

    class FakeWindll:
        user32 = FakeUser32()

    class FakeProcess:
        def name(self): return "Notepad.exe"

    monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)
    monkeypatch.setattr(psutil, "Process", lambda pid: FakeProcess())
    verifier = ToolVerifier()
    action = ToolCall("o1", "open_application", {"application": "Notepad"})
    result = ToolResult(
        call_id="o1", name="open_application", success=True, output=12324, verified=False,
        verification_status="uncertain", metadata={"hwnd": 12324, "pid": 4216, "application": "Notepad"},
    )
    checked = verifier.verify(action, result)
    assert checked.verified is True
    assert checked.verification_details["process_match"] is False
    assert checked.verification_details["semantic_process_match"] is True
    assert checked.verification_details["identity_source"] == "process_name"


def test_successful_application_launch_recovers_failed_discovery_step(monkeypatch):
    session, _ = make_open_app_session([])
    session.task = TaskState(goal="open YouTube Music application from my laptop, then play Never Say Never", task_id="app-task")
    lookup = ToolResult(
        call_id="find", name="find_application", success=False,
        error="Application not found in inventory.", verified=False,
        verification_status="failed",
    )
    opened = ToolResult(
        call_id="open", name="open_application", success=True,
        human_readable="Opened application 'YouTube Music'.", verified=True,
        verification_status="verified", metadata={"application": "YouTube Music", "hwnd": 123},
    )
    sequence = iter([lookup, opened])
    monkeypatch.setattr(session, "_run_tool", lambda call, **kwargs: next(sequence))
    handled = session._handle_explicit_desktop_application_request(
        "open YouTube Music application from my laptop, then play Never Say Never"
    )
    assert handled is False
    assert "find_application" not in session.task.failed_tools
    assert "find_application" in session.task.recovered_tools


def test_application_record_preserves_packaged_and_pwa_identity():
    from advi.environment import ApplicationRecord

    whatsapp = ApplicationRecord(
        name="WhatsApp",
        source="windows_start_apps",
        command=r"shell:AppsFolder\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App",
        app_id=r"5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App",
        package_family="5319275A.WhatsAppDesktop_cv1g1gvanyjgm",
        runtime_type="packaged",
    )
    music = ApplicationRecord(
        name="YouTube Music",
        source="windows_start_apps",
        command=r"shell:AppsFolder\Brave._crx_cinhimbnkkghhklpknlkffjgod",
        app_id=r"Brave._crx_cinhimbnkkghhklpknlkffjgod",
        runtime_type="pwa",
    )

    assert whatsapp.runtime_type == "packaged"
    assert whatsapp.package_family.startswith("5319275A.")
    assert whatsapp.identity_key.startswith("aumid:")
    assert music.runtime_type == "pwa"
    assert music.identity_key.startswith("aumid:")


def test_desktop_context_application_session_is_identity_aware():
    from advi.capabilities.desktop.context import ApplicationSessionState, DesktopContext
    from advi.environment import ApplicationRecord

    ctx = DesktopContext()
    record = ApplicationRecord(
        name="YouTube Music",
        source="windows_start_apps",
        command=r"shell:AppsFolder\Brave._crx_music",
        app_id=r"Brave._crx_music",
        runtime_type="pwa",
    )
    session = ctx.start_application_session("YouTube Music", record=record)
    session.bind_window(
        hwnd=123,
        pid=456,
        title="YouTube Music",
        process_name="brave.exe",
        state=ApplicationSessionState.READY,
    )
    snapshot = ctx.observation_snapshot()

    assert snapshot["application_session"]["application"] == "YouTube Music"
    assert snapshot["application_session"]["identity"]["runtime_type"] == "pwa"
    assert snapshot["application_session"]["identity"]["app_id"] == "Brave._crx_music"
    assert snapshot["application_session"]["window"]["hwnd"] == 123
    assert snapshot["application_session"]["state"] == "ready"


def test_application_window_matching_does_not_accept_partial_title_for_launch(monkeypatch):
    capability = DesktopCapability()
    # Hermetic: no real WhatsApp process windows (process identity is checked first).
    monkeypatch.setattr(capability, "_find_windows_for_process", lambda pid: [])
    monkeypatch.setattr(capability, "_find_window_candidates", lambda target, exact_only=False: [99] if exact_only and target.casefold() == "whatsapp" else [])
    assert capability._find_existing_application_window("WhatsApp", None) == 99


def test_application_session_state_can_represent_blocking_dialog():
    from advi.capabilities.desktop.context import ApplicationSessionState, DesktopContext

    ctx = DesktopContext()
    session = ctx.start_application_session("Microsoft Word")
    session.bind_window(
        hwnd=500,
        pid=700,
        title="Document1 - Word",
        process_name="WINWORD.EXE",
        state=ApplicationSessionState.BLOCKED_DIALOG,
        blocking_dialog={"title": "Office Activation"},
        reason="activation dialog is blocking the document",
    )
    assert ctx.application_session is not None
    assert ctx.application_session.state is ApplicationSessionState.BLOCKED_DIALOG
    assert ctx.application_session.blocking_dialog["title"] == "Office Activation"
