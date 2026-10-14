from types import SimpleNamespace

from advi.agent.session import AgentSession
from advi.agent.contracts import TaskStatus
from advi.providers.groq import _rate_limit_details, _build_rate_limit_error
from advi.tools.contracts import ToolSpec, ToolCall, ToolResult


class _Registry:
    def model_tools(self):
        names = [
            "open_application", "list_windows", "inspect_window", "focus_window", "new_document",
            "close_application", "type_text", "press_key", "hotkey", "click", "double_click", "right_click", "scroll",
            "read_screen", "save_document", "save_document_as", "close_document", "close_window",
            "list_applications", "find_application", "wait",
            "find_file", "read_file", "open_file", "open_folder", "list_files", "search_file_content",
            "create_file", "save_file", "append_file", "replace_file_text", "create_folder",
            "delete_file", "delete_folder", "copy_file", "move_file", "rename_file",
            "copy_folder", "move_folder", "rename_folder",
            "navigate", "search", "inspect_web_page", "read_web_page", "click_web_element", "type_web_element", "go_back", "go_forward", "reload_page", "research_web",
            "mcp_list_tools", "mcp_call",
            "email_read", "email_draft_create", "email_draft_read", "email_draft_update", "email_send", "email_reply",
            "contact_lookup", "memory_search", "memory_remember", "memory_forget", "david_fallback",
        ]
        caps = {
            "open_application": "desktop_control", "list_windows": "desktop_control", "inspect_window": "desktop_control", "focus_window": "desktop_control", "new_document": "desktop_control",
            "type_text": "desktop_control", "press_key": "desktop_control", "hotkey": "desktop_control", "click": "desktop_control", "double_click": "desktop_control", "right_click": "desktop_control", "scroll": "desktop_control",
            "read_screen": "desktop_control", "save_document": "desktop_control", "save_document_as": "desktop_control", "close_document": "desktop_control", "close_window": "desktop_control", "list_applications": "desktop_control", "find_application": "desktop_control", "wait": "desktop_control",
        }
        for n in names:
            if n not in caps:
                if n in {"navigate", "search", "inspect_web_page", "read_web_page", "click_web_element", "type_web_element", "go_back", "go_forward", "reload_page", "research_web"}:
                    caps[n] = "browser_control"
                elif n.startswith("email_"):
                    caps[n] = "email"
                elif n in {"contact_lookup", "memory_search", "memory_remember", "memory_forget"}:
                    caps[n] = "memory"
                elif n == "david_fallback":
                    caps[n] = "david_fallback"
                elif n in {"mcp_list_tools", "mcp_call"}:
                    caps[n] = "mcp"
                else:
                    caps[n] = "file_management"
        from advi.tools.contracts import ToolSpec
        return [ToolSpec(n, n.replace("_", " "), caps[n]) for n in names]


def _session_for_tools():
    session = AgentSession.__new__(AgentSession)
    session.tools = _Registry()
    return session


def _names(tools):
    return [t["function"]["name"] for t in tools]


def test_casual_turn_gets_no_tools():
    assert _names(_session_for_tools()._select_model_tools("hello", None)) == []


def test_file_turn_gets_search_surface_without_fallback_by_default():
    names = _names(_session_for_tools()._select_model_tools("find Passbook.pdf on my computer", None))
    assert names == ["find_file"]
    assert "email_read" not in names


def test_email_turn_gets_email_send_surface_without_stale_browser_tools():
    names = _names(_session_for_tools()._select_model_tools("send an email to David", None))
    assert "email_send" in names
    assert "contact_lookup" in names
    assert "email_read" not in names
    assert "open_application" not in names
    assert not any(name in names for name in {"navigate", "search", "click_web_element", "type_web_element", "inspect_web_page", "read_web_page"})


def test_groq_tpd_error_is_daily_with_retry_hint():
    scope, retry_after, detail = _rate_limit_details(Exception(
        "Rate limit reached on tokens per day (TPD): Limit 200000. Please try again in 24m42.192s."
    ))
    assert scope == "project_daily"
    assert retry_after and 1480 < retry_after < 1490
    assert "daily" in detail.lower()


def test_groq_tpm_error_is_rolling_limit():
    scope, retry_after, detail = _rate_limit_details(Exception(
        "Rate limit reached on tokens per minute (TPM): Please try again in 3.495s."
    ))
    assert scope == "rate_limit"
    assert retry_after and 3.4 < retry_after < 3.6
    assert "minute" in detail.lower()


