from __future__ import annotations

import codecs
import ctypes
import logging
import os
import re
import shutil
import subprocess
import fnmatch
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from advi.tools.contracts import ToolCall, ToolResult
from advi.environment import get_environment_paths
from advi.core.ocr import ocr_text_from_image

logger = logging.getLogger(__name__)


class FileCapability:
    """
    Filesystem Capability.
    Provides direct local file creation, reading, and path resolution (e.g. Desktop folder).
    """

    @staticmethod
    def _trash(path: Path) -> tuple[bool, str | None]:
        from .bulk import send_to_recycle_bin

        return send_to_recycle_bin(path)

    SUPPORTED_ACTIONS = {
        "play_local_media",
        "move_files",
        "organize_folder",
        "undo_file_operation",
        "save_file",
        "create_file",
        "append_file",
        "replace_file_text",
        "read_file",
        "delete_file",
        "copy_file",
        "move_file",
        "rename_file",
        "copy_folder",
        "move_folder",
        "rename_folder",
        "delete_folder",
        "list_files",
        "find_file",
        "search_file_content",
        "create_folder",
        "open_file",
        "open_folder",
    }

    def __init__(self, environment_watchtower: Any | None = None, desktop_context: Any | None = None) -> None:
        self.desktop_dir = self._get_desktop_dir()
        self.environment_watchtower = environment_watchtower
        # Shared with DesktopCapability so an opened document becomes the same
        # target for the following keyboard/mouse actions.
        self.desktop_context = desktop_context

    @staticmethod
    def _get_desktop_dir() -> Path:
        # Use the same canonical environment source as Watchtower and the rest
        # of ADVI; never derive a second user-home location independently.
        return get_environment_paths().desktop

    def resolve_path(self, target_path: str) -> Path:
        """Resolve friendly and common wrong-user Windows paths to canonical locations."""
        raw = str(target_path).strip()
        p = Path(raw).expanduser()
        if p.is_absolute():
            # Models sometimes invent the Windows username when they build a
            # path such as C:\Users\User\Downloads\x.pdf.  Recognize
            # the well-known user folders and map them to ADVI's actual profile.
            try:
                match = re.match(r"^([A-Za-z]):[\\/]Users[\\/][^\\/]+[\\/](Desktop|Documents|Downloads)(?:[\\/](.*))?$", str(p))
                if match:
                    aliases = {
                        "desktop": self.desktop_dir,
                        "documents": get_environment_paths().documents,
                        "downloads": get_environment_paths().downloads,
                    }
                    base = aliases[match.group(2).casefold()]
                    if match.group(3):
                        candidate = base / Path(match.group(3))
                    else:
                        candidate = base
                    # Preserve explicitly valid paths; remap only if the given
                    # path does not exist or points at a non-existent user profile.
                    if p.exists() or candidate.exists():
                        return p if p.exists() else candidate
                    return candidate
            except Exception:
                pass
            return p

        parts = p.parts
        if not parts:
            return self.desktop_dir

        home = Path(get_environment_paths().user_home)
        aliases = {
            "desktop": self.desktop_dir,
            "documents": get_environment_paths().documents,
            "downloads": get_environment_paths().downloads,
            # "Videos" used to fall through to Desktop\Videos.
            "videos": self._known_folder("18989B1D-99B5-455B-841C-AB7C74E4DDFC", home / "Videos"),
            "music": self._known_folder("4BD8D571-6D19-48D3-BE97-422220080E43", home / "Music"),
            "pictures": self._known_folder("33E28130-4E1E-4676-835A-98395C3BC3BB", home / "Pictures"),
            "photos": self._known_folder("33E28130-4E1E-4676-835A-98395C3BC3BB", home / "Pictures"),
        }
        base = aliases.get(parts[0].casefold())
        if base is not None:
            return base / Path(*parts[1:])

        # Default relative paths to Desktop for desktop-assistant convenience.
        return self.desktop_dir / p

    @staticmethod
    def _is_within(path: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                if path == root or root in path.parents:
                    return True
            except (OSError, RuntimeError):
                continue
        return False

    @classmethod
    def _path_requires_confirmation(cls, path: Path) -> bool:
        """Return True only for system-level Windows locations.

        Normal user files remain friction-free. System folders and Startup are
        contextual confirmation boundaries because changes there can affect the
        machine beyond the requested document. The path is resolved first so a
        symlink/junction cannot trivially bypass the check.
        """
        if os.name != "nt":
            return False

        try:
            resolved = path.resolve(strict=False)
        except OSError:
            resolved = Path(os.path.abspath(str(path)))

        # Keep normal user data (Desktop/Documents/Downloads/drives) friction-free.
        # Only Windows system binaries and installed program trees are protected
        # because writes there can destabilize the machine or an installed app.
        system_root = os.environ.get("SystemRoot")
        candidates = [
            str(Path(system_root) / "System32") if system_root else None,
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
        ]

        roots: list[Path] = []
        for raw in candidates:
            if not raw:
                continue
            try:
                roots.append(Path(raw).expanduser().resolve(strict=False))
            except OSError:
                continue
        return cls._is_within(resolved, roots)

    def _check_write_path(self, action: ToolCall, target_file: Path, *, operation: str, approval_granted: bool) -> ToolResult | None:
        if not self._path_requires_confirmation(target_file) or approval_granted:
            return None
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=False,
            requires_confirmation=True,
            confirmation_reason=f"{operation} targets a protected Windows location: {target_file}",
            error="This file operation targets a protected Windows location and needs confirmation.",
            metadata={"path": str(target_file), "protected_location": True, "operation": operation},
        )

    def execute(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        handler = getattr(self, f"_execute_{action.action}", None)
        if handler is None:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Unsupported file action: {action.action}",
            )

        try:
            return handler(action, approval_granted=approval_granted)
        except Exception as exc:
            logger.exception("File action failed: %s", action.action)
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=str(exc),
            )

    _PLACEHOLDER_PATTERNS = (
        # The whole document is one bracketed instruction: "[Insert a 1000-word speech here]".
        re.compile(r"^[\[<{(][^\]>})]{0,200}[\]>})]$"),
        re.compile(r"\b(?:insert|add|put|write|paste|place)\b.{0,80}\bhere\b"),
        re.compile(r"^(?:placeholder|todo|tbd|content goes here|text goes here)\W*$"),
        re.compile(r"\b(?:your|the)\s+(?:speech|content|text|essay|document|story)\s+(?:goes|will go)\s+here\b"),
    )

    @classmethod
    def _placeholder_rejection(cls, action: ToolCall, content: Any) -> ToolResult | None:
        """Refuse to write a stand-in instead of the requested text.

        In the runtime log the model, after two failed attempts, wrote
        "[Insert a 1000-word speech here]"; it verified and ADVI reported the
        speech as created. Only short content is checked, so real documents
        that merely mention these words are never blocked.
        """
        text = str(content or "").strip()
        if not text or len(text) > 400:
            return None
        lowered = text.casefold()
        if not any(pattern.search(lowered) for pattern in cls._PLACEHOLDER_PATTERNS):
            return None
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=False,
            error=(
                f"Content is a placeholder ({text[:80]!r}), not the requested text. "
                "Write the complete document text in `content`; never write a stand-in."
            ),
            verified=False,
            verification_status="failed",
            verification_details={"reason": "placeholder_content", "definitive_failure": True},
        )

    def _execute_save_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("filename") or action.target or ""
        content = action.parameters.get("content") or action.parameters.get("value") or ""
        placeholder = self._placeholder_rejection(action, content)
        if placeholder is not None:
            return placeholder

        if not path_str:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No file path or filename provided.",
            )

        target_file = self.resolve_path(str(path_str))
        blocked = self._check_write_path(action, target_file, operation="write", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        target_file.parent.mkdir(parents=True, exist_ok=True)

        data = str(content)
        suffix = target_file.suffix.casefold()
        structured_suffixes = {".docx", ".xlsx", ".pptx", ".pdf"}
        temp_file = target_file.with_name(f".{target_file.stem}.advi-save-{uuid.uuid4().hex}{suffix or '.tmp'}")
        parser = "text"
        try:
            if suffix in structured_suffixes:
                created, parser = self._create_structured_document(
                    temp_file, data,
                    pages=action.parameters.get("pages"),
                    slides=action.parameters.get("slides"),
                )
                if not created:
                    return ToolResult(
                        call_id=action.call_id, name=action.action, success=False,
                        error=f"Could not create a valid structured document for '{target_file.name}'.",
                        metadata={"path": str(target_file), "resolved_path": str(target_file), "format": suffix},
                    )
                os.replace(temp_file, target_file)

                # Never report a structured document as successfully saved until
                # its native parser can reopen it and recover at least some
                # structured content. This prevents a binary format from being
                # silently replaced by plain text with a matching extension.
                parsed_text, parsed_parser = self._read_structured_file(target_file)
                parser = parsed_parser or parser
                if suffix == ".pdf":
                    import fitz
                    document = fitz.open(str(target_file))
                    try:
                        page_count = len(document)
                    finally:
                        document.close()
                    if page_count < 1 or not parsed_text.strip():
                        raise RuntimeError("Generated PDF could not be reopened with PyMuPDF or contains no readable text.")
                elif not parsed_text.strip() and data.strip():
                    raise RuntimeError(f"Generated {suffix} document contains no readable content.")
            else:
                temp_file = target_file.with_name(f".{target_file.name}.advi-tmp")
                temp_file.write_text(data, encoding="utf-8")
                os.replace(temp_file, target_file)
        except Exception as exc:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error=f"Could not save '{target_file.name}': {exc}",
                metadata={"path": str(target_file), "resolved_path": str(target_file), "format": suffix or "text", "parser": parser},
            )
        finally:
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except OSError:
                pass
        size = target_file.stat().st_size

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=str(target_file),
            human_readable=f"Saved {parser} document '{target_file.name}' to {target_file.parent} ({size} bytes)." if parser != "text" else f"Saved file '{target_file.name}' to {target_file.parent} ({size} bytes).",
            verified=True,
            verification_details={"path": str(target_file), "exists": True, "size_bytes": size, "parser": parser},
            metadata={"path": str(target_file), "resolved_path": str(target_file), "size": size, "parser": parser},
        )

    @staticmethod
    def _partition_content(content: str, count: int) -> list[str]:
        """Split content into approximately even chunks without dropping text."""
        count = max(1, int(count))
        text = str(content or "")
        if count == 1:
            return [text]
        if not text:
            return [""] * count

        # Prefer paragraph boundaries so generated slides/pages remain readable.
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        if len(paragraphs) >= count:
            chunks: list[str] = []
            for index in range(count):
                start = round(index * len(paragraphs) / count)
                end = round((index + 1) * len(paragraphs) / count)
                chunk = "\n\n".join(paragraphs[start:end]).strip()
                chunks.append(chunk)
            return chunks

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= count:
            return [
                "\n".join(lines[round(index * len(lines) / count):round((index + 1) * len(lines) / count)]).strip()
                for index in range(count)
            ]

        # Do not split a short document into meaningless character fragments.
        # Preserve the whole request on the first page/slide and pad only when the
        # caller explicitly asked for more pages/slides than the content naturally needs.
        if len(text) < count * 200:
            return [text] + [""] * (count - 1)

        return [
            text[round(index * len(text) / count):round((index + 1) * len(text) / count)].strip()
            for index in range(count)
        ]

    @staticmethod
    def _create_structured_document(
        path: Path,
        content: str,
        *,
        pages: int | str | None = None,
        slides: int | str | None = None,
    ) -> tuple[bool, str]:
        """Create a valid common document based on its extension.

        Text formats stay plain text. Office/PDF formats are generated with the
        project's installed native Python libraries so a .docx/.xlsx/.pptx/.pdf
        is never a text file wearing a document extension.
        """
        suffix = path.suffix.casefold()
        if suffix not in {".docx", ".xlsx", ".pptx", ".pdf"}:
            return False, ""

        if suffix == ".docx":
            from docx import Document
            document = Document()
            requested_pages = max(1, int(pages)) if pages not in (None, "") else None
            if requested_pages and requested_pages > 1:
                chunks = FileCapability._partition_content(content, requested_pages)
                for index, chunk in enumerate(chunks):
                    document.add_paragraph(chunk)
                    if index < requested_pages - 1:
                        document.add_page_break()
            else:
                lines = content.splitlines() or [""]
                for line in lines:
                    document.add_paragraph(line)
            document.save(path)
            return True, "python-docx"

        if suffix == ".xlsx":
            from openpyxl import Workbook
            import csv
            from io import StringIO
            workbook = Workbook()
            sheet = workbook.active
            if content:
                rows = list(csv.reader(StringIO(content)))
                for row in rows:
                    converted = []
                    for value in row:
                        text = str(value).strip()
                        try:
                            if text and re.fullmatch(r"[-+]?\d+", text):
                                converted.append(int(text))
                            elif text and re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)", text):
                                converted.append(float(text))
                            else:
                                converted.append(value)
                        except (TypeError, ValueError):
                            converted.append(value)
                    sheet.append(converted)
            workbook.save(path)
            return True, "openpyxl"

        if suffix == ".pptx":
            from pptx import Presentation
            from pptx.dml.color import RGBColor
            from pptx.enum.shapes import MSO_SHAPE
            from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
            from pptx.util import Inches, Pt

            presentation = Presentation()
            presentation.slide_width = Inches(13.333)
            presentation.slide_height = Inches(7.5)
            requested_slides = max(1, int(slides)) if slides not in (None, "") else None
            count = requested_slides or max(1, min(12, len([x for x in content.split("\n\n") if x.strip()]) or 1))
            chunks = FileCapability._partition_content(content, count)
            if count > 1 and len(chunks) < count:
                chunks.extend([""] * (count-len(chunks)))

            navy = RGBColor(31, 63, 110)
            blue = RGBColor(45, 104, 180)
            teal = RGBColor(48, 143, 145)
            ink = RGBColor(34, 42, 52)
            muted = RGBColor(92, 103, 116)
            light = RGBColor(245, 248, 252)
            white = RGBColor(255, 255, 255)
            border = RGBColor(214, 222, 232)

            def add_textbox(slide, x, y, w, h, text, size, font="Aptos", bold=False, color=ink, align=PP_ALIGN.LEFT, valign=MSO_ANCHOR.TOP):
                box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
                tf = box.text_frame
                tf.clear(); tf.word_wrap = True; tf.vertical_anchor = valign
                p0 = tf.paragraphs[0]
                p0.alignment = align
                run = p0.add_run(); run.text = str(text)
                run.font.name = font; run.font.size = Pt(size); run.font.bold = bold; run.font.color.rgb = color
                return box

            def add_header(slide, title, index):
                slide.background.fill.solid(); slide.background.fill.fore_color.rgb = light
                bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(13.333), Inches(0.18))
                bar.fill.solid(); bar.fill.fore_color.rgb = blue; bar.line.fill.background()
                title_shape = slide.shapes.title
                title_shape.text = title
                title_shape.left=Inches(0.65); title_shape.top=Inches(0.45); title_shape.width=Inches(11.5); title_shape.height=Inches(0.65)
                for para in title_shape.text_frame.paragraphs:
                    para.font.name="Aptos Display"; para.font.size=Pt(25); para.font.bold=True; para.font.color.rgb=navy
                add_textbox(slide, 11.95, 0.5, 0.7, 0.35, f"{index:02d}", 11, font="Aptos", bold=True, color=teal, align=PP_ALIGN.RIGHT)
                footer = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.65), Inches(7.05), Inches(12.0), Inches(0.02))
                footer.fill.solid(); footer.fill.fore_color.rgb = border; footer.line.fill.background()
                add_textbox(slide, 0.65, 7.08, 4.5, 0.2, "ADVI • generated document", 8.5, color=muted)

            def markdown_table(lines):
                table=[]
                for line in lines:
                    t=line.strip()
                    if not (t.startswith("|") and t.endswith("|")):
                        continue
                    cells=[c.strip() for c in t[1:-1].split("|")]
                    if all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
                        continue
                    if cells: table.append(cells)
                if len(table)>=2:
                    cols=max(len(r) for r in table)
                    return [r + [""]*(cols-len(r)) for r in table]
                return []

            def add_table(slide, rows, y=1.55, h=4.75):
                cols=max(1, max(len(r) for r in rows)); row_count=len(rows)
                shape=slide.shapes.add_table(row_count, cols, Inches(0.75), Inches(y), Inches(11.8), Inches(h))
                tbl=shape.table
                for c in range(cols):
                    tbl.columns[c].width = Inches(11.8/cols)
                for r in range(row_count):
                    for c in range(cols):
                        cell=tbl.cell(r,c)
                        cell.text = str(rows[r][c])
                        cell.margin_left=Inches(0.07); cell.margin_right=Inches(0.07); cell.margin_top=Inches(0.05); cell.margin_bottom=Inches(0.05)
                        cell.fill.solid(); cell.fill.fore_color.rgb = navy if r==0 else white
                        for para in cell.text_frame.paragraphs:
                            para.font.name="Aptos"; para.font.size=Pt(10 if r==0 else 9.5); para.font.bold=(r==0); para.font.color.rgb=white if r==0 else ink
                            para.alignment=PP_ALIGN.LEFT
                return shape

            title_lines=[x.strip() for x in content.splitlines() if x.strip()]
            title=title_lines[0] if title_lines else "ADVI Document"
            if count == 1:
                slide=presentation.slides.add_slide(presentation.slide_layouts[5])
                slide.background.fill.solid(); slide.background.fill.fore_color.rgb = navy
                accent=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(0.25), Inches(7.5))
                accent.fill.solid(); accent.fill.fore_color.rgb=teal; accent.line.fill.background()
                title_shape = slide.shapes.title
                title_shape.text = title
                title_shape.left=Inches(0.9); title_shape.top=Inches(1.35); title_shape.width=Inches(11.6); title_shape.height=Inches(1.4)
                for para in title_shape.text_frame.paragraphs:
                    para.font.name="Aptos Display"; para.font.size=Pt(34); para.font.bold=True; para.font.color.rgb=white
                add_textbox(slide, 0.95, 3.0, 10.7, 0.7, "A polished document generated by ADVI", 17, font="Georgia", color=RGBColor(220,231,245))
                rest="\n".join(title_lines[1:])
                if rest:
                    add_textbox(slide, 0.95, 4.0, 11.0, 1.8, rest, 14, font="Aptos", color=white)
                add_textbox(slide, 0.95, 6.75, 5.5, 0.3, "ADVI • polished-colorful", 9.5, color=RGBColor(180,205,230))
            else:
                for i, chunk in enumerate(chunks[:count], start=1):
                    slide=presentation.slides.add_slide(presentation.slide_layouts[5])
                    lines=[ln.strip() for ln in str(chunk).splitlines() if ln.strip()]
                    slide_title = (lines[0] if i == 1 and lines else (title if i == 1 else f"{title} • Part {i}"))
                    add_header(slide, slide_title, i)
                    rest=lines[1:] if i==1 and lines else lines
                    rows=markdown_table(rest)
                    if rows:
                        add_table(slide, rows)
                    elif rest:
                        y=1.45
                        for line in rest[:8]:
                            if re.match(r"^#{1,3}\s+", line):
                                line=re.sub(r"^#{1,3}\s+", "", line)
                                add_textbox(slide, 0.75, y, 11.7, 0.4, line, 16, font="Aptos Display", bold=True, color=blue); y+=0.55
                            else:
                                card=slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.75), Inches(y), Inches(11.8), Inches(0.62))
                                card.fill.solid(); card.fill.fore_color.rgb=white; card.line.color.rgb=border
                                prefix="• " if not line.startswith("•") else ""
                                add_textbox(slide, 1.0, y+0.12, 11.15, 0.36, prefix+line, 12.5, color=ink)
                                y+=0.72
            presentation.save(path)
            return True, "python-pptx"

        if suffix == ".pdf":
            import fitz
            from .pdf_layout import render_polished_pdf, split_title

            fallback_title = re.sub(r"[_-]+", " ", path.stem).strip().title() or "ADVI Document"
            title, body = split_title(content, fallback_title)
            requested_pages = max(1, int(pages)) if pages not in (None, "") else None
            chunks = FileCapability._partition_content(body, requested_pages) if requested_pages else [body]
            render_polished_pdf(path, chunks, title=title)
            check = fitz.open(str(path))
            try:
                if len(check) < 1 or not any((page.get_text("text") or "").strip() for page in check):
                    raise RuntimeError("Generated PDF is empty or unreadable.")
            finally:
                check.close()
            return True, "pymupdf-polished"

        return False, ""

    def _execute_create_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("filename") or action.target or ""
        content = action.parameters.get("content") if action.parameters.get("content") is not None else action.parameters.get("value", "")
        if not path_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No file path or filename provided.")
        placeholder = self._placeholder_rejection(action, content)
        if placeholder is not None:
            return placeholder

        target_file = self.resolve_path(str(path_str))
        blocked = self._check_write_path(action, target_file, operation="create", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if target_file.exists():
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                output=str(target_file),
                human_readable=f"File '{target_file.name}' already exists; checking whether it matches the requested content.",
                error=f"File already exists: {target_file}",
                verified=False, verification_status="uncertain",
                verification_details={"path": str(target_file), "exists": True, "already_exists": True},
                metadata={"path": str(target_file), "resolved_path": str(target_file), "already_exists": True},
            )

        target_file.parent.mkdir(parents=True, exist_ok=True)
        data = str(content)
        suffix = target_file.suffix.casefold()

        # Office/PDF formats need their real binary container. Build to a unique
        # temporary path and publish atomically only after successful serialization.
        if suffix in {".docx", ".xlsx", ".pptx", ".pdf"}:
            temp_file = target_file.with_name(f".{target_file.stem}.advi-create-{uuid.uuid4().hex}{suffix}")
            parser = ""
            try:
                created, parser = self._create_structured_document(
                    temp_file, data,
                    pages=action.parameters.get("pages"),
                    slides=action.parameters.get("slides"),
                )
                if not created:
                    raise RuntimeError(f"Could not create structured document type '{suffix}'.")
                try:
                    temp_file.replace(target_file)
                except FileExistsError:
                    temp_file.unlink(missing_ok=True)
                    return ToolResult(
                        call_id=action.call_id, name=action.action, success=True,
                        output=str(target_file),
                        human_readable=f"File '{target_file.name}' already exists; checking whether it matches the requested content.",
                        verified=False, verification_status="uncertain",
                        verification_details={"path": str(target_file), "exists": True, "already_exists": True},
                        metadata={"path": str(target_file), "resolved_path": str(target_file), "already_exists": True},
                    )
            finally:
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            # Plain formats use exclusive creation so a race can never overwrite
            # an existing file.
            parser = "text"
            try:
                with target_file.open("x", encoding="utf-8", newline="") as handle:
                    handle.write(data)
            except FileExistsError:
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=True,
                    output=str(target_file),
                    human_readable=f"File '{target_file.name}' already exists; checking whether it matches the requested content.",
                    verified=False, verification_status="uncertain",
                    verification_details={"path": str(target_file), "exists": True, "already_exists": True},
                    metadata={"path": str(target_file), "resolved_path": str(target_file), "already_exists": True},
                )

        exists = target_file.is_file()
        size = target_file.stat().st_size if exists else 0
        return ToolResult(
            call_id=action.call_id, name=action.action, success=exists,
            output=str(target_file),
            human_readable=f"Created {parser} document '{target_file.name}' in {target_file.parent} ({size} bytes)." if exists else f"Could not create '{target_file.name}'.",
            verified=exists, verification_status="verified" if exists else "failed",
            verification_details={"path": str(target_file), "exists": exists, "size_bytes": size, "parser": parser},
            metadata={"path": str(target_file), "resolved_path": str(target_file), "size": size, "created": exists, "parser": parser},
        )

    def _execute_append_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("filename") or action.target or ""
        content = action.parameters.get("content") or action.parameters.get("value")
        if not path_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No file path or filename provided.")
        if content is None:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No content provided to append.")

        target_file = self.resolve_path(str(path_str))
        blocked = self._check_write_path(action, target_file, operation="append", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if not target_file.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"File not found: {target_file}")

        suffix = target_file.suffix.casefold()
        if suffix in {".pdf", ".docx", ".xlsx", ".pptx"}:
            # Structured files must be modified through their native libraries.
            # Appending bytes/text to a ZIP/PDF container would corrupt it.
            data = str(content)
            temp_file = target_file.with_name(f".{target_file.stem}.advi-append-{uuid.uuid4().hex}{suffix}")
            try:
                if suffix == ".docx":
                    from docx import Document
                    document = Document(str(target_file))
                    document.add_paragraph(data)
                    document.save(temp_file)
                elif suffix == ".xlsx":
                    from openpyxl import load_workbook
                    import csv
                    from io import StringIO
                    workbook = load_workbook(filename=str(target_file))
                    try:
                        sheet = workbook.active
                        for row in csv.reader(StringIO(data)):
                            sheet.append(list(row))
                        workbook.save(temp_file)
                    finally:
                        workbook.close()
                elif suffix == ".pptx":
                    from pptx import Presentation
                    presentation = Presentation(str(target_file))
                    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
                    lines = data.splitlines()
                    slide.shapes.title.text = lines[0] if lines else f"Slide {len(presentation.slides)}"
                    body = slide.placeholders[1].text_frame
                    body.clear()
                    for line in lines[1:] or ([""] if not lines else []):
                        paragraph = body.paragraphs[0] if not body.text else body.add_paragraph()
                        paragraph.text = line
                    presentation.save(temp_file)
                else:  # PDF
                    import fitz
                    document = fitz.open(str(target_file))
                    try:
                        page = document.new_page()
                        safe = (data.replace("\u2018", "'").replace("\u2019", "'")
                                     .replace("\u201c", '\"').replace("\u201d", '\"')
                                     .replace("\u2013", "-").replace("\u2014", "-")
                                     .replace("\u2026", "...").replace("\u00a0", " ").replace("\u202f", " "))
                        rc = page.insert_textbox(fitz.Rect(54, 54, 540, 760), safe[:1800], fontsize=11, lineheight=1.25)
                        if rc < 0:
                            raise RuntimeError("Appended PDF text did not fit on the new page.")
                        document.save(temp_file)
                    finally:
                        document.close()
                os.replace(temp_file, target_file)
            except Exception as exc:
                return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not append to structured file '{target_file.name}': {exc}")
            finally:
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            with target_file.open("a", encoding="utf-8") as handle:
                handle.write(str(content))
        size = target_file.stat().st_size
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=str(target_file),
            human_readable=f"Appended {len(str(content))} characters to {target_file.name}.",
            verified=True,
            verification_status="verified",
            verification_details={"path": str(target_file), "exists": True, "size_bytes": size},
            metadata={"path": str(target_file), "resolved_path": str(target_file), "appended_content": str(content), "size": size},
        )

    def _execute_replace_file_text(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("filename") or action.target or ""
        old_text = action.parameters.get("old_text")
        new_text = action.parameters.get("new_text")
        if not path_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No file path or filename provided.")
        if old_text is None or new_text is None:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Both old_text and new_text are required.")

        target_file = self.resolve_path(str(path_str))
        blocked = self._check_write_path(action, target_file, operation="replace", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if not target_file.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"File not found: {target_file}")
        suffix = target_file.suffix.casefold()
        parser = "text"
        if suffix in {".pdf", ".docx", ".xlsx", ".pptx"}:
            original, parser = self._read_structured_file(target_file)
        else:
            original = target_file.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(str(old_text))
        if occurrences == 0:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="The requested old_text was not found in the file.")
        updated = original.replace(str(old_text), str(new_text), 1)
        if suffix in {".pdf", ".docx", ".xlsx", ".pptx"}:
            temp_file = target_file.with_name(f".{target_file.stem}.advi-replace-{uuid.uuid4().hex}{suffix}")
            try:
                created, parser = self._create_structured_document(temp_file, updated)
                if not created:
                    raise RuntimeError(f"Could not rebuild structured document '{target_file.name}'.")
                os.replace(temp_file, target_file)
            except Exception as exc:
                return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not replace text in structured file '{target_file.name}': {exc}")
            finally:
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            target_file.write_text(updated, encoding="utf-8")
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=str(target_file),
            human_readable=f"Replaced one occurrence in {target_file.name}.",
            verified=True,
            verification_status="verified",
            verification_details={"path": str(target_file), "exists": True, "replacement_present": str(new_text) in target_file.read_text(encoding="utf-8", errors="replace")},
            metadata={"path": str(target_file), "resolved_path": str(target_file), "old_text": str(old_text), "new_text": str(new_text), "occurrences_before": occurrences},
        )

    @staticmethod
    def _read_structured_file(path: Path) -> tuple[str, str]:
        """Read common user document formats through their native Python parsers."""
        suffix = path.suffix.casefold()

        if suffix == ".pdf":
            try:
                import fitz  # PyMuPDF
            except ImportError as exc:
                raise RuntimeError("PDF reading requires PyMuPDF (package 'pymupdf').") from exc

            document = fitz.open(str(path))
            try:
                pages: list[str] = []
                has_embedded_text = False
                for index, page in enumerate(document):
                    text = str(page.get_text("text") or "").strip()
                    if text:
                        has_embedded_text = True
                        pages.append(f"[Page {index + 1}]\n{text}")
                        continue

                    # Scanned/image-only PDFs have no text layer. OCR only those
                    # pages so ordinary PDFs stay cheap. Limit raster size to keep
                    # a pathological document from consuming the whole interactive turn.
                    try:
                        import pytesseract
                        from PIL import Image
                    except ImportError:
                        pages.append(f"[Page {index + 1}]\n[No embedded text; OCR dependencies unavailable]")
                        continue
                    try:
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
                        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
                        ocr_text = ocr_text_from_image(image, pytesseract, page_segmentation_mode=11)
                        pages.append(f"[Page {index + 1}]\n{ocr_text or '[No readable text found]'}")
                    except Exception as exc:
                        logger.debug("PDF page OCR failed for %s page %s", path, index + 1, exc_info=True)
                        pages.append(f"[Page {index + 1}]\n[OCR failed: {exc}]")

                parser = "pymupdf" if has_embedded_text else "pymupdf+tesseract"
                return "\n\n".join(pages), parser
            finally:
                document.close()

        if suffix == ".docx":
            try:
                from docx import Document
                document = Document(str(path))
                parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
                for table in document.tables:
                    for row in table.rows:
                        parts.append(" | ".join(cell.text for cell in row.cells))
                return "\n".join(parts), "python-docx"
            except ImportError as exc:
                raise RuntimeError("Word document reading requires python-docx.") from exc

        if suffix == ".xlsx":
            try:
                from openpyxl import load_workbook
                workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
                try:
                    parts: list[str] = []
                    for sheet in workbook.worksheets:
                        parts.append(f"[Sheet: {sheet.title}]")
                        for row in sheet.iter_rows(values_only=True):
                            values = ["" if value is None else str(value) for value in row]
                            if any(values):
                                parts.append("\t".join(values))
                finally:
                    workbook.close()
                return "\n".join(parts), "openpyxl"
            except ImportError as exc:
                raise RuntimeError("Excel workbook reading requires openpyxl.") from exc

        if suffix == ".pptx":
            try:
                from pptx import Presentation
                presentation = Presentation(str(path))
                parts: list[str] = []
                for index, slide in enumerate(presentation.slides, start=1):
                    parts.append(f"[Slide {index}]")
                    for shape in slide.shapes:
                        text = getattr(shape, "text", "")
                        if text and text.strip():
                            parts.append(text.strip())
                return "\n".join(parts), "python-pptx"
            except ImportError as exc:
                raise RuntimeError("PowerPoint reading requires python-pptx.") from exc

        return "", ""

    @staticmethod
    def _read_image_with_ocr(path: Path) -> tuple[str, str]:
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Image OCR requires Pillow and pytesseract, plus a working Tesseract installation.") from exc
        with Image.open(path) as image:
            text = ocr_text_from_image(image, pytesseract, page_segmentation_mode=11)
        return text, "tesseract_ocr"

    @staticmethod
    def _decode_text_bytes(data: bytes) -> tuple[str, str, bool]:
        """Decode ordinary text files without turning Windows encodings into garbage.

        BOMs are authoritative. Without a BOM, UTF-8 is preferred, followed by the
        Windows ANSI code page (where available) and finally Latin-1 as a lossless
        byte-to-text fallback. NUL bytes without a Unicode BOM are treated as a
        binary signal rather than passed through as replacement characters.
        """
        if data.startswith(codecs.BOM_UTF8):
            return data.decode("utf-8-sig"), "utf-8-sig", True
        if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
            return data.decode("utf-16"), "utf-16", True
        if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
            return data.decode("utf-32"), "utf-32", True
        if b"\x00" in data:
            return "", "binary", False

        try:
            return data.decode("utf-8"), "utf-8", True
        except UnicodeDecodeError:
            pass

        if os.name == "nt":
            try:
                return data.decode("mbcs"), "windows-mbcs", True
            except UnicodeDecodeError:
                pass

        try:
            return data.decode("cp1252"), "cp1252", True
        except UnicodeDecodeError:
            return data.decode("latin-1"), "latin-1", True

    def _read_file_content(self, path: Path) -> tuple[str, str, bool]:
        suffix = path.suffix.casefold()
        image_suffixes = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
        if suffix in image_suffixes:
            text, parser = self._read_image_with_ocr(path)
            return text, parser, True

        structured_text, parser = self._read_structured_file(path)
        if parser:
            return structured_text, parser, True

        data = path.read_bytes()
        return self._decode_text_bytes(data)

    def _execute_read_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("filename") or action.target or ""
        if not path_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No file path provided.")

        target_file = self.resolve_path(str(path_str))
        if not target_file.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"File not found: {target_file}")
        if not target_file.is_file():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Not a file: {target_file}")

        offset = max(0, int(action.parameters.get("offset") or 0))
        max_chars = max(1, min(int(action.parameters.get("max_chars") or 8000), 16000))
        try:
            full_text, parser, readable = self._read_file_content(target_file)
        except Exception as exc:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error=f"Could not read '{target_file.name}': {exc}",
                metadata={"path": str(target_file), "resolved_path": str(target_file), "suffix": target_file.suffix.casefold()},
            )

        if not readable:
            size = target_file.stat().st_size
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error=f"'{target_file.name}' is a binary file without a native reader in ADVI yet.",
                metadata={"path": str(target_file), "resolved_path": str(target_file), "size_bytes": size, "format": target_file.suffix.casefold(), "readable_as_text": False, "open_file_available": True},
            )

        chunk = full_text[offset : offset + max_chars]
        end_offset = offset + len(chunk)
        has_more = end_offset < len(full_text)
        return ToolResult(
            call_id=action.call_id, name=action.action,
            success=True,
            output=chunk,
            human_readable=(
                f"Read {len(chunk)} characters from {target_file.name} using {parser}"
                + (f"; more content is available from character {end_offset}." if has_more else ".")
            ),
            verified=True,
            verification_status="verified",
            verification_details={
                "path": str(target_file), "exists": True, "parser": parser,
                "total_chars": len(full_text), "offset": offset,
                "returned_chars": len(chunk), "has_more": has_more,
            },
            metadata={
                "path": str(target_file), "resolved_path": str(target_file),
                "length": len(full_text), "offset": offset,
                "returned_chars": len(chunk), "has_more": has_more,
                "parser": parser, "format": target_file.suffix.casefold(),
            },
        )

    def _execute_delete_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.target or ""
        target_file = self.resolve_path(str(path_str))
        blocked = self._check_write_path(action, target_file, operation="delete", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if not target_file.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"File not found: {target_file}")

        recycled, problem = self._trash(target_file)
        if not recycled:
            return ToolResult(call_id=action.call_id, name=action.action, success=False,
                              error=f"I didn't delete '{target_file.name}': {problem}",
                              verification_details={"path": str(target_file), "reason": "recycle_bin_unavailable"})
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=str(target_file),
            human_readable=f"Moved {target_file.name} to the Recycle Bin.",
            verified=not target_file.exists(),
            verification_status="verified" if not target_file.exists() else "failed",
            verification_details={"path": str(target_file), "exists": target_file.exists()},
            metadata={"path": str(target_file), "resolved_path": str(target_file), "exists": target_file.exists()},
        )

    def _execute_copy_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        source_str = action.parameters.get("source") or action.parameters.get("from") or action.parameters.get("path") or ""
        destination_str = action.parameters.get("destination") or action.parameters.get("to") or action.parameters.get("target") or ""
        if not source_str or not destination_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Copy requires both source and destination paths.")
        source = self.resolve_path(str(source_str))
        destination = self.resolve_path(str(destination_str))
        if not source.exists() or not source.is_file():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Source file not found: {source}")
        blocked = self._check_write_path(action, destination, operation="copy", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Destination already exists: {destination}")
        shutil.copy2(source, destination)
        exists = destination.is_file()
        return ToolResult(call_id=action.call_id, name=action.action, success=exists, output=str(destination),
                          human_readable=f"Copied '{source.name}' to '{destination}'.",
                          verified=exists, verification_status="verified" if exists else "failed",
                          verification_details={"source": str(source), "destination": str(destination), "exists": exists},
                          metadata={"source": str(source), "destination": str(destination), "size_bytes": destination.stat().st_size if exists else None})

    def _execute_move_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        source_str = action.parameters.get("source") or action.parameters.get("from") or action.parameters.get("path") or ""
        destination_str = action.parameters.get("destination") or action.parameters.get("to") or action.parameters.get("target") or ""
        if not source_str or not destination_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Move requires both source and destination paths.")
        source = self.resolve_path(str(source_str))
        destination = self.resolve_path(str(destination_str))
        if not source.exists() or not source.is_file():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Source file not found: {source}")
        blocked = self._check_write_path(action, destination, operation="move", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Destination already exists: {destination}")
        shutil.move(str(source), str(destination))
        verified = destination.is_file() and not source.exists()
        return ToolResult(call_id=action.call_id, name=action.action, success=verified, output=str(destination),
                          human_readable=f"Moved '{source.name}' to '{destination}'.",
                          verified=verified, verification_status="verified" if verified else "uncertain",
                          verification_details={"source": str(source), "destination": str(destination), "source_exists": source.exists(), "destination_exists": destination.exists()},
                          metadata={"source": str(source), "destination": str(destination)})

    def _execute_rename_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("source") or action.target or ""
        new_name = str(action.parameters.get("new_name") or action.parameters.get("name") or action.parameters.get("destination") or "").strip()
        if not path_str or not new_name:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Rename requires the existing path and new name.")
        source = self.resolve_path(str(path_str))
        if not source.exists() or not source.is_file():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"File not found: {source}")
        # Rename is intentionally constrained to a filename, not an implicit move.
        destination = source.parent / Path(new_name).name
        blocked = self._check_write_path(action, destination, operation="rename", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if destination.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Destination already exists: {destination}")
        source.rename(destination)
        verified = destination.is_file() and not source.exists()
        return ToolResult(call_id=action.call_id, name=action.action, success=verified, output=str(destination),
                          human_readable=f"Renamed '{source.name}' to '{destination.name}'.",
                          verified=verified, verification_status="verified" if verified else "uncertain",
                          verification_details={"source": str(source), "destination": str(destination), "source_exists": source.exists(), "destination_exists": destination.exists()},
                          metadata={"source": str(source), "destination": str(destination)})

    def _execute_copy_folder(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        source_str = action.parameters.get("source") or action.parameters.get("from") or action.parameters.get("path") or action.parameters.get("folder") or ""
        destination_str = action.parameters.get("destination") or action.parameters.get("to") or action.parameters.get("target") or ""
        if not source_str or not destination_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Copy folder requires both source and destination paths.")
        source = self.resolve_path(str(source_str))
        destination = self.resolve_path(str(destination_str))
        if not source.exists() or not source.is_dir():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Source folder not found: {source}")
        # Standard file-manager semantics: copying A to an existing folder B
        # means B\A. An explicit non-existing destination still names the final folder.
        if destination.exists() and destination.is_dir():
            destination = destination / source.name
        blocked = self._check_write_path(action, destination, operation="copy folder", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if destination.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Destination already exists: {destination}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
        except OSError as exc:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not copy folder '{source}' to '{destination}': {exc}")
        verified = destination.is_dir() and source.exists()
        return ToolResult(call_id=action.call_id, name=action.action, success=verified, output=str(destination),
                          human_readable=f"Copied folder '{source.name}' to '{destination}'.", verified=verified,
                          verification_status="verified" if verified else "uncertain",
                          verification_details={"source":str(source),"destination":str(destination),"source_exists":source.exists(),"destination_exists":destination.is_dir()},
                          metadata={"source":str(source),"destination":str(destination),"kind":"folder"})

    def _execute_move_folder(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        source_str = action.parameters.get("source") or action.parameters.get("from") or action.parameters.get("path") or action.parameters.get("folder") or ""
        destination_str = action.parameters.get("destination") or action.parameters.get("to") or action.parameters.get("target") or ""
        if not source_str or not destination_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Move folder requires both source and destination paths.")
        source = self.resolve_path(str(source_str))
        destination = self.resolve_path(str(destination_str))
        if not source.exists() or not source.is_dir():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Source folder not found: {source}")
        # Standard file-manager semantics: moving A to an existing folder B
        # means B\A. An explicit non-existing destination remains the final folder.
        if destination.exists() and destination.is_dir():
            destination = destination / source.name
        blocked = self._check_write_path(action, destination, operation="move folder", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if destination.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Destination already exists: {destination}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        except OSError as exc:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not move folder '{source}' to '{destination}': {exc}")
        verified = destination.is_dir() and not source.exists()
        return ToolResult(call_id=action.call_id, name=action.action, success=verified, output=str(destination),
                          human_readable=f"Moved folder '{Path(source).name}' to '{destination}'.", verified=verified,
                          verification_status="verified" if verified else "uncertain",
                          verification_details={"source":str(source),"destination":str(destination),"source_exists":source.exists(),"destination_exists":destination.is_dir()},
                          metadata={"source":str(source),"destination":str(destination),"kind":"folder"})

    def _execute_rename_folder(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("source") or action.parameters.get("folder") or action.parameters.get("directory") or action.target or ""
        new_name = str(action.parameters.get("new_name") or action.parameters.get("name") or action.parameters.get("destination") or "").strip()
        if not path_str or not new_name:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Rename folder requires the existing folder path and new name.")
        source = self.resolve_path(str(path_str))
        if not source.exists() or not source.is_dir():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Folder not found: {source}")
        destination = source.parent / Path(new_name).name
        blocked = self._check_write_path(action, destination, operation="rename folder", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if destination.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Destination already exists: {destination}")
        try:
            source.rename(destination)
        except OSError as exc:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not rename folder '{source}': {exc}")
        verified = destination.is_dir() and not source.exists()
        return ToolResult(call_id=action.call_id, name=action.action, success=verified, output=str(destination),
                          human_readable=f"Renamed folder '{source.name}' to '{destination.name}'.", verified=verified,
                          verification_status="verified" if verified else "uncertain",
                          verification_details={"source":str(source),"destination":str(destination),"source_exists":source.exists(),"destination_exists":destination.is_dir()},
                          metadata={"source":str(source),"destination":str(destination),"kind":"folder"})

    def _execute_delete_folder(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("directory") or action.parameters.get("folder") or action.target or ""
        if not path_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No folder path provided.")
        target = self.resolve_path(str(path_str))
        blocked = self._check_write_path(action, target, operation="delete folder", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if not target.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Folder not found: {target}")
        if not target.is_dir():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Not a folder: {target}")
        recursive = bool(action.parameters.get("recursive", False))
        if not recursive and any(target.iterdir()):
            return ToolResult(call_id=action.call_id, name=action.action, success=False,
                              error=f"Folder '{target.name}' is not empty; set recursive=true to remove it with its contents.")
        recycled, problem = self._trash(target)
        if not recycled:
            return ToolResult(call_id=action.call_id, name=action.action, success=False,
                              error=f"I didn't delete folder '{target.name}': {problem}",
                              verification_details={"path": str(target), "reason": "recycle_bin_unavailable"})
        gone = not target.exists()
        return ToolResult(call_id=action.call_id, name=action.action, success=gone, output=str(target),
                          human_readable=f"Moved folder '{target.name}' to the Recycle Bin.",
                          verified=gone, verification_status="verified" if gone else "uncertain",
                          verification_details={"path": str(target), "exists": target.exists(), "recursive": recursive},
                          metadata={"path": str(target), "recursive": recursive})

    def _execute_create_folder(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("directory") or action.parameters.get("folder") or action.target or ""
        if not path_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No folder path provided.")
        target = self.resolve_path(str(path_str))
        blocked = self._check_write_path(action, target, operation="create folder", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        if target.exists():
            if target.is_dir():
                # The requested state already holds; failing here broke "try again"
                # on a half-finished "create a folder and move files" task.
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=True, output=str(target),
                    human_readable=f"Folder '{target.name}' already exists in {target.parent}.",
                    verified=True, verification_status="verified",
                    verification_details={"path": str(target), "exists": True, "is_directory": True, "already_exists": True},
                    metadata={"path": str(target), "resolved_path": str(target), "already_exists": True, "is_directory": True},
                )
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"A file already exists at: {target}")
        target.mkdir(parents=True, exist_ok=False)
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True, output=str(target),
            human_readable=f"Created folder '{target.name}' in {target.parent}.",
            verified=target.is_dir(),
            verification_status="verified" if target.is_dir() else "failed",
            verification_details={"path": str(target), "exists": target.exists(), "is_directory": target.is_dir()},
            metadata={"path": str(target), "resolved_path": str(target), "is_directory": True},
        )

    @staticmethod
    def _open_with_default_application(path: Path) -> str | int | None:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return None
        opener = shutil.which("xdg-open") or shutil.which("open")
        if not opener:
            raise RuntimeError("No operating-system file opener is available.")
        return subprocess.Popen([opener, str(path)], shell=False).pid

    @staticmethod
    def _find_window_for_path(
        path: Path,
        *,
        timeout: float = 6.0,
        previous_foreground_hwnd: int | None = None,
    ) -> tuple[int | None, str | None, str | None]:
        """Find the GUI window actually associated with an open request.

        Prefer a newly foreground window whose title matches the requested path.
        A pre-existing matching window is accepted only when it became the
        foreground window for the open request; this prevents an unrelated old
        document with the same filename from being selected.
        """
        if os.name != "nt":
            return None, None, None
        user32 = ctypes.windll.user32
        needle_names = {path.name.casefold(), path.stem.casefold()}
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            foreground = int(user32.GetForegroundWindow())
            candidates: list[tuple[int, str, bool]] = []
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def callback(hwnd, _lparam):
                hwnd = int(hwnd)
                try:
                    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                        return True
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length <= 0:
                        return True
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value.strip()
                    if not title:
                        return True
                    lowered = title.casefold()
                    if any(name and name in lowered for name in needle_names):
                        candidates.append((hwnd, title, hwnd == foreground))
                except Exception:
                    return True
                return True

            # Enumerate top-level windows. Without this call the callback is never
            # invoked, so open_file can launch successfully but remain permanently
            # unable to verify the associated window.
            user32.EnumWindows(EnumWindowsProc(callback), 0)

            # The foreground window is the strongest local evidence that the OS
            # association actually opened/activated the requested resource.
            foreground_matches = [item for item in candidates if item[2]]
            if foreground_matches:
                hwnd, title, _ = foreground_matches[0]
            else:
                fresh = [item for item in candidates if item[0] != previous_foreground_hwnd]
                if not fresh:
                    time.sleep(0.2)
                    continue
                # A single fresh title match is acceptable. Multiple old/new
                # candidates with the same filename remain ambiguous.
                if len(fresh) != 1:
                    time.sleep(0.2)
                    continue
                hwnd, title, _ = fresh[0]
            process_name = None
            try:
                import psutil
                pid_buf = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
                process_name = psutil.Process(int(pid_buf.value)).name()
            except Exception:
                pass
            return hwnd, title, process_name
        return None, None, None

    @staticmethod
    def _window_title_names_file(hwnd: int, path: Path) -> bool:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return False
        buffer = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buffer, 512)
        title = buffer.value.casefold()
        return bool(title) and (path.name.casefold() in title or path.stem.casefold() in title)

    @staticmethod
    def _activate_opened_window(hwnd: int) -> bool:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return False
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        return int(user32.GetForegroundWindow()) == int(hwnd)

    def _bind_opened_window(
        self,
        path: Path,
        *,
        previous_foreground_hwnd: int | None = None,
    ) -> tuple[int | None, str | None, str | None, bool]:
        hwnd, title, process_name = self._find_window_for_path(
            path, previous_foreground_hwnd=previous_foreground_hwnd
        )
        focused = False
        if hwnd:
            focused = self._activate_opened_window(hwnd)
            if self.desktop_context is not None:
                try:
                    app_name = Path(process_name).stem if process_name else (title or path.name)
                    self.desktop_context.set_application(app_name, hwnd=hwnd)
                    self.desktop_context.metadata["opened_path"] = str(path)
                    self.desktop_context.set_document(
                        path=str(path),
                        title=title or path.name,
                        dirty=False,
                    )
                except Exception:
                    pass
        return hwnd, title, process_name, focused

    def _execute_open_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("filename") or action.target or ""
        if not path_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No file path provided.")
        target = self.resolve_path(str(path_str))
        if self.desktop_context is not None:
            try:
                active_path = getattr(self.desktop_context, "active_document_path", None)
                hwnd = getattr(self.desktop_context, "target_hwnd", None)
                # A surviving browser/viewer window is not proof the document is
                # still in it: the log shows Edge reused after the PDF tab was
                # closed. Reuse only while the window title still names the file.
                if (
                    active_path
                    and Path(str(active_path)).resolve() == target.resolve()
                    and hwnd
                    and self._window_title_names_file(int(hwnd), target)
                ):
                    focused = bool(hwnd and getattr(self.desktop_context, "target_hwnd", None) and self._activate_opened_window(int(hwnd))) if hwnd else False
                    verified = bool(focused)
                    return ToolResult(
                        call_id=action.call_id, name=action.action, success=verified, output=str(target),
                        human_readable=(f"'{target.name}' is already open; reused its existing window." if verified
                                        else f"'{target.name}' is already tracked, but its window could not be focused."),
                        verified=verified, verification_status="verified" if verified else "uncertain",
                        verification_details={"method": "tracked_document_reuse", "hwnd": hwnd, "focused": focused},
                        metadata={"path": str(target), "resolved_path": str(target), "open_requested": False, "reused_existing": True, "window_hwnd": hwnd, "focused": focused},
                    )
            except Exception:
                pass
        if not target.exists():
            # Recover from a model-generated wrong-user/simplified path by searching
            # the exact basename. This lets "open report.pdf" work even when the
            # model does not already know whether the file lives in Downloads,
            # Documents, Desktop, or another accessible user directory.
            basename = target.name
            candidate_scopes: list[str] = []
            lowered = str(target).casefold()
            if "\\downloads\\" in lowered:
                candidate_scopes.append("downloads")
            elif "\\documents\\" in lowered:
                candidate_scopes.append("documents")
            elif "\\desktop\\" in lowered:
                candidate_scopes.append("desktop")
            for scope in candidate_scopes:
                try:
                    search_call = ToolCall(call_id=f"{action.call_id}-resolve", name="find_file", arguments={"name": basename, "scope": scope, "limit": 10})
                    search_result = self._execute_find_file(search_call, approval_granted=approval_granted)
                    found = search_result.output if isinstance(search_result.output, list) else []
                    exact = [Path(str(item)) for item in found if Path(str(item)).name.casefold() == basename.casefold()]
                    if len(exact) == 1:
                        target = exact[0]
                        break
                except Exception:
                    logger.debug("Open-file fallback search failed", exc_info=True)
            if not target.exists():
                try:
                    search_call = ToolCall(call_id=f"{action.call_id}-resolve-computer", name="find_file", arguments={"name": basename, "scope": "computer", "limit": 10})
                    search_result = self._execute_find_file(search_call, approval_granted=approval_granted)
                    found = search_result.output if isinstance(search_result.output, list) else []
                    exact = [Path(str(item)) for item in found if Path(str(item)).name.casefold() == basename.casefold()]
                    if len(exact) == 1:
                        target = exact[0]
                    elif len(exact) > 1:
                        return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"I found multiple files named '{basename}'. Please specify the folder.", metadata={"ambiguous_matches": [str(item) for item in exact[:10]], "query": basename})
                except Exception:
                    logger.debug("Open-file computer-wide fallback search failed", exc_info=True)
        if not target.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"File not found: {target}")
        if not target.is_file():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Not a file: {target}")
        previous_foreground = None
        if os.name == "nt":
            try:
                previous_foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0) or None
            except Exception:
                previous_foreground = None
        opener_pid = self._open_with_default_application(target)
        hwnd, title, process_name, focused = self._bind_opened_window(target, previous_foreground_hwnd=previous_foreground)
        observed = hwnd is not None
        success = observed if os.name == "nt" else True
        return ToolResult(
            call_id=action.call_id, name=action.action, success=success, output=str(target),
            human_readable=(f"Opened file '{target.name}'." if observed else f"Requested opening file '{target.name}', but I could not verify the associated window."),
            verified=observed, verification_status="verified" if observed else "uncertain",
            verification_details={"method": "window_observation", "hwnd": hwnd, "window_title": title, "process": process_name, "focused": focused},
            metadata={"path": str(target), "resolved_path": str(target), "open_requested": True, "opener_pid": opener_pid, "opened_kind": "file", "window_hwnd": hwnd, "window_title": title, "process": process_name, "focused": focused},
        )

    def _execute_open_folder(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        path_str = action.parameters.get("path") or action.parameters.get("directory") or action.parameters.get("folder") or action.target or ""
        if not path_str:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No folder path provided.")
        target = self.resolve_path(str(path_str))
        if not target.exists():
            folder_name = target.name
            roots = self._search_roots_for_scope("computer", get_environment_paths()) if hasattr(self, "_search_roots_for_scope") else [get_environment_paths().desktop, get_environment_paths().documents, get_environment_paths().downloads]
            matches: list[Path] = []
            deadline = time.monotonic() + 10.0
            for root in roots:
                root = Path(root)
                if not root.exists() or not root.is_dir():
                    continue
                try:
                    for current_root, dirs, _files in os.walk(root, topdown=True, followlinks=False):
                        if time.monotonic() >= deadline or len(matches) >= 10:
                            break
                        dirs[:] = [d for d in dirs if d not in {"$Recycle.Bin", "System Volume Information"}]
                        for name in dirs:
                            if name.casefold() == folder_name.casefold():
                                matches.append(Path(current_root) / name)
                                if len(matches) >= 10:
                                    break
                except (OSError, PermissionError):
                    continue
                if time.monotonic() >= deadline or len(matches) >= 10:
                    break
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"I found multiple folders named '{folder_name}'. Please specify the path.", metadata={"ambiguous_matches": [str(item) for item in matches]})
        if not target.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Folder not found: {target}")
        if not target.is_dir():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Not a folder: {target}")
        previous_foreground = None
        if os.name == "nt":
            try:
                previous_foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0) or None
            except Exception:
                previous_foreground = None
        opener_pid = self._open_with_default_application(target)
        hwnd, title, process_name, focused = self._bind_opened_window(target, previous_foreground_hwnd=previous_foreground)
        observed = hwnd is not None
        success = observed if os.name == "nt" else True
        return ToolResult(
            call_id=action.call_id, name=action.action, success=success, output=str(target),
            human_readable=(f"Opened folder '{target.name}'." if observed else f"Requested opening folder '{target.name}', but I could not verify the associated window."),
            verified=observed, verification_status="verified" if observed else "uncertain",
            verification_details={"method": "window_observation", "hwnd": hwnd, "window_title": title, "process": process_name, "focused": focused},
            metadata={"path": str(target), "resolved_path": str(target), "open_requested": True, "opener_pid": opener_pid, "opened_kind": "folder", "window_hwnd": hwnd, "window_title": title, "process": process_name, "focused": focused},
        )

    def _execute_find_file(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        """Find a local file or folder by exact name without requiring GUI navigation.

        Exact case-insensitive matching is used unless the requested name contains
        ``*`` or ``?``, in which case normal glob matching is used. The computer
        search is bounded by result count and elapsed time so a pathological disk
        scan cannot hang the agent indefinitely. The action name is kept as
        ``find_file`` for API compatibility; directories are valid results too.
        """
        name = str(action.parameters.get("name") or action.parameters.get("filename") or action.parameters.get("query") or "").strip()
        if not name:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No filename was provided.")

        scope = str(action.parameters.get("scope") or "computer").strip().casefold()
        limit = max(1, min(int(action.parameters.get("limit") or 20), 50))

        # Watchtower is useful as a fast cache for watched user roots, but it is
        # not the authoritative whole-computer search engine. Broad searches and
        # wildcards must use the bounded native scan below so a stale inventory
        # can never cause a false "not found" result.
        wildcard = any(ch in name for ch in "*?")
        use_watchtower_cache = (
            self.environment_watchtower is not None
            and not wildcard
        )
        paths = get_environment_paths()
        roots = self._search_roots_for_scope(scope, paths)

        wildcard = any(ch in name for ch in "*?")
        needle = name.casefold()
        # When the user omits an extension (e.g. "ADVI presentation"), an exact
        # filename match is impossible but an exact filename stem is still precise.
        # Keep fuzzy/substring matching out of the native search to avoid selecting
        # the wrong file.
        stem_needle = Path(name).stem.casefold() if not wildcard else ""
        stem_match = bool(stem_needle and Path(name).suffix == "")
        matches: list[str] = []
        stem_matches: list[str] = []
        # Partial matches: every word of the query appears in the name. Used only
        # when nothing matches exactly, and labelled as partial for confirmation.
        query_words = [w for w in re.findall(r"\w+", Path(name).stem.casefold() if not wildcard else "") if len(w) > 1]
        partial_matches: list[str] = []
        started = time.monotonic()
        deadline = started + 20.0
        seen: set[str] = set()

        # Watchtower is a cache, never the authority. Seed verified cached paths
        # so common watched-root searches are immediate, then continue the native
        # scan to catch files created/removed since the last snapshot.
        if use_watchtower_cache:
            finder = getattr(self.environment_watchtower, "find_files_by_name", None)
            if callable(finder):
                try:
                    cached = finder(name, limit=limit, scope=scope)
                except TypeError:
                    try:
                        cached = finder(name, limit=limit)
                    except Exception:
                        cached = []
                        logger.debug("Watchtower filename lookup failed", exc_info=True)
                except Exception:
                    cached = []
                    logger.debug("Watchtower filename lookup failed", exc_info=True)
                for value in cached or []:
                    candidate = Path(str(value))
                    try:
                        if candidate.is_file() and self._is_within(candidate.resolve(strict=False), [Path(root).resolve(strict=False) for root in roots]):
                            key = str(candidate).casefold()
                            if key not in seen:
                                seen.add(key)
                                matches.append(str(candidate))
                                if len(matches) >= limit:
                                    return self._file_search_result(action, name, scope, matches, started)
                    except OSError:
                        continue

        for root in roots:
            root = Path(root)
            if not root.exists() or not root.is_dir():
                continue
            try:
                for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
                    if time.monotonic() >= deadline:
                        break
                    dirs[:] = [d for d in dirs if d not in {"$Recycle.Bin", "System Volume Information"}]

                    # Directory names are valid targets too. Iterate them before files
                    # so a request such as "find the Projects folder" resolves to the
                    # folder itself rather than returning similarly named files first.
                    for dirname in dirs:
                        dirname_fold = dirname.casefold()
                        matched = fnmatch.fnmatchcase(dirname_fold, needle) if wildcard else dirname_fold == needle
                        if not matched:
                            continue
                        full = str(Path(current_root) / dirname)
                        key = full.casefold()
                        if key in seen:
                            continue
                        seen.add(key)
                        matches.append(full)
                        if len(matches) >= limit:
                            return self._file_search_result(action, name, scope, matches, started)

                    for filename in files:
                        filename_fold = filename.casefold()
                        matched = fnmatch.fnmatchcase(filename_fold, needle) if wildcard else filename_fold == needle
                        stem_hit = bool(stem_match and Path(filename).stem.casefold() == stem_needle)
                        if not matched and not stem_hit:
                            if query_words and len(partial_matches) < limit:
                                words = set(re.findall(r"\w+", filename_fold))
                                if all(any(w.startswith(q) for w in words) for q in query_words):
                                    partial_matches.append(str(Path(current_root) / filename))
                            continue
                        full = str(Path(current_root) / filename)
                        key = full.casefold()
                        if key in seen:
                            continue
                        seen.add(key)
                        if matched:
                            matches.append(full)
                            if len(matches) >= limit:
                                return self._file_search_result(action, name, scope, matches, started)
                        else:
                            stem_matches.append(full)
            except (OSError, PermissionError):
                continue
            if time.monotonic() >= deadline:
                break

        if not matches and stem_matches:
            matches = stem_matches[:limit]
        if not matches and partial_matches:
            partial_matches.sort(key=lambda value: len(Path(value).name))
            result = self._file_search_result(action, name, scope, partial_matches[:limit], started,
                                              timed_out=time.monotonic() >= deadline)
            return self._as_partial(result, name)
        return self._file_search_result(action, name, scope, matches, started, timed_out=time.monotonic() >= deadline)

    @staticmethod
    def _as_partial(result: ToolResult, name: str) -> ToolResult:
        """Label name-contains matches so the model confirms instead of assuming."""
        count = len(result.output or [])
        return ToolResult(
            call_id=result.call_id, name=result.name, success=True, output=result.output,
            human_readable=(f"No file is named exactly '{name}', but {count} file(s) contain all of its words. "
                            "Use one if it is clearly what the user meant; otherwise ask."),
            verified=result.verified, verification_status=result.verification_status,
            verification_details={**dict(result.verification_details or {}), "match": "partial"},
            metadata={**dict(result.metadata or {}), "match": "partial"},
        )

    def _search_roots_for_scope(self, scope: str, paths: Any) -> list[Path]:
        normalized = str(scope or "computer").casefold().strip()
        if normalized == "desktop":
            return [self.desktop_dir]
        if normalized in {"documents", "document"}:
            return [paths.documents]
        if normalized in {"downloads", "download"}:
            return [paths.downloads]
        if normalized in {"user", "home", "profile"}:
            return [paths.user_home]
        # Without these, "the Videos folder" fell back to a whole-computer scan
        # and hit the 20 s limit (runtime log, 2026-10-14).
        media = {"videos": ("Videos", "18989B1D-99B5-455B-841C-AB7C74E4DDFC"), "video": ("Videos", "18989B1D-99B5-455B-841C-AB7C74E4DDFC"),
                 "music": ("Music", "4BD8D571-6D19-48D3-BE97-422220080E43"),
                 "pictures": ("Pictures", "33E28130-4E1E-4676-835A-98395C3BC3BB"), "photos": ("Pictures", "33E28130-4E1E-4676-835A-98395C3BC3BB")}
        if normalized in media:
            folder, guid = media[normalized]
            return [self._known_folder(guid, Path(paths.user_home) / folder)]
        return self._search_roots_for_computer(paths)

    @staticmethod
    def _known_folder(guid: str, fallback: Path) -> Path:
        try:
            from advi.environment.environment import _windows_known_folder

            found = _windows_known_folder(guid)
            if found and Path(found).is_dir():
                return Path(found)
        except Exception:
            pass
        return fallback

    @staticmethod
    def _search_roots_for_computer(paths: Any) -> list[Path]:
        if os.name != "nt":
            return [paths.user_home]
        roots: list[Path] = []
        try:
            import psutil
            for partition in psutil.disk_partitions(all=False):
                mountpoint = Path(partition.mountpoint)
                opts = str(getattr(partition, "opts", "")).casefold()
                if "cdrom" in str(getattr(partition, "fstype", "")).casefold() or "cdrom" in opts:
                    continue
                roots.append(mountpoint)
        except Exception:
            system_drive = os.environ.get("SystemDrive")
            if system_drive:
                roots.append(Path(system_drive + "\\"))
        return roots or [paths.user_home]

    def _execute_search_file_content(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        """Search text content in one file/directory or a bounded machine scope."""
        raw_path = action.parameters.get("path") or action.parameters.get("file") or action.parameters.get("directory") or action.target or ""
        scope = str(action.parameters.get("scope") or "computer").strip().casefold()
        query = str(action.parameters.get("query") or action.parameters.get("text") or action.parameters.get("find") or "").strip()
        if not query:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No search text was provided.")
        try:
            limit = max(1, min(int(action.parameters.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20

        paths = get_environment_paths()
        if raw_path:
            root = self.resolve_path(str(raw_path))
            if not root.exists():
                return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Path not found: {root}")
            roots = [root]
        else:
            roots = self._search_roots_for_scope(scope, paths)

        if len(roots) == 1 and roots[0].is_file():
            def iter_candidates():
                yield roots[0]
        else:
            def iter_candidates():
                for base in roots:
                    if not base.exists() or not base.is_dir():
                        continue
                    for current_root, dirs, files in os.walk(base, topdown=True, followlinks=False):
                        dirs[:] = [d for d in dirs if d not in {"$Recycle.Bin", "System Volume Information"}]
                        for filename in files:
                            candidate = Path(current_root) / filename
                            if len(str(candidate)) < 32768:
                                yield candidate

        started = time.monotonic()
        deadline = started + 15.0
        matches: list[dict[str, Any]] = []
        query_fold = query.casefold()
        files_scanned = 0
        unreadable = 0

        for candidate in iter_candidates():
            if time.monotonic() >= deadline or len(matches) >= limit:
                break
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                files_scanned += 1
                text, parser, readable = self._read_file_content(candidate)
                if not readable:
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if query_fold not in line.casefold():
                        continue
                    index = line.casefold().find(query_fold)
                    start = max(0, index - 100)
                    excerpt = line[start:start + 320]
                    if start > 0:
                        excerpt = "…" + excerpt
                    if len(line) > start + len(excerpt):
                        excerpt += "…"
                    matches.append({
                        "path": str(candidate),
                        "line": line_number,
                        "match": query,
                        "excerpt": excerpt,
                        "parser": parser,
                    })
                    if len(matches) >= limit:
                        break
            except (OSError, PermissionError, RuntimeError) as exc:
                unreadable += 1
                logger.debug("Content search skipped %s: %s", candidate, exc)

        timed_out = time.monotonic() >= deadline
        summary = (
            f"Found {len(matches)} matching line(s) for '{query}'."
            if matches else f"Searched {files_scanned} file(s) for '{query}' and found no matches."
        )
        if timed_out:
            summary += " The search reached its time limit, so more results may exist."
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True, output=matches,
            human_readable=summary,
            verified=not timed_out,
            verification_status="verified" if not timed_out else "uncertain",
            verification_details={
                "method": "content_search", "query": query, "path": str(roots[0]) if len(roots) == 1 else None, "scope": scope,
                "count": len(matches), "files_scanned": files_scanned,
                "unreadable_files": unreadable, "timed_out": timed_out,
            },
            metadata={
                "query": query, "path": str(roots[0]) if len(roots) == 1 else None, "scope": scope, "count": len(matches),
                "files_scanned": files_scanned, "unreadable_files": unreadable, "timed_out": timed_out,
                "complete_search": not timed_out,
            },
        )

    @staticmethod
    def _file_search_result(action: ToolCall, name: str, scope: str, matches: list[str], started: float, *, timed_out: bool = False) -> ToolResult:
        elapsed = round(time.monotonic() - started, 2)
        if not matches:
            message = f"I searched the {scope} scope but found no file named '{name}'."
            if timed_out:
                message += " The search reached its time limit before finishing."
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True, output=[],
                human_readable=message, verified=not timed_out,
                verification_status="verified" if not timed_out else "uncertain",
                verification_details={"method": "filesystem_scan", "count": 0, "scope": scope, "timed_out": timed_out},
                metadata={"query": name, "scope": scope, "count": 0, "elapsed_seconds": elapsed, "timed_out": timed_out, "found": False, "complete_search": not timed_out},
            )
        message = f"Found {len(matches)} matching file(s)."
        if timed_out:
            message += " The search reached its time limit, so more matches may exist."
        needs_input = len(matches) > 1 and not timed_out
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True, output=matches, human_readable=message,
            verified=not timed_out, verification_status="verified" if not timed_out else "uncertain",
            verification_details={"method": "filesystem_scan", "count": len(matches), "scope": scope, "timed_out": timed_out},
            metadata={"query": name, "scope": scope, "count": len(matches), "elapsed_seconds": elapsed, "timed_out": timed_out, "complete_search": not timed_out, "needs_user_input": needs_input, "waiting_for": "which matching file to use" if needs_input else None},
        )

    @staticmethod
    def _list_entry(path: Path, name: str, *, include_metadata: bool) -> dict[str, Any]:
        item: dict[str, Any] = {"name": name, "path": str(path)}
        if include_metadata:
            try:
                stat = path.stat()
                created_epoch = float(getattr(stat, "st_ctime", stat.st_mtime))
                item.update({
                    "size_bytes": int(stat.st_size),
                    "modified_epoch": float(stat.st_mtime),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                    "created_epoch": created_epoch,
                    "created_at": datetime.fromtimestamp(created_epoch).astimezone().isoformat(timespec="seconds"),
                })
            except OSError:
                item.update({"size_bytes": None, "modified_epoch": None, "modified_at": None, "created_epoch": None, "created_at": None})
        return item

    @classmethod
    def _finalize_listing(
        cls,
        files: list[dict[str, Any]],
        folders: list[dict[str, Any]],
        *,
        limit: int,
        sort_by: str,
        descending: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        tagged = [(item, False) for item in files] + [(item, True) for item in folders]
        truncated = len(tagged) > limit
        if sort_by in {"modified_time", "created_time", "size"}:
            key_name = {"modified_time": "modified_epoch", "created_time": "created_epoch", "size": "size_bytes"}[sort_by]
            # Missing timestamps/sizes should sort last in either direction.
            # Keep the primary key numeric and use a separate missing flag so
            # `reverse=True` cannot accidentally promote unknown metadata.
            if descending:
                tagged.sort(
                    key=lambda pair: (
                        pair[0].get(key_name) is not None,
                        pair[0].get(key_name) if pair[0].get(key_name) is not None else float("-inf"),
                        str(pair[0].get("name") or "").casefold(),
                    ),
                    reverse=True,
                )
            else:
                tagged.sort(
                    key=lambda pair: (
                        pair[0].get(key_name) is None,
                        pair[0].get(key_name) if pair[0].get(key_name) is not None else 0,
                        str(pair[0].get("name") or "").casefold(),
                    )
                )
        else:
            tagged.sort(key=lambda pair: str(pair[0].get("name") or "").casefold(), reverse=descending)
        selected = tagged[:limit]
        return (
            [dict(item) for item, is_folder in selected if not is_folder],
            [dict(item) for item, is_folder in selected if is_folder],
            truncated,
        )

    def _execute_list_files(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        """List a bounded directory view with optional extension, recursion, and scope."""
        scope = str(action.parameters.get("scope") or "").strip().casefold()
        dir_str = str(action.parameters.get("directory") or action.parameters.get("path") or action.parameters.get("folder") or action.target or "").strip()
        paths = get_environment_paths()
        if not dir_str and scope:
            scope_paths = {
                "desktop": paths.desktop, "documents": paths.documents, "downloads": paths.downloads,
                "user": paths.user_home,
            }
            if scope in scope_paths:
                dir_path = scope_paths[scope]
            elif scope in {"computer", "pc", "laptop", "drives", "this pc"}:
                # A plain computer listing is a drive inventory. When the user
                # asks for recursion or an extension filter, treat every mounted
                # drive as the search root instead of returning only the drive names.
                recursive = bool(action.parameters.get("recursive", False))
                raw_extension = str(action.parameters.get("extension") or "").strip().casefold()
                extension = ("." + raw_extension.lstrip(".")) if raw_extension else ""
                # A computer-wide extension listing is inherently recursive;
                # otherwise "list PDFs on my laptop" would inspect only drive roots.
                if extension:
                    recursive = True
                limit = max(1, min(int(action.parameters.get("limit") or 100), 1000))
                sort_by = str(action.parameters.get("sort_by") or "name").strip().casefold()
                descending = bool(action.parameters.get("descending", False))
                include_metadata = bool(action.parameters.get("include_metadata", False) or sort_by in {"modified_time", "created_time", "size"})
                scan_limit = 5000 if sort_by in {"modified_time", "created_time", "size"} else limit + 1
                if not recursive and not extension:
                    drives: list[dict[str, str]] = []
                    if os.name == "nt":
                        try:
                            import psutil
                            for part in psutil.disk_partitions(all=False):
                                mount = Path(part.mountpoint)
                                if mount.exists():
                                    drives.append({"name": str(mount), "path": str(mount)})
                        except Exception:
                            pass
                    return ToolResult(call_id=action.call_id, name=action.action, success=True,
                                      output={"directory": "computer", "files": [], "folders": drives, "truncated": False, "extension": None, "recursive": False, "scope": "computer", "limit": limit},
                                      human_readable=f"Found {len(drives)} accessible drive(s) on this computer.", verified=True, verification_status="verified",
                                      verification_details={"method":"drive_inventory","scope":"computer","count":len(drives)},
                                      metadata={"scope":"computer","count":len(drives)})

                roots: list[Path] = []
                if os.name == "nt":
                    try:
                        import psutil
                        for part in psutil.disk_partitions(all=False):
                            mount = Path(part.mountpoint)
                            fstype = str(getattr(part, "fstype", "")).casefold()
                            opts = str(getattr(part, "opts", "")).casefold()
                            if "cdrom" in fstype or "cdrom" in opts:
                                continue
                            if mount.exists() and mount.is_dir():
                                roots.append(mount)
                    except Exception:
                        roots = []
                else:
                    roots = [Path("/")]
                files: list[dict[str, str]] = []
                folders: list[dict[str, str]] = []
                for root in roots:
                    try:
                        iterator = root.rglob("*") if recursive else root.iterdir()
                        for entry in iterator:
                            if entry.is_file():
                                if extension and entry.suffix.casefold() != extension:
                                    continue
                                files.append(self._list_entry(entry, entry.name, include_metadata=include_metadata))
                                if len(files) + len(folders) >= scan_limit:
                                    break
                            elif entry.is_dir() and not extension and not recursive:
                                folders.append(self._list_entry(entry, entry.name, include_metadata=include_metadata))
                                if len(files) + len(folders) >= scan_limit:
                                    break
                    except (OSError, PermissionError):
                        continue
                    if len(files) + len(folders) >= scan_limit:
                        break
                files, folders, truncated = self._finalize_listing(files, folders, limit=limit, sort_by=sort_by, descending=descending)
                count = len(files) + len(folders)
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=True,
                    output={"directory":"computer","files":files,"folders":folders,"truncated":truncated,"extension":extension or None,"recursive":recursive,"scope":"computer","limit":limit},
                    human_readable=f"Found {len(files)} files and {len(folders)} folders on this computer" + (f" matching {extension} files." if extension else "."),
                    verified=True, verification_status="verified",
                    verification_details={"method":"computer_filesystem_listing","scope":"computer","file_count":len(files),"directory_count":len(folders),"extension":extension or None,"recursive":recursive,"limit":limit},
                    metadata={"scope":"computer","count":count,"file_count":len(files),"directory_count":len(folders),"extension":extension or None,"recursive":recursive,"sort_by":sort_by,"descending":descending,"include_metadata":include_metadata,"limit":limit},
                )
            else:
                dir_path = self.desktop_dir
        elif dir_str:
            # Common natural aliases like "the Downloads folder" are normalized locally.
            dir_clean = re.sub(r"(?i)\b(the|my|folder|directory)\b", " ", dir_str).strip()
            dir_path = self.resolve_path(dir_clean)
        else:
            dir_path = self.desktop_dir

        if not dir_path.exists():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Directory not found: {dir_path}")
        if not dir_path.is_dir():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Not a directory: {dir_path}")

        raw_extension = str(action.parameters.get("extension") or "").strip().casefold()
        extension = ("." + raw_extension.lstrip(".")) if raw_extension else ""
        recursive = bool(action.parameters.get("recursive", False))
        limit = max(1, min(int(action.parameters.get("limit") or 100), 1000))
        sort_by = str(action.parameters.get("sort_by") or "name").strip().casefold()
        descending = bool(action.parameters.get("descending", False))
        include_metadata = bool(action.parameters.get("include_metadata", False) or sort_by in {"modified_time", "created_time", "size"})
        scan_limit = 5000 if sort_by in {"modified_time", "created_time", "size"} else limit + 1
        files: list[dict[str, Any]] = []
        folders: list[dict[str, Any]] = []
        try:
            iterator = dir_path.rglob("*") if recursive else dir_path.iterdir()
            for entry in sorted(iterator, key=lambda p: (not p.is_dir(), str(p).casefold())):
                if entry == dir_path:
                    continue
                relative_name = str(entry.relative_to(dir_path))
                if entry.is_dir():
                    if not extension:
                        folders.append(self._list_entry(entry, relative_name, include_metadata=include_metadata))
                elif entry.is_file():
                    if extension and entry.suffix.casefold() != extension:
                        continue
                    files.append(self._list_entry(entry, relative_name, include_metadata=include_metadata))
                if len(files) + len(folders) >= scan_limit:
                    break
        except (OSError, PermissionError) as exc:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not list directory: {exc}")

        files, folders, truncated = self._finalize_listing(files, folders, limit=limit, sort_by=sort_by, descending=descending)
        count = len(files) + len(folders)
        filter_label = f" matching {extension} files" if extension else ""
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True,
            output={"directory": str(dir_path), "files": files, "folders": folders, "truncated": truncated, "extension": extension or None, "recursive": recursive, "scope": scope or None, "limit": limit},
            human_readable=f"Found {len(files)} files and {len(folders)} folders in {dir_path.name}{filter_label}.", verified=True,
            verification_status="verified",
            verification_details={"method":"directory_listing","directory":str(dir_path),"file_count":len(files),"directory_count":len(folders),"extension":extension or None,"recursive":recursive,"sort_by":sort_by,"descending":descending,"limit":limit},
            metadata={"directory":str(dir_path),"count":count,"file_count":len(files),"directory_count":len(folders),"directories":[item["name"] for item in folders],"extension":extension or None,"recursive":recursive,"scope":scope or None,"sort_by":sort_by,"descending":descending,"include_metadata":include_metadata,"limit":limit},
        )

    # --- bulk operations (one call, verified, undoable) ---------------------
    def _bulk_result(self, action: ToolCall, outcome: dict[str, Any], *, summary: str) -> ToolResult:
        moved = outcome.get("moved") or []
        failed = outcome.get("failed") or []
        ok = not failed
        if failed:
            summary += f" {len(failed)} file(s) could not be moved: " + ", ".join(f["file"] for f in failed[:5]) + "."
        if moved:
            summary += " Say 'undo' to put them back."
        return ToolResult(
            call_id=action.call_id, name=action.action, success=ok or bool(moved),
            output={"moved": [Path(target).name for _source, target in moved][:50], "moved_count": len(moved),
                    "failed": failed[:20], "created_folders": outcome.get("created_dirs") or []},
            human_readable=summary, error=None if ok else summary,
            verified=ok, verification_status="verified" if ok else "uncertain",
            verification_details={"method": "bulk_move_disk_check", "moved": len(moved), "failed": len(failed),
                                  "journal_batch": outcome.get("batch_id")},
            metadata={"journal_batch": outcome.get("batch_id")},
        )

    def _execute_move_files(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        from .bulk import move_batch, select_files

        params = action.parameters
        source = self.resolve_path(str(params.get("source") or params.get("from") or ""))
        destination = self.resolve_path(str(params.get("destination") or params.get("to") or ""))
        if not source.is_dir():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Source folder not found: {source}")
        for path, operation in ((source, "move files out of"), (destination, "move files into")):
            blocked = self._check_write_path(action, path, operation=operation, approval_granted=approval_granted)
            if blocked is not None:
                return blocked
        files = [f for f in select_files(source, extensions=params.get("extensions"), name_contains=params.get("name_contains"))
                 if f.parent.resolve() != destination.resolve()]
        if not files:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True, output={"moved": [], "moved_count": 0},
                human_readable=f"No matching files to move in {source}.", verified=True, verification_status="verified",
                verification_details={"method": "bulk_move_disk_check", "moved": 0},
            )
        outcome = move_batch([(f, destination) for f in files], operation=f"move {len(files)} file(s) to {destination}")
        return self._bulk_result(action, outcome, summary=f"Moved {len(outcome['moved'])} file(s) from {source.name} to {destination}.")

    def _execute_organize_folder(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        from .bulk import move_batch, organize_plan

        folder = self.resolve_path(str(action.parameters.get("folder") or action.parameters.get("path") or ""))
        if not folder.is_dir():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Folder not found: {folder}")
        blocked = self._check_write_path(action, folder, operation="organize", approval_granted=approval_granted)
        if blocked is not None:
            return blocked
        plan, counts, left = organize_plan(folder)
        if not plan:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True, output={"moved_count": 0},
                human_readable=f"{folder.name} has no loose files of known types to organize.",
                verified=True, verification_status="verified", verification_details={"method": "bulk_move_disk_check", "moved": 0},
            )
        outcome = move_batch(plan, operation=f"organize {folder}")
        by_type = ", ".join(f"{count} to {name}" for name, count in sorted(counts.items()))
        summary = f"Organized {folder.name}: {by_type}."
        if left:
            summary += f" {left} file(s) of other types were left in place."
        return self._bulk_result(action, outcome, summary=summary)

    def _execute_undo_file_operation(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        from .bulk import undo_last

        outcome = undo_last()
        if outcome.get("restored") is None:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=outcome.get("message"))
        summary = f"Undid '{outcome.get('operation')}': {outcome['restored']} file(s) back in place."
        if outcome["failed"]:
            summary += f" {len(outcome['failed'])} could not be restored: " + ", ".join(f["file"] for f in outcome["failed"][:5]) + "."
        return ToolResult(
            call_id=action.call_id, name=action.action, success=bool(outcome["ok"] or outcome["restored"]),
            output=outcome, human_readable=summary, error=None if outcome["ok"] else summary,
            verified=bool(outcome["ok"]), verification_status="verified" if outcome["ok"] else "uncertain",
            verification_details={"method": "journal_undo", "restored": outcome["restored"], "failed": len(outcome["failed"])},
        )

    _MEDIA_EXTENSIONS = {"mp4", "mkv", "avi", "mov", "wmv", "webm", "m4v", "mp3", "wav", "m4a", "flac", "aac", "ogg", "wma"}

    def _execute_play_local_media(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        """Play a song or video saved on this computer in the default player.

        "Play here I am to worship" after listing the Videos folder went to
        YouTube (runtime log). Media folders are searched by query words, the
        best match is opened with its associated player, and the player window
        is verified by open_file.
        """
        query = str(action.parameters.get("query") or action.parameters.get("name") or "").strip()
        words = [w for w in re.findall(r"\w+", query.casefold()) if len(w) > 1]
        if not words:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="What should I play? No title was given.")
        paths = get_environment_paths()
        roots = [
            self._known_folder("18989B1D-99B5-455B-841C-AB7C74E4DDFC", Path(paths.user_home) / "Videos"),
            self._known_folder("4BD8D571-6D19-48D3-BE97-422220080E43", Path(paths.user_home) / "Music"),
            Path(paths.downloads), Path(paths.desktop),
        ]
        folder = str(action.parameters.get("folder") or "").strip()
        if folder:
            roots.insert(0, self.resolve_path(folder))
        deadline = time.monotonic() + 6.0
        scored: list[tuple[float, int, Path]] = []
        for root in roots:
            if not root.is_dir():
                continue
            base_depth = len(root.parts)
            for current, dirs, files in os.walk(root):
                if time.monotonic() > deadline:
                    break
                if len(Path(current).parts) - base_depth >= 3:
                    dirs[:] = []
                for filename in files:
                    if Path(filename).suffix.lstrip(".").casefold() not in self._MEDIA_EXTENSIONS:
                        continue
                    name_words = set(re.findall(r"\w+", Path(filename).stem.casefold()))
                    covered = sum(1 for w in words if any(n.startswith(w) for n in name_words)) / len(words)
                    if covered >= 0.75:
                        scored.append((covered, len(filename), Path(current) / filename))
        if not scored:
            searched = ", ".join(r.name for r in roots if r.is_dir())
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error=f"No song or video on this computer matches '{query}' (searched {searched}).",
                verified=True, verification_status="failed",
                verification_details={"method": "local_media_search", "found": 0},
            )
        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen = scored[0][2]
        opened = self._execute_open_file(ToolCall(call_id=f"{action.call_id}-open", name="open_file", arguments={"path": str(chosen)}))
        player = (opened.metadata or {}).get("process") or "the default player"
        summary = f"Playing '{chosen.name}' in {player}." if opened.success else f"I found '{chosen.name}' but couldn't confirm the player opened."
        return ToolResult(
            call_id=action.call_id, name=action.action, success=opened.success, output=str(chosen),
            human_readable=summary, error=None if opened.success else summary,
            verified=opened.verified, verification_status=opened.verification_status,
            verification_details={"method": "local_media_open", "path": str(chosen), "match": round(scored[0][0], 2),
                                  "alternatives": [p.name for _s, _l, p in scored[1:4]], **dict(opened.verification_details or {})},
            metadata={"path": str(chosen), "resolved_path": str(chosen), **dict(opened.metadata or {})},
        )
