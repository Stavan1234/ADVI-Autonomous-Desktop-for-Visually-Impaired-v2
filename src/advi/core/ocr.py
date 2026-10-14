from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
from typing import Any

logger = logging.getLogger(__name__)


def configure_tesseract(pytesseract: Any) -> str | None:
    """Locate a Windows Tesseract executable when PATH is incomplete.

    pytesseract is only a Python wrapper; Windows installations often place
    ``tesseract.exe`` outside PATH. Prefer an explicit environment override,
    then PATH, then the common installer locations.
    """
    configured = os.getenv("TESSERACT_CMD", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    path_binary = shutil.which("tesseract")
    if path_binary:
        candidates.append(Path(path_binary))

    if os.name == "nt":
        program_files = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        ]
        for root in program_files:
            if str(root) in {"", "."}:
                continue
            candidates.extend([
                root / "Tesseract-OCR" / "tesseract.exe",
                root / "tesseract" / "tesseract.exe",
            ])

    for candidate in candidates:
        try:
            if candidate.is_file():
                resolved = str(candidate.resolve(strict=True))
                try:
                    pytesseract.pytesseract.tesseract_cmd = resolved
                except Exception:
                    pass
                return resolved
        except (OSError, RuntimeError):
            continue
    return None


def ocr_text_from_image(image: Any, pytesseract: Any, *, page_segmentation_mode: int = 11) -> str:
    """OCR one image with lightweight preprocessing for desktop-sized screenshots."""
    configure_tesseract(pytesseract)
    processed = image
    try:
        from PIL import ImageOps

        if getattr(processed, "mode", "") not in {"L", "1"}:
            processed = ImageOps.grayscale(processed)
        processed = ImageOps.autocontrast(processed)

        width, height = getattr(processed, "size", (0, 0))
        if width and width < 1800:
            scale = 1800 / width
            processed = processed.resize((1800, max(1, int(height * scale))))
    except Exception:
        logger.debug("OCR preprocessing skipped", exc_info=True)

    text = pytesseract.image_to_string(processed, config=f"--psm {page_segmentation_mode}")
    return "\n".join(line.strip() for line in str(text).splitlines() if line.strip())
