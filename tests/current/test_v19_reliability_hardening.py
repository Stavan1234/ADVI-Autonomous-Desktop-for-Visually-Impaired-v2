from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import fitz

from advi.agent.session import AgentSession
from advi.agent.contracts import TaskState, TaskStatus
from advi.capabilities.files.executor import FileCapability
from advi.core.action_contracts import validate_and_normalize_tool_call
from advi.core.verification import ToolVerifier
from advi.tools.contracts import ToolCall, ToolResult


def _verified(name: str, output=None, **metadata):
    return ToolResult(
        call_id=f"{name}-1",
        name=name,
        success=True,
        output=output,
        verified=True,
        verification_status="verified",
        verification_details={"state": "verified"},
        metadata=metadata,
    )


def _new_session() -> AgentSession:
    session = AgentSession.__new__(AgentSession)
    session.pending_confirmation = None
    session._pending_proposal = None
    session._approved_gmail_fallback = None
    session.task = None
    session.last_tool = None
    session.last_tool_success = None
    session.last_tool_summary = None
    session.last_user_message = None
    session.last_assistant_message = None
    session._recent_tool_exchanges = []
    session._last_completed_goal = None
    session._last_completed_artifacts = {}
    session.watchdog = SimpleNamespace(begin_task=lambda: None)
    return session


def test_verbose_cancel_ends_pending_confirmation_locally():
    session = _new_session()
    session.task = TaskState(goal="send email", task_id="t1", status=TaskStatus.AWAITING_CONFIRMATION)
    session.pending_confirmation = ToolCall("c1", "email_send", {"draft_id": "d1", "to": "a@example.com"})
    result = session._resolve_pending_confirmation("ahhh no cancel the email task. I don't want to send email.")
    assert result and "cancelled" in result.text.lower()
    assert session.pending_confirmation is None
    assert session.task.status == TaskStatus.CANCELLED


def test_read_only_pending_email_question_does_not_mutate_draft(monkeypatch):
    session = _new_session()
    session.task = TaskState(goal="send email", task_id="t1", status=TaskStatus.AWAITING_CONFIRMATION)
    session.pending_confirmation = ToolCall("c1", "email_send", {"draft_id": "d1"})
    calls = []

    def fake_run(call, approved=False):
        calls.append(call)
        if call.name == "email_draft_read":
            return _verified("email_draft_read", SimpleNamespace(recipient="a@example.com", subject="Hello", body="Body"))
        raise AssertionError(f"unexpected mutation: {call.name}")

    monkeypatch.setattr(session, "_run_tool", fake_run)
    result = session._resolve_pending_confirmation("first read out the email to me")
    assert result and "Subject: Hello" in result.text and "Body: Body" in result.text
    assert [call.name for call in calls] == ["email_draft_read"]
    assert session.pending_confirmation is not None
    assert session.task.status == TaskStatus.AWAITING_CONFIRMATION


def test_email_body_edit_changes_only_body_and_preserves_recipient_subject(monkeypatch):
    session = _new_session()
    session.task = TaskState(goal="send email", task_id="t1", status=TaskStatus.AWAITING_CONFIRMATION)
    pending = ToolCall("c1", "email_send", {"draft_id": "d1", "to": "a@example.com", "subject": "Old subject", "body": "Old body"})
    session.pending_confirmation = pending
    calls = []

    def fake_run(call, approved=False):
        calls.append(call)
        if call.name == "email_draft_read":
            return _verified("email_draft_read", SimpleNamespace(recipient="a@example.com", subject="Old subject", body="Old body"))
        if call.name == "email_draft_update":
            assert call.arguments == {
                "draft_id": "d1", "to": "a@example.com", "subject": "Old subject", "body": "New body"
            }
            return _verified("email_draft_update")
        raise AssertionError(call.name)

    monkeypatch.setattr(session, "_run_tool", fake_run)
    result = session._resolve_pending_confirmation('change the body to "New body"')
    assert result and "Updated the draft" in result.text
    assert [call.name for call in calls] == ["email_draft_read", "email_draft_update"]
    assert session.pending_confirmation.arguments["subject"] == "Old subject"
    assert session.pending_confirmation.arguments["body"] == "New body"


