from __future__ import annotations

import logging
import os
from pathlib import Path


from .pipeline_trace import sanitize_string


class AdviFormatter(logging.Formatter):
    """Compact console format. User-facing output does not go through this handler."""

    def format(self, record: logging.LogRecord) -> str:
        msg = sanitize_string(record.getMessage())
        formatted = f"[{record.levelname}] [{record.name}] {msg}"
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            formatted = f"{formatted}\n{sanitize_string(record.exc_text)}"
        if record.stack_info:
            formatted = f"{formatted}\n{sanitize_string(self.formatStack(record.stack_info))}"
        return formatted


class AdviFileFormatter(logging.Formatter):
    """High-detail file format for logs/advi.log."""

    def format(self, record: logging.LogRecord) -> str:
        created = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        location = f"{record.filename}:{record.lineno} {record.funcName}"
        msg = sanitize_string(record.getMessage())
        formatted = (
            f"{created}.{int(record.msecs):03d} "
            f"[{record.levelname}] [{record.name}] "
            f"[{record.threadName}] [{location}] {msg}"
        )
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            formatted = f"{formatted}\n{sanitize_string(record.exc_text)}"
        if record.stack_info:
            formatted = f"{formatted}\n{sanitize_string(self.formatStack(record.stack_info))}"
        return formatted


def configure_logging(log_root: Path) -> None:
    log_root.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    logging.getLogger("advi").setLevel(logging.DEBUG)

    # Third-party chatter stays out of the ADVI diagnostic file unless it is a warning.
    for noisy in (
        "httpx",
        "httpcore",
        "urllib3",
        "google",
        "google_genai",
        "groq",
        "openai",
        "comtypes",
        "asyncio",
        "websockets",
        "websocket",
        "PIL",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not root.handlers:
        console = logging.StreamHandler()
        console.setLevel(getattr(logging, os.getenv("ADVI_CONSOLE_LOG_LEVEL", "WARNING").upper(), logging.WARNING))
        console.setFormatter(AdviFormatter())
        root.addHandler(console)

        file_handler = logging.FileHandler(log_root / "advi.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(AdviFileFormatter())
        root.addHandler(file_handler)

        trace_logger = logging.getLogger("advi.trace")
        trace_logger.setLevel(logging.DEBUG)
        trace_logger.propagate = True


