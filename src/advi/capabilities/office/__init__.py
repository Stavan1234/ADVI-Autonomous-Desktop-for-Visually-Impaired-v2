"""Microsoft Office skills (Word) exposed as single verified tool calls."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from advi.tools.contracts import ToolCall, ToolResult, VerificationStatus

from . import word


class OfficeCapability:
    SUPPORTED_ACTIONS = {"word_create_document"}

    def __init__(
        self,
        resolve_path: Callable[[str], Path] | None = None,
        builder: Callable[..., None] | None = None,
        opener: Callable[[Path], None] | None = None,
    ) -> None:
        self._resolve = resolve_path or _default_resolver()
        self._build = builder or word.build_with_word
        self._open = opener or _open_in_word

    def is_available(self) -> bool:
        return word.word_available()

    def _execute_word_create_document(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        return self.execute(call, approval_granted=approval_granted)

    def execute(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        params = call.parameters
        name = call.action

        def fail(message: str, **meta) -> ToolResult:
            return ToolResult(call_id=call.call_id, name=name, success=False, error=message,
                              verified=False, verification_status=VerificationStatus.FAILED, metadata=meta)

        content = str(params.get("content") or "").strip()
        title = str(params.get("title") or "").strip()
        raw_path = str(params.get("path") or "").strip()
        if not content:
            return fail("No document content was given.")
        if not raw_path:
            raw_path = (title or "Document")[:80]
        blocks = word.parse_blocks(content, title)
        doc_title = next((b.text for b in blocks if b.kind == "title"), "") or title or "Document"
        safe_title = re.sub(r'[<>:"/\\|?*]+', " ", word.inline_spans(doc_title)[0]).strip()[:80] or "Document"
        target = self._resolve(raw_path)
        # "save it as a pdf in documents/ADVI Test" (a folder) produced
        # "Documents/ADVI Test.pdf" next to the folder. A folder means inside it.
        if target.is_dir():
            target = target / safe_title
        elif target.suffix.lower() in {".docx", ".pdf", ".doc"} and target.with_suffix("").is_dir():
            target = target.with_suffix("") / f"{safe_title}{target.suffix.lower()}"
        suffix = target.suffix.lower()
        if suffix not in {".docx", ".pdf"}:
            # "report.doc", "notes" or "speech.txt" asked of Word -> a .docx
            target = target.with_suffix(".docx") if suffix in {".doc", ".txt", ".rtf", ""} else target.with_name(target.name + ".docx")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return fail(f"I can't save in {target.parent}: {exc}")
        target = word.unique_path(target)

        try:
            self._build(blocks, target)
        except Exception as exc:
            return fail(f"Word couldn't create the document: {exc}", failure_class="word_automation_failed")
        if not target.exists():
            return fail("Word reported success but the file isn't on disk.")
        check = word.read_back(target)
        if not check.get("ok"):
            return fail(f"The saved file {target.name} came out empty.")
        expected = [b.text for b in blocks if b.kind in {"title", "h1", "h2"}][:3]
        if target.suffix.lower() == ".docx":
            missing = [t for t in expected if word.inline_spans(t)[0] not in check["text"]]
            if missing:
                return fail(f"Saved {target.name}, but it is missing: {', '.join(missing)}.")

        opened = False
        if params.get("open", True) not in (False, "false", "no", 0):
            try:
                self._open(target)
                opened = True
            except Exception:
                opened = False
        kind = "PDF" if target.suffix.lower() == ".pdf" else "Word document"
        size = f"{check['pages']} page{'s' if check['pages'] != 1 else ''}" if "pages" in check else f"{check['words']} words"
        message = f"Created the {kind} {target.name} in {target.parent} ({size})" + (" and opened it." if opened else ".")
        return ToolResult(
            call_id=call.call_id, name=name, success=True, output=str(target),
            human_readable=message, verified=True, verification_status=VerificationStatus.VERIFIED,
            verification_details={"method": "word_com_saved_file_read_back", **{k: v for k, v in check.items() if k != "text"}},
            metadata={"path": str(target), "opened": opened, "app": "word"},
        )


def _default_resolver() -> Callable[[str], Path]:
    from advi.capabilities.files.executor import FileCapability

    return FileCapability().resolve_path


def _open_in_word(path: Path) -> None:
    os.startfile(str(path))  # type: ignore[attr-defined]
    if path.suffix.lower() == ".docx":
        from . import nags

        nags.watch_in_background()
