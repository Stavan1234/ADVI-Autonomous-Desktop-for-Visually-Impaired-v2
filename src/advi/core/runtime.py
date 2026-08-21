from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from .config import Settings, LOG_ROOT, RUNTIME_ROOT, load_settings
from .logging import configure_logging


logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    started: bool = False

    @classmethod
    def create(cls) -> "Runtime":
        settings = load_settings()
        return cls(settings=settings)

    def start(self) -> None:
        if self.started:
            logger.debug("Runtime.start() called again; already started.")
            return

        configure_logging(LOG_ROOT)
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        (RUNTIME_ROOT / "audio").mkdir(parents=True, exist_ok=True)

        logger.info("Advi runtime initialized.")
        logger.info("Project root: %s", Path(__file__).resolve().parents[3])
        logger.info("Python foundation ready.")
        logger.info("Piper executable configured: %s", self.settings.piper_exe)
        logger.info("Piper model configured: %s", self.settings.piper_model)

        if not self.settings.groq_api_key:
            logger.info("GROQ_API_KEY is not configured. Model features remain disabled.")
        if not self.settings.gemini_api_key:
            logger.info("GEMINI_API_KEY is not configured. Gemini features remain disabled.")

        self.started = True

    def shutdown(self) -> None:
        if not self.started:
            return
        logger.info("Advi runtime shutting down.")
        self.started = False
