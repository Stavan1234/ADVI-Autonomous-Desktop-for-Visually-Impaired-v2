from __future__ import annotations

import ctypes
import logging
import os
import time
from dataclasses import replace
from pathlib import Path
import re

from .application_launcher import launch as launch_application, resolve_launch_target


def _get_pyautogui():
    """Load pyautogui only when a desktop action actually executes."""
    import pyautogui
    return pyautogui
from typing import Any


from advi.tools.contracts import ToolCall, ToolResult
from advi.core.readiness import wait_until
from .context import ApplicationSessionState, DesktopContext
from .perception import ScreenPerception
from advi.environment import get_environment_paths

logger = logging.getLogger(__name__)

# Process identities for apps whose Start menu entry carries no executable.
_KNOWN_APP_PROCESSES: dict[str, tuple[str, ...]] = {
    "telegram": ("Telegram.exe",),
    "whatsapp": ("WhatsApp.Root.exe", "WhatsApp.exe"),
    "spotify": ("Spotify.exe",),
    "settings": ("SystemSettings.exe",),
    "calculator": ("CalculatorApp.exe",),
    "teams": ("ms-teams.exe", "Teams.exe"),
    "outlook": ("olk.exe", "OUTLOOK.EXE"),
    "photos": ("Photos.exe",),
    "notepad": ("Notepad.exe",),
    "paint": ("mspaint.exe",),
    # Office titles read "Excel (Product Activation Failed)" on this PC.
    "word": ("WINWORD.EXE",),
    "excel": ("EXCEL.EXE",),
    "powerpoint": ("POWERPNT.EXE",),
    "onenote": ("ONENOTE.EXE",),
}


_CLIPBOARD_API = None


def _clipboard_api():
    """user32/kernel32 with 64-bit-safe signatures for the clipboard calls.

    With ctypes' default int return type, GetClipboardData/GlobalAlloc handles
    were truncated to 32 bits and GlobalLock read a bad address: "Windows fatal
    exception: access violation" in _windows_clipboard_text (fresh-install test run).
    Private WinDLL instances keep these signatures away from other libraries.
    """
    global _CLIPBOARD_API
    if _CLIPBOARD_API is None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        _CLIPBOARD_API = (user32, kernel32)
    return _CLIPBOARD_API


