"""Phase 0 integrity fixes, each tied to a failure in the 30 Sep - 13 Oct runtime log."""
from __future__ import annotations

from types import SimpleNamespace

from advi.agent import AgentSession
from advi.agent.contracts import TaskState
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.providers.errors import LLMRateLimitError
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
from advi.tools.catalog import REQUEST_TOOLS_NAME, build_request_tools_spec, parse_requested_tools
from advi.tools.contracts import ToolResult, ToolSpec


class _ScriptedProvider(LLMProvider):
    """Replays scripted responses and records the tool names offered each call."""

    def __init__(self, script):
        self.script = list(script)
        self.offered: list[list[str]] = []
        self.descriptions: list[dict[str, str]] = []

    @property
    def name(self):
        return "scripted"

    @property
    def model(self):
        return "scripted-model"

    def chat(self, messages, *, purpose="chat"):
        return LLMResponse(text="ok", provider="scripted", model="scripted-model")

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.offered.append([t["function"]["name"] for t in tools])
        self.descriptions.append({t["function"]["name"]: t["function"]["description"] for t in tools})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class _RecordingEngine:
    def __init__(self):
        self.calls: list[ToolCall] = []

    def execute_tool_call(self, call, *, approval_granted=False):
        self.calls.append(call)
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            success=True,
            verified=True,
            verification_status="verified",
            human_readable=f"{call.name} done",
        )


def _session(provider, actions_by_capability):
    registry = CapabilityRegistry()
    for capability, actions in actions_by_capability.items():
        registry.register(Capability(
            name=capability,
            description=capability,
            supported_actions=set(actions),
            status=CapabilityStatus.AVAILABLE,
            handler=object(),
        ))
    tool_registry = ToolRegistry(registry)
    engine = _RecordingEngine()
    session = AgentSession(
        [provider],
        tool_registry=tool_registry,
        tool_runner=ToolRunner(engine, ToolGuard(tool_registry), tool_registry),
        tool_guard=ToolGuard(tool_registry),
    )
    return session, engine


def _tool_call(name, arguments, call_id):
    return LLMResponse(
        text="",
        provider="scripted",
        model="scripted-model",
        tool_calls=(LLMToolCall(call_id=call_id, name=name, arguments=arguments),),
    )


def _text(value):
    return LLMResponse(text=value, provider="scripted", model="scripted-model")


# --- request_tools: routing omissions must never become capability denials ---

def test_request_tools_loads_an_unrouted_capability_and_keeps_it_for_the_task():
    # Log prompt 76: "I meant in pdf create this speech" -> create_file was not
    # declared and ADVI claimed it could not create PDFs.
    provider = _ScriptedProvider([
        _tool_call(REQUEST_TOOLS_NAME, {"tools": ["create_file"]}, "c1"),
        _tool_call("create_file", {"path": "speech.pdf", "content": "Be brave."}, "c2"),
        _text("Created speech.pdf."),
    ])
    session, engine = _session(provider, {
        "desktop_control": {"open_application", "click"},
        "file_management": {"create_file", "find_file"},
    })

    session.respond("I meant in pdf create this speech")

    # Intent routing misses this word order, which is exactly the failure.
    assert "create_file" not in provider.offered[0]
    assert REQUEST_TOOLS_NAME in provider.offered[0]
    assert "create_file" in provider.descriptions[0][REQUEST_TOOLS_NAME]
    # After loading, the tool stays declared on every later round of the task.
    assert "create_file" in provider.offered[1]
    assert "create_file" in provider.offered[2]
    # The meta-tool is surface management; it never reaches the executor.
    assert [call.name for call in engine.calls] == ["create_file"]
    assert session.task.context["loaded_tools"] == ["create_file"]


def test_request_tools_rejects_unknown_names_without_side_effects():
    provider = _ScriptedProvider([
        _tool_call(REQUEST_TOOLS_NAME, {"tools": ["launch_rocket"]}, "c1"),
        _text("I can't do that."),
    ])
    session, engine = _session(provider, {"desktop_control": {"open_application", "click"}})

    session.respond("make the rocket fly")

    assert engine.calls == []
    assert session.task.context.get("loaded_tools", []) == []


def test_plain_conversation_gets_no_meta_tool():
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.tools = SimpleNamespace(model_tools=lambda: [], list=lambda **_: [ToolSpec("find_file", "find", "file_management")])
    assert session._select_model_tools("what is the capital of India") == []


def test_catalog_lists_only_tools_not_already_exposed():
    specs = [
        ToolSpec("create_file", "c", "file_management"),
        ToolSpec("find_file", "f", "file_management"),
        ToolSpec("email_send", "e", "email"),
    ]
    meta = build_request_tools_spec(specs, exposed=["find_file"])
    assert meta is not None
    assert "create_file" in meta.description and "email_send" in meta.description
    assert "find_file" not in meta.description
    assert build_request_tools_spec(specs, exposed=["create_file", "find_file", "email_send"]) is None


