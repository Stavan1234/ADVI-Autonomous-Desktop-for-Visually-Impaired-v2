"""Document creation regressions from the 2026-10-14 runtime log."""
from __future__ import annotations

import fitz
import pytest

from advi.capabilities.files.executor import FileCapability
from advi.capabilities.files.pdf_layout import split_title, wrap_text
from advi.core.verification import ToolVerifier
from advi.tools.contracts import ToolCall, ToolResult


def _speech(words: int = 1000) -> str:
    sentence = (
        "Courage is not the absence of fear but the decision that something else matters more, "
        "and every one of us carries that choice into each ordinary morning of our lives."
    )
    per_sentence = len(sentence.split())
    paragraphs, count = [], 0
    while count < words:
        body = " ".join([sentence] * 5)
        paragraphs.append(f"Paragraph {len(paragraphs) + 1}. {body}")
        count += 5 * per_sentence
    return "Be Brave, For God Is With You\n\n" + "\n\n".join(paragraphs)


def _verify(path, content, pages):
    arguments = {"path": str(path), "content": content, "style": "polished"}
    if pages is not None:
        arguments["pages"] = pages
    action = ToolCall("c1", "create_file", arguments)
    return ToolVerifier().verify(action, ToolResult(call_id="c1", name="create_file", success=True, output=str(path)))


@pytest.mark.parametrize("pages", [1, 2, None])
def test_long_speech_pdf_keeps_all_text_and_verifies(tmp_path, pages):
    # The old renderer clipped each paragraph to a 62pt box: 193 chars survived.
    path = tmp_path / "speech.pdf"
    content = _speech()

    created, parser = FileCapability._create_structured_document(path, content, pages=pages)
    assert created and parser == "pymupdf-polished"

    with fitz.open(str(path)) as document:
        text = " ".join(page.get_text("text") for page in document)
        page_count = len(document)
    words_in = len(content.split())
    words_out = len(text.split())
    assert words_out >= words_in, (words_in, words_out)
    last = content.count("Paragraph ")
    for n in (1, last // 2, last):
        assert f"Paragraph {n}." in text

    if pages is not None:
        assert page_count == pages
    result = _verify(path, content, pages)
    assert result.verified is True, result.verification_details


def test_overflow_beyond_requested_pages_flows_instead_of_dropping(tmp_path):
    path = tmp_path / "huge.pdf"
    content = _speech(4000)

    FileCapability._create_structured_document(path, content, pages=1)

    with fitz.open(str(path)) as document:
        text = " ".join(page.get_text("text") for page in document)
        assert len(document) > 1
    assert f"Paragraph {content.count('Paragraph ')}." in text


def test_long_first_line_is_body_not_a_clipped_title():
    paragraph = "This opening sentence is long enough that it is clearly a paragraph and not a title. " * 2
    title, body = split_title(paragraph + "\nSecond line.", "Speech")
    assert title == "Speech"
    assert paragraph.strip() in body
    assert split_title("# Report\nBody", "x") == ("Report", "Body")


def test_wrap_hard_splits_words_wider_than_a_line():
    font = fitz.Font("helv")
    lines = wrap_text("a " + "x" * 300, font, 10, 100)
    assert all(font.text_length(line, fontsize=10) <= 100 for line in lines)
    assert "".join(lines).replace(" ", "") == "a" + "x" * 300


@pytest.mark.parametrize("content", [
    "[Insert a 1000‑word speech here]",
    "<speech text>",
    "Your speech goes here.",
    "Placeholder",
    "Write the full speech here",
])
def test_placeholder_content_is_refused_before_writing(tmp_path, content):
    # Log 2026-10-14: "[Insert a 1000-word speech here]" verified and was reported as created.
    capability = FileCapability()
    path = tmp_path / "speech.pdf"
    result = capability._execute_create_file(ToolCall("c1", "create_file", {"path": str(path), "content": content}))
    assert result.success is False
    assert result.verification_details["reason"] == "placeholder_content"
    assert not path.exists()


def test_real_short_content_that_mentions_here_is_allowed():
    action = ToolCall("c1", "create_file", {"path": "x.txt", "content": "Meeting notes: we are here to plan the trip."})
    assert FileCapability._placeholder_rejection(action, action.arguments["content"]) is None
    long_text = "Please insert your card here. " + "Real paragraph text. " * 30
    assert FileCapability._placeholder_rejection(action, long_text) is None


def test_engine_removes_a_file_it_just_created_when_verification_fails(tmp_path):
    from advi.core.execution_engine import _discard_unverified_creation
    from advi.tools.contracts import VerificationStatus

    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.4 junk")
    call = ToolCall("c1", "create_file", {"path": str(path), "content": "x"})
    raw = ToolResult(call_id="c1", name="create_file", success=True, metadata={"created": True, "resolved_path": str(path)})
    failed = ToolResult(
        call_id="c1", name="create_file", success=False, error="Verification failed: content_mismatch",
        verified=False, verification_status=VerificationStatus.FAILED,
        verification_details={"definitive_failure": True, "reason": "content_mismatch"},
    )

    result = _discard_unverified_creation(call, raw, failed)

    assert not path.exists()
    assert result.verification_details["discarded_invalid_file"] == str(path)
    assert "removed" in result.error


def test_engine_never_removes_a_file_it_did_not_create(tmp_path):
    from advi.core.execution_engine import _discard_unverified_creation

    path = tmp_path / "existing.pdf"
    path.write_bytes(b"user file")
    call = ToolCall("c1", "create_file", {"path": str(path)})
    raw = ToolResult(call_id="c1", name="create_file", success=False, error="File already exists", metadata={"resolved_path": str(path)})
    failed = ToolResult(call_id="c1", name="create_file", success=False, verification_details={"definitive_failure": True})

    _discard_unverified_creation(call, raw, failed)

    assert path.exists()


def _open_with_tracked_window(tmp_path, monkeypatch, title_names_file):
    from types import SimpleNamespace

    target = tmp_path / "speech.pdf"
    target.write_bytes(b"%PDF-1.4")
    capability = FileCapability()
    capability.desktop_context = SimpleNamespace(active_document_path=str(target), target_hwnd=111)
    opened = []
    monkeypatch.setattr(FileCapability, "_window_title_names_file", staticmethod(lambda hwnd, path: title_names_file))
    monkeypatch.setattr(FileCapability, "_activate_opened_window", staticmethod(lambda hwnd: True))
    monkeypatch.setattr(capability, "_open_with_default_application", lambda path: opened.append(path) or 42)
    monkeypatch.setattr(capability, "_bind_opened_window", lambda path, previous_foreground_hwnd=None: (222, "speech.pdf - Edge", "msedge.exe", True))
    return capability._execute_open_file(ToolCall("c1", "open_file", {"path": str(target)})), opened


def test_closed_document_is_reopened_not_reported_as_already_open(tmp_path, monkeypatch):
    # Log 2026-10-14 turn 9: the PDF tab was closed but Edge survived; ADVI focused
    # Edge and said "already open; reused its existing window".
    result, opened = _open_with_tracked_window(tmp_path, monkeypatch, title_names_file=False)
    assert opened, "the file must actually be opened again"
    assert result.metadata.get("reused_existing") is not True
    assert result.verification_details["hwnd"] == 222


def test_document_still_showing_in_its_window_is_reused(tmp_path, monkeypatch):
    result, opened = _open_with_tracked_window(tmp_path, monkeypatch, title_names_file=True)
    assert not opened
    assert result.metadata["reused_existing"] is True
