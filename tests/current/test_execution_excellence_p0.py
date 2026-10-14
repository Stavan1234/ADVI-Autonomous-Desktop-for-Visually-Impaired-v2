from __future__ import annotations

from pathlib import Path

from advi.capabilities.desktop.application_launcher import LaunchTarget, launch
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.core.action_contracts import validate_and_normalize_tool_call
from advi.core.verification import ToolVerifier
from advi.tools import ToolCall, ToolRegistry


def test_pointer_tools_expose_semantic_target_and_coordinates(monkeypatch):
    registry = CapabilityRegistry()
    class Handler: pass
    handler = Handler()
    registry.register(Capability(
        name="desktop",
        description="desktop",
        supported_actions={"click", "double_click", "right_click"},
        status=CapabilityStatus.AVAILABLE,
        handler=handler,
    ))
    tools = ToolRegistry(registry)
    for name in ("click", "double_click", "right_click"):
        spec = tools.get(name)
        assert spec is not None
        properties = spec.parameters["properties"]
        assert "target" in properties
        assert "coordinates" in properties
        coords = properties["coordinates"]
        assert "anyOf" in coords


def test_pointer_tool_requires_target_or_coordinates():
    invalid, errors = validate_and_normalize_tool_call(ToolCall("1", "click", {}))
    assert invalid is None
    assert any("target" in e or "coordinates" in e for e in errors)


def test_pointer_coordinates_normalize_to_named_object():
    normalized, errors = validate_and_normalize_tool_call(
        ToolCall("1", "click", {"coordinates": [100, 200]})
    )
    assert errors == []
    assert normalized is not None
    assert normalized.arguments["coordinates"] == {"x": 100, "y": 200}


def test_application_launcher_never_uses_shell(monkeypatch):
    called = {}
    def fake_popen(args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return object()
    executable = Path("/bin/true")
    monkeypatch.setattr("advi.capabilities.desktop.application_launcher.subprocess.Popen", fake_popen)
    monkeypatch.setattr("advi.capabilities.desktop.application_launcher.os", type("OS", (), {"name": "posix"})())
    process, mode = launch(LaunchTarget(name="safe", executable=executable))
    assert process is not None
    assert mode == "executable"
    assert called["args"] == [str(Path("/bin/true"))]
    assert called["kwargs"]["shell"] is False


def test_navigate_verification_uses_exact_tab(monkeypatch):
    action = ToolCall("1", "navigate", {"url": "https://example.com/path"})
    result = __import__("advi.tools.contracts", fromlist=["ToolResult"]).ToolResult(
        call_id="1", name="navigate", success=True, output="https://example.com/path",
        metadata={"expected_url": "https://example.com/path", "tab_id": "owned-2", "cdp_port": 9222},
    )

    class FakeCDP:
        def __init__(self, port):
            assert port == 9222
        def get_tab_info(self, tab_id):
            assert tab_id == "owned-2"
            return {"id": "owned-2", "url": "https://example.com/path", "type": "page"}

    monkeypatch.setattr("advi.capabilities.browser.cdp.CDPBrowser", FakeCDP)
    verified, details = ToolVerifier()._verify_navigate(action, result)
    assert verified is True
    assert details["tab_id"] == "owned-2"
    assert details["source"] == "exact_cdp_tab"


def test_navigate_verification_does_not_accept_unrelated_tab(monkeypatch):
    action = ToolCall("1", "navigate", {"url": "https://example.com/path"})
    result = __import__("advi.tools.contracts", fromlist=["ToolResult"]).ToolResult(
        call_id="1", name="navigate", success=True, output="https://example.com/path",
        metadata={"expected_url": "https://example.com/path", "tab_id": "owned-2", "cdp_port": 9222},
    )

    class FakeCDP:
        def __init__(self, port): pass
        def get_tab_info(self, tab_id):
            return {"id": "other", "url": "https://example.com/path", "type": "page"}

    monkeypatch.setattr("advi.capabilities.browser.cdp.CDPBrowser", FakeCDP)
    verified, details = ToolVerifier()._verify_navigate(action, result)
    assert verified is False
    assert details["reason"] == "tab_url_unavailable"



def test_gmail_compose_redirect_is_verified(monkeypatch):
    action = ToolCall("1", "navigate", {"url": "https://mail.google.com/mail/?view=cm&fs=1&to=david%40example.com&su=Testing&body=Hello%20David"})
    result = __import__("advi.tools.contracts", fromlist=["ToolResult"]).ToolResult(
        call_id="1", name="navigate", success=True,
        output="https://mail.google.com/mail/u/0/?fs=1&to=david%40example.com&su=Testing&body=Hello+David&tf=cm",
        metadata={"expected_url": action.arguments["url"], "tab_id": "owned-2", "cdp_port": 9222},
    )
    monkeypatch.setattr(
        "advi.capabilities.browser.cdp.CDPBrowser.get_tab_info",
        lambda self, tab_id: {"id": tab_id, "url": result.output, "type": "page"},
    )
    verified, details = ToolVerifier()._verify_navigate(action, result)
    assert verified is True
    assert details["url_matched"] is True


def test_pdf_create_verification_uses_extracted_text(tmp_path):
    import fitz

    path = tmp_path / "verse.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((54, 80), "Be still, and know that I am God.")
    doc.save(path)
    doc.close()

    action = ToolCall("1", "create_file", {"path": str(path), "content": "Be still, and know that I am God."})
    result = __import__("advi.tools.contracts", fromlist=["ToolResult"]).ToolResult(
        call_id="1", name="create_file", success=True, output=str(path),
        metadata={"resolved_path": str(path), "path": str(path)},
    )
    verified, details = ToolVerifier()._verify_create_file(action, result)
    assert verified is True
    assert details["content_matches"] is True
