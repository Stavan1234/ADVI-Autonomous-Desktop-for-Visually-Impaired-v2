from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from advi.agent.contracts import TaskState, TaskStatus
from advi.agent.session import AgentSession
from advi.capabilities.files.executor import FileCapability
from advi.core.verification import ToolVerifier
from advi.providers.base import LLMProvider, LLMResponse
from advi.tools.contracts import ToolCall, ToolResult, VerificationStatus


class _Provider(LLMProvider):
    @property
    def name(self):
        return "test"

    @property
    def model(self):
        return "test"

    def chat(self, messages, *, purpose="chat"):
        return LLMResponse(text="ok", provider=self.name, model=self.model)

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        return LLMResponse(text="ok", provider=self.name, model=self.model)


def _verified_open_folder(call: ToolCall, path: str) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        name=call.name,
        success=True,
        output=path,
        human_readable=f"Opened folder '{Path(path).name}'.",
        verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={"path": path, "opened_kind": "folder", "focused": True},
        metadata={"path": path, "resolved_path": path, "open_requested": True, "opened_kind": "folder", "focused": True},
    )


def test_list_files_verifier_accepts_structured_listing_payload():
    action = ToolCall("l", "list_files", {"scope": "downloads", "extension": "pdf"})
    result = ToolResult(
        call_id="l",
        name="list_files",
        success=True,
        output={
            "directory": r"C:\Users\HP\Downloads",
            "files": [{"name": "one.pdf", "path": r"C:\Users\HP\Downloads\one.pdf"}],
            "folders": [],
        },
        human_readable="Found 1 file.",
        verified=False,
        verification_status=VerificationStatus.UNCERTAIN,
        metadata={"directory": r"C:\Users\HP\Downloads"},
    )

    checked = ToolVerifier().verify(action, result)

    assert checked.success is True
    assert checked.verified is True
    assert checked.verification_details["valid_listing"] is True
    assert checked.verification_details["count"] == 1


def test_recent_folder_alias_survives_a_new_file_artifact():
    task = TaskState(goal="open this folder", task_id="t1")
    task.record_artifacts({"file_path": r"C:\Users\HP\Downloads\doremon\verse.txt"})
    session = AgentSession.__new__(AgentSession)
    session.task = task
    session._last_completed_artifact_aliases = {
        "last_folder": r"C:\Users\HP\Downloads\doremon"
    }

    session._resolve_recent_reference("open this folder")

    assert task.operation_intent == "open_folder"
    assert task.current_target["value"] == r"C:\Users\HP\Downloads\doremon"
    assert task.protected_parameters["path"] == r"C:\Users\HP\Downloads\doremon"


def test_known_folder_reference_opens_locally_without_llm():
    path = r"C:\Users\HP\Downloads\doremon"
    task = TaskState(goal="open this folder", task_id="t1")
    task.record_artifacts({"folder": path})
    session = AgentSession.__new__(AgentSession)
    session.task = task
    session.last_tool = None
    session.last_tool_success = None
    session.last_tool_summary = None
    session._last_completed_artifact_aliases = {}
    session._ensure_active_task = lambda: task
    calls = []
    session._run_tool = lambda call: (calls.append(call) or _verified_open_folder(call, path))

    response = session._handle_local_folder_reference("open this folder which you created")

    assert response is not None
    assert "doremon" in response.text
    assert len(calls) == 1
    assert calls[0].name == "open_folder"
    assert calls[0].arguments["path"] == path
    assert session.task.status == TaskStatus.COMPLETED


def test_recent_read_about_query_does_not_request_filesystem_tools():
    from advi.capabilities.registry import CapabilityRegistry
    from advi.tools import ToolRegistry

    session = AgentSession.__new__(AgentSession)
    session.tools = ToolRegistry(CapabilityRegistry())
    session.task = None
    session._last_read_content = "This PDF is a school certificate and marksheet."
    session.last_user_message = "tell me what that pdf is about"

    tools = session._select_model_tools(session.last_user_message)

    assert tools == []


def test_pdf_visual_sanity_checks_all_requested_pages(tmp_path: Path):
    path = tmp_path / "ten_pages.pdf"
    content = "\n\n".join(f"Section {i}: ADVI document content." for i in range(1, 11))
    created, parser = FileCapability._create_structured_document(path, content, pages=10)
    assert created is True
    assert parser == "pymupdf-polished"

    action = ToolCall("p", "create_file", {"path": str(path), "content": content, "pages": 10, "style": "beautiful"})
    result = ToolResult(
        call_id="p",
        name="create_file",
        success=True,
        output=str(path),
        human_readable="created",
        verified=True,
        verification_status=VerificationStatus.VERIFIED,
        metadata={"resolved_path": str(path)},
    )
    checked = ToolVerifier().verify(action, result)

    assert checked.verified is True
    sanity = checked.verification_details["visual_sanity"]
    assert sanity["document_page_count"] == 10
    assert sanity["checked_page_count"] == 10
    assert len(sanity["checked_pages"]) == 10


def test_wrong_folder_complaint_reopens_last_known_folder_locally():
    path = r"C:\Users\HP\Downloads\doremon"
    task = TaskState(goal="open this folder", task_id="t1", status=TaskStatus.COMPLETED)
    session = AgentSession.__new__(AgentSession)
    session.task = task
    session.last_tool = "open_folder"
    session.last_tool_success = True
    session.last_tool_summary = "opened home"
    session._last_completed_artifact_aliases = {"last_folder": path}
    session._ensure_active_task = lambda: task
    calls = []
    session._run_tool = lambda call: (calls.append(call) or _verified_open_folder(call, path))

    response = session._handle_local_folder_reference("I guess you didn't open the doremon folder; you opened the home folder")

    assert response is not None
    assert calls and calls[0].name == "open_folder"
    assert calls[0].arguments["path"] == path
    assert task.status == TaskStatus.COMPLETED
