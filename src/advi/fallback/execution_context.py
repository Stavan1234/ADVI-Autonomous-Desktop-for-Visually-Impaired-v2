from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExecutionContext:
    """
    Persistent execution context maintained throughout an automation plan.

    Key Requirements Fulfilled:
    1. Target Tab Ownership: Retains the exact Chrome tab identity (target_tab_id)
       and Chrome window handle (chrome_window_hwnd).
    2. OS Focus vs Logical Target: Tracks current_application and target_hwnd
       separately from OS window focus.
    3. Tab Isolation: Prevents taking over unrelated open Chrome tabs.
    """

    current_application: str | None = None
    target_hwnd: int | None = None
    chrome_cdp_port: int = 9222
    chrome_process_id: int | None = None
    chrome_window_hwnd: int | None = None
    target_tab_id: str | None = None
    target_tab_url: str | None = None
    target_tab_title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def reset(self) -> None:
        """Reset execution context between distinct user tasks."""
        self.current_application = None
        self.target_hwnd = None
        self.chrome_process_id = None
        self.chrome_window_hwnd = None
        self.target_tab_id = None
        self.target_tab_url = None
        self.target_tab_title = None
        self.metadata.clear()
        logger.info("ExecutionContext reset.")

    def set_target_tab(
        self,
        tab_id: str,
        url: str | None = None,
        title: str | None = None,
    ) -> None:
        """Store the identity of the automation task's target Chrome tab."""
        self.target_tab_id = str(tab_id)
        if url is not None:
            self.target_tab_url = str(url)
        if title is not None:
            self.target_tab_title = str(title)
        self.current_application = "Chrome"
        logger.info(
            "ExecutionContext target tab set: tab_id=%s, url=%s, title=%r",
            self.target_tab_id,
            self.target_tab_url,
            self.target_tab_title,
        )

    def set_chrome_window(self, hwnd: int) -> None:
        """Store the HWND of the Chrome window owned by this task."""
        self.chrome_window_hwnd = int(hwnd)
        self.target_hwnd = int(hwnd)
        logger.info("ExecutionContext Chrome HWND set to %s", self.chrome_window_hwnd)

    def set_application(self, app_name: str, hwnd: int | None = None) -> None:
        """Set the active application name and optional window handle."""
        self.current_application = str(app_name).strip()
        if hwnd is not None:
            self.target_hwnd = int(hwnd)
            if self.current_application.lower() in {"chrome", "google chrome"}:
                self.chrome_window_hwnd = int(hwnd)
        logger.info(
            "ExecutionContext application set: %s (HWND=%s)",
            self.current_application,
            self.target_hwnd,
        )

    def is_chrome_target(self) -> bool:
        """Return True if the target application is Chrome with a valid tab ID."""
        if not self.current_application:
            return False
        is_chrome = self.current_application.lower() in {"chrome", "google chrome"}
        return is_chrome and bool(self.target_tab_id)
