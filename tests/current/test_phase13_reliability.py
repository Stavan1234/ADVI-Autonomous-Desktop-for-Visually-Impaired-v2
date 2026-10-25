"""Phase 13: failures from the second live voice session (2026-10-24)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from advi.agent import AgentSession
from advi.agent.names import replace_word, word_correction
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolGuard, ToolRegistry, ToolRunner
from advi.tools.contracts import ToolResult


class Provider(LLMProvider):
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
        self.calls.append(messages)
        return self._next()

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.calls.append(messages)
        return self._next()

    def _next(self):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Apps:
    """A Gmail draft and a WhatsApp that fails once, then works."""

    def __init__(self):
        self.draft = {"draft_id": "d1", "to": "david@example.com", "subject": "Testing", "body": "testing voice input of Adri"}
        self.sent = []
        self.whatsapp_failures = 1

    def execute_tool_call(self, call, *, approval_granted=False):
        ok = lambda **kw: ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True,
                                     verification_status="verified", **kw)
        if call.name == "email_draft_create":
            return ok(output=dict(self.draft), human_readable="Created the draft.", metadata=dict(self.draft))
        if call.name == "email_draft_read":
            return ok(output=dict(self.draft), human_readable="Read the draft.")
        if call.name == "email_draft_update":
            self.draft.update({k: call.arguments[k] for k in ("to", "subject", "body")})
            return ok(output=dict(self.draft), human_readable="Updated the draft.")
        if call.name == "email_send":
            self.sent.append(("email", dict(self.draft)))
            return ok(human_readable="Sent the email.", metadata={"message_id": "m1"})
        if call.name == "email_read":
            return ok(output=[{"from": "bank@example.com", "subject": "Statement"}], human_readable="Read 1 email matching 'in:inbox'.")
        if call.name == "whatsapp_send_message":
            if self.whatsapp_failures:
                self.whatsapp_failures -= 1
                return ToolResult(call_id=call.call_id, name=call.name, success=False, verified=True,
                                  verification_status="failed", error="WhatsApp is open but I couldn't bring its window to the front.")
            self.sent.append(("whatsapp", call.arguments["chat"], call.arguments["text"]))
            return ok(human_readable="Sent.", metadata={"chat_title": call.arguments["chat"]})
        if call.name == "media_control":
            return ok(human_readable="Paused.")
        raise AssertionError(call.name)


def session_with(responses):
    registry = CapabilityRegistry()
    registry.register(Capability(name="apps", description="apps", status=CapabilityStatus.AVAILABLE, handler=object(),
                                 supported_actions={"email_draft_create", "email_draft_read", "email_draft_update", "email_send",
                                                    "email_read", "whatsapp_send_message", "media_control"}))
    apps = Apps()
    tools = ToolRegistry(registry)
    runner = ToolRunner(apps, ToolGuard(tools))
    provider = Provider(responses)
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=runner.guard)
    session.local_intents_enabled = False
    return session, apps, provider


def call(name, **arguments):
    return LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall(f"c-{name}", name, arguments),))


def say(text):
    return LLMResponse(text=text, provider="fake", model="m")


# --- corrections -----------------------------------------------------------------------

def test_word_corrections():
    assert word_correction("It's aadvi, not aadri.") == ("ADVI", "aadri")
    assert word_correction("replace Monday with Tuesday") == ("Tuesday", "Monday")
    assert word_correction("change the message to hi") is None
    assert replace_word("voice input of Adri", "aadri", "ADVI") == "voice input of ADVI"
    assert replace_word("hello there", "Monday", "Tuesday") is None


def test_email_word_correction_changes_the_draft_and_asks_again():
    session, apps, _ = session_with([call("email_draft_create", to="david@example.com", subject="Testing", body="x"),
                                     call("email_send", draft_id="d1"), say("Sent.")])
    first = session.respond("send an email to david saying testing voice input of adri")
    assert session.pending_confirmation is not None and session.pending_confirmation.name == "email_send", first.text
    reply = session.respond("It's aadvi, not aadri.")
    assert "changed 'aadri' to 'ADVI'" in reply.text and "testing voice input of ADVI" in reply.text
    assert apps.sent == [] and apps.draft["body"] == "testing voice input of ADVI"
    session.respond("yes")
    assert apps.sent and apps.sent[0][1]["body"] == "testing voice input of ADVI"


def test_change_the_body_of_the_email_to_keeps_only_the_new_words():
    assert AgentSession._email_edit_value(
        "Can you change the body of the email to testing the email using voice input of Adri?"
    ) == "testing the email using voice input of Adri"


def test_after_an_ai_answer_about_a_pending_action_yes_is_spelled_out():
    session, apps, _ = session_with([call("whatsapp_send_message", chat="Siyu", text="hello"),
                                     say("Do you want me to change it? Say yes.")])
    session.respond("send hello to siyu on whatsapp")
    reply = session.respond("hmm is that the right greeting")
    assert "Right now, yes will send the WhatsApp message 'hello' to Siyu, unchanged." in reply.text


# --- retry ---------------------------------------------------------------------------------

def test_try_again_repeats_the_corrected_send_not_the_first_heard_name():
    session, apps, _ = session_with([call("whatsapp_send_message", chat="Siu", text="Hello"), say("Sent.")])
    session.respond("send hello to siu on whatsapp")
    session.respond("not a C U, it is S I Y U.")
    failed = session.respond("yes")
    assert "couldn't" in failed.text.casefold() and apps.sent == []
    again = session.respond("Try again.")
    assert "to Siyu" in again.text and "Siu " not in again.text
    session.respond("yes")
    assert apps.sent == [("whatsapp", "Siyu", "Hello")]


# --- read-only results and lost closing sentences --------------------------------------------

def test_inbox_read_keeps_the_ai_answer():
    session, _, _ = session_with([call("email_read", query="in:inbox", limit=1),
                                  say("Your latest email is from your bank: Statement.")])
    reply = session.respond("check my inbox for the latest message")
    assert reply.text == "Your latest email is from your bank: Statement."


def test_verified_pause_is_reported_plainly_when_the_ai_goes_quiet():
    from advi.providers.errors import LLMRateLimitError

    session, _, _ = session_with([call("media_control", action="pause"), LLMRateLimitError("per-minute limit")])
    reply = session.respond("pause the song")
    assert "couldn't verify the final state" not in reply.text and "Paused" in reply.text


def test_read_that_email_after_a_send_is_not_answered_with_the_sent_draft():
    session, _, _ = session_with([])
    session._last_email_draft = {"draft_id": "d1", "to": "david@example.com", "subject": "Testing",
                                 "body": "x", "sent": True}
    assert session._handle_local_email_draft_followup("Read out that email to me.") is None
    assert "The last email was sent" in session._handle_local_email_draft_followup("read the email you just sent").text


# --- more email actions: forward, trash, archive, mark ---------------------------------------------

class FakeGmail:
    def __init__(self):
        self.labels = {"m1": {"INBOX", "UNREAD"}}
        self.sent = []

    def get_message(self, message_id):
        return {"id": message_id, "labelIds": sorted(self.labels.get(message_id, set()))}

    def get_parsed_message(self, message_id):
        return SimpleNamespace(subject="Statement", sender="bank@example.com", date="today", recipients="me", body="Your statement.")

    def modify_labels(self, message_id, *, add=None, remove=None):
        self.labels[message_id] = (self.labels[message_id] | set(add or [])) - set(remove or [])
        return {}

    def trash_message(self, message_id):
        self.labels[message_id] = {"TRASH"}
        return {}

    def forward_message(self, message_id, to, note=""):
        self.sent.append((message_id, to, note))
        self.labels["f1"] = {"SENT"}
        return {"id": "f1"}


def gmail_call(name, **arguments):
    from advi.tools.contracts import ToolCall

    return ToolCall(call_id="g", name=name, arguments=arguments)


def test_email_mark_archive_trash_and_forward_are_checked_against_gmail():
    from advi.capabilities.gmail.executor import GmailCapability

    gmail = FakeGmail()
    capability = GmailCapability(service=gmail)
    read = capability.execute(gmail_call("email_mark", message_id="m1", state="read"))
    assert read.success and read.verified and "UNREAD" not in gmail.labels["m1"]
    starred = capability.execute(gmail_call("email_mark", message_id="m1", state="starred"))
    assert starred.success and "STARRED" in gmail.labels["m1"]
    archived = capability.execute(gmail_call("email_archive", message_id="m1"))
    assert archived.success and "INBOX" not in gmail.labels["m1"]
    forwarded = capability.execute(gmail_call("email_forward", message_id="m1", to="sam@example.com", note="FYI"))
    assert forwarded.success and forwarded.verified and gmail.sent == [("m1", "sam@example.com", "FYI")]
    trashed = capability.execute(gmail_call("email_trash", message_id="m1"))
    assert trashed.success and "30 days" in trashed.human_readable
    missing = capability.execute(gmail_call("email_trash"))
    assert not missing.success and "message_id" in missing.error


def test_forward_and_trash_need_a_yes_and_name_the_email():
    from advi.capabilities.registry import _ACTION_SEMANTICS

    assert _ACTION_SEMANTICS["email_forward"]["requires_confirmation"] is True
    assert _ACTION_SEMANTICS["email_trash"]["requires_confirmation"] is True
    assert not _ACTION_SEMANTICS["email_mark"].get("requires_confirmation")
    session, _, _ = session_with([])
    session._email_index = {"m1": {"from": "bank@example.com", "subject": "Statement"}}
    from advi.tools import ToolCall

    trash = ToolCall(call_id="t", name="email_trash", arguments={"message_id": "m1"})
    forward = ToolCall(call_id="f", name="email_forward", arguments={"message_id": "m1", "to": "sam@example.com"})
    assert session._confirmation_description(trash) == "move the email 'Statement' from bank@example.com to Trash"
    assert session._confirmation_description(forward) == "forward the email 'Statement' from bank@example.com to sam@example.com"


# --- Desktop Suite regression findings ---------------------------------------------------------------

def test_a_request_that_is_a_move_is_one_step():
    from advi.agent.session import _goal_has_more_steps

    assert not _goal_has_more_steps('move the folder named "myfolder" from desktop to documents.')
    assert _goal_has_more_steps("create a folder called pdf and move all the pdfs into it")
    assert _goal_has_more_steps("make a folder, then copy the photos into it")


def test_remove_empty_folders_in_one_verified_step(tmp_path):
    from advi.capabilities.files.executor import FileCapability
    from advi.tools.contracts import ToolCall

    for name in ("GroceryList", "Photos"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "item.txt").write_text("x")
    (tmp_path / "ToDoLists").mkdir()
    (tmp_path / "Old").mkdir()
    result = FileCapability().execute(ToolCall(call_id="r", name="remove_empty_folders", arguments={"path": str(tmp_path)}))
    assert result.success and result.verified
    assert sorted(result.output["removed"]) == ["Old", "ToDoLists"]
    assert (tmp_path / "Photos").exists() and not (tmp_path / "ToDoLists").exists()


def test_doing_the_work_then_checking_still_counts_as_done():
    session, _, _ = session_with([])
    session.task = None
    session._ensure_active_task()
    session.task.goal = "list the large files in downloads into report.txt"
    session.task.status = __import__("advi.agent.contracts", fromlist=["TaskStatus"]).TaskStatus.IN_PROGRESS
    session.task.tool_outcomes.extend([
        {"tool": "list_files", "success": True, "verified": True},
        {"tool": "create_file", "success": True, "verified": True},
        {"tool": "list_files", "success": True, "verified": True},
    ])
    session.last_tool, session.last_tool_success = "list_files", True
    assert session._has_verified_terminal_evidence()


def test_a_path_under_another_windows_profile_is_moved_to_this_users_profile(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / "HP"
    (home / "Documents" / "ADVI Bench" / "task").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    session, _, _ = session_with([])
    wrong = LLMToolCall("c", "save_document_as", {"path": r"C:\Users\Public\Documents\ADVI Bench\task\draft.txt"})
    fixed = session._remap_other_user_paths(wrong)
    assert fixed.arguments["path"] == str(home / "Documents" / "ADVI Bench" / "task" / "draft.txt")
    unrelated = LLMToolCall("c", "save_document_as", {"path": r"C:\Users\Public\Music\nowhere\x.txt"})
    assert session._remap_other_user_paths(unrelated) is unrelated


def test_put_this_speech_into_a_pdf_gets_the_whole_speech():
    speech = "Water is life. " * 120
    session, _, provider = session_with([say(speech), say("Sure, what would you like?"), say("Done.")])
    session.respond("tell me a long speech about water")
    session.respond("can you")
    session.respond("Take this speech which you just created and create a new PDF out of it")
    last = str(provider.calls[-1])
    assert "[YOUR PREVIOUS ANSWER, IN FULL]" in last and last.count("Water is life.") >= 100


def test_spoken_confirmations_are_short_and_the_screen_keeps_the_full_text():
    from advi.io.output import AdviResponse

    text = "Before I do that, I need your confirmation: send the WhatsApp message 'hello' to Siyu Say yes to continue or no to cancel."
    response = AdviResponse(text)
    assert response.for_speech() == "Shall I send the WhatsApp message 'hello' to Siyu? Yes or no?"
    assert response.for_display() == text


def test_three_folders_asked_at_once_are_created_in_one_round():
    created = []

    class Folders:
        def execute_tool_call(self, call, *, approval_granted=False):
            created.append(call.arguments["path"])
            return ToolResult(call_id=call.call_id, name=call.name, success=True, verified=True,
                              verification_status="verified", human_readable=f"Created {call.arguments['path']}.")

    registry = CapabilityRegistry()
    registry.register(Capability(name="files", description="files", status=CapabilityStatus.AVAILABLE,
                                 handler=object(), supported_actions={"create_folder"}))
    tools = ToolRegistry(registry)
    runner = ToolRunner(Folders(), ToolGuard(tools))
    calls = tuple(LLMToolCall(f"c{i}", "create_folder", {"path": f"Documents/Bench/{name}"})
                  for i, name in enumerate(("2024", "2025", "Archive")))
    provider = Provider([LLMResponse(text="", provider="f", model="m", tool_calls=calls), say("Created all three.")])
    session = AgentSession([provider], tool_registry=tools, tool_runner=runner, tool_guard=runner.guard)
    session.local_intents_enabled = False
    reply = session.respond("In Documents\\Bench, create three folders named 2024, 2025 and Archive.")
    assert created == ["Documents/Bench/2024", "Documents/Bench/2025", "Documents/Bench/Archive"]
    assert len(provider.calls) == 2 and "limit" not in reply.text


# --- Office tables ----------------------------------------------------------------------------------

def test_one_column_and_borderless_markdown_tables_become_word_tables():
    from advi.capabilities.office.word import parse_blocks

    one_column = parse_blocks("# Months\n\n| Month |\n|-------|\n| January |\n| February |\n")
    assert [b.kind for b in one_column] == ["title", "table"]
    assert one_column[1].rows == [["Month"], ["January"], ["February"]]
    borderless = parse_blocks("Planet | Distance\n--- | ---\nMercury | 58\nVenus | 108")
    assert borderless[0].kind == "table" and borderless[0].rows[1] == ["Mercury", "58"]
    assert [b.kind for b in parse_blocks("Use a | to separate.")] == ["normal"]  # a lone pipe is not a table


def test_word_rejects_a_document_whose_table_came_out_as_text(tmp_path):
    from advi.capabilities.office import OfficeCapability
    from advi.tools.contracts import ToolCall
    import docx

    def bad_builder(blocks, target):  # writes the table as literal text, like the live bug
        document = docx.Document()
        document.add_paragraph("| Month |")
        document.save(target)

    office = OfficeCapability(resolve_path=lambda p: tmp_path / p, builder=bad_builder, opener=lambda p: None)
    result = office.execute(ToolCall(call_id="w", name="word_create_document",
                                     arguments={"content": "| Month |\n|---|\n| May |", "path": "Months.docx"}))
    assert not result.success and "real Word table" in result.error


def test_excel_accepts_markdown_tab_and_csv_tables():
    from advi.capabilities.files.executor import FileCapability

    expected = [["Item", "Cost"], ["Rent", "9000"]]
    assert FileCapability._table_rows("| Item | Cost |\n|---|---|\n| Rent | 9000 |") == expected
    assert FileCapability._table_rows("Item\tCost\nRent\t9000") == expected
    assert FileCapability._table_rows("Item,Cost\nRent,9000\n") == expected


# --- WhatsApp chat titles with emoticons --------------------------------------------------------

def test_chat_with_an_emoticon_title_is_recognised_as_open():
    from advi.capabilities.apps.whatsapp import WhatsAppSkill

    composer = SimpleNamespace(name="Type a message to Siyu")
    skill = WhatsAppSkill(driver=SimpleNamespace())
    skill._composer = lambda doc: composer
    assert skill._chat_is_open(None, "Siyu ^_^", before_name="type a message to rahul")
    assert not skill._chat_is_open(None, "Siyu ^_^", before_name="type a message to siyu")  # nothing changed


# --- computer-wide search is bounded and stoppable -------------------------------------------------

def test_computer_wide_listing_stops_when_asked(tmp_path, monkeypatch):
    from advi.capabilities.files import executor as files
    from advi.core import cancel
    from advi.tools.contracts import ToolCall

    for i in range(3):
        (tmp_path / f"clip{i}.mp4").write_text("x")
    (tmp_path / "AppData").mkdir()
    (tmp_path / "AppData" / "hidden.mp4").write_text("x")
    home = SimpleNamespace(user_home=tmp_path, desktop=tmp_path / "Desktop", documents=tmp_path / "Documents",
                           downloads=tmp_path)
    import psutil

    monkeypatch.setattr(files, "get_environment_paths", lambda: home)
    monkeypatch.setattr(psutil, "disk_partitions", lambda all=False: [])  # only the test folder
    capability = files.FileCapability.__new__(files.FileCapability)
    result = capability._execute_list_files(ToolCall(call_id="c", name="list_files",
                                                     arguments={"scope": "computer", "extension": "mp4"}))
    names = {f["name"] for f in result.output["files"]}
    assert {"clip0.mp4", "clip1.mp4", "clip2.mp4"} <= names and "hidden.mp4" not in names

    cancel.STOP.set()
    try:
        stopped = capability._execute_list_files(ToolCall(call_id="c", name="list_files",
                                                          arguments={"scope": "computer", "extension": "mp4"}))
        assert stopped.output["stopped_early"] == "stopped" and "because you asked me to" in stopped.human_readable
    finally:
        cancel.STOP.clear()


def test_a_file_listing_survives_moving_the_chat_to_another_key():
    from advi.agent.session_handoff import ToolExchange

    result = {"ok": True, "output": {"files": [{"name": "huge.bin", "size_bytes": 5000000}]}}
    exchange = ToolExchange.from_calls_and_results([{"name": "list_files"}], [{"name": "list_files", "result": result}])
    assert exchange.results[0]["result"]["output"]["files"][0] == {"name": "huge.bin", "size_bytes": 5000000}


def test_play_local_media_finds_tone_wav_in_the_named_folder(tmp_path, monkeypatch):
    from advi.capabilities.files.executor import FileCapability
    from advi.tools.contracts import ToolCall

    (tmp_path / "tone.wav").write_bytes(b"RIFF")
    played = []
    capability = FileCapability()
    monkeypatch.setattr(capability, "_play_media_file",
                        lambda action, chosen, match, alternatives: played.append(chosen.name) or ToolResult(
                            call_id="p", name="play_local_media", success=True, verified=True))
    for query in ("tone.wav", "tone"):
        capability._execute_play_local_media(ToolCall(call_id="p", name="play_local_media",
                                                      arguments={"folder": str(tmp_path), "query": query}))
    assert played == ["tone.wav", "tone.wav"]


def test_a_verified_pause_is_simply_done():
    session, _, _ = session_with([call("media_control", action="pause"), say("Paused the video.")])
    reply = session.respond("Pause the video.")
    assert reply.text == "Paused the video."


@pytest.mark.parametrize("request_text, url", [
    ("Search Amazon for a wireless mouse.", "https://www.amazon.in/s?k=wireless+mouse"),
    ("Search Google for the weather in Mumbai.", "https://www.google.com/search?q=weather+in+Mumbai"),
    ("Search Flipkart for running shoes.", "https://www.flipkart.com/search?q=running+shoes"),
])
def test_a_search_on_a_named_site_uses_that_site(request_text, url):
    session, _, _ = session_with([])
    session.tools.get = lambda name: object()  # navigate is available
    session.last_user_message = request_text
    routed = session._search_on_the_named_site(LLMToolCall("s", "search", {"query": "x site:amazon.com"}))
    assert routed.name == "navigate" and routed.arguments == {"url": url}


def test_a_plain_search_keeps_the_open_site():
    session, _, _ = session_with([])
    session.tools.get = lambda name: object()
    session.last_user_message = "search for headphones"
    original = LLMToolCall("s", "search", {"query": "headphones"})
    assert session._search_on_the_named_site(original) is original


def test_a_site_search_answer_is_kept():
    from advi.agent.session import _SEARCH_RESULTS_URL

    assert _SEARCH_RESULTS_URL.search("fill on 'Search'. Submitted; now at https://www.google.com/search?q=weather")
    assert not _SEARCH_RESULTS_URL.search("fill on 'Comment'. Submitted; now at https://example.com/post/42")


def test_describing_who_sent_an_email_is_not_a_false_send_claim():
    session, _, _ = session_with([call("email_read", limit=1),
                                  say("The latest email was sent by bank@example.com with the subject Statement.")])
    reply = session.respond("Check my inbox and tell me who sent the latest email and what its subject is.")
    assert reply.text.startswith("The latest email was sent by bank@example.com")


def test_the_time_comes_from_this_pcs_clock():
    session, _, provider = session_with([say("I'm not able to retrieve the current time right now.")])
    session.local_intents_enabled = True
    reply = session.respond("What time is it?")
    assert reply.text.startswith("It's") and provider.calls == []


@pytest.mark.parametrize("request_text, app, allowed", [
    ("Close the video player.", "Brave", False),
    ("Close the video player.", "Media Player", True),
    ("Close Calculator.", "Calculator", True),
    ("Close Notepad.", "Microsoft Word", False),
    ("close the browser", "Brave", True),
    ("close word", "Microsoft Word", True),
])
def test_only_the_app_asked_for_is_closed(request_text, app, allowed):
    from advi.tools import ToolCall

    session, _, _ = session_with([])
    session.last_user_message = request_text
    refused = session._close_target_refusal(ToolCall(call_id="x", name="close_application", arguments={"application": app}))
    assert (refused is None) == allowed
    if not allowed:
        assert "not what the user asked to close" in refused.error


def test_scrolling_during_web_browsing_stays_in_the_browser():
    session, _, _ = session_with([])
    session.tools.get = lambda name: object()
    session._browser_url = "https://www.amazon.in/s?k=wireless+mouse"
    session.last_tool = "inspect_web_page"
    session._foreground_title = lambda: "Claude"
    assert session._keep_browsing_in_the_browser(LLMToolCall("s", "scroll", {"direction": "down"})).name == "read_web_page"
    assert session._keep_browsing_in_the_browser(LLMToolCall("o", "observe_window", {})).name == "inspect_web_page"
    notepad = LLMToolCall("n", "scroll", {"application": "Notepad", "direction": "down"})
    assert session._keep_browsing_in_the_browser(notepad) is notepad  # a named desktop app on purpose


def test_a_rejected_call_that_never_ran_does_not_fail_the_task():
    session, _, _ = session_with([call("media_control", action="pause", application="browser"),
                                  call("media_control", action="pause"), say("Paused.")])
    reply = session.respond("Pause the video.")
    assert reply.text == "Paused." and "rejected" not in reply.text.casefold()


def test_video_clip_finds_clip_mp4(tmp_path, monkeypatch):
    from advi.capabilities.files.executor import FileCapability
    from advi.tools.contracts import ToolCall

    (tmp_path / "clip.mp4").write_bytes(b"x")
    played = []
    capability = FileCapability()
    monkeypatch.setattr(capability, "_play_media_file",
                        lambda action, chosen, match, alternatives: played.append(chosen.name) or ToolResult(
                            call_id="p", name="play_local_media", success=True, verified=True))
    capability._execute_play_local_media(ToolCall(call_id="p", name="play_local_media",
                                                  arguments={"folder": str(tmp_path), "query": "video clip"}))
    assert played == ["clip.mp4"]
