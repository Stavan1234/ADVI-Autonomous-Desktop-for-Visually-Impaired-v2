"""ADVI fixes found by the Desktop Suite dev run (2026-10-23), each a general improvement."""
from __future__ import annotations

import zipfile
from types import SimpleNamespace

from advi.agent.contracts import TaskState, TaskStatus
from advi.agent.session import AgentSession
from advi.capabilities.files.executor import FileCapability
from advi.tools.contracts import ToolCall


def _files():
    return FileCapability()


# waa-zip-backup: "Create a zip archive ... from all files in Downloads" had no tool.
def test_zip_files_zips_a_folder_and_reads_it_back(tmp_path):
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "SampleText.txt").write_text("sample", encoding="utf-8")
    (tmp_path / "Downloads" / "notes.md").write_text("# n", encoding="utf-8")
    target = tmp_path / "Downloads" / "DownloadsBackup.zip"
    result = _files().execute(ToolCall("z", "zip_files", {"source": str(tmp_path / "Downloads"), "destination": str(target)}))
    assert result.success and result.verified, result.error
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == ["SampleText.txt", "notes.md"]


def test_zip_never_overwrites(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "out.zip").write_bytes(b"old")
    result = _files().execute(ToolCall("z", "zip_files", {"source": str(tmp_path), "destination": str(tmp_path / "out.zip")}))
    assert not result.success and "already exists" in result.error


# waa-notepad-count: listing stopped at 20 lines and "Found 20" was taken as the count (real: 37).
def test_content_search_counts_every_occurrence_in_a_file(tmp_path):
    path = tmp_path / "largefile.txt"
    lines = [f"line {i}" + (" example" if i < 37 else "") + (" example" if i == 3 else "") for i in range(400)]
    path.write_text("\n".join(lines), encoding="utf-8")
    result = _files().execute(ToolCall("s", "search_file_content", {"query": "example", "path": str(path)}))
    assert "appears 38 time(s) on 37 line(s)" in result.human_readable
    assert "showing the first 20 lines" in result.human_readable
    assert result.verification_details["occurrences"] == 38


# advi-word-pdf: "a PDF named Water" was saved as "Drinking Water.pdf".
def _said(text):
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.last_user_message = text
    return session


def test_spoken_file_name_reaches_word():
    call = ToolCall("w", "word_create_document", {"path": "documents/ADVI Bench/advi-word-pdf.pdf", "content": "# Drinking Water"})
    fixed = _said("Write a short note about drinking water in Word and save it as a PDF named Water in Documents\\ADVI Bench\\advi-word-pdf.")._restore_spoken_file_name(call)
    assert fixed.arguments["name"] == "Water"


def test_spoken_file_name_renames_a_text_file_path():
    call = ToolCall("c", "create_file", {"path": "documents/list.txt", "content": "x"})
    fixed = _said("create a text file named groceries in documents")._restore_spoken_file_name(call)
    assert fixed.arguments["path"] == "documents/groceries.txt"
    same = _said("create a text file named shopping.txt in documents")._restore_spoken_file_name(
        ToolCall("c", "create_file", {"path": "documents/shopping.txt", "content": "x"}))
    assert same.arguments["path"] == "documents/shopping.txt"


def test_word_uses_the_spoken_name_inside_a_folder(tmp_path, monkeypatch):
    from advi.capabilities.office import OfficeCapability

    folder = tmp_path / "advi-word-pdf"
    folder.mkdir()
    saved = []
    cap = OfficeCapability.__new__(OfficeCapability)
    cap._resolve = lambda raw: tmp_path / raw
    cap._build = lambda blocks, target: saved.append(target) or (_ for _ in ()).throw(RuntimeError("stop"))
    for path in ("advi-word-pdf.pdf", "advi-word-pdf/Notes.pdf"):
        cap.execute(ToolCall("w", "word_create_document", {"path": path, "content": "# Drinking Water\n\nText", "name": "Water"}))
    assert [p.relative_to(tmp_path).as_posix() for p in saved] == ["advi-word-pdf/Water.pdf", "advi-word-pdf/Water.pdf"]


# waa-notepad-count: a wrong open_folder on a file, then a verified save, still said "I couldn't complete that".
def _finishing(outcomes, failed):
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="count", task_id="t")
    session.task.tool_outcomes.extend(outcomes)
    side = {"open_folder": "medium", "type_text": "high", "save_file": "high", "list_files": "low"}
    session.tools = SimpleNamespace(get=lambda name: SimpleNamespace(side_effect=side.get(name, "high")))
    return session._superseded_by_later_success(failed)


def test_a_harmless_wrong_step_is_superseded_by_a_verified_finish():
    done = [{"tool": "open_folder", "success": False}, {"tool": "save_file", "success": True, "verified": True}]
    assert _finishing(done, ["open_folder"])


def test_risky_failures_and_look_only_finishes_are_not_superseded():
    done = [{"tool": "type_text", "success": False}, {"tool": "save_file", "success": True, "verified": True}]
    assert not _finishing(done, ["type_text"])
    looked = [{"tool": "open_folder", "success": False}, {"tool": "list_files", "success": True, "verified": True}]
    assert not _finishing(looked, ["open_folder"])