def test_requested_tool_names_are_normalized_and_bounded():
    assert parse_requested_tools({"tools": "create_file, open_file"}) == ["create_file", "open_file"]
    assert parse_requested_tools({"tools": ["a", "a", " b "]}) == ["a", "b"]
    assert len(parse_requested_tools({"tools": [str(i) for i in range(20)]})) == 8
    assert parse_requested_tools({}) == []


def test_completed_local_app_launch_reports_the_app_instead_of_crashing():
    # _respond_turn referenced an undefined name `app` on this path (NameError).
    provider = _ScriptedProvider([])
    session, engine = _session(provider, {"desktop_control": {"find_application", "open_application"}})

    response = session.respond("open the notepad application")

    assert [call.name for call in engine.calls] == ["find_application", "open_application"]
    assert response.text.startswith("Opened ")
    assert provider.offered == []


# --- completion evidence: a verified result must be able to complete its task ---

def test_real_verifier_output_for_a_pdf_counts_as_terminal_evidence(tmp_path):
    # Log prompts 24, 37, 63: verified PDFs/PPTX ended "couldn't verify" because
    # the verifier's mode string was missing from the terminal-evidence check.
    import fitz

    from advi.core.execution_strategy import infer_execution_strategy, terminal_evidence_matches
    from advi.core.verification import ToolVerifier

    pdf_path = tmp_path / "Engineering.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Engineering overview.")
    document.save(str(pdf_path))
    document.close()
    action = ToolCall(call_id="c1", name="create_file", arguments={"path": str(pdf_path)})
    verified = ToolVerifier().verify(
        action, ToolResult(call_id="c1", name="create_file", success=True, output=str(pdf_path))
    )
    assert verified.verified is True

    goal = "create a ten page pdf about Engineering"
    assert terminal_evidence_matches(infer_execution_strategy(goal), "create_file", verified, goal)


def test_verified_folder_creation_and_memory_writes_complete_the_task():
    # Log prompts 2, 13, 26 ended "uncertain" after verified writes.
    from advi.agent.contracts import TaskStatus

    for tool, goal in (
        ("create_folder", "create a folder called ADVI-TEST on the desktop"),
        ("memory_remember", "remember that my team is Anurodh, Joel and me"),
    ):
        session = AgentSession.__new__(AgentSession)
        session.task = TaskState(goal=goal, task_id="t1", status=TaskStatus.IN_PROGRESS)
        session.last_user_message = goal
        session.last_tool = tool
        session.last_tool_success = True
        session._task_observation = lambda: None
        assert session._has_verified_terminal_evidence(), tool


# --- protected parameters: the user's literal guards the name, not the folder ---

def _task_session(task):
    session = AgentSession.__new__(AgentSession)
    session.task = task
    return session


def test_called_filename_is_extracted_without_its_description():
    # Log prompt 15 executed path "a pdf called speech.pdf" on the Desktop.
    assert AgentSession._extract_explicit_file_target(
        "make a long speech, create a pdf called speech.pdf inside that folder"
    ) == "speech.pdf"
    assert AgentSession._extract_explicit_file_target("open Admit Card.pdf") == "Admit Card.pdf"
    assert AgentSession._extract_explicit_file_target("open the Text Analysis.pdf") == "Text Analysis.pdf"


def test_model_path_that_adds_a_folder_to_the_users_filename_is_kept():
    task = TaskState(goal="create a pdf called speech.pdf inside that folder", task_id="t1")
    task.protect_parameters({"path": "speech.pdf"}, operation_intent="create_file")
    resolved = r"C:\Users\HP\Desktop\ADVI-TEST\speech.pdf"

    call = _task_session(task)._apply_protected_task_parameters(
        ToolCall(call_id="1", name="create_file", arguments={"path": resolved, "content": "x"})
    )

    assert call.arguments["path"] == resolved


def test_model_path_with_a_different_filename_is_still_overridden():
    task = TaskState(goal="create speech.pdf", task_id="t1")
    task.protect_parameters({"path": "speech.pdf"}, operation_intent="create_file")

    call = _task_session(task)._apply_protected_task_parameters(
        ToolCall(call_id="1", name="create_file", arguments={"path": r"C:\Users\HP\Desktop\talk.pdf"})
    )

    assert call.arguments["path"] == "speech.pdf"


def test_protected_full_path_from_a_reference_always_wins():
    task = TaskState(goal="open that pdf", task_id="t1")
    task.protect_parameters({"path": r"C:\Users\HP\Desktop\ADVI-AI.pdf"}, operation_intent="open_file")

    call = _task_session(task)._apply_protected_task_parameters(
        ToolCall(call_id="1", name="open_file", arguments={"path": r"C:\Users\HP\Documents\ADVI-AI.pdf"})
    )

    assert call.arguments["path"] == r"C:\Users\HP\Desktop\ADVI-AI.pdf"


# --- routing: chat-app messages are not email ---