def test_pending_file_content_slot_binds_followup_to_original_target(monkeypatch):
    session = _new_session()
    monkeypatch.setattr(session, "_record", lambda *args, **kwargs: None)
    started = session._start_pending_file_input(
        "Inside ADVI_TEST create a file called notes.txt containing:"
    )
    assert started and "notes.txt" in started.text
    assert session.task is not None
    assert session.task.status == TaskStatus.AWAITING_INPUT
    assert session.task.context["pending_input_path"].endswith("Desktop/ADVI_TEST/notes.txt")

    executed = []
    monkeypatch.setattr(
        session,
        "_run_tool",
        lambda call: (executed.append(call) or _verified("create_file", str(call.arguments))),
    )
    result = session._consume_pending_file_input("ADVI filesystem test.")
    assert result and "Created" in result.text
    assert executed[0].name == "create_file"
    assert executed[0].arguments["path"].endswith("Desktop/ADVI_TEST/notes.txt")
    assert executed[0].arguments["content"] == "ADVI filesystem test."
    assert session.task.status == TaskStatus.COMPLETED


def test_pending_youtube_music_proposal_yes_runs_without_provider(monkeypatch):
    session = _new_session()
    session.task = TaskState(goal="open YouTube Music application and play Never Say Never", task_id="t1")
    session.last_user_message = session.task.goal
    session._pending_proposal = {
        "kind": "youtube_music_browser_fallback",
        "app": "YouTube Music",
        "query": "Never Say Never",
        "url": "https://music.youtube.com/",
        "original_request": session.task.goal,
    }
    executed = []

    def fake_run(call, approved=False):
        executed.append(call.name)
        return _verified(call.name)

    monkeypatch.setattr(session, "_run_tool", fake_run)
    monkeypatch.setattr(session, "_maybe_execute_youtube_first_result", lambda result: _verified("click_web_element"))
    result = session._resolve_pending_proposal("yes")
    assert result and "started playback" in result.text.lower()
    assert executed == ["navigate", "search"]
    assert session._pending_proposal is None


def test_explicit_application_request_preflights_find_then_open(monkeypatch):
    session = _new_session()
    session.last_user_message = "open YouTube Music application from my laptop, search for \"Never Say Never\" and play it"
    session.task = TaskState(goal=session.last_user_message, task_id="t1")
    calls = []

    def fake_run(call, approved=False):
        calls.append(call.name)
        return _verified(call.name, {"application": "YouTube Music", "found": True})

    monkeypatch.setattr(session, "_run_tool", fake_run)
    handled = session._handle_explicit_desktop_application_request(session.last_user_message)
    assert handled is False  # compound request continues into the normal agent loop
    assert calls == ["find_application", "open_application"]
    assert session.task.context["preopened_application"] == "YouTube Music"


def test_provider_era_tool_aliases_resolve():
    session = _new_session()
    from advi.tools.registry import ToolRegistry
    registry = SimpleNamespace(
        get_action_spec=lambda name: SimpleNamespace(action=name) if name in {"navigate", "open_file", "type_text"} else None,
        get_registered_action_spec=lambda name: SimpleNamespace(action=name) if name in {"navigate", "open_file", "type_text"} else None,
    )
    session.tools = ToolRegistry(registry)
    assert session.tools.runtime_name("browser_open") == "navigate"
    assert session.tools.runtime_name("repo_browser.open_file") == "open_file"
    assert session.tools.runtime_name("keyboard_type") == "type_text"