def test_file_create_surface_is_small():
    names = _names(_session_for_tools()._select_model_tools("create a file called report.txt", None))
    assert len(names) <= 2
    assert "create_file" in names or "david_fallback" in names


def test_browser_surface_is_small():
    names = _names(_session_for_tools()._select_model_tools("open YouTube and search for a song", None))
    assert len(names) <= 7
    assert "navigate" in names


def test_email_confirmation_uses_draft_metadata():
    session = AgentSession.__new__(AgentSession)
    session.task = SimpleNamespace(artifacts={"to": "david@example.com", "subject": "Good night"})
    call = type("Call", (), {"name": "email_send", "arguments": {"draft_id": "draft-1"}})()
    text = session._confirmation_description(call)
    assert "david@example.com" in text
    assert "Good night" in text


def test_natural_file_creation_phrase_selects_create_file():
    names = _names(_session_for_tools()._select_model_tools("create a file called report.txt", None))
    assert names == ["create_file"]


def test_inbox_read_does_not_expose_send_stack():
    names = _names(_session_for_tools()._select_model_tools("check my inbox", None))
    assert "email_read" in names
    assert "email_send" not in names
    assert "email_draft_create" not in names




def test_specific_application_lookup_exposes_find_application():
    names = _names(_session_for_tools()._select_model_tools("locate the WhatsApp application and open it", None))
    assert "find_application" in names
    assert "open_application" in names
    assert "david_fallback" not in names


def test_application_name_followup_gets_native_discovery_without_david():
    names = _names(_session_for_tools()._select_model_tools("try locating it, maybe its name is whatsapp desktop", None))
    assert "list_applications" in names
    assert "open_application" in names
    assert "david_fallback" not in names

def test_word_writing_exposes_small_interaction_cluster():
    names = _names(_session_for_tools()._select_model_tools("open Word and write hello", None))
    assert "open_application" in names
    assert "new_document" in names
    assert "type_text" in names
    assert "click" in names
    assert "double_click" in names
    assert "right_click" in names
    assert "david_fallback" not in names




def test_failed_task_can_resume_on_explicit_retry():
    session = AgentSession.__new__(AgentSession)
    task = SimpleNamespace(
        status=type("Status", (), {"value": "failed"})(),
        goal="create demo.txt and write hello",
        artifacts={"path": "C:/Users/test/Desktop/demo.txt"},
        context={"terminal_reason": "provider_failure: LLMRateLimitError"},
        waiting_for=None,
    )
    # Use the real enum so the branch is exercised exactly as production uses it.
    from advi.agent.contracts import TaskStatus
    task.status = TaskStatus.FAILED
    session.task = task
    session.pending_confirmation = None
    session._prepare_user_turn("try again")
    assert session.task is task
    assert session.task.status == TaskStatus.IN_PROGRESS
    assert session.task.goal == "create demo.txt and write hello"
    assert session.task.artifacts["path"].endswith("demo.txt")
    assert "terminal_reason" not in session.task.context


def test_groq_rate_limit_error_is_human_readable():
    error = _build_rate_limit_error(Exception("Rate limit reached on tokens per day (TPD): Limit 200000. Please try again in 16m17.184s."))
    assert "daily token quota is exhausted" in str(error).lower()
    assert "16 minutes" in str(error)


def test_mcp_turn_gets_only_mcp_tools():
    session = _session_for_tools()
    session.tools.model_tools = lambda: [
        ToolSpec("mcp_list_tools", "list mcp tools", "mcp"),
        ToolSpec("mcp_call", "call mcp tool", "mcp"),
    ]
    names = _names(session._select_model_tools("use MCP tools", None))
    assert names == ["mcp_list_tools", "mcp_call"]


def test_file_search_can_match_exact_stem_without_extension():
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from advi.capabilities.files.executor import FileCapability
    from advi.tools.contracts import ToolCall

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ADVI presentation.pptx").write_text("x", encoding="utf-8")
        cap = FileCapability()
        cap.desktop_dir = root
        result = cap._execute_find_file(ToolCall(call_id="1", name="find_file", arguments={"name":"ADVI presentation","scope":"desktop","limit":10}))
        assert result.success
        assert result.output == [str(root / "ADVI presentation.pptx")]


def test_browser_click_request_exposes_only_browser_pointer_tools():
    names = _names(_session_for_tools()._select_model_tools("click the first video on YouTube", None))
    assert "click_web_element" in names
    assert "click" not in names
    assert "double_click" not in names


