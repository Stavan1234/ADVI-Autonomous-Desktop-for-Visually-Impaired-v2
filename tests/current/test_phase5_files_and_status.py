"""Files, status honesty and provider recovery, from the 2026-10-14 evening log."""
from __future__ import annotations

from pathlib import Path

import pytest

from advi.agent.contracts import TaskState, TaskStatus
from advi.agent.session import AgentSession
from advi.capabilities.files import bulk
from advi.capabilities.files.executor import FileCapability
from advi.providers.errors import LLMRequestError
from advi.tools.contracts import ToolCall

from test_phase0_integrity import _ScriptedProvider, _session, _text


@pytest.fixture()
def journal(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    monkeypatch.setattr(bulk, "journal_path", lambda: path)
    return path


def _files(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text(name)


def _cap(monkeypatch, tmp_path):
    cap = FileCapability()
    monkeypatch.setattr(cap, "resolve_path", lambda value: Path(value) if Path(value).is_absolute() else tmp_path / value)
    return cap


# --- bulk operations ---

def test_move_all_pdfs_in_one_call_and_undo(tmp_path, monkeypatch, journal):
    # Log turn 16: one move_file per PDF; the task died after the first file.
    downloads = tmp_path / "Downloads"
    _files(downloads, "a.pdf", "b.PDF", "notes.txt", "photo.jpg")
    cap = _cap(monkeypatch, tmp_path)

    moved = cap.execute(ToolCall("m", "move_files", {"source": str(downloads), "destination": str(downloads / "PDF"), "extensions": ["pdf"]}))

    assert moved.success and moved.verified and moved.output["moved_count"] == 2
    assert sorted(p.name for p in (downloads / "PDF").iterdir()) == ["a.pdf", "b.PDF"]
    assert (downloads / "notes.txt").exists()
    assert "undo" in moved.human_readable

    undone = cap.execute(ToolCall("u", "undo_file_operation", {}))
    assert undone.success and undone.verified
    assert (downloads / "a.pdf").exists() and not (downloads / "PDF").exists()


def test_name_clashes_are_renamed_never_overwritten(tmp_path, monkeypatch, journal):
    downloads = tmp_path / "Downloads"
    _files(downloads, "a.pdf")
    _files(downloads / "PDF", "a.pdf")
    cap = _cap(monkeypatch, tmp_path)
    cap.execute(ToolCall("m", "move_files", {"source": str(downloads), "destination": str(downloads / "PDF"), "extensions": "pdf"}))
    assert sorted(p.name for p in (downloads / "PDF").iterdir()) == ["a (2).pdf", "a.pdf"]


def test_organize_by_type_leaves_unknown_types_and_skips_partial_downloads(tmp_path, monkeypatch, journal):
    downloads = tmp_path / "Downloads"
    _files(downloads, "cv.pdf", "song.mp3", "setup.exe", "clip.mp4", "data.weird", "big.zip.crdownload")
    cap = _cap(monkeypatch, tmp_path)

    result = cap.execute(ToolCall("o", "organize_folder", {"folder": str(downloads)}))

    assert result.success and result.output["moved_count"] == 4
    assert (downloads / "Documents" / "cv.pdf").exists() and (downloads / "Installers" / "setup.exe").exists()
    assert (downloads / "data.weird").exists() and (downloads / "big.zip.crdownload").exists()
    assert "left in place" in result.human_readable


def test_undo_with_nothing_recorded_says_so(tmp_path, monkeypatch, journal):
    cap = _cap(monkeypatch, tmp_path)
    result = cap.execute(ToolCall("u", "undo_file_operation", {}))
    assert not result.success and "no file operation" in result.error.casefold()


def test_create_folder_that_exists_is_success(tmp_path, monkeypatch):
    # Log turn 17: "try again" failed because the folder from the first try existed.
    (tmp_path / "PDF").mkdir()
    cap = _cap(monkeypatch, tmp_path)
    result = cap.execute(ToolCall("c", "create_folder", {"path": str(tmp_path / "PDF")}))
    assert result.success and result.verified and result.metadata["already_exists"]


def test_find_file_returns_labelled_partial_matches(tmp_path, monkeypatch):
    # Log turn 12: "here I am to worship" vs "HERE I AM TO WORSHIP - Hillsong (karaoke version).mp4".
    videos = tmp_path / "Videos"
    _files(videos, "HERE I AM TO WORSHIP - Hillsong (karaoke version).mp4", "other.mp4")
    cap = FileCapability()
    monkeypatch.setattr(cap, "_search_roots_for_scope", lambda scope, paths: [videos])
    result = cap.execute(ToolCall("f", "find_file", {"name": "here I am to worship", "scope": "videos"}))
    assert [Path(p).name for p in result.output] == ["HERE I AM TO WORSHIP - Hillsong (karaoke version).mp4"]
    assert result.metadata["match"] == "partial"


# --- status honesty ---

def _bare_session(goal, last_tool, *, verified=True):
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal=goal, task_id="t", status=TaskStatus.IN_PROGRESS)
    session.task.record_tool_outcome(name=last_tool, success=True, verified=verified, verification_status="verified" if verified else "uncertain", summary="")
    session.last_user_message = goal
    session.last_tool = last_tool
    session.last_tool_success = True
    session._task_observation = lambda: None
    return session


def test_verified_skills_complete_their_task():
    # Log turns 2, 11, 19: a sent WhatsApp message / playing video reported "couldn't verify".
    assert _bare_session("open whatsapp web, and send hello to New Chapter", "whatsapp_send_message")._has_verified_terminal_evidence()
    assert _bare_session("can you play here i am to worship video", "play_media")._has_verified_terminal_evidence()
    assert not _bare_session("send hello to Joel on whatsapp and then play a song", "whatsapp_send_message")._has_verified_terminal_evidence()


def test_first_step_of_a_bigger_request_is_not_completion():
    # Log turn 9: two folders created, nothing moved, reported done.
    assert not _bare_session("create a folder called PDF and move all these pdf files into it", "create_folder")._has_verified_terminal_evidence()
    assert _bare_session("create a folder called PDF in downloads", "create_folder")._has_verified_terminal_evidence()
    assert _bare_session("organize my downloads by file type", "organize_folder")._has_verified_terminal_evidence()


def test_a_reply_without_actions_is_not_overridden():
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="open word and write a speech", task_id="t", status=TaskStatus.UNCERTAIN)
    assert session._authoritative_task_response() is None