def test_failed_task_cannot_be_overridden_by_model_success_text():
    session = _new_session()
    session.task = TaskState(goal="create document", task_id="t1", status=TaskStatus.IN_PROGRESS)
    session.task.failed_tools.append("create_file")
    session.last_tool_summary = "File already exists: India_Report.docx"
    session._record = lambda *args, **kwargs: None
    from advi.providers.base import LLMResponse
    result = session._continue_execution(LLMResponse(text="Created India_Report.docx successfully.", provider="fake", model="fake"), [])
    assert "couldn't complete" in result.text.lower()
    assert "already exists" in result.text.lower()


def test_recent_listing_returns_modified_metadata_sorted(tmp_path: Path):
    cap = FileCapability()
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    import os, time
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    result = cap._execute_list_files(ToolCall(
        "l1", "list_files", {
            "directory": str(tmp_path), "sort_by": "modified_time", "descending": True,
            "include_metadata": True, "limit": 10,
        }
    ))
    assert result.success
    assert result.output["files"][0]["name"] == "new.txt"
    assert result.output["files"][0]["modified_at"]
    assert result.output["files"][0]["modified_epoch"] >= result.output["files"][1]["modified_epoch"]


def test_structured_document_verification_uses_content_anchors(tmp_path: Path):
    path = tmp_path / "report.pdf"
    document = fitz.open()
    for text in [
        "Alpha heading and introduction with useful context.",
        "Renderer footer inserted between sections.",
        "Beta section explains the core architecture clearly.",
        "Another footer line between page blocks.",
        "Gamma conclusion summarizes the requested report.",
    ]:
        page = document.new_page()
        page.insert_text((50, 80), text, fontsize=12)
    document.save(path)
    document.close()

    expected = (
        "Alpha heading and introduction with useful context.\n"
        "Beta section explains the core architecture clearly.\n"
        "Gamma conclusion summarizes the requested report."
    )
    action = ToolCall("v1", "create_file", {"path": str(path), "content": expected, "pages": 5})
    result = ToolResult("v1", "create_file", True, output=str(path), verified=False, verification_status="uncertain", metadata={"resolved_path": str(path)})
    verified = ToolVerifier().verify(action, result)
    assert verified.success is True
    assert verified.verified is True
    assert verified.verification_details["content_matches"] is True


def test_existing_matching_file_is_idempotently_verified(tmp_path: Path):
    cap = FileCapability()
    path = tmp_path / "same.txt"
    path.write_text("hello world", encoding="utf-8")
    first = cap._execute_create_file(ToolCall("c1", "create_file", {"path": str(path), "content": "hello world"}))
    assert first.success is False
    assert first.verified is False
    checked = ToolVerifier().verify(ToolCall("c1", "create_file", {"path": str(path), "content": "hello world"}), first)
    assert checked.success is True
    assert checked.verified is True
    assert checked.metadata["already_exists"] is True


def test_blank_email_subject_is_valid_in_contract():
    call = ToolCall("e1", "email_send", {"to": "a@example.com", "subject": "", "body": "hello"})
    normalized, errors = validate_and_normalize_tool_call(call)
    assert errors == []
    assert normalized is not None


def test_local_file_content_search_avoids_provider(monkeypatch):
    session = _new_session()
    calls = []
    def fake_run(call, approved=False):
        calls.append(call)
        return _verified("search_file_content", [
            {"path": "C:/Docs/a.txt", "line": 4},
            {"path": "C:/Docs/b.txt", "line": 9},
        ])
    monkeypatch.setattr(session, "_run_tool", fake_run)
    result = session._handle_local_file_content_search('Find any file containing the words "artificial intelligence".')
    assert result and "2 matching file result" in result.text
    assert calls[0].name == "search_file_content"
    assert calls[0].arguments["query"] == "artificial intelligence"
    assert session.task.status == TaskStatus.COMPLETED