def test_explicit_david_request_exposes_david_surface():
    names = _names(_session_for_tools()._select_model_tools("use David fallback to do this", None))
    assert "david_fallback" in names


def test_create_pdf_request_exposes_create_file():
    names = _names(_session_for_tools()._select_model_tools("create a PDF called verse.pdf", None))
    assert names == ["create_file"]


def test_successful_david_fallback_clears_prior_failures():
    from advi.agent.contracts import TaskState, TaskStatus
    session = AgentSession.__new__(AgentSession)
    task = TaskState(goal="play Praises on YouTube", task_id="task-1")
    task.failed_tools.extend(["search", "click_web_element"])
    session.task = task
    for name in list(task.failed_tools):
        task.record_tool_outcome(name=name, success=False, verified=False, verification_status="failed", summary="failed")
    # Exercise the same recovery rule used by the production runner.
    for name in list(task.failed_tools):
        task.mark_recovered(name)
    task.mark_recovered("david_fallback")
    assert task.failed_tools == []
    assert task.recovered_tools[-1] == "david_fallback"


def test_explicit_david_email_continuation_uses_supplied_address_without_llm_routing():
    from types import SimpleNamespace
    from advi.agent.contracts import TaskState, TaskStatus
    from advi.tools.contracts import ToolResult

    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(
        goal="send an email to David saying testing email, but use David fallback method",
        task_id="task-david",
        status=TaskStatus.AWAITING_INPUT,
        waiting_for="the email address for this contact",
        context={"fallback_requested": True},
    )
    session.last_user_message = session.task.goal
    session.last_assistant_message = None
    session.last_tool = "contact_lookup"
    session.last_tool_success = True
    session.last_tool_summary = None
    session.pending_confirmation = None
    session._approved_gmail_fallback = None
    session._record = lambda *args, **kwargs: None
    session._requires_confirmation = lambda _call: False
    calls = []

    def run_tool(call, approved=False):
        calls.append((call.name, dict(call.arguments), approved))
        return ToolResult(
            call_id=call.call_id, name=call.name, success=True,
            output=[], human_readable="David completed the fallback email task.",
            verified=True, verification_status="verified",
        )

    session._run_tool = run_tool
    response = session._resume_explicit_david_email("05davidalmeida@gmail.com")
    assert response is not None
    assert calls and calls[0][0] == "david_fallback"
    assert "05davidalmeida@gmail.com" in calls[0][1]["request"]
    assert session.task.status == TaskStatus.COMPLETED



def test_referenced_file_followup_exposes_native_open_tools():
    session = _session_for_tools()
    session._last_completed_artifacts = {"path": r"C:\Users\test\Downloads\sunday.txt"}
    names = _names(session._select_model_tools("open that file physically", None))
    assert "open_file" in names
    assert "find_file" in names


def test_open_any_pdf_exposes_filesystem_read_and_open_tools():
    names = _names(_session_for_tools()._select_model_tools("open any pdf on my laptop and read a few lines", None))
    assert "find_file" in names
    assert "open_file" in names
    assert "read_file" in names


def test_list_folder_request_exposes_file_listing():
    names = _names(_session_for_tools()._select_model_tools("tell me all the root folders and files available in Documents folder. list it", None))
    assert "list_files" in names


def test_create_file_followup_can_open_created_file():
    names = _names(_session_for_tools()._select_model_tools("open the file I just created", "create_file"))
    assert "open_file" in names

def test_playlist_word_does_not_trigger_filesystem_tools():
    names = _names(_session_for_tools()._select_model_tools("open a playlist", None))
    assert "list_files" not in names


def test_non_file_artifacts_do_not_make_open_it_a_file_followup():
    session = _session_for_tools()
    session._last_completed_goal = "send_email"
    session._last_completed_artifacts = {"recipient_email": "person@example.com", "message_id": "abc123"}
    names = _names(session._select_model_tools("open that", None))
    assert "open_file" not in names
    assert "find_file" not in names


def test_natural_pdf_listing_request_exposes_listing_tool():
    names = _names(_session_for_tools()._select_model_tools("In Downloads list all pdf files", None))
    assert "list_files" in names


def test_natural_pdf_listing_with_limit_exposes_listing_tool():
    names = _names(_session_for_tools()._select_model_tools("now in download folder list down top 5 pdf present there", None))
    assert "list_files" in names


