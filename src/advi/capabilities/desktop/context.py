from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

class ApplicationSessionState(str, Enum):
    DISCOVERING = "discovering"
    LAUNCHING = "launching"
    STABILIZING = "stabilizing"
    READY = "ready"
    BLOCKED_DIALOG = "blocked_dialog"
    AUTH_REQUIRED = "auth_required"
    PERMISSION_REQUIRED = "permission_required"
    RECOVERY_SCREEN = "recovery_screen"
    NOT_RESPONDING = "not_responding"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass
class ApplicationSession:
    """Live identity/session record for the application ADVI is attached to."""

    session_id: str
    application: str
    identity_key: str | None = None
    source: str | None = None
    runtime_type: str = "unknown"
    app_id: str | None = None
    package_family: str | None = None
    executable: str | None = None
    command: str | None = None
    hwnd: int | None = None
    pid: int | None = None
    window_title: str | None = None
    process_name: str | None = None
    state: ApplicationSessionState = ApplicationSessionState.UNKNOWN
    blocking_dialog: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_observed_at: float | None = None
    last_transition_reason: str | None = None

    @classmethod
    def create(cls, application: str, *, record: Any | None = None) -> "ApplicationSession":
        record_dict = record.to_dict() if hasattr(record, "to_dict") else (record if isinstance(record, dict) else {})
        return cls(
            session_id=f"app-{uuid.uuid4().hex[:12]}",
            application=str(application).strip(),
            identity_key=str(record_dict.get("identity_key") or "") or None,
            source=str(record_dict.get("source") or "") or None,
            runtime_type=str(record_dict.get("runtime_type") or "unknown"),
            app_id=str(record_dict.get("app_id") or "") or None,
            package_family=str(record_dict.get("package_family") or "") or None,
            executable=str(record_dict.get("executable") or "") or None,
            command=str(record_dict.get("command") or "") or None,
            state=ApplicationSessionState.DISCOVERING,
        )

    def bind_window(
        self,
        *,
        hwnd: int,
        pid: int | None = None,
        title: str | None = None,
        process_name: str | None = None,
        state: ApplicationSessionState | str | None = None,
        blocking_dialog: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        self.hwnd = int(hwnd)
        self.pid = int(pid) if pid is not None else None
        self.window_title = str(title).strip() if title else None
        self.process_name = str(process_name).strip() if process_name else None
        self.blocking_dialog = blocking_dialog
        self.last_observed_at = time.monotonic()
        if state is not None:
            self.state = ApplicationSessionState(str(state)) if not isinstance(state, ApplicationSessionState) else state
        if reason:
            self.last_transition_reason = str(reason)[:500]

    def transition(self, state: ApplicationSessionState | str, *, reason: str | None = None) -> None:
        self.state = ApplicationSessionState(str(state)) if not isinstance(state, ApplicationSessionState) else state
        self.last_transition_reason = str(reason)[:500] if reason else None
        self.last_observed_at = time.monotonic()

    def matches_application(self, application: str | None) -> bool:
        if not application:
            return True
        requested = " ".join(str(application).strip().casefold().replace("_", " ").split())
        current = " ".join(str(self.application).strip().casefold().replace("_", " ").split())
        if requested == current:
            return True
        aliases = {
            "microsoft word": "word",
            "microsoft excel": "excel",
            "microsoft powerpoint": "powerpoint",
            "google chrome": "chrome",
        }
        return aliases.get(requested, requested) == aliases.get(current, current)

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "application": self.application,
            "identity": {
                "identity_key": self.identity_key,
                "source": self.source,
                "runtime_type": self.runtime_type,
                "app_id": self.app_id,
                "package_family": self.package_family,
                "executable": self.executable,
                "command": self.command,
            },
            "window": {
                "hwnd": self.hwnd,
                "pid": self.pid,
                "title": self.window_title,
                "process": self.process_name,
            },
            "state": self.state.value,
            "blocking_dialog": self.blocking_dialog,
            "last_transition_reason": self.last_transition_reason,
            "observation_age": (max(0.0, time.monotonic() - self.last_observed_at) if self.last_observed_at else None),
        }



