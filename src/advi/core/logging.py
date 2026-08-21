from __future__ import annotations

import logging
from pathlib import Path


class AdviFormatter(logging.Formatter):
    """Single foundation-level log format; user-facing output comes later."""

    def format(self, record: logging.LogRecord) -> str:
        return f"[{record.levelname}] [{record.name}] {record.getMessage()}"


def configure_logging(log_root: Path) -> None:
    log_root.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Avoid duplicate handlers when tests or an embedding process initialize Advi twice.
    if root.handlers:
        return

    formatter = AdviFormatter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_root / "advi.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