def test_named_pdf_open_exposes_find_and_open():
    names = _names(_session_for_tools()._select_model_tools("open 1023146_Stavan.pdf from Downloads", None))
    assert "find_file" in names
    assert "open_file" in names
    assert "list_files" not in names


def test_unnamed_pdf_open_exposes_listing_and_open():
    names = _names(_session_for_tools()._select_model_tools("open a pdf from download folder", None))
    assert "list_files" in names
    assert "open_file" in names


def test_plain_application_open_uses_semantic_open_action():
    names = _names(_session_for_tools()._select_model_tools("open Calculator", None))
    assert names == ["open_application"]


def test_playlist_is_browser_not_filesystem_or_application():
    names = _names(_session_for_tools()._select_model_tools("open a playlist", None))
    assert "list_files" not in names
    assert "open_application" not in names
    assert "navigate" in names


def test_explicit_mcp_turn_can_add_david_after_mcp_failure():
    session = _session_for_tools()
    session.tools.model_tools = lambda: [
        ToolSpec("mcp_list_tools", "list mcp tools", "mcp"),
        ToolSpec("mcp_call", "call mcp tool", "mcp"),
        ToolSpec("david_fallback", "david fallback", "david"),
    ]
    from advi.agent.contracts import TaskState, TaskStatus
    session.task = TaskState(goal="use MCP to find the PDF in Downloads", task_id="mcp-task", status=TaskStatus.IN_PROGRESS)
    session.task.context["fallback_available"] = True
    session.last_tool_success = False
    names = _names(session._select_model_tools("use MCP to find the PDF in Downloads", "mcp_call"))
    assert "mcp_call" not in names
    assert "david_fallback" in names



