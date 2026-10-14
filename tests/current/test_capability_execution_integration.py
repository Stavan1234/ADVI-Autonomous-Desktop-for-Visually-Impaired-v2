from pathlib import Path

from advi.capabilities.files.executor import FileCapability
from advi.capabilities.gmail.executor import GmailCapability
from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.desktop.executor import DesktopCapability
from advi.capabilities.memory.executor import MemoryCapability
from advi.capabilities.desktop.context import DesktopContext
from advi.tools.contracts import ToolCall


def test_file_capability_round_trip(tmp_path):
    cap = FileCapability()
    cap.desktop_dir = tmp_path

    created = cap.execute(ToolCall(call_id="test", name="create_file", arguments={"path": "note.txt", "content": "hello"}))
    assert created.success and created.verified

    read = cap.execute(ToolCall(call_id="test", name="read_file", arguments={"path": "note.txt"}))
    assert read.success
    assert read.data == "hello"

    listing = cap.execute(ToolCall(call_id="test", name="list_files", arguments={"directory": str(tmp_path)}))
    assert listing.success
    assert "note.txt" in [item["name"] for item in listing.data["files"]]
    assert listing.data["files"][0]["path"] == str(tmp_path / "note.txt")

    deleted = cap.execute(ToolCall(call_id="test", name="delete_file", arguments={"path": "note.txt"}))
    assert deleted.success and deleted.verified


def test_browser_capability_real_executor_with_fake_cdp(monkeypatch):
    cap = BrowserCapability(context=DesktopContext())
    cap.context.set_target_tab("tab-1", url="https://example.com", title="Example")

    class FakeCDP:
        def list_tabs(self):
            return [{"id": "tab-1", "type": "page", "url": "https://example.com"}]

        def evaluate_script(self, tab_id, script):
            assert tab_id == "tab-1"
            if "innerText" in script:
                return {"value": "Example page"}
            if "document.querySelector" in script:
                return {"value": True}
            return {"value": True}

        def navigate_tab(self, tab_id, url):
            assert tab_id == "tab-1"
            return True

    cap.cdp = FakeCDP()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)

    nav = cap.execute(ToolCall(call_id="test", name="navigate", arguments={"url": "https://example.com/test"}))
    assert nav.success and nav.data == "https://example.com/test"

    page = cap.execute(ToolCall(call_id="test", name="read_web_page"))
    assert page.success and page.data == "Example page"
    assert page.metadata["tab_id"] == "tab-1"

    click = cap.execute(ToolCall(call_id="test", name="click_web_element", arguments={"selector": "#ok"}))
    assert click.success


def test_gmail_capability_actions_with_fake_service():
    class FakeService:
        def create_draft(self, **kwargs):
            return type("Draft", (), {"draft_id": "d1"})()

        def get_draft(self, draft_id):
            return {"draft_id": draft_id, "subject": "Test"}

        def update_draft(self, **kwargs):
            return {"id": kwargs["draft_id"]}

        def send_email(self, **kwargs):
            return {"id": "m1"}

        def send_draft(self, draft_id):
            return {"id": "m2"}

        def list_messages(self, query, max_results):
            return [{"id": "1"}]

    cap = GmailCapability(FakeService())
    assert cap.execute(ToolCall(call_id="test", name="email_draft_create", arguments={"to": "a@b.com", "subject": "T", "body": "B"})).verified
    assert cap.execute(ToolCall(call_id="test", name="email_draft_read", arguments={"draft_id": "d1"})).success
    assert cap.execute(ToolCall(call_id="test", name="email_draft_update", arguments={"draft_id": "d1", "body": "B2"})).verified
    assert cap.execute(ToolCall(call_id="test", name="email_send", arguments={"to": "a@b.com", "subject": "T", "body": "B"})).verified
    assert cap.execute(ToolCall(call_id="test", name="email_send", arguments={"draft_id": "d1"})).verified
    assert cap.execute(ToolCall(call_id="test", name="email_read", arguments={"query": "is:inbox", "limit": 1})).verified


def test_desktop_capability_real_executor_wait_only(monkeypatch):
    cap = DesktopCapability(context=DesktopContext())
    monkeypatch.setattr("advi.capabilities.desktop.executor.time.sleep", lambda _seconds: None)
    result = cap.execute(ToolCall(call_id="test", name="wait", arguments={"duration": 0.1}))
    assert result.success
    assert result.data == 0.1


def test_memory_capability_retrieval_with_fake_retriever():
    class FakeRetriever:
        def retrieve(self, query, limit=5):
            return [f"fact:{query}"]

    cap = MemoryCapability(retriever=FakeRetriever())
    result = cap.execute(ToolCall(call_id="test", name="memory_retrieval", arguments={"query": "favorite editor"}))
    assert result.success and result.verified
    assert result.data == ["fact:favorite editor"]


def test_email_read_summary_mode_omits_full_body():
    from advi.capabilities.gmail.executor import GmailCapability
    from advi.tools import ToolCall

    class Msg:
        id = "m1"; thread_id = "t1"; sender = "a@example.com"; recipients = ["me@example.com"]
        cc = []; subject = "Hello"; date = "today"; snippet = "Hello there"; body = "FULL BODY" * 1000
        label_ids = ["INBOX"]

    class Service:
        def ensure_ready(self): pass
        def search_messages(self, query, limit): return [{"id": "m1"}]
        def get_parsed_message(self, message_id): return Msg()

    result = GmailCapability(Service()).execute(ToolCall("1", "email_read", {"query": "is:inbox", "limit": 5}))
    assert result.success and "body" not in result.output[0]


def test_email_read_single_message_includes_bounded_body():
    from advi.capabilities.gmail.executor import GmailCapability
    from advi.tools import ToolCall

    class Msg:
        id = "m1"; thread_id = "t1"; sender = "a@example.com"; recipients = ["me@example.com"]
        cc = []; subject = "Hello"; date = "today"; snippet = "Hello there"; body = "FULL BODY" * 1000
        label_ids = ["INBOX"]

    class Service:
        def ensure_ready(self): pass
        def search_messages(self, query, limit): return [{"id": "m1"}]
        def get_parsed_message(self, message_id): return Msg()

    result = GmailCapability(Service()).execute(ToolCall("1", "email_read", {"query": "is:inbox", "limit": 1}))
    assert result.success and result.output[0]["body"].startswith("FULL BODY")
    assert len(result.output[0]["body"]) <= 4200


def test_mcp_capability_exposes_canonical_handlers():
    from advi.capabilities.mcp import MCPCapability
    from advi.tools import ToolCall

    class Manager:
        def is_available(self): return True
        def list_tools(self, server=None): return [{"name": "add", "server": server or "calc"}]
        def call_tool(self, server, tool, arguments): return {"output": {"result": 3}, "is_error": False}

    cap = MCPCapability(Manager())
    assert callable(getattr(cap, "_execute_mcp_list_tools"))
    assert callable(getattr(cap, "_execute_mcp_call"))
    listed = cap.execute(ToolCall("1", "mcp_list_tools", {}))
    called = cap.execute(ToolCall("2", "mcp_call", {"server": "calc", "tool": "add", "arguments": {"a": 1}}))
    assert listed.success and listed.verified
    assert called.success and called.verified and called.output["result"] == 3


def test_mcp_capability_close_is_idempotent():
    from advi.capabilities.mcp import MCPCapability

    class Manager:
        def __init__(self): self.closed = 0
        def is_available(self): return True
        def close(self): self.closed += 1

    manager = Manager()
    cap = MCPCapability(manager)
    cap.close(); cap.close()
    assert manager.closed == 2