# advi-open-site: "Open wikipedia.org" passed, but the answer was "I couldn't verify the final state".
def _opened(goal, outcomes, last):
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal=goal, task_id="t")
    session.task.status = TaskStatus.IN_PROGRESS
    session.task.tool_outcomes.extend(outcomes)
    session.last_tool, session.last_tool_success, session.last_user_message = last, True, goal
    return session._has_verified_terminal_evidence()


def test_opening_a_site_ends_the_task_even_after_a_look():
    steps = [{"tool": "open_browser", "success": True, "verified": True},
             {"tool": "inspect_web_page", "success": True, "verified": True}]
    assert _opened("Open wikipedia.org.", steps, "inspect_web_page")
    assert _opened("open youtube", steps[:1], "open_browser")
    # More was asked: opening is only step one.
    assert not _opened("open amazon and search speakers", steps, "inspect_web_page")
    assert not _opened("go to bbc and tell me the top story", steps[:1], "open_browser")


def test_counting_requests_get_the_exact_counting_tool():
    source = open("src/advi/agent/session.py", encoding="utf-8").read()
    assert 'add("search_file_content", "find_file")' in source
    assert "never count by reading" in source


def test_superseded_misstep_does_not_block_completion():
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="count example and save it", task_id="t")
    session.task.status = TaskStatus.IN_PROGRESS
    session.task.failed_tools.append("open_folder")
    session.task.tool_outcomes.extend([{"tool": "open_folder", "success": False},
                                       {"tool": "save_file", "success": True, "verified": True}])
    side = {"open_folder": "medium"}
    session.tools = SimpleNamespace(get=lambda name: SimpleNamespace(side_effect=side.get(name, "high")))
    session.last_tool, session.last_tool_success, session.last_user_message = "save_file", True, session.task.goal
    assert session._has_verified_terminal_evidence()


def test_a_file_save_is_not_mistaken_for_a_memory_claim():
    import time as _time

    session = AgentSession.__new__(AgentSession)
    session._turn_started_at = _time.monotonic()
    saved = "I counted 37 instances and I've saved it to example_count.txt."
    assert session._no_unbacked_memory_claim(saved) == saved
    assert "haven't saved" in session._no_unbacked_memory_claim("Got it. I'll remember that.")
    assert "haven't saved" in session._no_unbacked_memory_claim("I've saved that to my memory.")


# All 3 Groq keys hit the per-minute limit and ADVI told the user "try again in about 20 seconds".
def _manager(cooldowns=(), daily=()):
    import time as _time

    from advi.agent.provider_session import ProviderSessionManager

    providers = [SimpleNamespace(name=f"groq-{i}", quota_group=f"g{i}") for i in range(3)]
    manager = ProviderSessionManager(providers)
    now = _time.monotonic()
    for i, seconds in enumerate(cooldowns):
        manager.health_state.cooldown_until[f"g{i}"] = now + seconds
    for i in daily:
        manager.health_state.quota_exhausted_until[f"g{i}"] = now + 3600
    slept, told = [], []
    manager._sleep, manager.wait_notice = slept.append, told.append
    return manager, slept, told


def _limit(text="rate-limited; try again in about 20 seconds", after=20):
    from advi.providers.errors import LLMRateLimitError

    error = LLMRateLimitError(text)
    error.retry_after_seconds = after
    return error


def test_a_short_limit_is_waited_out_for_the_first_free_key_and_retried():
    manager, slept, told = _manager(cooldowns=(18, 9, 22))
    calls = []

    def send():
        calls.append(1)
        if len(calls) == 1:
            raise _limit()
        return "answer"

    assert manager._waiting_out_short_limits(send) == "answer"
    assert len(calls) == 2 and 9 <= slept[0] <= 10 and told == slept


def test_a_daily_quota_or_a_long_limit_is_reported_not_waited():
    import pytest as _pytest

    from advi.providers.errors import LLMRateLimitError

    for manager_args, error in (({"daily": (0, 1, 2)}, _limit()),
                                ({"cooldowns": (90, 90, 90)}, _limit(after=90)),
                                ({}, _limit("the daily quota is used up", after=10))):
        manager, slept, _ = _manager(**manager_args)
        with _pytest.raises(LLMRateLimitError):
            manager._waiting_out_short_limits(lambda: (_ for _ in ()).throw(error))
        assert slept == []


def test_a_second_limit_after_waiting_is_reported():
    import pytest as _pytest

    from advi.providers.errors import LLMRateLimitError

    manager, slept, _ = _manager(cooldowns=(5,))
    with _pytest.raises(LLMRateLimitError):
        manager._waiting_out_short_limits(lambda: (_ for _ in ()).throw(_limit()))
    assert len(slept) == 1  # waited once, never loops


# WebVoyager dev run: the answer to "is the floral bedspread available in blue?" was replaced by a template.
def _ended(status, outcomes):
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="x", task_id="t")
    session.task.status = status
    session.task.tool_outcomes.extend(outcomes)
    session.last_tool_summary = "Extracted 3060 characters from page."
    return session._authoritative_task_response()