def test_list_files_supports_extension_and_limit_filters(tmp_path):
    from advi.capabilities.files.executor import FileCapability
    from advi.tools.contracts import ToolCall

    (tmp_path / "one.pdf").write_text("a", encoding="utf-8")
    (tmp_path / "two.pdf").write_text("b", encoding="utf-8")
    (tmp_path / "three.pdf").write_text("c", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("d", encoding="utf-8")
    cap = FileCapability()
    cap.desktop_dir = tmp_path
    result = cap._execute_list_files(ToolCall(
        "l1", "list_files", {"directory": str(tmp_path), "extension": "pdf", "limit": 2}
    ))
    assert result.success
    assert [item["name"] for item in result.output["files"]] == ["one.pdf", "three.pdf"]
    assert result.output["folders"] == []
    assert result.output["truncated"] is True


def test_mcp_failure_leaves_david_available_for_recovery():
    session = _session_for_tools()
    names = [
        "mcp_list_tools", "mcp_call", "david_fallback",
        "find_file", "list_files", "open_file",
    ]
    session.tools.model_tools = lambda: [ToolSpec(n, n.replace("_", " "), "mcp" if n.startswith("mcp_") else "file") for n in names]
    from advi.agent.contracts import TaskState
    session.task = TaskState(goal="use MCP to open a PDF from Downloads", task_id="mcp-task")
    session.task.context["fallback_available"] = True
    selected = _names(session._select_model_tools("use MCP to open a PDF from Downloads", "mcp_call"))
    assert "mcp_call" in selected
    assert "david_fallback" in selected


def test_actionable_unknown_request_gets_bounded_general_control_surface():
    names = _names(_session_for_tools()._select_model_tools("could you do something unusual on my laptop for me", None))
    assert "find_application" in names
    assert "find_file" in names
    assert "open_file" in names
    assert "click" in names
    assert "navigate" in names
    assert len(names) < 30


def test_generic_search_is_browser_not_filesystem():
    names = _names(_session_for_tools()._select_model_tools("search the web for cozy anime", None))
    assert "search" in names
    assert "find_file" not in names


def test_generic_list_about_files_is_filesystem_but_list_about_topic_is_not():
    names = _names(_session_for_tools()._select_model_tools("list the files in Downloads", None))
    assert "list_files" in names
    names = _names(_session_for_tools()._select_model_tools("list funny movies", None))
    assert "list_files" not in names


def test_artifact_mutation_followup_exposes_native_tools():
    session = _session_for_tools()
    session._last_completed_goal = "create_file"
    session._last_completed_artifacts = {"path": r"C:\Users\test\Downloads\report.txt"}
    names = _names(session._select_model_tools("move it to Documents", None))
    assert "move_file" in names


def test_send_file_followup_exposes_email_surface():
    session = _session_for_tools()
    session._last_completed_goal = "create_file"
    session._last_completed_artifacts = {"path": r"C:\Users\test\Downloads\report.txt"}
    names = _names(session._select_model_tools("send it to David", None))
    assert "email_draft_create" in names
    assert "email_send" in names


def test_failed_mcp_continuation_prefers_david_instead_of_repeating_mcp():
    session = _session_for_tools()
    from advi.agent.contracts import TaskState, TaskStatus
    session.task = TaskState(goal="use MCP to do this", task_id="mcp-task", status=TaskStatus.IN_PROGRESS, context={"fallback_available": True})
    session.last_tool = "mcp_call"
    session.last_tool_success = False
    names = _names(session._select_model_tools("continue", "mcp_call"))
    assert "david_fallback" in names
    assert "mcp_call" not in names



def test_play_music_routes_to_browser_not_filesystem():
    names = _names(_session_for_tools()._select_model_tools("play some music on youtube", None))
    assert "search" in names
    assert "click_web_element" in names
    assert "list_files" not in names


def test_natural_app_close_phrase_gets_close_application():
    names = _names(_session_for_tools()._select_model_tools("please close WhatsApp for me", None))
    assert "close_application" in names
    assert "find_application" in names


def test_open_this_pc_routes_to_filesystem_folder_operation():
    names = _names(_session_for_tools()._select_model_tools("open this PC", None))
    assert "open_folder" in names


def test_unusual_action_phrase_gets_bounded_cross_domain_surface():
    names = _names(_session_for_tools()._select_model_tools("please do something weird on my laptop", None))
    assert "find_application" in names
    assert "find_file" in names
    assert "navigate" in names
    assert len(names) < 30



def test_windows_folder_variants_stay_filesystem_only():
    session = _session_for_tools()
    for phrase in ("open Downloads", "go to my Downloads folder", "open this PC"):
        names = _names(session._select_model_tools(phrase, None))
        assert "open_folder" in names
        assert "open_application" not in names
        assert not any(name in names for name in {"navigate", "search", "click_web_element"})


def test_pdf_listing_phrase_selects_list_files_only():
    names = _names(_session_for_tools()._select_model_tools("list the PDFs in Downloads", None))
    assert names == ["list_files"]


def test_bare_url_navigation_is_browser_only():
    names = _names(_session_for_tools()._select_model_tools("go to example.com", None))
    assert "navigate" in names
    assert "open_application" not in names
    assert "find_file" not in names


def test_open_youtube_is_browser_only():
    names = _names(_session_for_tools()._select_model_tools("open YouTube", None))
    assert "navigate" in names
    assert "open_application" not in names


def test_start_chrome_remains_application_task():
    names = _names(_session_for_tools()._select_model_tools("start Chrome", None))
    assert "open_application" in names
    assert "navigate" not in names


def test_run_spotify_remains_application_task():
    names = _names(_session_for_tools()._select_model_tools("run Spotify", None))
    assert "open_application" in names
    assert "navigate" not in names


def test_application_phrase_with_similarity_is_discoverable():
    names = _names(_session_for_tools()._select_model_tools("open WhatsApp or similar", None))
    assert "find_application" in names
    assert "open_application" in names


def test_focus_application_phrase_uses_small_surface():
    names = _names(_session_for_tools()._select_model_tools("bring Telegram to front", None))
    assert set(names).issubset({"find_application", "list_applications", "list_windows", "inspect_window", "focus_window", "open_application", "wait"})
    assert "focus_window" in names


def test_folder_mutations_use_only_discovery_plus_requested_operation():
    session = _session_for_tools()
    for phrase, op in (
        ("create a folder called Test", "create_folder"),
        ("delete this folder", "delete_folder"),
        ("move the folder Test to Desktop", "move_folder"),
        ("copy the folder Test to Desktop", "copy_folder"),
        ("rename the folder Test to Renamed", "rename_folder"),
    ):
        names = _names(session._select_model_tools(phrase, None))
        assert op in names
        assert len(names) <= 2


def test_office_document_editing_exposes_app_editor_surface():
    names = _names(_session_for_tools()._select_model_tools("edit report.docx and write this paragraph", None))
    assert "open_application" in names
    assert "open_file" in names
    assert "type_text" in names
    assert "save_document" in names
    assert "close_document" in names



def test_plain_application_launch_keeps_initial_surface_small():
    names = _names(_session_for_tools()._select_model_tools("open Calculator", None))
    assert names == ["open_application"]


def test_natural_load_application_verb_uses_semantic_open_action():
    names = _names(_session_for_tools()._select_model_tools("load Calculator", None))
    assert names == ["open_application"]


def test_natural_fetch_local_pdf_exposes_filesystem_access():
    names = _names(_session_for_tools()._select_model_tools("fetch the PDF from Downloads", None))
    assert "open_file" in names or "list_files" in names or "find_file" in names


def test_david_execution_loop_rejects_overlong_plan():
    from advi.fallback.execution_loop import ExecutionLoop
    from advi.fallback.action_plan_schema import Action, ActionPlan
    actions = [Action(action="wait", parameters={"seconds": 0}) for _ in range(41)]
    result = ExecutionLoop().run(ActionPlan(actions=actions))
    assert not result.success
    assert result.metadata.get("reason") == "maximum_action_count_reached"


def test_filename_mutation_without_file_noun_exposes_requested_operation():
    names = _names(_session_for_tools()._select_model_tools("rename report.docx to final.docx", None))
    assert "find_file" in names
    assert "rename_file" in names


def test_browser_followup_inherits_browser_context():
    names = _names(_session_for_tools()._select_model_tools("click the first result", "search"))
    assert "click_web_element" in names
    assert "click" not in names


def test_pending_file_clarification_gets_file_continuation_tools():
    session = _session_for_tools()
    session.task = SimpleNamespace(waiting_for="which matching file to use", goal="find a PDF", context={})
    names = _names(session._select_model_tools("the first one", "find_file"))
    assert "open_file" in names
    assert "read_file" in names


def test_actionable_turn_keeps_known_tools_when_runtime_surface_temporarily_empty():
    """A transient availability snapshot must not create a tool-choice-none LLM turn."""
    from advi.tools.contracts import ToolSpec

    class EmptyButDeclaredRegistry:
        def model_tools(self):
            return []

        def get(self, name, *, include_unavailable=True):
            declared = {
                "find_application": "desktop_control",
                "open_application": "desktop_control",
                "list_files": "file_management",
                "find_file": "file_management",
                "open_file": "file_management",
                "open_folder": "file_management",
            }
            cap = declared.get(name)
            return ToolSpec(name, name.replace("_", " "), cap) if cap else None

    session = AgentSession.__new__(AgentSession)
    session.tools = EmptyButDeclaredRegistry()
    names = _names(session._select_model_tools("open Calculator", None))
    assert names
    assert "open_application" in names

    names = _names(session._select_model_tools("list all PDFs in Downloads", None))
    assert "list_files" in names


def test_known_unavailable_tool_can_still_resolve_to_native_execution_boundary():
    """Tool lookup should recognize a registered action even before its capability is available."""
    from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
    from advi.tools.registry import ToolRegistry

    class Handler:
        SUPPORTED_ACTIONS = {"open_file"}

    cap = Capability(
        name="file_management",
        description="files",
        supported_actions={"open_file"},
        status=CapabilityStatus.UNAVAILABLE,
        handler=Handler(),
        availability_probe=None,
        availability_reason="temporarily unavailable",
    )
    registry = CapabilityRegistry()
    registry.register(cap)
    tools = ToolRegistry(registry)
    spec = tools.get("open_file")
    assert spec is not None
    assert spec.name == "open_file"
    assert tools.runtime_name("open_file") == "open_file"


def test_natural_file_edit_phrase_routes_to_append_file_not_chat():
    names = _names(_session_for_tools()._select_model_tools("append this text to notes.txt", None))
    assert "append_file" in names
    assert "search" not in names


def test_natural_ordinal_pdf_open_routes_to_filesystem():
    names = _names(_session_for_tools()._select_model_tools("open the first PDF in Downloads", None))
    assert "open_file" in names
    assert "list_files" in names
    assert "find_file" in names
    assert "click_web_element" not in names


def test_memory_search_does_not_route_to_browser():
    names = _names(_session_for_tools()._select_model_tools("search my memory for ADVI", None))
    assert "memory_search" in names
    assert not any(name in names for name in {"navigate", "search", "click_web_element", "inspect_web_page"})


def test_keyboard_enter_is_native_key_action():
    names = _names(_session_for_tools()._select_model_tools("press Enter", None))
    assert names == ["press_key"]


def test_keyboard_shortcut_is_native_hotkey_action():
    names = _names(_session_for_tools()._select_model_tools("press Ctrl plus S", None))
    assert names == ["hotkey"]


def test_youtube_play_request_exposes_browser_workflow():
    names = _names(_session_for_tools()._select_model_tools("open YouTube, search for worship music, and play the first video", None))
    assert {"navigate", "search", "inspect_web_page", "click_web_element"}.issubset(names)
    assert "click" not in names
    assert "open_application" not in names


def test_browser_back_followup_stays_in_browser_context():
    names = _names(_session_for_tools()._select_model_tools("go back", "search"))
    assert "go_back" in names
    assert "click_web_element" in names
    assert "click" not in names


def test_replace_phrase_with_greeting_text_still_routes_to_filesystem():
    names = _names(_session_for_tools()._select_model_tools("replace hello with hi in notes.txt", None))
    assert "replace_file_text" in names
    assert "find_file" in names


def test_search_memory_is_narrow_to_retrieval():
    names = _names(_session_for_tools()._select_model_tools("search my memory for ADVI", None))
    assert names == ["memory_search"]


def test_remember_memory_is_narrow_to_write():
    names = _names(_session_for_tools()._select_model_tools("remember that I like ADVI", None))
    assert names == ["memory_remember"]


def test_forget_memory_is_narrow_to_delete():
    names = _names(_session_for_tools()._select_model_tools("forget that memory about ADVI", None))
    assert names == ["memory_forget"]


def test_browser_search_followup_does_not_expose_desktop_typing():
    names = _names(_session_for_tools()._select_model_tools("type my name into the search box", "search"))
    assert "type_web_element" in names
    assert "type_text" not in names
    assert "click" not in names


def test_mcp_request_routes_to_mcp_actions():
    names = _names(_session_for_tools()._select_model_tools("use MCP to list tools", None))
    assert names == ["mcp_list_tools", "mcp_call"]


def test_natural_folder_creation_and_mutation_phrasings_stay_filesystem_only():
    session = _session_for_tools()
    cases = {
        "make a directory called ADVI": {"create_folder"},
        "move the Projects folder to Documents": {"find_file", "move_folder"},
        "copy the Projects folder to Documents": {"find_file", "copy_folder"},
        "rename the Projects folder to ADVI": {"find_file", "rename_folder"},
        "delete the Projects folder": {"find_file", "delete_folder"},
        "write hello to notes.txt": {"find_file", "save_file"},
        "edit notes.txt": {"find_file", "read_file", "replace_file_text"},
        "insert hello into notes.txt": {"find_file", "append_file"},
        "fill in the search box": {"navigate", "search", "inspect_web_page", "click_web_element", "type_web_element"},
        "refresh this page": {"reload_page"},
        "reply to the latest email": {"email_read", "email_reply"},
        "read my inbox and reply to the latest email": {"email_read", "email_reply"},
        "send an email to David": {"contact_lookup", "email_send"},
    }
    for phrase, required in cases.items():
        names = set(_names(session._select_model_tools(phrase, None)))
        assert required.issubset(names), (phrase, sorted(names))
        # A deterministic domain request should not be widened to unrelated desktop/browser families.
        if "folder" in phrase or "notes.txt" in phrase or "email" in phrase:
            assert "open_application" not in names, (phrase, sorted(names))
            assert not ({"navigate", "search", "click_web_element", "type_web_element"} & names), (phrase, sorted(names))


def test_find_file_can_find_a_directory_by_exact_name(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import advi.capabilities.files.executor as files_executor
    target = tmp_path / "Projects"
    target.mkdir()
    paths = SimpleNamespace(
        user_home=tmp_path,
        desktop=tmp_path,
        documents=tmp_path / "Documents",
        downloads=tmp_path / "Downloads",
        pictures=tmp_path / "Pictures",
        videos=tmp_path / "Videos",
        music=tmp_path / "Music",
    )
    monkeypatch.setattr(files_executor, "get_environment_paths", lambda: paths)
    cap = files_executor.FileCapability()
    result = cap.execute(ToolCall("find-dir", "find_file", {"name": "Projects", "scope": "user", "limit": 10}))
    assert result.success
    assert any(str(target) == value for value in result.data)


def test_type_web_element_resolves_open_shadow_root(monkeypatch):
    from advi.capabilities.browser.executor import BrowserCapability
    from advi.capabilities.desktop.context import DesktopContext

    class ShadowCDP:
        def list_tabs(self):
            return [{"id": "tab-1", "type": "page", "url": "https://example.test", "title": "Example"}]

        def evaluate_js(self, tab_id, expression, timeout=5.0):
            if "queryDeep" in expression and "input" in expression:
                return {"success": True, "before": "", "after": "hello", "selector": "input.search", "tag": "input", "role": None}
            return True

    cap = BrowserCapability(context=DesktopContext(target_tab_id="tab-1", target_tab_url="https://example.test"))
    cap.cdp = ShadowCDP()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall("shadow", "type_web_element", {"selector": "input.search", "text": "hello"}))
    assert result.success is True
    assert result.verified is True


def test_set_file_content_routes_to_save_file():
    names = _names(_session_for_tools()._select_model_tools("set notes.txt to hello", None))
    assert names == ["find_file", "save_file"]


def test_focused_browser_routes_human_style_result_click():
    session = _session_for_tools()
    names = _names(session._select_model_tools(
        "click the first result",
        None,
        observation={"active_application": "Chrome", "browser": {"url": "https://www.youtube.com/results"}},
    ))
    assert "click_web_element" in names
    assert "inspect_web_page" in names
    assert "click" not in names


def test_explicit_browser_search_box_does_not_expose_desktop_typing():
    names = _names(_session_for_tools()._select_model_tools("type hello in the search box", None))
    assert "type_web_element" in names
    assert "type_text" not in names
    assert "click" not in names


def test_open_google_is_browser_but_open_chrome_is_desktop_app():
    google_names = _names(_session_for_tools()._select_model_tools("open Google", None))
    chrome_names = _names(_session_for_tools()._select_model_tools("open Google Chrome", None))
    assert "navigate" in google_names
    assert "open_application" not in google_names
    assert "open_application" in chrome_names
    assert "navigate" not in chrome_names


def test_office_generation_phrases_expose_structured_create_file():
    session = _session_for_tools()
    cases = (
        "create a ppt of 10 slides about India",
        "create word doc for an India report",
        "create an excel sheet for expenses",
        "make a presentation about India",
    )
    for request in cases:
        names = _names(session._select_model_tools(request, None))
        assert "create_file" in names, (request, names)



def test_memory_request_exposes_memory_tool_and_not_only_email_tools():
    names = _names(_session_for_tools()._select_model_tools("remember this: David is on my team", None))
    assert "memory_remember" in names


def test_ppt_and_word_create_requests_are_not_limited_to_gui_only_tools():
    session = _session_for_tools()
    for request in ("create a ppt of 10 slides on India", "create word doc for India history"):
        names = _names(session._select_model_tools(request, None))
        assert "create_file" in names
        assert "open_application" not in names


def test_repeated_same_youtube_request_starts_fresh_task_boundary():
    session = _session_for_tools()
    session.task = SimpleNamespace(
        status=TaskStatus.COMPLETED,
        goal='play a praise song on youtube',
        artifacts={},
        context={},
    )
    session.last_tool = "click_web_element"
    session.last_tool_success = True
    session.last_tool_summary = "Playing the selected YouTube video."
    session.pending_confirmation = None
    session._approved_gmail_fallback = None
    session._recent_tool_exchanges = __import__('collections').deque(maxlen=6)
    session._last_completed_goal = None
    session._last_completed_artifacts = {}
    session._prepare_user_turn("play a praise song on youtube")
    assert session.task is None
    assert session.last_tool is None
    assert session.last_tool_success is None


def test_youtube_search_can_auto_select_first_result_for_play_request():
    session = AgentSession.__new__(AgentSession)
    session.last_user_message = "play a praise song on youtube"
    captured = {}
    def fake_run_tool(call):
        captured["call"] = call
        return ToolResult(
            call_id=call.call_id, name=call.name, success=True, output="https://www.youtube.com/watch?v=abc",
            verified=True, verification_status="verified",
            verification_details={"method": "youtube_result_href_navigation"},
        )
    session._run_tool = fake_run_tool
    result = ToolResult(
        call_id="search-1", name="search", success=True, output="praise song",
        verified=True, verification_status="verified",
        verification_details={"result_candidates": [{"result_index":1,"title":"First Praise Song","href":"https://www.youtube.com/watch?v=abc","selector":"a[href*=\"/watch?v=abc\"]"}]},
    )
    auto = session._maybe_execute_youtube_first_result(result)
    assert auto is not None and auto.success and auto.verified
    assert captured["call"].name == "click_web_element"
    assert captured["call"].arguments["selector"] == 'a[href*="/watch?v=abc"]'
