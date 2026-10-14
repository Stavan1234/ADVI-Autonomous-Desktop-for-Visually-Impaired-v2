from __future__ import annotations

from pathlib import Path

import fitz

from advi.agent.session_handoff import SessionHandoff
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.core.verification import ToolVerifier
from advi.memory.long_term import LongTermMemory
from advi.tools.contracts import ToolCall, ToolResult, VerificationStatus


def test_oversized_session_handoff_uses_safe_bounded_context():
    handoff = SessionHandoff(
        goal="create a report",
        task_status="in_progress",
        active_context={f"key_{i}": "x" * 2000 for i in range(20)},
        recent_conversation=tuple(
            {"role": "user", "content": "y" * 3000} for _ in range(8)
        ),
    )

    payload = handoff.to_payload()
    assert len(str(payload)) > 0
    assert len(__import__("json").dumps(payload, ensure_ascii=False, default=str)) <= 7000
    assert "recent_tool_exchanges" not in payload or isinstance(payload["recent_tool_exchanges"], list)



def test_handoff_preserves_protected_literal_values_when_history_is_trimmed():
    body = "x" * 12000
    handoff = SessionHandoff(
        goal="send an email",
        task_status="in_progress",
        operation_intent="send_email",
        protected_parameters={"recipient": "person@example.com", "body": body},
        active_context={f"key_{i}": "y" * 2000 for i in range(20)},
        recent_conversation=tuple(
            {"role": "user", "content": "z" * 3000} for _ in range(8)
        ),
    )
    payload = handoff.to_payload()
    assert payload["protected_parameters"]["body"] == body
    assert payload["protected_parameters"]["recipient"] == "person@example.com"


def test_structured_document_content_mismatch_is_verification_failure(tmp_path: Path):
    pdf_path = tmp_path / "wrong.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Completely unrelated document content.")
    document.save(str(pdf_path))
    document.close()

    action = ToolCall(
        call_id="pdf-mismatch",
        name="save_file",
        arguments={
            "path": str(pdf_path),
            "content": "Water is essential for life and ecosystems.",
            "style": "beautiful fonts and hierarchy",
        },
    )
    result = ToolResult(
        call_id="pdf-mismatch",
        name="save_file",
        success=True,
        output=str(pdf_path),
    )

    verified = ToolVerifier().verify(action, result)
    assert verified.success is False
    assert verified.verified is False
    assert verified.verification_status is VerificationStatus.FAILED
    assert verified.verification_details["content_matches"] is False
    assert verified.verification_details["definitive_failure"] is True


def test_structured_document_without_requested_content_still_uses_structure_validation(tmp_path: Path):
    pdf_path = tmp_path / "structure-only.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "A real PDF document.")
    document.save(str(pdf_path))
    document.close()

    action = ToolCall(call_id="pdf-structure", name="save_file", arguments={"path": str(pdf_path)})
    result = ToolResult(call_id="pdf-structure", name="save_file", success=True, output=str(pdf_path))

    verified = ToolVerifier().verify(action, result)
    assert verified.success is True
    assert verified.verified is True
    assert verified.verification_status is VerificationStatus.VERIFIED
    assert verified.verification_details["verification_mode"] == "native_structure_and_content"


def test_save_contact_reports_persistence_result(tmp_path: Path):
    memory = LongTermMemory(tmp_path / "memory.db")
    assert memory.save_contact("David", "david@example.com") is True
    assert memory.save_contact("", "david@example.com") is False
    assert memory.save_contact("Invalid", "not-an-email") is False
    contacts = memory.find_contacts("David")
    assert len(contacts) == 1
    assert contacts[0].email == "david@example.com"


def test_capability_registry_remains_authoritative_for_capability_list():
    registry = CapabilityRegistry()
    registry.register(
        Capability(
            name="desktop_control",
            description="Desktop",
            supported_actions={"open_application", "click"},
            status=CapabilityStatus.AVAILABLE,
        )
    )
    registry.register(
        Capability(
            name="memory",
            description="Memory",
            supported_actions={"contact_lookup"},
            status=CapabilityStatus.AVAILABLE,
        )
    )
    assert {cap.name for cap in registry.list_all()} == {"desktop_control", "memory"}
