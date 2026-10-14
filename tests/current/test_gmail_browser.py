from __future__ import annotations

from types import SimpleNamespace

from advi.agent import AgentSession
from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.gmail.executor import GmailCapability
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.capabilities.desktop.context import DesktopContext
from advi.core.execution_engine import ExecutionEngine
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
from advi.tools.contracts import ToolResult


def test_native_browser_never_adopts_first_unowned_tab():
    cap = BrowserCapability(context=DesktopContext(), cdp_port=9333)
    class FakeCDP:
        def list_tabs(self):
            return [{"id": "user-tab", "type": "page", "url": "https://private.example/", "title": "Private"}]
        def create_new_tab(self, url):
            return None
    cap.cdp = FakeCDP()
    assert cap._ensure_chrome_running() is False
    assert cap.context.target_tab_id is None




def test_david_browser_never_adopts_an_existing_user_tab_when_dedicated_creation_fails():
    from advi.fallback.cdp_browser import CDPBrowser

    browser = CDPBrowser(port=9333)
    browser.list_tabs = lambda: [{"id": "user-tab", "type": "page", "url": "https://private.example/"}]
    browser._http_get_json = lambda path: (_ for _ in ()).throw(RuntimeError("debug endpoint unavailable"))

    assert browser.create_new_tab("about:blank") is None

def test_native_research_requires_owned_tab():
    cap = BrowserCapability(context=DesktopContext())
    class FakeCDP:
        def list_tabs(self):
            return [{"id": "user-tab", "type": "page", "url": "https://private.example/", "title": "Private"}]
        def create_new_tab(self, url):
            return None
    cap.cdp = FakeCDP()
    result = cap._execute_research_web(ToolCall("r1", "research_web", {"query": "test"}))
    assert not result.success
    assert "ADVI-owned" in result.error


def test_gmail_unavailable_exposes_structured_fallback_context():
    cap = GmailCapability(None)
    result = cap.execute(ToolCall("g1", "email_draft_create", {"to": "john@example.com", "subject": "Ready", "body": "The project is ready."}))
    assert not result.success
    assert result.metadata["fallback_recommended"] is True
    assert result.output["fallback"]["task"]["to"] == "john@example.com"
    assert result.output["fallback"]["task"]["body"] == "The project is ready."


def test_gmail_ambiguous_send_failure_does_not_recommend_browser_retry():
    class FailingService:
        def send_email(self, **kwargs):
            raise TimeoutError("request timed out")
    cap = GmailCapability(FailingService())
    result = cap.execute(ToolCall("g2", "email_send", {"to": "john@example.com", "subject": "Ready", "body": "The project is ready."}))
    assert not result.success
    assert result.metadata["failure_class"] == "uncertain_external_effect"
    assert not result.metadata.get("fallback_recommended", False)


class SequenceProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = list(responses)
    @property
    def name(self): return "fake"
    @property
    def model(self): return "fake-model"
    def chat(self, messages, *, purpose="chat"): return self.responses.pop(0)


def test_approved_gmail_failure_enriches_david_fallback_without_user_procedure():
    class FakeGmail:
        SUPPORTED_ACTIONS = {"email_send"}
        def execute(self, call, *, approval_granted=False):
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                success=False,
                error="Gmail service is not configured",
                metadata={
                    "fallback_recommended": True,
                    "fallback_context": {
                        "operation":"email_send",
                        "to":"john@example.com",
                        "subject":"Ready",
                        "body":"The project is ready.",
                    },
                },
            )
        def is_available(self): return True
    class FakeDavid:
        def __init__(self): self.calls=[]
        def is_available(self): return True
        def process(self, request, *, approved=False):
            self.calls.append((request, "", approved))
            return SimpleNamespace(success=True, text="Sent through Gmail.", results=[], metadata={})

    registry = CapabilityRegistry()
    gmail = FakeGmail()
    registry.register(Capability("email", "gmail", {"email_send"}, CapabilityStatus.AVAILABLE, handler=gmail))
    from advi.fallback.capability import DavidFallbackCapability
    from advi.fallback.david_adapter import DavidAdapter
    david_service = FakeDavid()
    david = DavidFallbackCapability(DavidAdapter(david_service))
    registry.register(Capability("david_fallback", "david", {"david_fallback"}, CapabilityStatus.AVAILABLE, handler=david))
    tools = ToolRegistry(registry)
    guard = ToolGuard(tools)
    runner = ToolRunner(ExecutionEngine(registry=registry), guard, tools)
    session = AgentSession([SequenceProvider([
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("e1", "email_send", {"to":"john@example.com","subject":"Ready","body":"The project is ready."}),)),
        LLMResponse(text="", provider="fake", model="m", tool_calls=(LLMToolCall("d1", "david_fallback", {"request":"Send this email in Gmail", "reason":"gmail_native_failure"}),)),
        LLMResponse(text="Sent through the browser fallback.", provider="fake", model="m"),
    ])], tool_registry=tools, tool_runner=runner, tool_guard=guard)
    first = session.respond("Send an email to john@example.com saying the project is ready.")
    assert "confirmation" in first.text.lower()
    final = session.respond("yes")
    assert final.text == "Sent through the browser fallback."
    assert len(david_service.calls) == 1
    request, reason, approved = david_service.calls[0]
    assert "john@example.com" in request
    assert "Ready" in request
    assert "The project is ready." in request
    assert approved is True


def test_email_reply_executes_only_once():
    calls = []
    class FakeService:
        def reply_to_message(self, *, message_id, body):
            calls.append((message_id, body))
            return {"id": "sent-1"}
        def get_message(self, message_id):
            return {"id": message_id}
    result = GmailCapability(FakeService()).execute(
        ToolCall("r1", "email_reply", {"message_id": "m1", "body": "Hello"})
    )
    assert result.success
    assert len(calls) == 1


def test_email_send_prefers_actual_sent_headers_for_narration():
    class FakeService:
        def send_draft(self, draft_id):
            return {"id": "sent-2"}
        def get_message(self, message_id):
            return {"id": message_id}
        def get_parsed_message(self, message_id):
            return SimpleNamespace(recipients="real@example.com", subject="Actual Subject")
    result = GmailCapability(FakeService()).execute(
        ToolCall("s1", "email_send", {"draft_id": "d1"})
    )
    assert result.success
    assert "real@example.com" in result.human_readable
    assert "Actual Subject" in result.human_readable
