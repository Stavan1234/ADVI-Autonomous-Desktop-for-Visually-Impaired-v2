"""Polished PDF rendering that never drops text.

The previous renderer placed every paragraph in a fixed 62pt box via
``insert_textbox``, which silently discards whatever does not fit, and stopped
writing at the bottom of each page. Any paragraph longer than ~4 lines vanished;
a 1000-word speech rendered as 193 characters and failed content verification.

This module wraps text by measured glyph width, splits paragraphs across pages,
and picks the largest body size at which the content fits the requested page
count. Content that cannot fit even at the smallest size flows onto extra pages
rather than being lost.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PAGE_W, PAGE_H = 595, 842
LEFT, RIGHT = 42, 553
TOP, BOTTOM = 122, 790
CARD_PAD_X, CARD_PAD_Y = 11, 8
BLOCK_GAP = 8
BODY_SIZES = (10.8, 10.0, 9.4, 8.8, 8.2, 7.6)
TABLE_SIZE = 8.5
TITLE_MAX_CHARS = 90

NAVY = (0.12, 0.25, 0.43)
BLUE = (0.18, 0.41, 0.70)
INK = (0.13, 0.16, 0.20)
PALE = (0.95, 0.97, 0.99)
CARD_EDGE = (0.86, 0.88, 0.91)
HEADING_FILL = (0.92, 0.95, 0.98)
TABLE_EDGE = (0.78, 0.82, 0.88)
TABLE_FILL = (0.985, 0.988, 0.992)
WHITE = (1, 1, 1)
LABEL = (0.78, 0.88, 0.97)


@dataclass
class _Block:
    kind: str  # "heading" | "para" | "table"
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class _Fonts:
    body: Any
    head: Any
    body_file: str | None
    head_file: str | None


_FONT_CACHE: _Fonts | None = None
_PROBE_TEXT = "Brave, for God is with you 1000"


def _round_trips(fontfile: str) -> bool:
    """True when text written with this font extracts back unchanged.

    Some fonts (Calibri Bold here) map the space glyph to U+00A0 in the
    generated ToUnicode table, so extracted text no longer matches what was
    written. That breaks content verification and screen-reader output.
    """
    import fitz

    try:
        document = fitz.open()
        page = document.new_page()
        page.insert_text((40, 60), _PROBE_TEXT, fontsize=12, fontname="probe", fontfile=fontfile)
        extracted = page.get_text("text").strip()
        document.close()
    except Exception:
        return False
    return extracted == _PROBE_TEXT


def _first_usable(candidates: list[Path]) -> str | None:
    return next((str(p) for p in candidates if p.is_file() and _round_trips(str(p))), None)


def _load_fonts() -> _Fonts:
    import fitz

    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
    windir = os.environ.get("WINDIR")
    fonts_dir = Path(windir) / "Fonts" if windir else None
    names_head = ["segoeuib.ttf", "segoeuisb.ttf", "arialbd.ttf", "calibrib.ttf"]
    names_body = ["segoeui.ttf", "arial.ttf", "calibri.ttf"]
    head_file = _first_usable(
        ([fonts_dir / n for n in names_head] if fonts_dir else [])
        + [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]
    )
    body_file = _first_usable(
        ([fonts_dir / n for n in names_body] if fonts_dir else [])
        + [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    )
    body = fitz.Font(fontfile=body_file) if body_file else fitz.Font("helv")
    head = fitz.Font(fontfile=head_file) if head_file else fitz.Font("hebo")
    _FONT_CACHE = _Fonts(body=body, head=head, body_file=body_file, head_file=head_file)
    return _FONT_CACHE


def wrap_text(text: str, font: Any, size: float, width: float) -> list[str]:
    """Greedy word wrap by measured width; hard-splits words wider than a line."""
    lines: list[str] = []
    current = ""
    for word in str(text).split():
        candidate = f"{current} {word}" if current else word
        if font.text_length(candidate, fontsize=size) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        while len(word) > 1 and font.text_length(word, fontsize=size) > width:
            cut = len(word) - 1
            while cut > 1 and font.text_length(word[:cut], fontsize=size) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines


def parse_blocks(text: str) -> list[_Block]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    blocks: list[_Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|") and line.endswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].startswith("|") and lines[i].endswith("|"):
                cells = [c.strip() for c in lines[i][1:-1].split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
                    rows.append(cells)
                i += 1
            if len(rows) >= 2:
                blocks.append(_Block("table", rows=rows))
            else:
                blocks.extend(_Block("para", " | ".join(r)) for r in rows)
            continue
        if re.match(r"^#{1,3}\s+", line) or (len(line) < 90 and line.endswith(":")):
            blocks.append(_Block("heading", re.sub(r"^#{1,3}\s+", "", line).rstrip(":")))
        else:
            blocks.append(_Block("para", line))
        i += 1
    return blocks


def _layout(chunks: list[list[_Block]], size: float, fonts: _Fonts) -> list[list[tuple]]:
    """Return per-page drawing ops. Each chunk starts on a new page."""
    pages: list[list[tuple]] = []
    y = TOP

    def new_page() -> None:
        nonlocal y
        pages.append([])
        y = TOP

    line_h = size * 1.32
    text_w = RIGHT - LEFT - 2 * CARD_PAD_X
    for blocks in chunks:
        new_page()
        for block in blocks:
            if block.kind == "heading":
                h_size = size + 1.2
                height = h_size * 1.32 + 14
                if y + height > BOTTOM and y > TOP:
                    new_page()
                pages[-1].append(("rect", (LEFT, y, RIGHT, y + height), HEADING_FILL, HEADING_FILL, 0))
                for n, line in enumerate(wrap_text(block.text, fonts.head, h_size, text_w)[:2]):
                    pages[-1].append(("text", (LEFT + 10, y + 7 + h_size + n * h_size * 1.32), line, h_size, "head", BLUE))
                y += height + BLOCK_GAP + 4
            elif block.kind == "para":
                remaining = wrap_text(block.text, fonts.body, size, text_w)
                while remaining:
                    fit = int((BOTTOM - y - 2 * CARD_PAD_Y) // line_h)
                    if fit < 1:
                        new_page()
                        continue
                    part, remaining = remaining[:fit], remaining[fit:]
                    height = len(part) * line_h + 2 * CARD_PAD_Y
                    pages[-1].append(("rect", (LEFT, y, RIGHT, y + height), CARD_EDGE, PALE, 0.5))
                    for n, line in enumerate(part):
                        baseline = y + CARD_PAD_Y + size + n * line_h
                        pages[-1].append(("text", (LEFT + CARD_PAD_X, baseline), line, size, "body", INK))
                    y += height + BLOCK_GAP
                    if remaining:
                        new_page()
            else:
                cols = max(len(r) for r in block.rows)
                col_w = (RIGHT - LEFT) / cols
                for r, row in enumerate(block.rows):
                    wrapped = [
                        wrap_text(row[c] if c < len(row) else "", fonts.body, TABLE_SIZE, col_w - 10) or [""]
                        for c in range(cols)
                    ]
                    row_h = max(len(w) for w in wrapped) * TABLE_SIZE * 1.3 + 10
                    if y + row_h > BOTTOM and y > TOP:
                        new_page()
                    fill = NAVY if r == 0 else TABLE_FILL
                    color = WHITE if r == 0 else INK
                    for c in range(cols):
                        x0 = LEFT + c * col_w
                        pages[-1].append(("rect", (x0, y, x0 + col_w, y + row_h), TABLE_EDGE, fill, 0.6))
                        for n, line in enumerate(wrapped[c]):
                            baseline = y + 5 + TABLE_SIZE + n * TABLE_SIZE * 1.3
                            pages[-1].append(("text", (x0 + 5, baseline), line, TABLE_SIZE, "body", color))
                    y += row_h
                y += BLOCK_GAP + 8
    return pages


def split_title(content: str, fallback: str) -> tuple[str, str]:
    """Use a short first line as the title; otherwise keep all text as body."""
    lines = str(content or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip():
            first = line.strip()
            if len(first) <= TITLE_MAX_CHARS:
                return re.sub(r"^#{1,3}\s+", "", first), "\n".join(lines[index + 1:])
            break
    return fallback, str(content or "")


def render_polished_pdf(path: Path, chunks: list[str], *, title: str) -> int:
    """Render ``chunks`` (one per requested page) to ``path``; return page count."""
    import fitz

    fonts = _load_fonts()
    parsed = [parse_blocks(chunk) for chunk in chunks] or [[]]
    target = len(parsed)
    pages = _layout(parsed, BODY_SIZES[-1], fonts)
    for size in BODY_SIZES:
        candidate = _layout(parsed, size, fonts)
        if len(candidate) <= target:
            pages = candidate
            break

    document = fitz.open()
    try:
        total = len(pages)
        for index, ops in enumerate(pages, start=1):
            page = document.new_page(width=PAGE_W, height=PAGE_H)
            page.draw_rect(fitz.Rect(0, 0, PAGE_W, 92), color=NAVY, fill=NAVY)
            title_size = 24 if index == 1 else 18
            while title_size > 11 and fonts.head.text_length(title, fontsize=title_size) > RIGHT - LEFT:
                title_size -= 1
            _insert_text(page, (LEFT, 52), title, title_size, fonts, "head", WHITE)
            _insert_text(page, (LEFT, 78), f"ADVI • polished-colorful  •  {index:02d}/{total:02d}", 8.5, fonts, "body", LABEL)
            for op in ops:
                if op[0] == "rect":
                    _, rect, edge, fill, width = op
                    page.draw_rect(fitz.Rect(*rect), color=edge, fill=fill, width=width)
                else:
                    _, point, text, size, which, color = op
                    _insert_text(page, point, text, size, fonts, which, color)
        document.save(str(path))
        return total
    finally:
        document.close()


def _insert_text(page: Any, point: tuple[float, float], text: str, size: float, fonts: _Fonts, which: str, color: tuple) -> None:
    fontfile = fonts.head_file if which == "head" else fonts.body_file
    if fontfile:
        page.insert_text(point, text, fontsize=size, fontname=f"advi{which}", fontfile=fontfile, color=color)
    else:
        page.insert_text(point, text, fontsize=size, fontname="hebo" if which == "head" else "helv", color=color)
