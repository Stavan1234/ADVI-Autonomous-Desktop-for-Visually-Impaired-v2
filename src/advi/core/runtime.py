from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
from pathlib import Path
from typing import Any

from .config import Settings, LOG_ROOT, RUNTIME_ROOT, load_settings, MEMORY_ROOT, MEMORY_DATABASE
from .logging import configure_logging
from ..memory.long_term import LongTermMemory
from ..environment import EnvironmentWatchtower, get_environment_paths


from advi.core.build_info import RELEASE_ID, source_fingerprint

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    """
    Central runtime lifecycle manager for ADVI.
    Owns settings, persistent memory store connection, session ID, and graceful shutdown.
    """
    settings: Settings
    started: bool = False
    memory: LongTermMemory | None = None
    session_id: int | None = None
    session_summary: str | None = None
    environment_watchtower: EnvironmentWatchtower | None = None
    _resources: list[Any] = field(default_factory=list, repr=False)
    _shutting_down: bool = field(default=False, init=False, repr=False)
    last_start_error: str | None = field(default=None, init=False)
    last_shutdown_errors: list[str] = field(default_factory=list, init=False, repr=False)
    _lifecycle_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    @classmethod
    def create(cls) -> "Runtime":
        settings = load_settings()
        return cls(settings=settings)

    def register_resource(self, resource: Any) -> Any:
        """Register an externally-owned resource for reverse-order shutdown."""
        if resource is None:
            return resource
        with self._lifecycle_lock:
            if resource not in self._resources:
                self._resources.append(resource)
        return resource

    def start(self) -> None:
        """Start the runtime transactionally.

        If initialization fails, ADVI is left in a stopped state and any
        resources registered during startup are still cleaned up.
        """
        with self._lifecycle_lock:
            if self.started:
                logger.debug("Runtime.start() called again; already started.")
                return

            self.last_start_error = None
            self._shutting_down = False

            try:
                configure_logging(LOG_ROOT)
                MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
                RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
                (RUNTIME_ROOT / "audio").mkdir(parents=True, exist_ok=True)

                logger.info("Advi runtime initializing.")
                logger.info("Project root: %s", Path(__file__).resolve().parents[3])
                logger.info("ADVI release=%s source_fingerprint=%s", RELEASE_ID, source_fingerprint(Path(__file__).resolve().parents[3]))
                logger.info("Piper executable: %s", self.settings.piper_exe)
                logger.info("Piper model: %s", self.settings.piper_model)

                groq_keys = getattr(self.settings, "groq_api_keys", None) or ((getattr(self.settings, "groq_api_key", None),) if getattr(self.settings, "groq_api_key", None) else ())
                if not groq_keys:
                    logger.info("GROQ_API_KEY is not configured.")
                else:
                    logger.info("Groq key pool configured: %s key(s).", len(groq_keys))
                gemini_keys = getattr(self.settings, "gemini_api_keys", None) or ((getattr(self.settings, "gemini_api_key", None),) if getattr(self.settings, "gemini_api_key", None) else ())
                if not gemini_keys:
                    logger.info("GEMINI_API_KEY is not configured.")

                try:
                    self.environment_watchtower = EnvironmentWatchtower(
                        paths=get_environment_paths(),
                        inventory_enabled=True,
                        full_filesystem_inventory=getattr(self.settings, "environment_full_inventory", False),
                        process_poll_seconds=float(getattr(self.settings, "environment_process_poll_seconds", 10.0)),
                    )
                    self.environment_watchtower.start()
                    self.register_resource(self.environment_watchtower)
                except Exception as exc:
                    logger.warning("Could not start environment watchtower: %s", exc)
                    self.environment_watchtower = None

                try:
                    self.memory = LongTermMemory(MEMORY_DATABASE)
                    self.session_id = self.memory.start_session()
                    logger.info("Persistent SQLite memory initialized. Session ID: %s", self.session_id)
                except Exception as exc:
                    logger.warning("Could not initialize SQLite memory: %s", exc)
                    self.memory = None
                    self.session_id = None

                self.started = True
            except Exception as exc:
                self.last_start_error = f"{type(exc).__name__}: {exc}"
                logger.exception("ADVI runtime startup failed.")
                self.started = False
                self.shutdown()
                raise

    def shutdown(self, session_summary: str | None = None) -> None:
        """Shut down safely, attempting every cleanup step exactly once."""
        with self._lifecycle_lock:
            if self._shutting_down:
                logger.debug("Runtime.shutdown() called while shutdown is already in progress.")
                return
            if not self.started and not self._resources:
                return

            self._shutting_down = True
            self.last_shutdown_errors.clear()
            logger.info("Advi runtime shutting down.")
            summary = session_summary or self.session_summary or ""

            try:
                if self.started and self.memory and self.session_id:
                    try:
                        self.memory.end_session(self.session_id, summary=summary)
                        logger.info("Closed session ID %s with summary: %r", self.session_id, summary)
                    except Exception as exc:
                        self.last_shutdown_errors.append(
                            f"session_end: {type(exc).__name__}: {exc}"
                        )
                        logger.exception("Failed to end session cleanly: %s", exc)

                self._close_resources()
            finally:
                self.started = False
                self._shutting_down = False

    def _close_resources(self) -> None:
        """Close registered resources in reverse order without short-circuiting."""
        resources = list(reversed(self._resources))
        self._resources.clear()
        for resource in resources:
            closer = getattr(resource, "close", None)
            if not callable(closer):
                continue
            try:
                closer()
            except Exception as exc:
                self.last_shutdown_errors.append(
                    f"resource:{type(resource).__name__}: {type(exc).__name__}: {exc}"
                )
                logger.exception("Failed to close runtime resource %r", resource)

    def __enter__(self) -> "Runtime":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.shutdown()
        return None