@dataclass
class DesktopContext:
    """
    Persistent desktop & browser execution context maintained during automation.
    Tracks target window handle (HWND), application identity, and browser target tab.
    """
    current_application: str | None = None
    target_hwnd: int | None = None
    target_pid: int | None = None
    target_title: str | None = None
    target_process_name: str | None = None
    active_document_path: str | None = None
    active_document_title: str | None = None
    document_dirty: bool | None = None
    chrome_cdp_port: int = 9222
    chrome_process_id: int | None = None
    chrome_window_hwnd: int | None = None
    target_tab_id: str | None = None
    target_tab_url: str | None = None
    target_tab_title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    last_observed_at: float | None = None
    last_observed_domain: str | None = None
    observation_revision: int = 0
    application_session: ApplicationSession | None = None

    def reset(self) -> None:
        """Reset context between distinct tasks."""
        self.current_application = None
        self.target_hwnd = None
        self.target_pid = None
        self.target_title = None
        self.target_process_name = None
        self.active_document_path = None
        self.active_document_title = None
        self.document_dirty = None
        self.chrome_process_id = None
        self.chrome_window_hwnd = None
        self.target_tab_id = None
        self.target_tab_url = None
        self.target_tab_title = None
        self.metadata.clear()
        self.last_observed_at = None
        self.last_observed_domain = None
        self.observation_revision = 0
        self.application_session = None
        logger.info("DesktopContext reset.")

    def start_application_session(self, application: str, *, record: Any | None = None) -> ApplicationSession:
        session = ApplicationSession.create(application, record=record)
        self.application_session = session
        self.current_application = str(application).strip()
        return session

    def update_application_session(
        self,
        *,
        hwnd: int | None = None,
        pid: int | None = None,
        title: str | None = None,
        process_name: str | None = None,
        state: ApplicationSessionState | str | None = None,
        blocking_dialog: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> ApplicationSession | None:
        session = self.application_session
        if session is None:
            return None
        if hwnd is not None:
            session.bind_window(
                hwnd=hwnd, pid=pid, title=title, process_name=process_name,
                state=state, blocking_dialog=blocking_dialog, reason=reason,
            )
        else:
            session.blocking_dialog = blocking_dialog
            if state is not None or reason:
                session.transition(state or session.state, reason=reason)
        return session

    def clear_application_session(self) -> None:
        self.application_session = None

    def mark_observed(self, domain: str | None = None) -> None:
        """Record that the current context was freshly observed for a domain."""
        self.last_observed_at = time.monotonic()
        self.last_observed_domain = str(domain).strip().casefold() if domain else None
        self.observation_revision += 1

    def invalidate_observation(self, domain: str | None = None) -> None:
        """Invalidate cached observation freshness after a state-changing action."""
        requested = str(domain or "").strip().casefold()
        if not requested or requested == self.last_observed_domain:
            self.last_observed_at = None
        self.observation_revision += 1

    def observation_is_fresh(self, domain: str | None = None, *, max_age: float = 1.5) -> bool:
        """Return whether the cached observation is recent and belongs to the domain."""
        if self.last_observed_at is None:
            return False
        requested = str(domain or "").strip().casefold()
        if requested and self.last_observed_domain and requested != self.last_observed_domain:
            return False
        return (time.monotonic() - self.last_observed_at) <= max(0.0, float(max_age))

    def set_target_tab(
        self,
        tab_id: str,
        url: str | None = None,
        title: str | None = None,
    ) -> None:
        self.invalidate_observation("browser")
        self.target_tab_id = str(tab_id)
        if url is not None:
            self.target_tab_url = str(url)
        if title is not None:
            self.target_tab_title = str(title)
        # A browser tab can be the execution surface of an installed PWA (for example
        # YouTube Music) rather than the user's requested application itself. Do not
        # erase that application identity merely because CDP bookkeeping was updated.
        if self.application_session is None or self.application_session.matches_application("Chrome"):
            self.current_application = "Chrome"

    def set_chrome_window(self, hwnd: int) -> None:
        self.chrome_window_hwnd = int(hwnd)
        self.target_hwnd = int(hwnd)

    def set_window_target(
        self,
        *,
        app_name: str,
        hwnd: int,
        pid: int | None = None,
        title: str | None = None,
        process_name: str | None = None,
    ) -> None:
        self.invalidate_observation("desktop")
        self.current_application = str(app_name).strip()
        self.target_hwnd = int(hwnd)
        self.target_pid = int(pid) if pid is not None else None
        self.target_title = str(title).strip() if title else None
        self.target_process_name = str(process_name).strip() if process_name else None
        if self.current_application.lower() in {"chrome", "google chrome"}:
            self.chrome_window_hwnd = int(hwnd)
            self.chrome_process_id = self.target_pid

    def clear_window_target(self) -> None:
        self.target_hwnd = None
        self.target_pid = None
        self.target_title = None
        self.target_process_name = None

    def set_document(self, *, path: str | None = None, title: str | None = None, dirty: bool | None = None) -> None:
        self.active_document_path = str(path) if path else None
        self.active_document_title = str(title).strip() if title else None
        self.document_dirty = dirty

    def clear_document(self) -> None:
        self.active_document_path = None
        self.active_document_title = None
        self.document_dirty = None

    def set_application(self, app_name: str, hwnd: int | None = None) -> None:
        self.current_application = str(app_name).strip()
        if hwnd is not None:
            self.set_window_target(app_name=app_name, hwnd=hwnd)

    def is_chrome_target(self) -> bool:
        if not self.current_application:
            return False
        is_chrome = self.current_application.lower() in {"chrome", "google chrome"}
        return is_chrome and bool(self.target_tab_id)

    def observation_snapshot(self, domain: str | None = None) -> dict[str, Any]:
        """Return authoritative live state for one execution domain.

        When a domain is supplied, incompatible state is intentionally omitted.
        This prevents stale browser state from being presented as evidence during a
        desktop task (and vice versa). The unfiltered form remains available for
        callers that explicitly need the complete local context.
        """
        requested = str(domain or "").strip().casefold()
        snapshot: dict[str, Any] = {}

        desktop_domains = {"desktop", "filesystem", "system", "generic"}
        include_browser = requested in {"", "browser"}
        include_desktop = requested in desktop_domains or requested == ""

        if include_desktop and self.application_session is not None:
            snapshot["application_session"] = self.application_session.snapshot()
        if include_desktop and self.current_application:
            snapshot["active_application"] = self.current_application
        if include_desktop and self.target_hwnd is not None:
            snapshot["target"] = {
                "hwnd": self.target_hwnd,
                "pid": self.target_pid,
                "title": self.target_title,
                "process": self.target_process_name,
            }
        if include_desktop and (self.active_document_path or self.active_document_title or self.document_dirty is not None):
            snapshot["document"] = {
                "path": self.active_document_path,
                "title": self.active_document_title,
                "dirty": self.document_dirty,
            }
        if include_browser and self.target_tab_id:
            snapshot["browser"] = {
                "tab_id": self.target_tab_id,
                "url": self.target_tab_url,
                "title": self.target_tab_title,
                "cdp_port": self.chrome_cdp_port,
            }
        if self.last_observed_at is not None:
            snapshot["observation_age"] = max(0.0, time.monotonic() - self.last_observed_at)
            snapshot["observation_source_domain"] = self.last_observed_domain
        snapshot["observation_revision"] = self.observation_revision
        if requested:
            snapshot["observation_domain"] = requested
        return snapshot or None