def test_whatsapp_send_a_message_does_not_expose_email_tools():
    # Log prompt 62 offered email_send/email_draft_create for a WhatsApp task.
    names = [
        "open_application", "find_application", "list_windows", "inspect_window", "type_text",
        "press_key", "click", "wait", "email_draft_create", "email_send", "contact_lookup",
    ]
    capability = {name: ("email" if name.startswith("email") or name == "contact_lookup" else "desktop_control") for name in names}
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.tools = SimpleNamespace(model_tools=lambda: [ToolSpec(n, n, capability[n]) for n in names])

    offered = [t["function"]["name"] for t in session._select_model_tools(
        'open whatsapp, search Anurodh, send a message "Hello"'
    )]

    assert "open_application" in offered
    assert not {"email_send", "email_draft_create"} & set(offered)


# --- local system queries: no provider round for device facts ---

def test_terse_and_natural_device_status_phrases_route_locally():
    # Log prompts 41-47: four "laptop status" rephrasings and "storage" were refused.
    check = AgentSession._is_local_system_request
    assert check("laptop status") == "get_system_status"
    assert check("give me laptop status") == "get_system_status"
    assert check("how is my laptop") == "get_system_status"
    assert check("how much storage do I have?") == "get_system_info"
    assert check("storage") == "get_system_info"
    assert check("check battery level") == "get_battery_status"


def test_actions_that_mention_disks_or_status_are_not_system_queries():
    check = AgentSession._is_local_system_request
    assert check("copy it to disk") is None
    assert check("send an email to David with my system status") is None
    assert check("status") is None


# --- stop and diagnosis never wait on the language provider ---

def test_bare_stop_with_no_task_is_answered_locally():
    provider = _ScriptedProvider([])
    session, _engine = _session(provider, {"desktop_control": {"open_application"}})

    for phrase in ("stop", "shut up", "be quiet."):
        assert session.respond(phrase).text == "Okay."

    assert provider.offered == []


def test_provider_failure_is_named_and_explained_locally_on_follow_up():
    # Log prompts 17-22: six turns of "I couldn't respond just now" with no cause.
    provider = _ScriptedProvider([
        LLMRateLimitError("Rate limit reached: tokens per day (TPD) limit exceeded"),
    ])
    session, _engine = _session(provider, {"desktop_control": {"open_application"}})

    first = session.respond("open notepad and type hello")
    assert "quota" in first.text

    follow_up = session.respond("what is the problem?")
    assert "quota" in follow_up.text
    assert "open notepad and type hello" in follow_up.text
    assert len(provider.offered) == 1


# --- no-progress detection: unchanged screens after actions end the loop ---

def test_progress_monitor_flags_unchanged_screens_only_after_actions():
    from advi.core import progress

    state = progress.new_state()
    chat_list = "WhatsApp Chats Search or start a new chat Anurodh 9:41 PM Joel 8:02 PM"

    assert progress.record_observation(state, "read_screen", chat_list) is None  # first look
    assert progress.record_observation(state, "read_screen", chat_list) is None  # no action between
    progress.record_action(state)
    verdict = progress.record_observation(state, "read_screen", chat_list.replace("9:41", "9:42"))
    assert verdict["screen_changed"] is False and verdict["stalls"] == 1

    progress.record_action(state)
    verdict = progress.record_observation(state, "read_screen", "Anurodh online Type a message Hello")
    assert verdict["screen_changed"] is True and verdict["stalls"] == 0
    assert progress.record_observation(state, "find_file", "x") is None


def test_whatsapp_style_loop_stops_honestly_when_the_screen_never_changes():
    # Log prompt 62: type/hotkey/read_screen repeated until the 25-call limit.
    unchanged = {"text": "WhatsApp Chats Search or start a new chat Anurodh Joel"}

    class _ScreenEngine(_RecordingEngine):
        def execute_tool_call(self, call, *, approval_granted=False):
            result = super().execute_tool_call(call, approval_granted=approval_granted)
            if call.name == "read_screen":
                return ToolResult(
                    call_id=call.call_id, name=call.name, success=True, verified=True,
                    verification_status="verified", output=unchanged,
                )
            return result

    script = [_tool_call("read_screen", {}, "r0")]
    for i in range(1, 6):
        script.append(_tool_call("type_text", {"text": f"Anurodh {i}"}, f"t{i}"))
        script.append(_tool_call("read_screen", {}, f"r{i}"))
    provider = _ScriptedProvider(script + [_text("unreachable")])

    registry = CapabilityRegistry()
    registry.register(Capability(
        name="desktop_control", description="desktop",
        supported_actions={"read_screen", "type_text", "open_application"},
        status=CapabilityStatus.AVAILABLE, handler=object(),
    ))
    tool_registry = ToolRegistry(registry)
    engine = _ScreenEngine()
    session = AgentSession(
        [provider],
        tool_registry=tool_registry,
        tool_runner=ToolRunner(engine, ToolGuard(tool_registry), tool_registry),
        tool_guard=ToolGuard(tool_registry),
        max_tool_rounds=40,
    )

    response = session.respond("read the screen and type Anurodh into the search box")

    assert "didn't change what's on screen" in response.text
    assert session.task.context["terminal_reason"] == "no_progress"
    # Three stalls: 1 initial read + 3 x (type, read) = 7 calls instead of the call limit.
    assert len(engine.calls) == 7