def test_a_browsing_answer_is_kept():
    browsed = [{"tool": "search", "success": True, "verified": True},
               {"tool": "read_web_page", "success": True, "verified": True}]
    assert _ended(TaskStatus.UNCERTAIN, browsed) is None
    missed_search = [{"tool": "search", "success": False}, {"tool": "click_web_element", "success": True},
                     {"tool": "read_web_page", "success": True}]
    assert _ended(TaskStatus.FAILED, missed_search) is None


def test_typing_or_a_failed_ending_is_still_reported():
    typed = [{"tool": "type_web_element", "success": True, "verified": False}]
    assert "couldn't verify" in _ended(TaskStatus.UNCERTAIN, typed)
    failed_end = [{"tool": "search", "success": True}, {"tool": "click_web_element", "success": False}]
    assert "couldn't complete" in _ended(TaskStatus.FAILED, failed_end)


# apple-090: the "Buy iMac" link only opens the price page.
def test_shop_links_browse_but_paying_stays_gated():
    from advi.capabilities.browser.executor import _CONSEQUENTIAL_CLICK, _browse_only_link

    assert _CONSEQUENTIAL_CLICK.search("buy imac") and _browse_only_link({"role": "link"}, "Buy iMac")
    assert _browse_only_link({"role": "link"}, "Book now")
    assert not _browse_only_link({"role": "button"}, "Buy now")
    assert not _browse_only_link({"role": "link"}, "Place your order")
    assert not _browse_only_link({"role": "link"}, "Confirm payment")
    assert not _browse_only_link({"role": "link"}, "Delete account")


# allrecipes-016: the same search six times, reworded with the site's name.
def test_a_reworded_repeat_search_is_not_run_again():
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="Locate a gluten-free brownie recipe on https://www.allrecipes.com/ with 50 reviews.", task_id="t")
    ran = []
    session._run_tool_impl = lambda call, approved=False: ran.append(call) or SimpleNamespace(
        success=True, name="search", metadata={"current_url": "https://www.allrecipes.com/search?q=gluten"}, human_readable="ok")
    from advi.providers.base import LLMToolCall

    first = session._run_tool(LLMToolCall(call_id="1", name="search", arguments={"query": "allrecipes.com gluten free brownies"}))
    again = session._run_tool(LLMToolCall(call_id="2", name="search", arguments={"query": "gluten free brownies site:allrecipes.com"}))
    other = session._run_tool(LLMToolCall(call_id="3", name="search", arguments={"query": "vegan brownies"}))
    assert len(ran) == 2 and first.success and other.success
    assert "Already searched" in again.human_readable and "read_web_page" in again.human_readable


# waa-remove-empty: ADVI couldn't tell which folders were empty, so it asked the user.
def test_listings_say_which_folders_are_empty(tmp_path):
    (tmp_path / "ToDoLists").mkdir()
    (tmp_path / "Photos").mkdir()
    (tmp_path / "Photos" / "a.txt").write_text("a", encoding="utf-8")
    result = _files().execute(ToolCall("l", "list_files", {"directory": str(tmp_path)}))
    folders = {f["name"]: f for f in result.output["folders"]}
    assert folders["ToDoLists"]["empty"] is True and folders["Photos"]["items"] == 1


# waa-move-folder: "waa-move-folder/Desktop" became "waa-move- /Desktop".
def test_a_folder_named_with_folder_in_it_is_found(tmp_path):
    target = tmp_path / "waa-move-folder" / "Desktop"
    target.mkdir(parents=True)
    result = _files().execute(ToolCall("l", "list_files", {"directory": str(target)}))
    assert result.success, result.error


def test_the_downloads_folder_phrase_still_works():
    files = _files()
    result = files.execute(ToolCall("l", "list_files", {"directory": "the Downloads folder", "limit": 1}))
    assert result.success, result.error


# waa-hide-file: no way to hide a file.
def test_hide_and_unhide_a_file(tmp_path):
    import ctypes

    path = tmp_path / "secret.txt"
    path.write_text("s", encoding="utf-8")
    assert _files().execute(ToolCall("h", "set_file_hidden", {"path": str(path)})).verified
    assert ctypes.windll.kernel32.GetFileAttributesW(str(path)) & 0x2
    shown = _files().execute(ToolCall("h", "set_file_hidden", {"path": str(path), "hidden": False}))
    assert shown.verified and not ctypes.windll.kernel32.GetFileAttributesW(str(path)) & 0x2


def test_reports_count_which_model_answered():
    from benchmarks.common.metrics import model_line

    line = model_line({"a": {"models": ["openai/gpt-oss-120b"]}, "b": {"models": ["openai/gpt-oss-120b"]},
                       "c": {"models": ["gemini-3.8-flash", "openai/gpt-oss-120b"]}})
    assert "openai/gpt-oss-120b: 2 task(s)" in line and "gemini-3.8-flash + openai/gpt-oss-120b: 1 task(s)" in line
