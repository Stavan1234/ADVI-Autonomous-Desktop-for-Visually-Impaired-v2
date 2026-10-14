from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.desktop.context import DesktopContext
from advi.tools.contracts import ToolCall
from advi.core.action_contracts import validate_and_normalize_tool_call
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus


class ResearchCDP:
    def __init__(self):
        self.url = "about:blank"
        self.title = ""
        self.calls = []

    def list_tabs(self):
        return [{"id": "tab-1", "type": "page", "url": self.url, "title": self.title}]

    def get_tab_info(self, tab_id):
        assert tab_id == "tab-1"
        return {"id": tab_id, "type": "page", "url": self.url, "title": self.title}

    def navigate_tab(self, tab_id, url):
        self.calls.append(("navigate", url))
        self.url = url
        if "google.com/search" in url:
            self.title = "Google"
        else:
            self.title = "Example Documentation"
        return True

    def evaluate_script(self, tab_id, script):
        self.calls.append(("evaluate", script))
        if "querySelectorAll('a[href]')" in script:
            return {"value": {"candidates": [
                {"text": "Example Documentation", "href": "https://docs.example.com/guide", "score": 8},
                {"text": "Other Result", "href": "https://other.example.com", "score": 1},
            ]}}
        if "document.body ? document.body.innerText" in script:
            return {"value": "This is the documentation content that should be returned."}
        return {"value": True}


def make_cap():
    cap = BrowserCapability(context=DesktopContext())
    cap.context.set_target_tab("tab-1", url="about:blank", title="")
    fake = ResearchCDP()
    cap.cdp = fake
    return cap, fake


def test_research_web_runs_search_select_open_read(monkeypatch):
    cap, cdp = make_cap()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)
    result = cap.execute(ToolCall(call_id="test", name="research_web", arguments={"query": "ADVI architecture", "target": "documentation"}))

    assert result.success
    assert "documentation content" in result.data
    assert result.metadata["query"] == "ADVI architecture"
    assert result.metadata["selected_result"]["href"] == "https://docs.example.com/guide"
    assert result.metadata["current_url"] == "https://docs.example.com/guide"
    assert result.metadata["content_available"] is True
    assert [call[0] for call in cdp.calls].count("navigate") == 2


def test_research_web_fails_honestly_when_no_results(monkeypatch):
    cap, cdp = make_cap()
    monkeypatch.setattr(cap, "_ensure_chrome_running", lambda: True)

    original = cdp.evaluate_script
    def empty_results(tab_id, script):
        if "querySelectorAll('a[href]')" in script:
            return {"value": {"candidates": []}}
        return original(tab_id, script)
    cdp.evaluate_script = empty_results

    result = cap.execute(ToolCall(call_id="test", name="research_web", arguments={"query": "nothing"}))
    assert not result.success
    assert "no usable result links" in (result.error or "")
    assert result.metadata["tab_id"] == "tab-1"


def test_research_web_contract_normalizes_aliases_and_limit():
    action = ToolCall(call_id="test", name="research_web", arguments={"query": "python", "site": "docs", "limit": "1200"})
    normalized, errors = validate_and_normalize_tool_call(action)
    assert not errors
    assert normalized is not None
    assert normalized.parameters["target"] == "docs"
    assert normalized.parameters["max_chars"] == "1200"


def test_browser_registry_exposes_research_web():
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="browser_control",
        description="Browser",
        supported_actions={"research_web"},
        status=CapabilityStatus.AVAILABLE,
    ))
    spec = registry.get_action_spec("research_web")
    assert spec is not None
    assert spec.action == "research_web"
    assert "query" in spec.parameters