class DesktopCapability:
    """
    Desktop & OS Automation Capability.
    Responsible for launching applications, managing windows, typing,
    hotkeys, mouse clicks, and perception.
    """

    SUPPORTED_ACTIONS = {
        "open_application",
        "new_document",
        "save_document",
        "save_document_as",
        "close_document",
        "close_window",
        "close_application",
        "focus_window",
        "type_text",
        "press_key",
        "hotkey",
        "click",
        "double_click",
        "right_click",
        "scroll",
        "wait",
        "list_applications",
        "find_application",
        "list_windows",
        "inspect_window",
        "read_screen",
        "look_at_screen",
    }

    def __init__(
        self,
        context: DesktopContext | None = None,
        perception: ScreenPerception | None = None,
        environment_watchtower: Any | None = None,
        screen_describer: Any | None = None,
    ) -> None:
        self.context = context or DesktopContext()
        self.perception = perception or ScreenPerception(desktop_context=self.context)
        self.environment_watchtower = environment_watchtower
        self._screen_describer = screen_describer
        self._describer_loaded = screen_describer is not None

    def execute(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        handler = getattr(self, f"_execute_{action.action}", None)
        if handler is None:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Unsupported desktop action: {action.action}",
            )

        try:
            return handler(action)
        except Exception as exc:
            logger.exception("Desktop action execution failed: %s", action.action)
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=str(exc),
            )

    # -------------------------------------------------------------
    # Window & Application Launching
    # -------------------------------------------------------------
    # Office on this PC shows "Microsoft Office Activation Wizard" a few seconds
    # after it starts; it blocked closing Word ("You cannot close Microsoft Word
    # because a dialog box is open") and any work in the app.
    def _execute_open_application(self, action: ToolCall) -> ToolResult:
        result = self._open_application(action)
        if result.success and self._is_office(action):
            from advi.capabilities.office import nags

            nags.watch_in_background()
        return result

    def _execute_close_application(self, action: ToolCall) -> ToolResult:
        if not self._is_office(action):
            return self._close_application(action)
        from advi.capabilities.office import nags

        cleared = nags.dismiss_once()
        if cleared:
            time.sleep(0.8)
        result = self._close_application(action)
        if not result.success and nags.dismiss_once():
            time.sleep(0.8)
            result = self._close_application(action)
        return result

    @staticmethod
    def _is_office(action: ToolCall) -> bool:
        from advi.capabilities.office import nags

        name = action.parameters.get("application") or action.parameters.get("app") or action.parameters.get("name") or action.target
        return os.name == "nt" and nags.is_office_app(str(name or ""))

    def _open_application(self, action: ToolCall) -> ToolResult:
        app_name = action.parameters.get("application") or action.target or ""
        app_clean = str(app_name).strip()
        if not app_clean:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Application name was not specified.")

        resolved_record = None
        if self.environment_watchtower is not None:
            try:
                resolved_record = self.environment_watchtower.find_application(app_clean)
                existing = self._find_existing_application_window(app_clean, resolved_record)
                if existing:
                    pid = self._window_process_id(existing)
                    self._remember_window_target(app_clean, existing, pid=pid)
                    session = self.context.start_application_session(app_clean, record=resolved_record)
                    focused = self._activate_window(existing)
                    blocking_dialog_hwnd = self._find_blocking_dialog(existing, pid) if pid else None
                    blocking_dialog = self._dialog_snapshot(blocking_dialog_hwnd) if blocking_dialog_hwnd else None
                    blocked = bool(blocking_dialog)
                    usable = focused or blocked
                    session.bind_window(
                        hwnd=existing,
                        pid=pid,
                        title=self._window_title(existing),
                        process_name=self._process_name(pid),
                        state=(self._application_state_for_dialog(blocking_dialog) if blocked else ApplicationSessionState.READY if usable else ApplicationSessionState.STABILIZING),
                        blocking_dialog=self._window_state(blocking_dialog) if blocking_dialog else None,
                        reason="existing application instance attached" if usable else "existing instance found but not focused",
                    )
                    return ToolResult(
                        call_id=action.call_id, name=action.action, success=usable, output=existing,
                        human_readable=(
                            f"'{app_clean}' is already open; focused its window."
                            if focused else
                            f"'{app_clean}' is already open, but a dialog is blocking it."
                            if blocked else
                            f"'{app_clean}' is already open, but its window could not be focused."
                        ),
                        error=None if usable else f"Could not focus the existing '{app_clean}' window.",
                        verified=focused, verification_status="verified" if focused else ("uncertain" if blocked else "failed"),
                        verification_details={"method": "windows_window_state", "hwnd": existing, "visible": True, "focused": focused, "ready": usable, "blocking_dialog": blocking_dialog, "application_session": session.snapshot()},
                        metadata={"pid": pid, "hwnd": existing, "application": app_clean, "already_running": True, "window_found": True, "window_visible": True, "window_focused": focused, "window_ready": usable, "blocking_dialog": blocking_dialog, "catalog_match": resolved_record.to_dict() if resolved_record else None, "application_session": session.snapshot()},
                    )
            except Exception:
                logger.debug("Existing application lookup failed for %s", app_clean, exc_info=True)

        target = resolve_launch_target(app_clean, resolved_record)
        if target is None:
            recovery = self._windows_search_and_launch(app_clean, action, record=resolved_record)
            if recovery is not None:
                return recovery
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=(f"Application '{app_clean}' is not in ADVI's trusted application catalog or installed-application inventory."), metadata={"application": app_clean, "catalog_match": None})

        process = None
        launched_via = "unknown"
        session = self.context.start_application_session(app_clean, record=resolved_record)
        session.transition(ApplicationSessionState.LAUNCHING, reason="launch requested")
        try:
            process, launched_via = launch_application(target)
            session.transition(ApplicationSessionState.STABILIZING, reason=f"launch accepted via {launched_via}")
            readiness = self._wait_for_application_state(
                app_clean,
                session=session,
                record=resolved_record,
                process=process,
                timeout=12.0,
            )
            hwnd = readiness.get("hwnd")
            launcher_pid = readiness.get("launcher_pid")
            window_pid = readiness.get("window_pid")
            pid = window_pid or launcher_pid
            blocking_dialog = readiness.get("blocking_dialog")
            visible = bool(readiness.get("visible"))
            focused = bool(readiness.get("focused"))
            ready = bool(readiness.get("ready"))
            blocked = bool(readiness.get("blocked"))

            if hwnd:
                self._remember_window_target(app_clean, int(hwnd), pid=pid)

            # A launched application with a user-actionable blocker is a successful
            # launch but not a ready application. Keep the task alive so the model
            # can inspect/resolve the blocker instead of reopening the app.
            opened = bool(hwnd and visible)
            if not opened:
                recovery = self._windows_search_and_launch(app_clean, action, existing_process=process, record=resolved_record)
                if recovery is not None:
                    return recovery

            return ToolResult(
                call_id=action.call_id, name=action.action, success=opened, output=pid or hwnd,
                human_readable=(
                    f"Opened application '{app_clean}'."
                    if ready else
                    f"Opened application '{app_clean}', but a dialog is blocking it."
                    if blocked else
                    f"Started application '{app_clean}', but it is still loading."
                    if hwnd else
                    f"Started application '{app_clean}', but its window could not be established."
                ),
                error=None if opened else f"Could not establish a usable window for '{app_clean}'.",
                verified=ready,
                verification_status="verified" if ready else ("uncertain" if blocked or hwnd else "failed"),
                verification_details={"method": "application_session_readiness", **readiness, "application_session": session.snapshot()},
                metadata={"pid": pid, "launcher_pid": launcher_pid, "window_pid": window_pid, "hwnd": hwnd, "application": app_clean, "launched_via": launched_via, "process_started": process is not None, "window_found": bool(hwnd), "window_visible": visible, "window_focused": focused, "window_ready": ready, "blocking_dialog": blocking_dialog, "catalog_match": resolved_record.to_dict() if resolved_record is not None else None, "application_session": session.snapshot()},
            )
        except Exception as exc:
            recovery = self._windows_search_and_launch(app_clean, action, record=resolved_record)
            if recovery is not None:
                return recovery
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Failed to open '{app_clean}': {exc}")

    def _wait_for_application_state(
        self,
        application: str,
        *,
        session: Any,
        record: Any | None,
        process: Any | None,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """Wait for a launched application to become stable or expose a blocker.

        This is condition-driven rather than sleep-driven: a window must be visible,
        correctly bound to the launch target, foregroundable, and free of a related
        blocking dialog before it is considered ready. A blocker is returned as a
        first-class state so the caller can surface it instead of reopening the app.
        """
        deadline = time.monotonic() + max(0.5, float(timeout))
        first_visible_at: float | None = None
        last_signature: tuple[Any, ...] | None = None
        stable_samples = 0
        last: dict[str, Any] = {
            "application": application,
            "hwnd": None,
            "launcher_pid": getattr(process, "pid", None) if process is not None else None,
            "window_pid": None,
            "visible": False,
            "focused": False,
            "ready": False,
            "blocked": False,
            "blocking_dialog": None,
            "state_reason": "waiting_for_application_window",
        }

        while time.monotonic() < deadline:
            hwnd = None
            if session.hwnd and self._window_state(session.hwnd):
                hwnd = int(session.hwnd)
            if hwnd is None and process is not None:
                try:
                    if process.poll() is None:
                        hwnd = self._find_window_by_pid(int(process.pid))
                except Exception:
                    hwnd = None
            if hwnd is None:
                try:
                    hwnd = self._find_existing_application_window(application, record)
                except Exception:
                    hwnd = None

            if hwnd:
                window_pid = self._window_process_id(int(hwnd))
                window_state = self._window_state(int(hwnd)) or {}
                if not self._application_window_matches_record(application, int(hwnd), record):
                    last.update({
                        "hwnd": int(hwnd),
                        "window_pid": window_pid,
                        "visible": bool(window_state.get("visible")),
                        "focused": bool(self._is_foreground_window(int(hwnd))),
                        "state_reason": "window_identity_mismatch",
                        "identity_mismatch": True,
                        "window_title": window_state.get("title"),
                        "process_name": window_state.get("process"),
                    })
                    # Do not bind this window to the requested application. Keep waiting
                    # for the real packaged/PWA window to appear.
                    time.sleep(0.25)
                    continue
                visible = bool(window_state.get("visible"))
                if visible and first_visible_at is None:
                    first_visible_at = time.monotonic()
                if visible and window_state.get("minimized"):
                    self._activate_window(int(hwnd))
                    window_state = self._window_state(int(hwnd)) or window_state
                focused = bool(self._is_foreground_window(int(hwnd)))
                if visible and not focused:
                    self._activate_window(int(hwnd))
                    focused = bool(self._is_foreground_window(int(hwnd)))
                    window_state = self._window_state(int(hwnd)) or window_state

                blocker_hwnd = self._find_blocking_dialog(int(hwnd), window_pid) if visible else None
                blocker = self._dialog_snapshot(blocker_hwnd) if blocker_hwnd else None
                if blocker:
                    session.bind_window(
                        hwnd=int(hwnd),
                        pid=window_pid,
                        title=window_state.get("title"),
                        process_name=window_state.get("process"),
                        state=self._application_state_for_dialog(blocker),
                        blocking_dialog=blocker,
                        reason="related blocking dialog detected",
                    )
                    last.update({
                        "hwnd": int(hwnd),
                        "window_pid": window_pid,
                        "visible": visible,
                        "focused": focused,
                        "blocked": True,
                        "ready": False,
                        "blocking_dialog": blocker,
                        "state_reason": blocker.get("classification") or "blocked_dialog",
                    })
                    return last

                signature = self._readiness_signature(int(hwnd))
                if visible:
                    if signature == last_signature:
                        stable_samples += 1
                    else:
                        stable_samples = 1
                        last_signature = signature
                    elapsed_visible = (time.monotonic() - first_visible_at) if first_visible_at else 0.0
                    # Two identical UI samples are enough when UIA is available;
                    # if UIA cannot enumerate controls, require a small settle period
                    # after the window becomes visible instead of waiting indefinitely.
                    stable = (focused and stable_samples >= 2) if signature is not None else (focused and elapsed_visible >= 0.75)
                    # Some apps never hold an identical UI sample (Windows 11 Notepad),
                    # and Windows can refuse focus to a background process. A window
                    # visible for 2.5 s with no blocking dialog is usable; reporting
                    # it as "still loading" stalled tasks (runtime log).
                    stable = stable or elapsed_visible >= 2.5
                    if stable:
                        session.bind_window(
                            hwnd=int(hwnd),
                            pid=window_pid,
                            title=window_state.get("title"),
                            process_name=window_state.get("process"),
                            state=ApplicationSessionState.READY,
                            blocking_dialog=None,
                            reason="application window is visible, focused, and stable",
                        )
                        last.update({
                            "hwnd": int(hwnd),
                            "window_pid": window_pid,
                            "visible": visible,
                            "focused": focused,
                            "ready": True,
                            "blocked": False,
                            "blocking_dialog": None,
                            "state_reason": "ready",
                        })
                        return last

                session.bind_window(
                    hwnd=int(hwnd),
                    pid=window_pid,
                    title=window_state.get("title"),
                    process_name=window_state.get("process"),
                    state=ApplicationSessionState.STABILIZING,
                    blocking_dialog=None,
                    reason="application window found but still stabilizing",
                )
                last.update({
                    "hwnd": int(hwnd),
                    "window_pid": window_pid,
                    "visible": visible,
                    "focused": focused,
                    "state_reason": "stabilizing",
                })
            time.sleep(0.2)

        if last.get("hwnd"):
            session.transition(ApplicationSessionState.STABILIZING, reason="application did not reach stable ready state before timeout")
        return last

    def _readiness_signature(self, hwnd: int) -> tuple[Any, ...] | None:
        """Return a small UI signature used only to detect stabilization."""
        try:
            from pywinauto import Desktop
            window = Desktop(backend="uia").window(handle=int(hwnd))
            if not window.exists(timeout=0.15):
                return None
            title = str(window.window_text() or "").strip()
            children = []
            for control in window.descendants():
                if len(children) >= 24:
                    break
                try:
                    if not control.is_visible():
                        continue
                    control_type = str(getattr(control.element_info, "control_type", "") or "")
                    if control_type in {"Pane", "Group", "Window"}:
                        continue
                    text = str(control.window_text() or "").strip()
                    if text or control_type in {"Button", "Edit", "ComboBox", "List", "Document", "Tab", "Hyperlink"}:
                        children.append((control_type, text[:90]))
                except Exception:
                    continue
            return (title, tuple(children))
        except Exception:
            return None

    def _find_blocking_dialog(self, target_hwnd: int, target_pid: int | None) -> int | None:
        """Return a visible foreground dialog that belongs to the tracked application."""
        if os.name != "nt" or not target_hwnd:
            return None
        foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if not foreground or foreground == int(target_hwnd):
            return None
        state = self._window_state(foreground)
        if not state or not state.get("visible"):
            return None

        owned = self._is_window_owned_by(foreground, int(target_hwnd))
        same_process = bool(target_pid and state.get("pid") == int(target_pid))
        title = str(state.get("title") or "").casefold()
        known_blocker = any(marker in title for marker in (
            "activation", "activate", "sign in", "sign-in", "login", "permission",
            "allow", "restore", "recovery", "recover", "unsaved", "update",
            "privacy", "are you sure", "save as", "open file",
        ))
        if state.get("is_dialog") and (owned or same_process):
            return foreground
        if known_blocker and (owned or same_process):
            return foreground
        return None

    @staticmethod
    def _is_window_owned_by(child_hwnd: int, owner_hwnd: int) -> bool:
        if os.name != "nt" or not child_hwnd or not owner_hwnd:
            return False
        user32 = ctypes.windll.user32
        current = int(child_hwnd)
        seen: set[int] = set()
        for _ in range(8):
            if current in seen or not current:
                return False
            seen.add(current)
            owner = int(user32.GetWindow(current, 4) or 0)  # GW_OWNER
            if owner == int(owner_hwnd):
                return True
            current = owner
        return False

    def _dialog_snapshot(self, hwnd: int | None) -> dict[str, Any] | None:
        if not hwnd:
            return None
        state = self._window_state(int(hwnd))
        if not state:
            return None
        controls = self._dialog_control_summary(int(hwnd))
        text_parts: list[str] = []
        try:
            from pywinauto import Desktop
            dialog = Desktop(backend="uia").window(handle=int(hwnd))
            if dialog.exists(timeout=0.2):
                root_text = str(dialog.window_text() or "").strip()
                if root_text:
                    text_parts.append(root_text)
                for control in dialog.descendants():
                    try:
                        if not control.is_visible():
                            continue
                        control_type = str(getattr(control.element_info, "control_type", "") or "")
                        if control_type not in {"Text", "Button", "Edit", "CheckBox", "RadioButton", "ComboBox", "Hyperlink"}:
                            continue
                        value = self._control_text(control) or str(control.window_text() or "").strip()
                        if value and value not in text_parts:
                            text_parts.append(value)
                        if len(text_parts) >= 20:
                            break
                    except Exception:
                        continue
        except Exception:
            pass
        combined = " | ".join(text_parts)[:1800]
        classification = self._classify_dialog(state.get("title"), combined)
        return {
            **state,
            "classification": classification,
            "text": combined or None,
            "controls": controls,
        }

    @staticmethod
    def _application_state_for_dialog(dialog: dict[str, Any] | None) -> ApplicationSessionState:
        classification = str((dialog or {}).get("classification") or "blocked_dialog").casefold()
        mapping = {
            "activation_required": ApplicationSessionState.AUTH_REQUIRED,
            "auth_required": ApplicationSessionState.AUTH_REQUIRED,
            "permission_required": ApplicationSessionState.PERMISSION_REQUIRED,
            "recovery_screen": ApplicationSessionState.RECOVERY_SCREEN,
            "blocked_dialog": ApplicationSessionState.BLOCKED_DIALOG,
        }
        return mapping.get(classification, ApplicationSessionState.BLOCKED_DIALOG)

    @staticmethod
    def _classify_dialog(title: str | None, text: str | None) -> str:
        haystack = f"{title or ''} {text or ''}".casefold()
        if any(marker in haystack for marker in ("activation", "activate", "unlicensed", "product activation")):
            return "activation_required"
        if any(marker in haystack for marker in ("sign in", "sign-in", "login", "log in", "account")):
            return "auth_required"
        if any(marker in haystack for marker in ("permission", "allow", "administrator", "access denied")):
            return "permission_required"
        if any(marker in haystack for marker in ("restore", "recovery", "recover", "unsaved")):
            return "recovery_screen"
        return "blocked_dialog"

    def _windows_search_and_launch(
        self, app_name: str, action: ToolCall, *, existing_process: Any | None = None, record: Any | None = None
    ) -> ToolResult | None:
        """Last-resort Windows Search launch without OCR/whole-screen clicking.

        Native AppsFolder resolution is preferred by the caller. When that path is
        unavailable, Windows Search is still useful, but ADVI must never click an
        arbitrary OCR hit such as a Word document named "WhatsApp Case Study".
        Keyboard activation is deterministic and the resulting window is accepted
        only after authoritative application-identity validation.
        """
        if os.name != "nt":
            return None
        try:
            # Re-attempt a trusted AppsFolder command if the watchtower has one.
            # This is safer than using Windows Search at all.
            shell_target = str(getattr(record, "command", "") or "")
            if shell_target.casefold().startswith("shell:appsfolder\\"):
                try:
                    from .application_launcher import LaunchTarget
                    target = LaunchTarget(name=app_name, shell_target=shell_target)
                    process, launched_via = launch_application(target)
                    deadline = time.monotonic() + 12.0
                    while time.monotonic() < deadline:
                        hwnd = self._find_existing_application_window(app_name, record)
                        if hwnd and self._application_window_matches_record(app_name, hwnd, record):
                            pid = self._window_process_id(hwnd)
                            self._remember_window_target(app_name, hwnd, pid=pid)
                            focused = self._activate_window(hwnd)
                            visible = bool(self._is_window_visible(hwnd))
                            return ToolResult(
                                call_id=action.call_id, name=action.action, success=bool(visible and focused), output=hwnd,
                                human_readable=f"Opened application '{app_name}' through Windows AppsFolder.",
                                error=None if visible and focused else f"Opened '{app_name}', but could not focus its window.",
                                verified=bool(visible and focused),
                                verification_status="verified" if visible and focused else "failed",
                                verification_details={"method":"windows_apps_folder_recovery","application":app_name,"hwnd":hwnd,"pid":pid,"launched_via":launched_via},
                                metadata={"application":app_name,"hwnd":hwnd,"pid":pid,"recovery":"windows_apps_folder","launched_via":launched_via},
                            )
                        time.sleep(0.25)
                except Exception:
                    logger.debug("Direct AppsFolder retry failed for %s", app_name, exc_info=True)

            pyautogui = _get_pyautogui()
            pyautogui.hotkey("win", "s")
            time.sleep(0.35)
            pyautogui.write(str(app_name), interval=0.04)
            time.sleep(0.9)
            # Windows Search normally selects the Apps result after typing. Enter
            # activates that structured result; importantly, there is no OCR click.
            pyautogui.press("enter")

            deadline = time.monotonic() + 10.0
            hwnd = None
            mismatch_seen: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                candidates: list[int] = []
                if existing_process is not None:
                    try:
                        pid = int(existing_process.pid)
                        candidate = self._find_window_by_pid(pid)
                        if candidate:
                            candidates.append(candidate)
                    except Exception:
                        pass
                try:
                    exact = self._find_window_candidates(app_name, exact_only=True)
                    candidates.extend(exact)
                except Exception:
                    pass
                # Also inspect currently visible windows, but accept only an identity match.
                for candidate in list(dict.fromkeys(candidates)):
                    if self._application_window_matches_record(app_name, int(candidate), record):
                        hwnd = int(candidate)
                        break
                    state = self._window_state(int(candidate)) or {}
                    mismatch_seen = {"hwnd":int(candidate),"title":state.get("title"),"process_name":state.get("process"),"pid":state.get("pid")}
                if hwnd:
                    break
                time.sleep(0.25)

            if not hwnd:
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=False,
                    error=f"Windows Search did not produce a verified '{app_name}' application window.",
                    metadata={"application":app_name,"recovery":"windows_search_keyboard","identity_mismatch":mismatch_seen},
                )

            pid = self._window_process_id(hwnd)
            self._remember_window_target(app_name, hwnd, pid=pid)
            focused = self._activate_window(hwnd)
            visible = bool(self._is_window_visible(hwnd))
            ready = bool(visible and focused and self._application_window_matches_record(app_name, hwnd, record))
            return ToolResult(
                call_id=action.call_id, name=action.action, success=ready, output=hwnd,
                human_readable=(f"Opened application '{app_name}' through Windows Search." if ready else f"Windows Search opened a window, but it was not verified as '{app_name}'."),
                error=None if ready else f"Could not verify that the Windows Search result was '{app_name}'.",
                verified=ready, verification_status="verified" if ready else "failed",
                verification_details={"method":"windows_search_keyboard","application":app_name,"hwnd":hwnd,"pid":pid,"visible":visible,"focused":focused,"ready":ready},
                metadata={"application":app_name,"hwnd":hwnd,"pid":pid,"recovery":"windows_search_keyboard"},
            )
        except Exception as exc:
            logger.debug("Windows Search launch recovery failed: %s", exc, exc_info=True)
            return None

    def _application_window_matches_record(self, application: str, hwnd: int, record: Any | None) -> bool:
        """Reject false positives such as a Word document containing the app name."""
        if not hwnd:
            return False
        if record is None:
            # Without an application catalog record there is no stronger identity
            # anchor. _find_existing_application_window already uses exact-title
            # matching in this case, so preserve that safe caller contract.
            return True
        title = str(self._window_title(hwnd) or "").strip().casefold()
        pid = self._window_process_id(hwnd)
        process = str(self._process_name(pid) or "").strip().casefold()
        requested = " ".join(str(application).strip().casefold().replace("_", " ").split())
        runtime_type = str(getattr(record, "runtime_type", "") or "").casefold()

        executable = str(getattr(record, "executable", "") or "").split(",",1)[0].strip().strip('"')
        exe_name = Path(executable).name.casefold() if executable else ""
        if exe_name and process:
            return process == exe_name

        # Strong exact app-window title patterns for packaged/MSIX applications.
        title_norm = " ".join(title.replace("|", " - ").split())
        req_norm = requested
        exact_title = title_norm == req_norm
        prefix_title = title_norm.startswith(req_norm + " - ") or title_norm.startswith(req_norm + " | ")

        # Process identity beats titles: Win11 Notepad is "Untitled - Notepad" and
        # Telegram is "Telegram (16372)"; both failed the title check and every
        # "open notepad" waited 12 s and reported "still loading".
        known = _KNOWN_APP_PROCESSES.get(req_norm.removeprefix("microsoft "))
        if process and known and process in {name.casefold() for name in known}:
            return True
        if process and process.removesuffix(".exe") == req_norm.replace(" ", ""):
            return True

        if runtime_type == "packaged":
            # Office windows/documents are never a valid identity for packaged apps,
            # even when the document title is exactly the requested app name.
            office = ("winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe")
            if process in office:
                return False
            return exact_title or prefix_title

        if runtime_type == "pwa":
            command = str(getattr(record, "command", "") or "").casefold()
            host = ""
            if "brave._crx_" in command:
                host = "brave.exe"
            elif "chrome._crx_" in command:
                host = "chrome.exe"
            elif "edge._crx_" in command or "msedge" in command:
                host = "msedge.exe"
            if host and process != host:
                return False
            return exact_title or prefix_title or requested in title_norm and process == host if host else (exact_title or prefix_title)

        if process and exe_name:
            return process == exe_name
        return exact_title or prefix_title

    def _execute_save_document(self, action: ToolCall) -> ToolResult:
        """Save the current document in the tracked application without guessing a path."""
        pyautogui = _get_pyautogui()
        target_ready, target_state = self._ensure_target_window()
        if not target_ready:
            return self._target_unavailable_result(
                action,
                "The target application window is not available for saving the current document.",
                target_state,
            )

        before_title = self._window_title(self.context.target_hwnd) if self.context.target_hwnd else None
        before_dirty = self._title_looks_dirty(before_title)
        path = self.context.active_document_path

        pyautogui.hotkey("ctrl", "s")
        deadline = time.monotonic() + 6.0
        dialog_hwnd = None
        after_title = before_title
        while time.monotonic() < deadline:
            hwnd = self.context.target_hwnd
            pid = self.context.target_pid
            dialog_hwnd = self._find_foreground_dialog_for_pid(pid) if pid else None
            if dialog_hwnd:
                break
            after_title = self._window_title(hwnd) if hwnd else after_title
            after_dirty = self._title_looks_dirty(after_title)
            if before_dirty is True and after_dirty is False:
                break
            # An already-clean document may legitimately remain unchanged.
            if before_dirty is False:
                break
            time.sleep(0.15)

        if dialog_hwnd:
            dialog_state = self._window_state(dialog_hwnd) or {"hwnd": dialog_hwnd}
            controls = self._dialog_control_summary(int(dialog_hwnd))
            detail = {
                "application": self.context.current_application,
                "document_path": path,
                "before_title": before_title,
                "after_title": after_title,
                "save_dialog": dialog_state,
                "dialog_controls": controls,
                "reason": "save_dialog_requires_user_or_model_choice",
            }
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True, output=detail,
                human_readable="The application opened a save dialog; the document still needs a save location or choice.",
                verified=False, verification_status="uncertain",
                verification_details=detail, metadata=detail,
            )

        after_dirty = self._title_looks_dirty(after_title)
        file_exists = bool(path and Path(path).is_file())
        # If we already had a known file path and it is still clean, Ctrl+S has a
        # useful observable postcondition. For a new/untitled document, no path
        # means Windows necessarily needs a Save As interaction.
        verified = bool(path and file_exists and after_dirty is False)
        self.context.document_dirty = after_dirty
        detail = {
            "application": self.context.current_application,
            "document_path": path,
            "before_title": before_title,
            "after_title": after_title,
            "before_dirty": before_dirty,
            "after_dirty": after_dirty,
            "file_exists": file_exists,
            "shortcut": "Ctrl+S",
        }
        return ToolResult(
            call_id=action.call_id, name=action.action, success=bool(verified or path is None),
            output=path or {"application": self.context.current_application},
            human_readable=(
                f"Saved the current document{f' to {Path(path).name}' if path else ''}."
                if verified else
                "The current document has no known file path; saving requires a Save As choice."
            ),
            verified=verified,
            verification_status="verified" if verified else "uncertain",
            verification_details=detail, metadata=detail,
        )

    @staticmethod
    def _title_looks_dirty(title: str | None) -> bool | None:
        if not title:
            return None
        value = str(title).strip()
        # Common Windows editor conventions put an asterisk beside unsaved state.
        # Keep this deliberately conservative; absence of an asterisk is not proof
        # that every application has persisted its state.
        if "*" in value:
            return True
        return False

    def _execute_save_document_as(self, action: ToolCall) -> ToolResult:
        """Save the current document to an explicit path through the app's Save As dialog."""
        pyautogui = _get_pyautogui()
        raw_path = action.parameters.get("path") or action.parameters.get("filename") or action.target or ""
        if not str(raw_path).strip():
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No destination path was provided.")

        target_path = self._resolve_user_path(str(raw_path))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_ready, target_state = self._ensure_target_window()
        if not target_ready:
            return self._target_unavailable_result(
                action,
                "The target application window is not available for Save As.",
                target_state,
            )

        pyautogui.hotkey("ctrl", "shift", "s")
        deadline = time.monotonic() + 8.0
        dialog_hwnd: int | None = None
        while time.monotonic() < deadline:
            dialog_hwnd = self._find_foreground_dialog_for_pid(self.context.target_pid)
            if dialog_hwnd:
                break
            time.sleep(0.15)

        if not dialog_hwnd:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True,
                output=str(target_path),
                human_readable=f"Requested Save As to '{target_path.name}', but the save dialog was not established.",
                verified=False, verification_status="uncertain",
                verification_details={"path": str(target_path), "reason": "save_dialog_not_observed"},
                metadata={"path": str(target_path), "resolved_path": str(target_path), "reason": "save_dialog_not_observed"},
            )

        dialog = self._get_uia_window(dialog_hwnd)
        if dialog is None:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True,
                output=str(target_path),
                human_readable="The Save As dialog is visible, but I could not inspect its controls.",
                verified=False, verification_status="uncertain",
                verification_details={"path": str(target_path), "dialog_hwnd": dialog_hwnd, "reason": "save_dialog_uia_unavailable"},
                metadata={"path": str(target_path), "resolved_path": str(target_path), "dialog_hwnd": dialog_hwnd},
            )

        # The common file dialog's filename box has the stable control ID 1001.
        # Its file list exposes every Name/Type/Size cell as an Edit too, so a
        # "first edit" fallback would type into the list instead.
        filename_control = self._find_dialog_edit_by_id(dialog, "1001") or self._find_dialog_edit(
            dialog, preferred_titles=("File name:", "File name")
        )
        save_button = self._find_dialog_button(dialog, titles=("Save", "&Save"))
        if filename_control is None or save_button is None:
            controls = self._dialog_control_summary(int(dialog_hwnd))
            detail = {"path": str(target_path), "dialog_hwnd": dialog_hwnd, "controls": controls, "reason": "save_dialog_controls_not_resolved"}
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True, output=detail,
                human_readable="The Save As dialog is open, but I could not identify its filename and Save controls.",
                verified=False, verification_status="uncertain", verification_details=detail, metadata=detail,
            )

        # The runtime log shows set_edit_text "succeeding" while Notepad saved
        # under its default name in another folder. Read the field back, fall
        # back to typing, and never press Save with an unconfirmed filename.
        def filename_matches() -> bool:
            current = self._dialog_edit_value(filename_control)
            return current is not None and current.strip().casefold() == str(target_path).casefold()

        # Verified live (Notepad, 2026-10-14): set_edit_text changes the box's
        # text but not the dialog's own filename, so it saved "Untitled.txt".
        # Real keystrokes update both. Type, read back, retry once.
        from pywinauto import keyboard as _keyboard

        escaped = "".join("{%s}" % ch if ch in "{}+^%~()" else ch for ch in str(target_path))
        for _attempt in range(2):
            try:
                filename_control.click_input()
                time.sleep(0.15)
                _keyboard.send_keys("^a{BACKSPACE}")
                _keyboard.send_keys(escaped, with_spaces=True, pause=0.005)
                time.sleep(0.25)
            except Exception:
                logger.debug("Typing the Save As filename failed", exc_info=True)
            if filename_matches():
                break
        if not filename_matches():
            try:
                pyautogui.press("esc")
            except Exception:
                pass
            detail = {
                "path": str(target_path), "dialog_hwnd": dialog_hwnd,
                "reason": "filename_not_confirmed", "field_value": self._dialog_edit_value(filename_control),
            }
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error="I couldn't confirm the file name in the Save As dialog, so I cancelled it instead of saving under the wrong name.",
                verified=False, verification_status="failed", metadata=detail, verification_details=detail,
            )

        try:
            save_button.click_input()
        except Exception as exc:
            detail = {"path": str(target_path), "dialog_hwnd": dialog_hwnd, "reason": f"save_button_click_failed: {exc}"}
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Could not activate the Save button.", metadata=detail, verification_details=detail)

        # Some applications show an overwrite confirmation after Save. This action
        # explicitly names the destination, so accepting an overwrite is part of
        # the requested Save As operation rather than an arbitrary recovery choice.
        time.sleep(0.35)
        self._accept_overwrite_confirmation(self.context.target_pid)

        deadline = time.monotonic() + 7.0
        exists = False
        while time.monotonic() < deadline:
            exists = target_path.is_file()
            if exists:
                break
            time.sleep(0.15)

        title = self._window_title(self.context.target_hwnd) if self.context.target_hwnd else None
        dirty = self._title_looks_dirty(title)
        verified = bool(exists and dirty is False)
        self.context.set_document(path=str(target_path), title=title or target_path.name, dirty=dirty)
        detail = {
            "path": str(target_path), "dialog_hwnd": dialog_hwnd, "file_exists": exists,
            "window_title": title, "dirty": dirty, "method": "windows_save_as_dialog"
        }
        saved_elsewhere = None
        if not exists and dirty is False and title:
            # The app saved, just not where asked; its title names the real file.
            saved_elsewhere = re.split(r"\s+-\s+", title.lstrip("*"), maxsplit=1)[0].strip() or None
            detail["saved_under_name"] = saved_elsewhere
        if verified:
            message = f"Saved the current document as '{target_path.name}'."
        elif saved_elsewhere:
            message = (f"The app saved the document as '{saved_elsewhere}' instead of '{target_path.name}', "
                       "probably in its last-used folder.")
        else:
            message = f"Save As was requested for '{target_path.name}', but the saved state could not be verified."
        return ToolResult(
            call_id=action.call_id, name=action.action, success=verified, output=str(target_path),
            human_readable=message,
            verified=verified, verification_status="verified" if verified else "uncertain",
            verification_details=detail, metadata={**detail, "resolved_path": str(target_path)},
        )

    @staticmethod
    def _resolve_user_path(raw_path: str) -> Path:
        path = Path(str(raw_path).strip()).expanduser()
        if path.is_absolute():
            return path
        parts = path.parts
        env = get_environment_paths()
        aliases = {
            "desktop": env.desktop,
            "documents": env.documents,
            "downloads": env.downloads,
        }
        base = aliases.get(parts[0].casefold()) if parts else env.desktop
        if base is not None:
            return base / Path(*parts[1:])
        return env.desktop / path

    @staticmethod
    def _dialog_edit_value(control: Any) -> str | None:
        """Read back a dialog edit field (ValuePattern first, then window text)."""
        for reader in ("get_value", "window_text"):
            try:
                value = getattr(control, reader)()
            except Exception:
                continue
            if value is not None:
                return str(value)
        return None

    @staticmethod
    def _get_uia_window(hwnd: int) -> Any | None:
        if os.name != "nt" or not hwnd:
            return None
        try:
            from pywinauto import Desktop
            window = Desktop(backend="uia").window(handle=int(hwnd))
            return window if window.exists(timeout=0.3) else None
        except Exception:
            return None

    @staticmethod
    def _find_dialog_edit(dialog: Any, *, preferred_titles: tuple[str, ...]) -> Any | None:
        try:
            for title in preferred_titles:
                candidate = dialog.child_window(title=title, control_type="Edit")
                if candidate.exists(timeout=0.15):
                    return candidate
        except Exception:
            pass
        return None

    @staticmethod
    def _find_dialog_edit_by_id(dialog: Any, automation_id: str) -> Any | None:
        try:
            for edit in dialog.descendants(control_type="Edit"):
                if str(edit.element_info.automation_id) == automation_id:
                    return edit
        except Exception:
            pass
        return None

    @staticmethod
    def _find_first_dialog_edit(dialog: Any) -> Any | None:
        try:
            edits = dialog.descendants(control_type="Edit")
            return edits[0] if edits else None
        except Exception:
            return None

    @staticmethod
    def _find_dialog_button(dialog: Any, *, titles: tuple[str, ...]) -> Any | None:
        try:
            for title in titles:
                candidate = dialog.child_window(title=title, control_type="Button")
                if candidate.exists(timeout=0.15):
                    return candidate
            buttons = dialog.descendants(control_type="Button")
            for button in buttons:
                text = str(button.window_text() or "").strip().casefold().replace("&", "")
                if text == "save":
                    return button
        except Exception:
            pass
        return None

    def _accept_overwrite_confirmation(self, pid: int | None) -> bool:
        hwnd = self._find_foreground_dialog_for_pid(pid)
        if not hwnd:
            return False
        dialog = self._get_uia_window(hwnd)
        if dialog is None:
            return False
        try:
            for title in ("Replace", "Yes", "Replace File", "&Yes"):
                button = dialog.child_window(title=title, control_type="Button")
                if button.exists(timeout=0.1):
                    button.click_input()
                    return True
        except Exception:
            pass
        return False

    def _execute_new_document(self, action: ToolCall) -> ToolResult:
        """Create a new document/tab in the intended active application.

        This is deliberately a semantic primitive rather than asking the model
        to guess a sequence of clicks. The runtime first re-establishes the
        application target, then uses the application's conventional shortcut.
        """
        pyautogui = _get_pyautogui()
        application = str(action.parameters.get("application") or self.context.current_application or "").strip()
        if not application:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error="No active application is available for creating a new document."
            )

        target_ready, target_state = self._ensure_target_window()
        if not target_ready:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error="The target application window is not available or focused; refusing to create a document in another window.",
                metadata={"target_state": target_state, "application": application},
            )

        before_hwnd = self.context.target_hwnd
        before_title = self._window_title(before_hwnd) if before_hwnd else None
        app_key = application.casefold()
        shortcut = ("ctrl", "n")
        pyautogui.hotkey(*shortcut)
        time.sleep(0.5)

        # A new document normally keeps the same app window but changes its
        # document title/tab state. If Windows creates a child/new window, allow
        # target re-discovery as well.
        hwnd = self._find_window_by_name(application) or before_hwnd
        if hwnd:
            self._remember_window_target(application, hwnd)
            focused = self._activate_window(hwnd)
        else:
            focused = False

        after_title = self._window_title(hwnd) if hwnd else None
        changed = bool(after_title and before_title and after_title != before_title)
        # A newly created document no longer refers to the file that was previously
        # open in the application. Keep the document title but clear the old path.
        self.context.clear_document()
        self.context.set_document(title=after_title, dirty=self._title_looks_dirty(after_title))
        # Tabbed editors can keep the top-level title unchanged. In that case the
        # successful shortcut plus a still-focused intended window is the best
        # local evidence available; follow-up typing is still protected by the
        # target HWND guard.
        verified = bool(focused and (changed or hwnd != before_hwnd))
        detail = {
            "application": application,
            "shortcut": "Ctrl+N",
            "before_hwnd": before_hwnd,
            "after_hwnd": hwnd,
            "before_title": before_title,
            "after_title": after_title,
            "title_changed": changed,
            "focused": focused,
            "app_key": app_key,
        }
        return ToolResult(
            call_id=action.call_id, name=action.action, success=focused,
            output={"application": application, "hwnd": hwnd},
            human_readable=f"Created a new document in {application}." if focused else f"Requested a new document in {application}, but I could not re-establish its window.",
            verified=verified, verification_status="verified" if verified else ("uncertain" if focused else "failed"),
            verification_details=detail, metadata=detail,
        )

    def _execute_list_windows(self, action: ToolCall) -> ToolResult:
        """Return a bounded snapshot of visible top-level Windows windows."""
        limit = max(1, min(int(action.parameters.get("limit") or 80), 150))
        if os.name != "nt":
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error="Native window inventory is only available on Windows.",
            )

        user32 = ctypes.windll.user32
        foreground = int(user32.GetForegroundWindow())
        process_names: dict[int, str] = {}
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pid = int(proc.info.get("pid") or 0)
                    if pid:
                        process_names[pid] = str(proc.info.get("name") or "")
                except Exception:
                    continue
        except Exception:
            pass

        rows: list[dict[str, Any]] = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            hwnd = int(hwnd)
            try:
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                title_buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title_buf, length + 1)
                title = title_buf.value.strip()
                if not title:
                    return True
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, 256)
                pid_buf = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
                pid = int(pid_buf.value)
                owner = int(user32.GetWindow(hwnd, 4))  # GW_OWNER
                class_name = class_buf.value.strip() or None
                rows.append({
                    "hwnd": hwnd,
                    "title": title,
                    "pid": pid,
                    "process": process_names.get(pid),
                    "class_name": class_name,
                    "owner_hwnd": owner or None,
                    "is_dialog": bool(class_name == "#32770" or owner),
                    "foreground": hwnd == foreground,
                    "advi_target": hwnd == self.context.target_hwnd,
                })
            except Exception:
                return True
            return len(rows) < limit

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        rows.sort(key=lambda row: (not bool(row.get("foreground")), str(row.get("title", "")).casefold()))
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True, output=rows[:limit],
            human_readable=f"Found {len(rows[:limit])} visible windows.",
            verified=True, verification_status="verified",
            verification_details={"method": "windows_enumwindows", "count": len(rows[:limit]), "foreground_hwnd": foreground},
            metadata={"count": len(rows[:limit]), "foreground_hwnd": foreground},
        )

    def _execute_inspect_window(self, action: ToolCall) -> ToolResult:
        """Inspect one desktop window and expose bounded UIA control state.

        This is the native observation primitive for messy GUI states: dialogs,
        restored documents, permission prompts, notifications, and controls.
        It never clicks or changes state.
        """
        if os.name != "nt":
            return ToolResult(call_id=action.call_id, name=action.action, success=False,
                              error="Window inspection is only available on Windows.")
        raw_hwnd = action.parameters.get("hwnd")
        target = action.parameters.get("title") or action.parameters.get("window") or action.parameters.get("name") or action.target
        hwnd: int | None = None
        if raw_hwnd not in (None, ""):
            try:
                hwnd = int(raw_hwnd)
            except (TypeError, ValueError):
                return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Invalid window handle.")
        if hwnd is None and target:
            hwnd = self._find_window_by_name(str(target))
        if hwnd is None:
            hwnd = self.context.target_hwnd
        if hwnd is None:
            try:
                hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0) or None
            except Exception:
                hwnd = None
        if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not locate window: {target or 'current foreground window'}")

        state = self._window_state(hwnd) or {"hwnd": int(hwnd)}
        controls: list[dict[str, Any]] = []
        try:
            from pywinauto import Desktop
            window = Desktop(backend="uia").window(handle=int(hwnd))
            if window.exists(timeout=0.3):
                descendants = window.descendants()
                for index, control in enumerate(descendants):
                    if index >= 80:
                        break
                    try:
                        if not control.is_visible():
                            continue
                        control_type = str(getattr(control.element_info, "control_type", "") or "")
                        title = str(control.window_text() or "").strip()
                        value = self._control_text(control)
                        if not title and not value and control_type not in {"Pane", "Window", "Document"}:
                            continue
                        controls.append({
                            "control_type": control_type or None,
                            "title": title[:300] or None,
                            "value": value[:500] if value else None,
                            "enabled": bool(control.is_enabled()) if hasattr(control, "is_enabled") else None,
                            "focused": bool(control.has_focus()) if hasattr(control, "has_focus") else None,
                        })
                    except Exception:
                        continue
        except Exception as exc:
            state["uia_error"] = str(exc)

        state["controls"] = controls[:80]
        state["control_count"] = len(controls)
        blocker_hwnd = self._find_blocking_dialog(int(hwnd), state.get("pid"))
        blocker = self._dialog_snapshot(blocker_hwnd) if blocker_hwnd else None
        state["blocking_dialog"] = blocker
        if self.context.application_session is not None and int(hwnd) == int(self.context.application_session.hwnd or 0):
            session_state = self._application_state_for_dialog(blocker) if blocker else (ApplicationSessionState.READY if state.get("foreground") else ApplicationSessionState.STABILIZING)
            self.context.application_session.bind_window(
                hwnd=int(hwnd),
                pid=state.get("pid"),
                title=state.get("title"),
                process_name=state.get("process"),
                state=session_state,
                blocking_dialog=blocker,
                reason="window inspection refreshed application state",
            )
            state["application_session"] = self.context.application_session.snapshot()
        self.context.last_observed_at = time.monotonic()
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=state,
            human_readable=f"Inspected window '{state.get('title') or target or hwnd}'.",
            verified=True,
            verification_status="verified",
            verification_details={"method": "windows_window_and_uia", "hwnd": int(hwnd), "control_count": len(controls)},
            metadata={"hwnd": int(hwnd), "target": target, "control_count": len(controls)},
        )

    @property
    def screen_describer(self) -> Any | None:
        if not self._describer_loaded:
            from .screen_describer import describer_from_settings

            self._screen_describer = describer_from_settings()
            self._describer_loaded = True
        return self._screen_describer

    def _execute_look_at_screen(self, action: ToolCall) -> ToolResult:
        """Ask the vision model about the screen; OCR text when vision is unavailable.

        The answer is an observation of the screen, never proof that a task
        succeeded, and never a place to click.
        """
        params = action.parameters
        question = str(params.get("question") or "").strip() or None
        application = str(params.get("application") or params.get("app") or params.get("window") or "").strip() or None
        if application is None and not params.get("foreground"):
            # "What's on my screen" means the whole screen; automatic looks
            # pass foreground=True and only send the window ADVI is working in.
            application = "full_screen"

        describer = self.screen_describer
        vision_error = "no Gemini API key is configured" if describer is None else None
        if describer is not None:
            try:
                shot = self.perception.window_image(application)
                answer, cached = describer.describe(shot["image"], question, window_title=shot["window"])
            except Exception as exc:
                logger.info("look_at_screen vision failed: %s", exc)
                vision_error = str(exc)
            else:
                note = "" if shot["settled"] else " (The screen was still changing when I looked.)"
                return ToolResult(
                    call_id=action.call_id,
                    name=action.action,
                    success=True,
                    output=answer + note,
                    human_readable=answer + note,
                    verified=True,
                    verification_status="verified",
                    verification_details={"method": "vision_model", "evidence": "visual_observation_only"},
                    metadata={
                        "scope": shot["scope"], "window": shot["window"], "settled": shot["settled"],
                        "model": getattr(describer, "model", None), "cached": cached, "question": question,
                    },
                )

        if params.get("foreground"):
            # Automatic looks come after a structured observation already ran;
            # OCR would only repeat it.
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error=f"Vision is unavailable: {vision_error}", metadata={"vision_error": vision_error},
            )
        ocr = self._execute_read_screen(ToolCall(
            call_id=action.call_id,
            name="read_screen",
            arguments={"application": None if application == "full_screen" else application},
        ))
        if not ocr.success:
            return replace(ocr, name=action.action, error=f"I couldn't look at the screen ({vision_error}), and {ocr.error}")
        text = f"I couldn't get a picture description ({vision_error}), but this is the text I can read: {ocr.output}"
        return replace(ocr, name=action.action, output=text, human_readable=text,
                       metadata={**dict(ocr.metadata or {}), "vision_error": vision_error})

    def _execute_read_screen(self, action: ToolCall) -> ToolResult:
        """Read visible screen text through the existing OCR perception layer."""
        application = (
            action.parameters.get("application")
            or action.parameters.get("app")
            or action.parameters.get("window")
            or self.context.current_application
        )
        reader = getattr(self.perception, "read_screen_text", None)
        if not callable(reader):
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="OCR perception is not available in the current build.",
            )
        try:
            result = reader(application=str(application).strip() if application else None)
        except Exception as exc:
            logger.exception("Screen OCR failed")
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Screen OCR failed: {exc}",
            )
        text = str(result.get("text") or "").strip()
        if not text:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=str(result.get("error") or "No readable text was detected on the screen."),
                metadata={"screenshot_path": result.get("screenshot_path"), "application": application},
            )
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=text,
            human_readable=f"Read {len(text)} characters from the screen.",
            verified=True,
            verification_status="verified",
            verification_details={"method": "tesseract_ocr", "character_count": len(text)},
            metadata={"screenshot_path": result.get("screenshot_path"), "application": application},
        )

    def _execute_find_application(self, action: ToolCall) -> ToolResult:
        name = str(action.parameters.get("name") or action.parameters.get("query") or action.target or "").strip()
        if not name:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Application name was not specified.")
        if self.environment_watchtower is None:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Installed-application inventory is unavailable.")
        try:
            record = self.environment_watchtower.find_application(name)
        except Exception as exc:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Application discovery failed: {exc}")
        if record is None:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True, output=[],
                human_readable=f"No installed application matched '{name}'.",
                verified=True, verification_status="verified",
                verification_details={"method":"windows_application_discovery", "query":name, "found":False},
                metadata={"query":name, "found":False},
            )
        running = False
        try:
            finder = getattr(self.environment_watchtower, "find_running_application", None)
            running = bool(finder(record.name)) if callable(finder) else False
        except Exception:
            running = False
        data = record.to_dict() if hasattr(record, "to_dict") else {
            "name": record.name, "source": record.source, "executable": getattr(record, "executable", None),
            "command": getattr(record, "command", None), "path": getattr(record, "path", None),
        }
        data["running"] = running
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True, output=data,
            human_readable=f"Found installed application '{record.name}'.",
            verified=True, verification_status="verified",
            verification_details={"method":"windows_application_discovery", "query":name, "found":True, "source":record.source},
            metadata={"query":name, "found":True, "application":record.name, "source":record.source},
        )

    def _execute_list_applications(self, action: ToolCall) -> ToolResult:
        query = str(action.parameters.get("query") or "").strip()
        try:
            limit = max(1, min(int(action.parameters.get("limit") or 120), 250))
        except (TypeError, ValueError):
            limit = 120
        if self.environment_watchtower is None:
            from .application_launcher import BUILTIN_APPLICATIONS
            rows = [
                {"name": name, "running": False, "source": "builtin"}
                for name in sorted(BUILTIN_APPLICATIONS)
                if not query or query.casefold() in name.casefold()
            ][:limit]
        else:
            try:
                if query:
                    # Targeted discovery should query Windows directly first. This
                    # is much faster than rebuilding the entire Start/Registry/AppX
                    # catalog merely to locate one program such as WhatsApp.
                    finder = getattr(self.environment_watchtower, "find_application", None)
                    targeted = finder(query) if callable(finder) else None
                    if targeted is not None:
                        rows = [{
                            "name": targeted.name,
                            "running": bool(getattr(self.environment_watchtower, "find_running_application", lambda _n: None)(targeted.name)),
                            "source": targeted.source,
                        }]
                    else:
                        self.environment_watchtower.refresh_applications()
                        try:
                            rows = self.environment_watchtower.list_applications(limit=limit, query=query)
                        except TypeError:
                            rows = self.environment_watchtower.list_applications(limit=limit)
                            rows = [row for row in rows if query.casefold() in str(row.get("name") or "").casefold()]
                else:
                    self.environment_watchtower.refresh_applications()
                    try:
                        rows = self.environment_watchtower.list_applications(limit=limit, query=query)
                    except TypeError:
                        rows = self.environment_watchtower.list_applications(limit=limit)
            except Exception as exc:
                return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Application inventory unavailable: {exc}")
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True,
            output=rows,
            human_readable=(f"Found {len(rows)} installed applications" + (f" matching '{query}'." if query else ".")),
            verified=True, verification_status="verified",
            verification_details={"source": "environment_watchtower", "count": len(rows), "query": query or None},
            metadata={"count": len(rows), "source": "environment_watchtower", "query": query or None},
        )

    def _execute_focus_window(self, action: ToolCall) -> ToolResult:
        target = action.parameters.get("title") or action.target or self.context.current_application
        hwnd = self.context.target_hwnd

        if not hwnd and target:
            hwnd = self._find_window_by_name(str(target))

        if not hwnd:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Could not locate window to focus: {target}",
            )

        success = self._activate_window(hwnd)
        if success:
            self._remember_window_target(str(target), hwnd)
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=success,
            output=hwnd,
            human_readable=(f"Focused window {target}." if success else f"Could not focus window {target}."),
            verified=success,
            verification_status="verified" if success else "failed",
            verification_details={"method": "foreground_window", "hwnd": hwnd, "focused": success},
            metadata={"hwnd": hwnd, "focused": success},
        )

    def _execute_close_document(self, action: ToolCall) -> ToolResult:
        """Close the active document, surfacing an unsaved-changes prompt instead of discarding it."""
        pyautogui = _get_pyautogui()
        target_ready, target_state = self._ensure_target_window(allow_owned_dialog=True)
        if not target_ready:
            return self._target_unavailable_result(
                action, "The target application window is not available for closing the document.", target_state
            )

        before_hwnd = self.context.target_hwnd
        before_title = self._window_title(before_hwnd) if before_hwnd else None
        before_path = self.context.active_document_path
        before_dirty = self.context.document_dirty
        pyautogui.hotkey("ctrl", "w")

        deadline = time.monotonic() + 5.0
        dialog_hwnd = None
        while time.monotonic() < deadline:
            dialog_hwnd = self._find_foreground_dialog_for_pid(self.context.target_pid)
            if dialog_hwnd:
                break
            hwnd = self.context.target_hwnd
            if hwnd and not ctypes.windll.user32.IsWindow(hwnd):
                break
            time.sleep(0.15)

        if dialog_hwnd:
            dialog_state = self._window_state(dialog_hwnd) or {"hwnd": dialog_hwnd}
            controls = self._dialog_control_summary(int(dialog_hwnd))
            detail = {
                "application": self.context.current_application,
                "document_path": before_path,
                "before_title": before_title,
                "before_dirty": before_dirty if before_dirty is not None else self._title_looks_dirty(before_title),
                "close_dialog": dialog_state,
                "dialog_controls": controls,
                "reason": "unsaved_document_requires_choice",
            }
            return ToolResult(
                call_id=action.call_id, name=action.action, success=True, output=detail,
                human_readable="The document has a blocking close dialog, likely asking whether to save changes.",
                verified=False, verification_status="uncertain", verification_details=detail, metadata=detail,
            )

        hwnd_still_exists = bool(before_hwnd and ctypes.windll.user32.IsWindow(before_hwnd)) if os.name == "nt" else False
        if not hwnd_still_exists:
            self.context.clear_document()
            self.context.clear_window_target()
        after_title = self._window_title(before_hwnd) if hwnd_still_exists else None
        changed = bool(before_title and after_title and before_title != after_title)
        closed = not hwnd_still_exists or changed or self.context.active_document_path is None
        if closed:
            self.context.clear_document()
        detail = {
            "application": self.context.current_application,
            "before_title": before_title,
            "after_title": after_title,
            "document_path": before_path,
            "closed": closed,
            "method": "ctrl_w_window_observation",
        }
        return ToolResult(
            call_id=action.call_id, name=action.action, success=closed, output=before_path or before_title,
            human_readable="Closed the current document." if closed else "Requested closing the current document, but it is still open.",
            verified=closed, verification_status="verified" if closed else "uncertain",
            verification_details=detail, metadata=detail,
        )

    @staticmethod
    def _windows_titled_for_app(application: str) -> list[int]:
        """Visible top-level windows titled "<anything> - <application>" (Windows' standard form)."""
        if os.name != "nt":
            return []
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        suffix = f" - {application}".casefold()
        found: list[int] = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # skip owned popups
                buffer = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buffer, 512)
                title = buffer.value.strip().lstrip("*").casefold()
                if title.endswith(suffix) or title == application.casefold():
                    found.append(int(hwnd))
            return True

        user32.EnumWindows(proc(callback), 0)
        return found

    def _close_application(self, action: ToolCall) -> ToolResult:
        """Gracefully close all visible top-level windows belonging to one app."""
        application = str(action.parameters.get("application") or action.parameters.get("app") or action.parameters.get("name") or action.target or "").strip()
        if not application:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Application name was not specified.")

        record = None
        if self.environment_watchtower is not None:
            try:
                record = self.environment_watchtower.find_application(application)
            except Exception:
                logger.debug("Application lookup failed during close_application", exc_info=True)

        hwnds: list[int] = []
        if record is not None and getattr(record, "executable", None):
            executable = Path(str(record.executable).split(",", 1)[0]).name
            hwnds.extend(self._find_windows_by_process_name(executable))
        elif record is not None and getattr(record, "name", None):
            hwnds.extend(self._find_window_candidates(str(record.name), exact_only=True))
        if not hwnds:
            hwnds.extend(self._find_window_candidates(application, exact_only=True))
        if not hwnds:
            # Packaged apps (Windows 11 Notepad) have no executable path, and their
            # titles read "<document> - Notepad", so the exact-title lookup above
            # found nothing (runtime log: close_application failed on Word too).
            try:
                found = self._find_existing_application_window(application, record)
            except Exception:
                found = None
            if found:
                hwnds.append(int(found))
            hwnds.extend(self._windows_titled_for_app(application))
        hwnds = list(dict.fromkeys(int(h) for h in hwnds if h))

        # If the app is discoverable but has no visible window, do not terminate
        # the process behind the user's back. Report the state for David/UI recovery.
        if not hwnds:
            return ToolResult(call_id=action.call_id, name=action.action, success=False,
                              error=f"I found application '{application}', but it has no closable visible window right now.",
                              verification_status="uncertain",
                              verification_details={"method":"application_window_discovery","application":application,"window_count":0},
                              metadata={"application":application,"window_count":0,"record":record.to_dict() if record is not None else None})

        user32 = ctypes.windll.user32
        for hwnd in hwnds:
            try:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                logger.debug("WM_CLOSE failed for hwnd=%s", hwnd, exc_info=True)
        deadline = time.monotonic() + 5.0
        remaining: list[int] = hwnds
        while time.monotonic() < deadline:
            remaining = [h for h in hwnds if user32.IsWindow(h) and user32.IsWindowVisible(h)]
            if not remaining:
                break
            time.sleep(0.15)

        closed = not remaining
        if closed and self.context.current_application and self.context.current_application.casefold() == application.casefold():
            self.context.clear_window_target()
        return ToolResult(
            call_id=action.call_id, name=action.action, success=closed, output={"application":application,"windows":hwnds,"remaining":remaining},
            human_readable=(f"Closed {application}." if closed else f"I asked {application} to close, but {len(remaining)} window(s) remain open."),
            verified=closed, verification_status="verified" if closed else "uncertain",
            verification_details={"method":"application_window_absence","application":application,"windows":hwnds,"remaining":remaining},
            metadata={"application":application,"window_count":len(hwnds),"remaining_windows":remaining},
        )

    def _execute_close_window(self, action: ToolCall) -> ToolResult:
        hwnd = self.context.target_hwnd or (self._find_window_by_name(action.target) if action.target else None)
        if not hwnd:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No target window available to close.",
            )

        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and user32.IsWindow(hwnd):
            time.sleep(0.15)

        gone = not bool(user32.IsWindow(hwnd))
        if gone and self.context.target_hwnd == hwnd:
            self.context.clear_window_target()
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=gone,
            human_readable=("Closed window successfully." if gone else "The window did not close; it may be waiting for input."),
            verified=gone,
            verification_status="verified" if gone else "uncertain",
            verification_details={"method": "windows_window_state", "hwnd": hwnd, "exists_after_request": not gone},
        )

    def _target_unavailable_result(
        self,
        action: ToolCall,
        message: str,
        target_state: dict[str, Any],
    ) -> ToolResult:
        """Return a model-visible recovery packet when the intended window is blocked."""
        foreground = target_state.get("foreground_window") or {}
        dialog = bool(target_state.get("blocking_dialog") or target_state.get("blocking_same_process"))
        title = str(foreground.get("title") or "").strip()
        controls = self._dialog_control_summary(int(foreground.get("hwnd") or 0)) if dialog else []
        recovery = {
            "target": {
                "hwnd": target_state.get("target_hwnd"),
                "application": self.context.current_application,
                "title": target_state.get("target_title"),
                "pid": target_state.get("target_pid"),
            },
            "foreground": foreground,
            "blocking_dialog": dialog,
            "dialog_controls": controls,
            "recovery_hint": (
                "Inspect or interact with the blocking dialog, then retry the original action."
                if dialog else
                "Re-establish the target window before retrying the original action."
            ),
        }
        detail = message + (f" A blocking dialog is visible: '{title}'." if dialog and title else "")
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=False,
            error=detail,
            output=recovery,
            human_readable=detail,
            verification_status="failed",
            verification_details={"method": "desktop_target_guard", **recovery},
            metadata={"target_state": target_state},
        )

    @staticmethod
    def _dialog_control_summary(hwnd: int) -> list[dict[str, Any]]:
        if os.name != "nt" or not hwnd:
            return []
        controls: list[dict[str, Any]] = []
        try:
            from pywinauto import Desktop
            dialog = Desktop(backend="uia").window(handle=hwnd)
            if not dialog.exists(timeout=0.2):
                return []
            for index, control in enumerate(dialog.descendants()):
                if index >= 40:
                    break
                try:
                    if not control.is_visible():
                        continue
                    control_type = str(getattr(control.element_info, "control_type", "") or "")
                    if control_type not in {"Button", "Edit", "CheckBox", "RadioButton", "ComboBox", "List", "Text", "Hyperlink"}:
                        continue
                    text = str(control.window_text() or "").strip()
                    value = DesktopCapability._control_text(control)
                    controls.append({
                        "control_type": control_type,
                        "text": text[:220] or None,
                        "value": value[:400] if value else None,
                        "enabled": bool(control.is_enabled()) if hasattr(control, "is_enabled") else None,
                    })
                except Exception:
                    continue
        except Exception:
            return []
        return controls

    # -------------------------------------------------------------
    # Keyboard & Typing
    # -------------------------------------------------------------
    @staticmethod
    def _control_text(control: Any) -> str | None:
        """Read a bounded UIA control value using lightweight accessors."""
        for method_name in ("get_value", "window_text"):
            method = getattr(control, method_name, None)
            if callable(method):
                try:
                    value = method()
                    if value is not None:
                        return str(value)[:2000]
                except Exception:
                    continue
        try:
            texts = getattr(control, "texts", None)
            if callable(texts):
                values = texts()
                if values:
                    return " ".join(str(v) for v in values)[:2000]
        except Exception:
            pass
        return None

    @classmethod
    def _control_snapshot(cls, control: Any) -> dict[str, Any]:
        """Capture only cheap UIA state useful for post-action evidence."""
        if control is None:
            return {}
        snapshot: dict[str, Any] = {}
        text = cls._control_text(control)
        if text is not None:
            snapshot["text"] = text
        for name in ("has_focus", "is_selected", "get_toggle_state", "is_expanded"):
            method = getattr(control, name, None)
            if not callable(method):
                continue
            try:
                snapshot[name] = method()
            except Exception:
                continue
        return snapshot

    @staticmethod
    def _snapshot_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
        keys = set(before) | set(after)
        return any(before.get(key) != after.get(key) for key in keys if key != "has_focus")

    @staticmethod
    def _control_actionability(control: Any) -> dict[str, Any]:
        """Capture cheap pre-actionability signals for a UIA target."""
        details: dict[str, Any] = {"visible": None, "enabled": None, "exists": None}
        for name, key in (("is_visible", "visible"), ("is_enabled", "enabled"), ("exists", "exists")):
            method = getattr(control, name, None)
            if callable(method):
                try:
                    details[key] = bool(method())
                except TypeError:
                    try:
                        details[key] = bool(method(timeout=0.1))
                    except Exception:
                        pass
                except Exception:
                    pass
        details["actionable"] = not any(value is False for value in (details.get("visible"), details.get("enabled"), details.get("exists")))
        return details

    @staticmethod
    def _focus_control(control: Any) -> bool:
        method = getattr(control, "set_focus", None)
        if not callable(method):
            return False
        try:
            method()
            return True
        except Exception:
            return False

    @staticmethod
    def _text_delta_is_expected(before: str | None, after: str | None, expected: str) -> bool:
        if before is None or after is None or before == after or not expected:
            return False
        if expected not in after:
            return False
        if expected not in before:
            return True
        start = 0
        while True:
            index = after.find(expected, start)
            if index < 0:
                break
            if after[:index] + after[index + len(expected):] == before:
                return True
            start = index + 1
        return False

    def _resolve_pointer_target(self, target: str, action: str | None = "click") -> Any | None:
        """Resolve a semantic target, allowing one re-observation for stale UI.

        Windows UI trees can change between the model request and the physical
        action because of dialogs, notifications, document restoration, or app
        redraws. A single bounded re-query is much cheaper and safer than handing
        every transient mismatch back to the LLM.
        """
        for attempt in range(2):
            try:
                result = self.perception.find(
                    target=str(target),
                    action=action,
                    application=self.context.current_application,
                )
                if result.found:
                    return result.target
            except Exception:
                logger.debug(
                    "Could not resolve desktop target '%s' (attempt %d).",
                    target, attempt + 1, exc_info=True,
                )
            if attempt == 0:
                time.sleep(0.12)
                self._ensure_target_window(allow_owned_dialog=True)
        return None

    @staticmethod
    def _windows_clipboard_text() -> str | None:
        """Read plain Unicode text from the Windows clipboard, when available."""
        if os.name != "nt":
            return None
        user32, kernel32 = _clipboard_api()
        CF_UNICODETEXT = 13
        try:
            if not user32.OpenClipboard(None):
                return None
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return None
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return None
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        except Exception:
            return None

    @staticmethod
    def _set_windows_clipboard_text(text: str) -> bool:
        """Replace the Windows clipboard with Unicode text without external dependencies."""
        if os.name != "nt":
            return False
        user32, kernel32 = _clipboard_api()
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        GMEM_ZEROINIT = 0x0040
        data = (str(text) + "\x00").encode("utf-16-le")
        handle = None
        try:
            if not user32.OpenClipboard(None):
                return False
            try:
                user32.EmptyClipboard()
                handle = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
                if not handle:
                    return False
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    kernel32.GlobalFree(handle)
                    handle = None
                    return False
                try:
                    ctypes.memmove(ptr, data, len(data))
                finally:
                    kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                    kernel32.GlobalFree(handle)
                    handle = None
                    return False
                # Ownership transfers to the clipboard after SetClipboardData.
                handle = None
                return True
            finally:
                user32.CloseClipboard()
        except Exception:
            if handle:
                try:
                    kernel32.GlobalFree(handle)
                except Exception:
                    pass
            try:
                user32.CloseClipboard()
            except Exception:
                pass
            return False

    @classmethod
    def _paste_unicode_text(cls, text: str, pyautogui: Any) -> dict[str, Any]:
        """Paste arbitrary Unicode/multiline text and restore the prior text clipboard when possible."""
        if os.name != "nt":
            pyautogui.write(text, interval=0.01)
            return {"method": "pyautogui_write", "clipboard_restored": False}

        previous = cls._windows_clipboard_text()
        if not cls._set_windows_clipboard_text(text):
            pyautogui.write(text, interval=0.01)
            return {"method": "pyautogui_write_fallback", "clipboard_restored": False}
        try:
            pyautogui.hotkey("ctrl", "v")
            return {"method": "unicode_clipboard_paste", "clipboard_restored": False}
        finally:
            # Restore the user's previous text clipboard after the paste has been
            # accepted. If the original clipboard was non-text, leave the pasted
            # text in place rather than destroying an unknown format.
            if previous is not None:
                time.sleep(0.05)
                restored = cls._set_windows_clipboard_text(previous)
            else:
                restored = False

            if previous is not None:
                return {"method": "unicode_clipboard_paste", "clipboard_restored": restored}

    def _execute_type_text(self, action: ToolCall) -> ToolResult:
        pyautogui = _get_pyautogui()
        text = action.parameters.get("value") or action.parameters.get("text")
        if text is None:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No text value provided to type.",
            )

        expected_text = str(text)
        target = action.target or action.parameters.get("target")
        target_ready, target_state = self._ensure_target_window(allow_owned_dialog=bool(target))
        if not target_ready:
            return self._target_unavailable_result(action, "The target application window is not available for typing.", target_state)
        observed_control = self._resolve_pointer_target(str(target), action="type_text") if target else None
        if target and observed_control is None:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False, output=expected_text,
                error=f"Could not locate the requested typing target '{target}'.",
                verified=False, verification_status="uncertain",
                verification_details={"method": "target_resolution_gate", "target": str(target), "reason": "target_not_found"},
            )

        focused_for_typing = self._focus_control(observed_control) if observed_control is not None else False
        if observed_control is not None:
            focus_result = wait_until(
                lambda: bool(self._control_snapshot(observed_control).get("has_focus")),
                timeout=1.0,
                poll_interval=0.05,
            )
            focused_for_typing = bool(focus_result.ready)
            if not focused_for_typing:
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=False, output=expected_text,
                    error=f"The typing target '{target}' could not be focused safely.",
                    verified=False, verification_status="uncertain",
                    verification_details={"method": "focus_readiness_gate", "target": str(target), "wait": {"elapsed": focus_result.elapsed, "attempts": focus_result.attempts}},
                )

        target_ready, target_state = self._ensure_target_window(allow_owned_dialog=bool(target))
        if not target_ready:
            return self._target_unavailable_result(action, "The target application window changed before typing could occur.", target_state)
        if observed_control is not None:
            # Re-focus after the final window-ownership check, then verify again.
            self._focus_control(observed_control)
            final_focus = wait_until(
                lambda: bool(self._control_snapshot(observed_control).get("has_focus")),
                timeout=0.75,
                poll_interval=0.05,
            )
            if not final_focus.ready:
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=False, output=expected_text,
                    error=f"The typing target '{target}' lost focus before typing.",
                    verified=False, verification_status="uncertain",
                    verification_details={"method": "final_focus_gate", "target": str(target)},
                )

        before_snapshot = self._control_snapshot(observed_control)
        before_text = before_snapshot.get("text")
        input_method = self._paste_unicode_text(expected_text, pyautogui)

        def read_after() -> dict[str, Any] | bool:
            snapshot = self._control_snapshot(observed_control)
            value = snapshot.get("text")
            return snapshot if self._text_delta_is_expected(before_text, value, expected_text) else False

        after_result = wait_until(
            read_after,
            timeout=1.25 if observed_control is not None else 0.5,
            poll_interval=0.05,
        )
        after_snapshot = after_result.value if isinstance(after_result.value, dict) else self._control_snapshot(observed_control)
        after_text = after_snapshot.get("text")
        text_delta = self._text_delta_is_expected(before_text, after_text, expected_text)

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=expected_text,
            human_readable=f"Typed {len(expected_text)} characters.",
            metadata={
                "text_length": len(expected_text),
                "target": str(target) if target else None,
                "observation": {
                    "method": "uia_control_value" if observed_control is not None else None,
                    "before_text": before_text,
                    "after_text": after_text,
                    "text_delta_observed": text_delta,
                    "focused_for_typing": focused_for_typing,
                    "input_method": input_method,
                },
            },
        )

    def _execute_press_key(self, action: ToolCall) -> ToolResult:
        pyautogui = _get_pyautogui()
        key = action.parameters.get("key") or action.parameters.get("value") or action.target
        if not key:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No key specified.",
            )

        clean_key = str(key).strip().lower()
        target_ready, target_state = self._ensure_target_window(allow_owned_dialog=True)
        if not target_ready:
            return self._target_unavailable_result(action, "The target application window is not available for this key press.", target_state)
        target_ready, target_state = self._ensure_target_window(allow_owned_dialog=True)
        if not target_ready:
            return self._target_unavailable_result(action, "The target application window changed before the key press could occur.", target_state)
        time.sleep(0.05)
        pyautogui.press(clean_key)
        time.sleep(0.1)

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=clean_key,
            human_readable=f"Pressed key '{clean_key}'.",
        )

    def _execute_hotkey(self, action: ToolCall) -> ToolResult:
        pyautogui = _get_pyautogui()
        keys = action.parameters.get("keys") or action.parameters.get("hotkey")
        if isinstance(keys, str):
            key_list = [k.strip().lower() for k in keys.split("+")]
        elif isinstance(keys, list):
            key_list = [str(k).strip().lower() for k in keys]
        else:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="Invalid hotkey specification.",
            )

        target_ready, target_state = self._ensure_target_window(allow_owned_dialog=True)
        if not target_ready:
            return self._target_unavailable_result(action, "The target application window is not available for this shortcut.", target_state)
        target_ready, target_state = self._ensure_target_window(allow_owned_dialog=True)
        if not target_ready:
            return self._target_unavailable_result(action, "The target application window changed before the shortcut could occur.", target_state)
        time.sleep(0.05)
        pyautogui.hotkey(*key_list)
        time.sleep(0.2)

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output="+".join(key_list),
            human_readable=f"Executed shortcut {'+'.join(key_list)}.",
        )

    # -------------------------------------------------------------
    # Mouse & Perception
    # -------------------------------------------------------------
    @staticmethod
    def _pointer_coordinates(action: ToolCall) -> tuple[int, int] | None:
        coords = action.parameters.get("coordinates")
        if isinstance(coords, dict) and "x" in coords and "y" in coords:
            return int(coords["x"]), int(coords["y"])
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            return int(coords[0]), int(coords[1])
        return None

    def _execute_click(self, action: ToolCall) -> ToolResult:
        pyautogui = _get_pyautogui()
        target = action.target or action.parameters.get("target")
        coords = self._pointer_coordinates(action)

        if coords is not None:
            target_ready, target_state = self._ensure_target_window(allow_owned_dialog=bool(target))
            if not target_ready:
                return self._target_unavailable_result(action, "The target application window is not available for this click.", target_state)
            pyautogui.click(*coords)
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=True,
                output={"x": coords[0], "y": coords[1]},
                human_readable=f"Clicked at coordinates ({coords[0]}, {coords[1]}).",
                metadata={"coordinates": {"x": coords[0], "y": coords[1],}, "observation": {"method": "coordinate_action", "definitive": False}},
            )

        if target:
            target_ready, target_state = self._ensure_target_window(allow_owned_dialog=True)
            if not target_ready:
                return self._target_unavailable_result(action, "The target application window is not available for this click.", target_state)
            control = self._resolve_pointer_target(str(target), action=action.action)
            if control is not None:
                try:
                    actionability = self._control_actionability(control)
                    if not actionability.get("actionable", True):
                        return ToolResult(
                            call_id=action.call_id, name=action.action, success=False, output=str(target),
                            error=f"The target '{target}' is visible but not currently actionable.",
                            verified=False, verification_status="uncertain",
                            verification_details={"method":"desktop_actionability_gate", "target":str(target), **actionability},
                            metadata={"target":str(target), "actionability":actionability},
                        )
                    before_snapshot = self._control_snapshot(control)
                    if hasattr(control, "click_input"):
                        control.click_input()
                    elif isinstance(control, (tuple, list)):
                        pyautogui.click(control[0], control[1])
                    else:
                        return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Resolved target '{target}' is not clickable.")
                    time.sleep(0.15)
                    after_snapshot = self._control_snapshot(control)
                    focused_before = before_snapshot.get("has_focus") is True
                    focused_after = after_snapshot.get("has_focus") is True
                    focus_changed_to_target = (not focused_before) and focused_after
                    state_changed = self._snapshot_changed(before_snapshot, after_snapshot)
                    return ToolResult(
                        call_id=action.call_id, name=action.action, success=True, output=str(target),
                        human_readable=f"Clicked on '{target}'.",
                        metadata={"target": str(target), "observation": {
                            "method": "uia_state_delta",
                            "before": before_snapshot,
                            "after": after_snapshot,
                            "target_focused_after": focused_after,
                            "target_focus_changed": focus_changed_to_target,
                            "state_changed": state_changed,
                        }},
                    )
                except Exception as exc:
                    return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not click target '{target}': {exc}")
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not locate target '{target}' to click.")

        return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Click requires a target or coordinates.")

    def _execute_double_click(self, action: ToolCall) -> ToolResult:
        pyautogui = _get_pyautogui()
        coords = self._pointer_coordinates(action)
        target = action.target or action.parameters.get("target")
        if coords is not None:
            target_ready, target_state = self._ensure_target_window(allow_owned_dialog=bool(target))
            if not target_ready:
                return self._target_unavailable_result(action, "The target application window is not available for this double-click.", target_state)
            pyautogui.doubleClick(*coords)
            return ToolResult(call_id=action.call_id, name=action.action, success=True, output={"x": coords[0], "y": coords[1]}, human_readable=f"Double-clicked at coordinates ({coords[0]}, {coords[1]}).", metadata={"coordinates": {"x": coords[0], "y": coords[1]}, "observation": {"method": "coordinate_action", "definitive": False}})
        if target:
            target_ready, target_state = self._ensure_target_window(allow_owned_dialog=True)
            if not target_ready:
                return self._target_unavailable_result(action, "The target application window is not available for this double-click.", target_state)
            control = self._resolve_pointer_target(str(target), action=action.action)
            if control is not None:
                try:
                    actionability = self._control_actionability(control)
                    if not actionability.get("actionable", True):
                        return ToolResult(
                            call_id=action.call_id, name=action.action, success=False, output=str(target),
                            error=f"The target '{target}' is visible but not currently actionable.",
                            verified=False, verification_status="uncertain",
                            verification_details={"method":"desktop_actionability_gate", "target":str(target), **actionability},
                            metadata={"target":str(target), "actionability":actionability},
                        )
                    before_snapshot = self._control_snapshot(control)
                    if hasattr(control, "double_click_input"):
                        control.double_click_input()
                    elif isinstance(control, (tuple, list)):
                        pyautogui.doubleClick(control[0], control[1])
                    else:
                        return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Resolved target '{target}' is not double-clickable.")
                    time.sleep(0.15)
                    after_snapshot = self._control_snapshot(control)
                    focused_before = before_snapshot.get("has_focus") is True
                    focused_after = after_snapshot.get("has_focus") is True
                    focus_changed_to_target = (not focused_before) and focused_after
                    state_changed = self._snapshot_changed(before_snapshot, after_snapshot)
                    return ToolResult(call_id=action.call_id, name=action.action, success=True, output=str(target), human_readable=f"Double-clicked on '{target}'.", metadata={"target": str(target), "observation": {
                        "method": "uia_state_delta",
                        "before": before_snapshot,
                        "after": after_snapshot,
                        "target_focused_after": focused_after,
                        "target_focus_changed": focus_changed_to_target,
                        "state_changed": state_changed,
                    }})
                except Exception as exc:
                    return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not double-click target '{target}': {exc}")
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not locate target '{target}' to double-click.")
        return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Double-click requires a target or coordinates.")

    def _execute_right_click(self, action: ToolCall) -> ToolResult:
        pyautogui = _get_pyautogui()
        coords = self._pointer_coordinates(action)
        target = action.target or action.parameters.get("target")
        if coords is not None:
            target_ready, target_state = self._ensure_target_window(allow_owned_dialog=bool(target))
            if not target_ready:
                return self._target_unavailable_result(action, "The target application window is not available for this right-click.", target_state)
            pyautogui.rightClick(*coords)
            return ToolResult(call_id=action.call_id, name=action.action, success=True, output={"x": coords[0], "y": coords[1]}, human_readable=f"Right-clicked at coordinates ({coords[0]}, {coords[1]}).", metadata={"coordinates": {"x": coords[0], "y": coords[1]}, "observation": {"method": "coordinate_action", "definitive": False}})
        if target:
            target_ready, target_state = self._ensure_target_window(allow_owned_dialog=True)
            if not target_ready:
                return self._target_unavailable_result(action, "The target application window is not available for this right-click.", target_state)
            control = self._resolve_pointer_target(str(target), action=action.action)
            if control is not None:
                try:
                    actionability = self._control_actionability(control)
                    if not actionability.get("actionable", True):
                        return ToolResult(
                            call_id=action.call_id, name=action.action, success=False, output=str(target),
                            error=f"The target '{target}' is visible but not currently actionable.",
                            verified=False, verification_status="uncertain",
                            verification_details={"method":"desktop_actionability_gate", "target":str(target), **actionability},
                            metadata={"target":str(target), "actionability":actionability},
                        )
                    before_snapshot = self._control_snapshot(control)
                    if hasattr(control, "right_click_input"):
                        control.right_click_input()
                    elif isinstance(control, (tuple, list)):
                        pyautogui.rightClick(control[0], control[1])
                    else:
                        return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Resolved target '{target}' is not right-clickable.")
                    time.sleep(0.15)
                    after_snapshot = self._control_snapshot(control)
                    focused_before = before_snapshot.get("has_focus") is True
                    focused_after = after_snapshot.get("has_focus") is True
                    focus_changed_to_target = (not focused_before) and focused_after
                    state_changed = self._snapshot_changed(before_snapshot, after_snapshot)
                    return ToolResult(call_id=action.call_id, name=action.action, success=True, output=str(target), human_readable=f"Right-clicked on '{target}'.", metadata={"target": str(target), "observation": {
                        "method": "uia_state_delta",
                        "before": before_snapshot,
                        "after": after_snapshot,
                        "target_focused_after": focused_after,
                        "target_focus_changed": focus_changed_to_target,
                        "state_changed": state_changed,
                    }})
                except Exception as exc:
                    return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not right-click target '{target}': {exc}")
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not locate target '{target}' to right-click.")
        return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Right-click requires a target or coordinates.")

    def _execute_scroll(self, action: ToolCall) -> ToolResult:
        pyautogui = _get_pyautogui()
        target_ready, target_state = self._ensure_target_window()
        if not target_ready:
            return self._target_unavailable_result(action, "The target application window is not available for scrolling.", target_state)
        amount = int(action.parameters.get("amount", 300))
        direction = str(action.parameters.get("direction", "down")).lower()
        clicks = -amount if direction == "down" else amount
        pyautogui.scroll(clicks)
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=clicks,
            human_readable=f"Scrolled {direction}.",
        )

    def _execute_wait(self, action: ToolCall) -> ToolResult:
        condition = str(action.parameters.get("condition") or "").strip().casefold()
        if condition in {"application_ready", "window_ready", "dialog_cleared", "ui_stable"}:
            application = str(
                action.parameters.get("application")
                or self.context.current_application
                or ""
            ).strip()
            session = self.context.application_session
            if not application or session is None:
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=False,
                    error="No active application session is available for a readiness wait.",
                )
            timeout = float(action.parameters.get("timeout") or action.parameters.get("duration") or action.parameters.get("seconds") or 10.0)
            state = self._wait_for_application_state(
                application,
                session=session,
                record=None,
                process=None,
                timeout=min(max(timeout, 0.5), 30.0),
            )
            if condition == "dialog_cleared":
                success = bool(state.get("ready") and not state.get("blocking_dialog"))
            elif condition == "ui_stable":
                success = bool(state.get("ready"))
            else:
                success = bool(state.get("ready"))
            return ToolResult(
                call_id=action.call_id, name=action.action, success=success, output=state,
                human_readable=(
                    f"'{application}' is ready." if success else
                    f"'{application}' is still not ready."
                ),
                error=None if success else (
                    f"'{application}' is blocked by a dialog." if state.get("blocking_dialog") else
                    f"'{application}' did not reach ready state before the wait timed out."
                ),
                verified=success, verification_status="verified" if success else "uncertain",
                verification_details={"method": "application_session_wait", "condition": condition, **state},
                metadata={"application": application, "condition": condition, **state},
            )

        duration = float(action.parameters.get("duration") or action.parameters.get("seconds") or 1.0)
        if "milliseconds" in action.parameters or (duration > 60 and "seconds" not in action.parameters):
            # The model sends 1000 meaning one second; "Waited for 1000.0s" (sweep, 2026-10-25).
            duration = float(action.parameters.get("milliseconds") or duration) / 1000.0
        duration = max(0.1, min(duration, 30.0))
        time.sleep(duration)
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=duration,
            human_readable=f"Waited for {duration:.1f}s.",
        )

    def _dismiss_safe_transient_dialog(self, hwnd: int, pid: int | None) -> bool:
        """Dismiss a clearly informational one-button dialog owned by the target app."""
        if os.name != "nt" or not hwnd or not pid:
            return False
        safe_labels = {"ok", "close", "dismiss", "got it", "not now", "skip", "continue"}
        try:
            from pywinauto import Desktop
            dialog = Desktop(backend="uia").window(handle=hwnd)
            if not dialog.exists(timeout=0.2):
                return False
            buttons = []
            for control in dialog.descendants(control_type="Button"):
                try:
                    if not control.is_visible() or not control.is_enabled():
                        continue
                    label = (control.window_text() or "").strip().casefold()
                    if label in safe_labels:
                        buttons.append(control)
                except Exception:
                    continue
            # Only auto-dismiss when exactly one benign choice exists. A multi-choice
            # dialog can contain a destructive alternative and must be surfaced.
            if len(buttons) != 1:
                return False
            buttons[0].click_input()
            deadline = time.monotonic() + 1.5
            user32 = ctypes.windll.user32
            while time.monotonic() < deadline:
                if not user32.IsWindow(hwnd):
                    return True
                time.sleep(0.1)
        except Exception:
            logger.debug("Could not dismiss transient dialog %s", hwnd, exc_info=True)
        return False

    def _ensure_target_window(self, *, allow_owned_dialog: bool = False) -> tuple[bool, dict[str, Any]]:
        """Establish the correct interactive window before touching the desktop.

        This is intentionally the final gate before keyboard/mouse input. It can:
        * rebind a replaced HWND when the same application's window was recreated;
        * recover focus after a notification or unrelated foreground window steals it;
        * recognize a blocking dialog owned by the tracked application;
        * bind the current foreground window when no prior target exists.

        It never silently chooses among multiple ambiguous application windows.
        """
        if os.name != "nt":
            if self.context.target_hwnd:
                return True, {"target_hwnd": self.context.target_hwnd, "target_known": True}
            return True, {"target_hwnd": None, "target_known": False}

        user32 = ctypes.windll.user32
        hwnd = int(self.context.target_hwnd or 0)

        # No target yet: adopt the current foreground only as an explicit
        # continuation target. This lets commands such as "press Enter" work
        # naturally without pretending we know which app the user meant when the
        # machine has no foreground window.
        if not hwnd:
            foreground = int(user32.GetForegroundWindow() or 0)
            state = self._window_state(foreground) if foreground else None
            if not state or not state.get("visible"):
                return False, {"target_known": False, "foreground_window": state}
            self.context.set_window_target(
                app_name=self.context.current_application or self._friendly_application_name(state),
                hwnd=foreground,
                pid=state.get("pid"),
                title=state.get("title"),
                process_name=state.get("process"),
            )
            self.context.last_observed_at = time.monotonic()
            return True, {
                "target_hwnd": foreground,
                "target_known": True,
                "target_bound_from_foreground": True,
                "foreground_window": state,
            }

        try:
            if not user32.IsWindow(hwnd):
                rebound = self._rebind_target_window()
                if rebound:
                    hwnd = rebound
                else:
                    self.context.clear_window_target()
                    if self.context.application_session is not None:
                        self.context.application_session.transition(ApplicationSessionState.CLOSED, reason="tracked application window no longer exists")
                    return False, {
                        "target_hwnd": hwnd,
                        "target_known": False,
                        "target_window_gone": True,
                        "rebind_attempted": True,
                    }

            visible = self._is_window_visible(hwnd)
            if not visible:
                restored = self._activate_window(hwnd)
                visible = self._is_window_visible(hwnd)
                if not visible:
                    if self.context.application_session is not None:
                        self.context.application_session.transition(ApplicationSessionState.STABILIZING, reason="tracked application window is not visible")
                    return False, {
                        "target_hwnd": hwnd,
                        "target_known": True,
                        "target_visible": False,
                        "restore_attempted": restored,
                    }

            actual_pid = self._window_process_id(hwnd)
            expected_pid = self.context.target_pid
            if expected_pid is not None and actual_pid is not None and actual_pid != expected_pid:
                old = {
                    "target_hwnd": hwnd,
                    "expected_pid": expected_pid,
                    "actual_pid": actual_pid,
                    "expected_title": self.context.target_title,
                    "actual_title": self._window_title(hwnd),
                    "target_reused": True,
                }
                rebound = self._rebind_target_window()
                if rebound:
                    hwnd = rebound
                    actual_pid = self._window_process_id(hwnd)
                else:
                    self.context.clear_window_target()
                    return False, old

            foreground = int(user32.GetForegroundWindow() or 0)
            if foreground != hwnd:
                foreground_state = self._window_state(foreground) if foreground else None
                blocker_hwnd = self._find_blocking_dialog(hwnd, actual_pid)
                blocker_state = self._dialog_snapshot(blocker_hwnd) if blocker_hwnd else None
                same_process = bool(
                    foreground_state
                    and actual_pid is not None
                    and foreground_state.get("pid") == actual_pid
                )
                owned_dialog = bool(blocker_state or (same_process and foreground_state and foreground_state.get("is_dialog")))

                # A same-process/owned dialog is a legitimate state transition. If the
                # caller named a dialog/control, allow it to be targeted. Otherwise
                # surface the blocker to the model instead of pretending the parent
                # window is interactive.
                if owned_dialog:
                    if allow_owned_dialog:
                        if self.context.application_session is not None:
                            self.context.application_session.blocking_dialog = foreground_state
                            self.context.application_session.transition(self._application_state_for_dialog(foreground_state), reason="same-process dialog is blocking the application")
                        self.context.last_observed_at = time.monotonic()
                        return True, {
                            "target_hwnd": hwnd,
                            "target_known": True,
                            "target_visible": visible,
                            "target_focused": False,
                            "foreground_window": foreground_state,
                            "blocking_same_process": True,
                            "blocking_dialog": True,
                            "dialog_allowed": True,
                        }
                    if self._dismiss_safe_transient_dialog(foreground, actual_pid):
                        refocused = self._activate_window(hwnd)
                        refocused = refocused and self._is_foreground_window(hwnd)
                        if refocused:
                            self.context.last_observed_at = time.monotonic()
                            return True, {
                                "target_hwnd": hwnd,
                                "target_known": True,
                                "target_visible": self._is_window_visible(hwnd),
                                "target_focused": True,
                                "foreground_window": self._window_state(hwnd),
                                "blocking_dialog": True,
                                "dialog_auto_dismissed": True,
                            }
                    if self.context.application_session is not None:
                        self.context.application_session.blocking_dialog = blocker_state or foreground_state
                        self.context.application_session.transition(self._application_state_for_dialog(blocker_state), reason="related dialog is blocking the application")
                    return False, {
                        "target_hwnd": hwnd,
                        "target_known": True,
                        "target_visible": visible,
                        "target_focused": False,
                        "foreground_window": foreground_state,
                        "blocking_same_process": True,
                        "blocking_dialog": True,
                    }

                # Ordinary notifications, taskbar popups and unrelated apps should
                # not steal the task target. Re-focus the exact tracked window.
                self._activate_window(hwnd)

            focused = self._is_foreground_window(hwnd)
            state = self._window_state(hwnd) or {}
            self.context.last_observed_at = time.monotonic()
            if focused:
                self.context.target_title = state.get("title") or self.context.target_title
                self.context.target_pid = actual_pid or self.context.target_pid
                self.context.target_process_name = state.get("process") or self.context.target_process_name
                if self.context.application_session is not None:
                    self.context.application_session.bind_window(
                        hwnd=hwnd,
                        pid=actual_pid,
                        title=state.get("title") or self.context.target_title,
                        process_name=state.get("process") or self.context.target_process_name,
                        state=ApplicationSessionState.READY,
                        blocking_dialog=None,
                        reason="target application window is visible and focused",
                    )
            return focused, {
                "target_hwnd": hwnd,
                "target_known": True,
                "target_visible": visible,
                "target_focused": focused,
                "target_pid": actual_pid,
                "target_title": state.get("title") or self.context.target_title,
                "foreground_window": self._window_state(int(user32.GetForegroundWindow() or 0)),
            }
        except Exception as exc:
            logger.debug("Could not restore desktop target window %s", hwnd, exc_info=True)
            return False, {
                "target_hwnd": hwnd,
                "target_known": True,
                "target_error": str(exc),
            }

    @staticmethod
    def _friendly_application_name(state: dict[str, Any]) -> str:
        process = str(state.get("process") or "").strip()
        if process:
            return re.sub(r"(?i)\\.exe$", "", process)
        return str(state.get("title") or "Unknown application").strip()

    @staticmethod
    def _normalized_name(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold().replace(".exe", ""))

    def _rebind_target_window(self) -> int | None:
        """Recover a target after its top-level HWND disappeared or changed."""
        application = self.context.current_application
        expected_pid = self.context.target_pid
        expected_process = self.context.target_process_name
        expected_title = self.context.target_title

        candidates: list[int] = []
        if expected_pid:
            candidates = self._find_windows_for_process(expected_pid)
        if not candidates and expected_process:
            candidates = self._find_windows_by_process_name(expected_process)
        if not candidates and application:
            candidates = self._find_window_candidates(application, exact_only=True)

        if not candidates:
            return None
        if len(candidates) == 1:
            hwnd = candidates[0]
        else:
            # Prefer an exact previous title, then the current foreground. Never
            # make an arbitrary choice when multiple unrelated windows remain.
            exact_title = [h for h in candidates if self._window_title(h) and str(self._window_title(h)).casefold() == str(expected_title or "").casefold()]
            if len(exact_title) == 1:
                hwnd = exact_title[0]
            else:
                try:
                    foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0) if os.name == "nt" else 0
                except Exception:
                    foreground = 0
                if foreground in candidates:
                    hwnd = foreground
                else:
                    return None

        self._remember_window_target(
            application or self._friendly_application_name(self._window_state(hwnd) or {}),
            hwnd,
            pid=expected_pid,
        )
        return hwnd

    @staticmethod
    def _find_windows_for_process(pid: int) -> list[int]:
        if os.name != "nt" or not pid:
            return []
        user32 = ctypes.windll.user32
        results: list[int] = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def callback(hwnd, _lparam):
            hwnd = int(hwnd)
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            actual = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(actual))
            if int(actual.value) == int(pid):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    results.append(hwnd)
            return True
        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return results

    def _find_windows_by_process_name(self, process_name: str) -> list[int]:
        needle = self._normalized_name(process_name)
        if not needle:
            return []
        results: list[int] = []
        for pid in self._process_ids_for_name(process_name):
            results.extend(self._find_windows_for_process(pid))
        return list(dict.fromkeys(results))

    @staticmethod
    def _process_ids_for_name(process_name: str) -> list[int]:
        try:
            import psutil
            needle = re.sub(r"(?i)\\.exe$", "", str(process_name).casefold())
            return [int(p.info["pid"]) for p in psutil.process_iter(["pid", "name"]) if str(p.info.get("name") or "").casefold().removesuffix(".exe") == needle]
        except Exception:
            return []

    @staticmethod
    def _find_foreground_dialog_for_pid(pid: int | None) -> int | None:
        if os.name != "nt" or not pid:
            return None
        user32 = ctypes.windll.user32
        foreground = int(user32.GetForegroundWindow() or 0)
        if not foreground:
            return None
        state = DesktopCapability._window_state(foreground)
        if state and state.get("pid") == int(pid) and state.get("is_dialog") and state.get("visible"):
            return foreground
        return None

    @staticmethod
    def _find_window_candidates(target_name: str, *, exact_only: bool = False) -> list[int]:
        if os.name != "nt":
            return []
        user32 = ctypes.windll.user32
        needle = str(target_name).strip().casefold()
        if not needle:
            return []
        exact: list[int] = []
        partial: list[int] = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def callback(hwnd, _lparam):
            hwnd = int(hwnd)
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip().casefold()
            if title == needle:
                exact.append(hwnd)
            elif not exact_only and (needle in title or title in needle):
                partial.append(hwnd)
            return True
        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return exact or ([] if exact_only else partial)

    @staticmethod
    def _window_process_id(hwnd: int) -> int | None:
        if os.name != "nt" or not hwnd:
            return None
        try:
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
            return int(pid.value) or None
        except Exception:
            return None

    @staticmethod
    def _process_name(pid: int | None) -> str | None:
        if not pid:
            return None
        try:
            import psutil
            return str(psutil.Process(int(pid)).name())
        except Exception:
            return None

    @classmethod
    def _window_state(cls, hwnd: int) -> dict[str, Any] | None:
        if os.name != "nt" or not hwnd:
            return None
        try:
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return None
            title = cls._window_title(hwnd)
            pid = cls._window_process_id(hwnd)
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            owner = int(user32.GetWindow(hwnd, 4))  # GW_OWNER
            return {
                "hwnd": int(hwnd),
                "title": title,
                "pid": pid,
                "process": cls._process_name(pid),
                "class_name": class_buf.value.strip() or None,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "minimized": bool(user32.IsIconic(hwnd)),
                "is_dialog": bool(class_buf.value.strip() == "#32770" or owner),
                "owner_hwnd": owner or None,
                "foreground": cls._is_foreground_window(hwnd),
            }
        except Exception:
            return None

    @classmethod
    def _window_title(cls, hwnd: int | None) -> str | None:
        if os.name != "nt" or not hwnd:
            return None
        try:
            user32 = ctypes.windll.user32
            length = user32.GetWindowTextLengthW(int(hwnd))
            if length <= 0:
                return None
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(int(hwnd), buf, length + 1)
            return buf.value.strip() or None
        except Exception:
            return None

    def _remember_window_target(self, application: str, hwnd: int, *, pid: int | None = None) -> None:
        actual_pid = pid or self._window_process_id(hwnd)
        self.context.set_window_target(
            app_name=application,
            hwnd=hwnd,
            pid=actual_pid,
            title=self._window_title(hwnd),
            process_name=self._process_name(actual_pid),
        )

    @staticmethod
    def _foreground_window_title() -> str | None:
        if os.name != "nt":
            return None
        try:
            user32 = ctypes.windll.user32
            hwnd = int(user32.GetForegroundWindow())
            if not hwnd:
                return None
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return None
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value.strip() or None
        except Exception:
            return None

    @staticmethod
    def _is_window_visible(hwnd: int) -> bool:
        if os.name != "nt":
            return True
        try:
            return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    @staticmethod
    def _is_foreground_window(hwnd: int) -> bool:
        if os.name != "nt":
            return False
        try:
            return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
        except Exception:
            return False

    # -------------------------------------------------------------
    # Windows Helpers
    # -------------------------------------------------------------
    @staticmethod
    def _activate_window(hwnd: int) -> bool:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return False
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if int(user32.GetForegroundWindow()) == int(hwnd):
            return True

        # Some Windows focus restrictions can reject SetForegroundWindow even
        # though the target window is valid. pywinauto has a second, UIA-aware
        # focus path that often succeeds in those cases.
        try:
            from pywinauto import Desktop
            window = Desktop(backend="uia").window(handle=hwnd)
            restore = getattr(window, "restore", None)
            if callable(restore):
                restore()
            focus = getattr(window, "set_focus", None)
            if callable(focus):
                focus()
            time.sleep(0.15)
        except Exception:
            pass
        return int(user32.GetForegroundWindow()) == int(hwnd)

    def _find_existing_application_window(self, application: str, record: Any = None) -> int | None:
        """Find an existing instance using application identity, never title-only fuzzy matches."""
        process_names: list[str] = []
        runtime_type = str(getattr(record, "runtime_type", "") or "").casefold() if record is not None else ""
        if record is not None and getattr(record, "executable", None):
            raw = str(record.executable).split(",", 1)[0].strip().strip('"')
            if Path(raw).name:
                process_names.append(Path(raw).name)

        # Store apps often expose only an AppID (launched through explorer.exe),
        # so a running Telegram (window "Telegram (16372)" with its unread
        # count) was never recognised and "open telegram" failed after 12-25 s.
        if not process_names:
            known = _KNOWN_APP_PROCESSES.get(re.sub(r"^microsoft\s+", "", application.strip().casefold()))
            if known:
                process_names.extend(known)

        # Some Start Apps records identify classic desktop apps with an AppID
        # but omit the executable. Reuse the same canonical launch resolver used
        # for starting the app so an already-running Word/Excel/etc. instance can
        # still be matched by its real process identity.
        if not process_names:
            try:
                resolved_target = resolve_launch_target(application, record)
                if resolved_target is not None and resolved_target.executable is not None:
                    process_names.append(Path(str(resolved_target.executable)).name)
            except Exception:
                logger.debug("Could not derive executable identity for %s", application, exc_info=True)

        # Chromium PWAs are hosted by the browser process. When Windows gives us
        # only an AppsFolder/AUMID identity, use the exact application title or a
        # known host process; never accept a partial title such as a Word document
        # named "WhatsApp Case Study" for the WhatsApp application.
        if runtime_type == "pwa":
            command = str(getattr(record, "command", "") or "").casefold() if record is not None else ""
            if "brave._crx_" in command:
                process_names.append("brave.exe")
            elif "chrome._crx_" in command:
                process_names.append("chrome.exe")
            elif "edge._crx_" in command or "msedge" in command:
                process_names.append("msedge.exe")

        candidates: list[int] = []
        try:
            import psutil
            if process_names:
                wanted = {name.casefold() for name in process_names}
                for proc in psutil.process_iter(["pid", "name"]):
                    try:
                        if str(proc.info.get("name") or "").casefold() not in wanted:
                            continue
                        candidates.extend(self._find_windows_for_process(int(proc.info["pid"])))
                    except Exception:
                        continue
        except Exception:
            pass

        if candidates:
            candidates = list(dict.fromkeys(candidates))
            try:
                foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0) if os.name == "nt" else 0
            except Exception:
                foreground = 0
            if foreground in candidates:
                return foreground
            if len(candidates) == 1:
                return candidates[0]
            # Several windows of the same app (two Telegram instances, several
            # Word documents): "open X" means its most recently used window.
            topmost = self._topmost_of(candidates)
            if topmost:
                return topmost

        # Application-title matching is exact-only at launch time. A fuzzy title
        # is appropriate for explicit "focus the Word window" requests, but is
        # unsafe for deciding whether an application is already running.
        exact = self._find_window_candidates(application, exact_only=True)
        if exact:
            try:
                foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0) if os.name == "nt" else 0
            except Exception:
                foreground = 0
            if foreground in exact:
                return foreground
            if len(exact) == 1:
                return exact[0]
        return None

    @staticmethod
    def _topmost_of(hwnds: list[int]) -> int | None:
        """The first of ``hwnds`` in Z-order (EnumWindows walks top to bottom)."""
        if os.name != "nt" or not hwnds:
            return None
        wanted = {int(h) for h in hwnds}
        found: list[int] = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            if int(hwnd or 0) in wanted:
                found.append(int(hwnd))
                return False
            return True

        try:
            ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
        except Exception:
            return None
        return found[0] if found else None

    @staticmethod
    def _find_window_by_pid(pid: int) -> int | None:
        """Resolve a visible top-level window for a PID without choosing an arbitrary document."""
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        candidates: list[int] = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            owner_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if int(owner_pid.value) == int(pid):
                candidates.append(int(hwnd))
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        if not candidates:
            return None
        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground in candidates:
            return foreground
        if len(candidates) == 1:
            return candidates[0]
        # Multiple same-process windows with no foreground winner are ambiguous.
        return None

    @staticmethod
    def _find_window_by_name(target_name: str) -> int | None:
        """Return a deterministic window match; never pick an arbitrary duplicate."""
        if os.name != "nt":
            return None
        candidates = DesktopCapability._find_window_candidates(target_name)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        try:
            foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
            if foreground in candidates:
                return foreground
        except Exception:
            pass
        # Exact title matches are already preferred by _find_window_candidates.
        # Multiple partial matches are intentionally ambiguous; returning None
        # is safer than opening/focusing an unintended document.
        return None