def test_stub_content_for_a_written_piece_is_refused():
    # Log turn 13: a "sermon" PPT whose whole content was "Beautiful sermon."
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="create a ppt called Amazing_Grace and write beautiful sermon in it", task_id="t")
    refused = session._too_short_for_request(ToolCall("c", "create_file", {"path": "Amazing_Grace.pptx", "content": "Beautiful sermon."}))
    assert refused is not None and "sermon" in refused.error
    ok = session._too_short_for_request(ToolCall("c", "create_file", {"path": "x.pptx", "content": "word " * 150}))
    assert ok is None


def test_cancel_with_nothing_running_never_reaches_the_model():
    # Log turn 25: "cancel all active task" with no task made the model close Word.
    provider = _ScriptedProvider([])
    session, engine = _session(provider, {"desktop_control": {"close_application"}})
    for phrase in ("cancel all active task", "cancel all the current active tasks", "stop everything"):
        assert "nothing to cancel" in session.respond(phrase).text.casefold() or session.respond(phrase).text == "Okay."
    assert provider.offered == [] and engine.calls == []


# --- provider rejection recovery ---

def test_a_rejected_undeclared_call_runs_without_another_round():
    # Log: 9 rejections of real tools ("not in request.tools") each cost a round.
    rejection = LLMRequestError("attempted to call tool 'create_folder' which was not in request.tools")
    rejection.failed_call = {"name": "create_folder", "arguments": {"path": "PDF"}}
    provider = _ScriptedProvider([rejection, _text("Created the PDF folder.")])
    session, engine = _session(provider, {"file_management": {"create_folder", "find_file"}, "desktop_control": {"click"}})

    response = session.respond("make a new directory named PDF please")

    assert [call.name for call in engine.calls] == ["create_folder"]
    assert len(provider.offered) == 2  # the rejected request + the tool-result round; no resend
    assert response.text == "Created the PDF folder."


# --- media control ---

def test_bare_stop_after_playback_pauses_without_the_model():
    from test_phase0_integrity import _tool_call

    provider = _ScriptedProvider([_tool_call("play_media", {"query": "Overflow"}, "c1"), _text("Playing Overflow.")])
    session, engine = _session(provider, {"browser_control": {"play_media", "media_control"}})

    session.respond("play overflow on youtube")  # the AI handles the request
    asked = len(provider.offered)
    assert session.respond("stop").text  # local; the scripted provider has no more replies

    assert [call.name for call in engine.calls] == ["play_media", "media_control"]
    assert engine.calls[-1].arguments == {"action": "pause"}
    assert len(provider.offered) == asked  # "stop" itself needed no AI request


def test_pause_and_skip_requests_offer_media_control():
    from types import SimpleNamespace
    from advi.tools.contracts import ToolSpec

    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.tools = SimpleNamespace(model_tools=lambda: [ToolSpec("media_control", "m", "browser_control"), ToolSpec("navigate", "n", "browser_control")])
    for phrase in ("pause the music", "skip this song", "stop the video", "resume"):
        offered = {t["function"]["name"] for t in session._select_model_tools(phrase)}
        assert "media_control" in offered, phrase


# --- safe deletes ---

def test_delete_goes_through_the_recycle_bin(tmp_path, _no_real_recycle_bin):
    target = tmp_path / "old.txt"
    target.write_text("x")
    result = FileCapability().execute(ToolCall("d", "delete_file", {"path": str(target)}))
    assert result.success and "Recycle Bin" in result.human_readable
    assert _no_real_recycle_bin == [str(target)]


def test_when_the_recycle_bin_is_unavailable_nothing_is_deleted(tmp_path, monkeypatch):
    target = tmp_path / "keep.txt"
    target.write_text("x")
    folder = tmp_path / "keep_dir"
    (folder / "inner").mkdir(parents=True)
    monkeypatch.setattr(FileCapability, "_trash", staticmethod(lambda path: (False, "drive E has no Recycle Bin")))
    cap = FileCapability()

    file_result = cap.execute(ToolCall("d", "delete_file", {"path": str(target)}))
    folder_result = cap.execute(ToolCall("d", "delete_folder", {"path": str(folder), "recursive": True}))

    assert not file_result.success and "didn't delete" in file_result.error and target.exists()
    assert not folder_result.success and folder.exists()


def test_non_empty_folder_needs_recursive(tmp_path):
    folder = tmp_path / "full"
    (folder / "a.txt").parent.mkdir()
    (folder / "a.txt").write_text("x")
    result = FileCapability().execute(ToolCall("d", "delete_folder", {"path": str(folder)}))
    assert not result.success and "not empty" in result.error and folder.exists()
