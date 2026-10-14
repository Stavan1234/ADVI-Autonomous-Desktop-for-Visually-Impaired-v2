"""Word documents through Word's automation interface (works in 'Product Activation Failed')."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from advi.agent.session import AgentSession
from advi.capabilities.office import OfficeCapability
from advi.capabilities.office import word
from advi.tools.contracts import ToolCall, ToolSpec


def test_markdown_becomes_word_structure():
    blocks = word.parse_blocks(
        "# Courage\nIntro with **bold**.\n## Why\n- one\n1. first\n> quote\n| A | B |\n|---|---|\n| 1 | 2 |\nend"
    )
    assert [b.kind for b in blocks] == ["title", "normal", "h2", "bullet", "number", "quote", "table", "normal"]
    assert blocks[6].rows == [["A", "B"], ["1", "2"]]
    assert word.inline_spans("Intro with **bold** and *it*.") == ("Intro with bold and it.", [(11, 15, "bold"), (20, 22, "italic")])


def test_title_argument_is_not_repeated():
    blocks = word.parse_blocks("# Courage\ntext", title="Courage")
    assert [b.kind for b in blocks] == ["title", "normal"]


def _cap(tmp_path, built):
    def builder(blocks, target):
        built.append((blocks, target))
        import docx

        if target.suffix == ".pdf":
            target.write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj")
            return
        document = docx.Document()
        for b in blocks:
            document.add_paragraph(word.inline_spans(b.text)[0])
        document.save(str(target))

    return OfficeCapability(resolve_path=lambda p: tmp_path / p, builder=builder, opener=lambda path: None)


def test_saves_where_asked_and_never_overwrites(tmp_path):
    built = []
    cap = _cap(tmp_path, built)
    first = cap.execute(ToolCall("w", "word_create_document", {"path": "Speeches/courage", "content": "# Courage\nBe brave."}))
    second = cap.execute(ToolCall("w", "word_create_document", {"path": "Speeches/courage.docx", "content": "# Courage\nAgain."}))
    assert first.success and first.verified and first.output.endswith("courage.docx")
    assert second.output.endswith("courage (2).docx")
    assert "opened" in first.human_readable


def test_pdf_export_and_doc_suffix(tmp_path):
    cap = _cap(tmp_path, [])
    pdf = cap.execute(ToolCall("w", "word_create_document", {"path": "r.pdf", "content": "hello"}))
    doc = cap.execute(ToolCall("w", "word_create_document", {"path": "old.doc", "content": "hello"}))
    assert pdf.success and "1 page" in pdf.human_readable
    assert doc.output.endswith("old.docx")


def test_word_failure_is_reported_not_claimed(tmp_path):
    def broken(blocks, target):
        raise TimeoutError("Word stopped responding while building the document.")

    cap = OfficeCapability(resolve_path=lambda p: tmp_path / p, builder=broken, opener=lambda path: None)
    result = cap.execute(ToolCall("w", "word_create_document", {"path": "x.docx", "content": "hi"}))
    assert not result.success and "stopped responding" in result.error and not (tmp_path / "x.docx").exists()


def test_word_requests_route_to_the_word_tool_not_typing():
    session = AgentSession.__new__(AgentSession)
    session.task = None
    names = ["word_create_document", "create_file", "open_application", "type_text", "new_document", "save_document_as"]
    session.tools = SimpleNamespace(model_tools=lambda: [ToolSpec(n, n, "x") for n in names])
    for phrase in ("open word and write a speech on courage", "create a word document about AI and save it on desktop",
                   "write an essay in ms word"):
        offered = {t["function"]["name"] for t in session._select_model_tools(phrase)}
        assert "word_create_document" in offered, phrase
        assert not {"type_text", "new_document"} & offered, (phrase, offered)
    offered = {t["function"]["name"] for t in session._select_model_tools("create a pdf about AI")}
    assert "word_create_document" not in offered


def test_a_folder_path_saves_inside_the_folder(tmp_path):
    # "save it as a pdf in documents/ADVI Test" made Documents/ADVI Test.pdf.
    (tmp_path / "ADVI Test").mkdir()
    cap = _cap(tmp_path, [])
    as_pdf = cap.execute(ToolCall("w", "word_create_document", {"path": "ADVI Test.pdf", "content": "# Morning Walks\nWalk."}))
    plain = cap.execute(ToolCall("w", "word_create_document", {"path": "ADVI Test", "content": "# Study Tips\nRead."}))
    assert Path(as_pdf.output) == tmp_path / "ADVI Test" / "Morning Walks.pdf"
    assert Path(plain.output) == tmp_path / "ADVI Test" / "Study Tips.docx"


def test_model_misspelling_of_a_spoken_name_is_restored():
    # Live run: "save it in documents/ADVI Test" -> path "documents/ADMI Test".
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.last_user_message = "create a word document about time management and save it in documents/ADVI Test"
    fixed = session._restore_spoken_path_words(ToolCall("c", "word_create_document", {"path": "documents/ADMI Test/Time Management.docx", "content": "x"}))
    assert fixed.arguments["path"] == "documents/ADVI Test/Time Management.docx"
    same = session._restore_spoken_path_words(ToolCall("c", "word_create_document", {"path": "desktop/Report.docx", "content": "x"}))
    assert same.arguments["path"] == "desktop/Report.docx"


def test_activation_popups_are_told_apart_from_office_windows():
    from advi.capabilities.office.nags import is_nag

    assert is_nag("Microsoft Office Activation Wizard", "NUIDialog")
    assert is_nag("Unlicensed Product", "NUIDialog")
    # Every Office title here carries "(Product Activation Failed)"; the first
    # live run closed PowerPoint's own loading window because of it.
    assert not is_nag("Opening - PowerPoint (Product Activation Failed)", "NUIDialog")
    assert not is_nag("Word (Product Activation Failed)", "OpusApp")
    assert not is_nag("Save As", "#32770")


def test_store_apps_are_recognised_by_process_not_title(monkeypatch):
    # "Telegram (16372)" and "Untitled - Notepad" failed the title rule: 12-25 s waits.
    from advi.capabilities.desktop.executor import DesktopCapability

    cap = DesktopCapability()
    record = SimpleNamespace(executable=None, runtime_type="packaged", command=r"shell:AppsFolder\Telegram.TelegramDesktop")
    monkeypatch.setattr(cap, "_window_title", lambda hwnd: "Telegram (16372)")
    monkeypatch.setattr(cap, "_window_process_id", lambda hwnd: 7)
    monkeypatch.setattr(cap, "_process_name", lambda pid: "Telegram.exe")
    assert cap._application_window_matches_record("Telegram", 1, record)
    monkeypatch.setattr(cap, "_window_title", lambda hwnd: "Untitled - Notepad")
    monkeypatch.setattr(cap, "_process_name", lambda pid: "Notepad.exe")
    assert cap._application_window_matches_record("Notepad", 1, record)
    monkeypatch.setattr(cap, "_process_name", lambda pid: "WINWORD.EXE")
    monkeypatch.setattr(cap, "_window_title", lambda hwnd: "Notepad")
    assert not cap._application_window_matches_record("Notepad", 1, record)