def test_local_recent_download_listing_is_sorted_and_metadata_aware(monkeypatch):
    session = _new_session()
    calls = []
    payload = {
        "files": [
            {"name": "new.pdf", "modified_at": "2026-09-29T01:00:00+05:30"},
            {"name": "old.pdf", "modified_at": "2026-09-28T01:00:00+05:30"},
        ],
        "truncated": False,
    }
    monkeypatch.setattr(session, "_run_tool", lambda call, approved=False: (calls.append(call) or _verified("list_files", payload)))
    result = session._handle_local_recent_downloads("Which files in Downloads were modified recently?")
    assert result and "most recently modified" in result.text
    assert calls[0].arguments["scope"] == "downloads"
    assert calls[0].arguments["sort_by"] == "modified_time"
    assert calls[0].arguments["descending"] is True
    assert session.task.status == TaskStatus.COMPLETED


def test_local_email_from_contact_resolves_message_not_address(monkeypatch):
    session = _new_session()
    calls = []
    def fake_run(call, approved=False):
        calls.append(call)
        if call.name == "contact_lookup":
            return _verified("contact_lookup", [{"name": "David Almeida", "email": "05davidalmeida@gmail.com"}])
        if call.name == "email_read":
            assert call.arguments == {"query": "from:05davidalmeida@gmail.com", "limit": 1}
            return _verified("email_read", [{"subject": "Hello", "date": "today", "body": "Hi Stavan"}])
        raise AssertionError(call.name)
    monkeypatch.setattr(session, "_run_tool", fake_run)
    result = session._handle_local_email_from_contact("Find the email from David.")
    assert result and "Subject: Hello" in result.text and "Hi Stavan" in result.text
    assert [c.name for c in calls] == ["contact_lookup", "email_read"]


def test_common_desktop_compound_request_preopens_known_app():
    assert AgentSession._extract_common_desktop_app_request("Open Calculator and calculate 125 multiplied by 37.") == "Calculator"
    assert AgentSession._extract_common_desktop_app_request('Open Notepad and type "Hello ADVI".') == "Notepad"


def test_preopened_app_exposes_interaction_tools(monkeypatch):
    session = _new_session()
    session.tools = SimpleNamespace(model_tools=lambda: [])
    session.task = TaskState(goal="Open Calculator and calculate 125 multiplied by 37.", task_id="t1")
    session.task.set_context("preopened_application", "Calculator")
    # Use the real selector against a registry-like stub that advertises the relevant actions.
    specs = {name: SimpleNamespace(name=name, to_openai_tool=lambda n=name: {"function": {"name": n}}) for name in ["type_text","press_key","hotkey","read_screen","list_windows","inspect_window","focus_window","click","wait"]}
    session.tools.model_tools = lambda: list(specs.values())
    tools = session._select_model_tools(session.task.goal)
    names = {t["function"]["name"] for t in tools}
    assert {"type_text", "press_key", "read_screen"}.issubset(names)


def test_get_rid_of_folder_exposes_delete_folder():
    session = _new_session()
    specs = {name: SimpleNamespace(name=name, to_openai_tool=lambda n=name: {"function": {"name": n}}) for name in ["find_file","delete_folder"]}
    session.tools = SimpleNamespace(model_tools=lambda: list(specs.values()), get=lambda name, **kwargs: specs.get(name))
    tools = session._select_model_tools("get rid of the College Stuff folder")
    names = {t["function"]["name"] for t in tools}
    assert "delete_folder" in names


def test_missing_provider_alias_is_normalized_before_retry():
    session = _new_session()
    spec = SimpleNamespace(to_openai_tool=lambda: {"function": {"name": "navigate"}})
    session.tools = SimpleNamespace(runtime_name=lambda name: "navigate" if name == "browser_open" else name, get=lambda name, **kwargs: spec if name == "navigate" else None)
    tools = session._tools_with_missing_tool([], "browser_open")
    assert tools == [{"function": {"name": "navigate"}}]
