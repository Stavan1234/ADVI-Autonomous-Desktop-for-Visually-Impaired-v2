from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import shutil
import time
from pathlib import Path
from typing import Any



def _get_pyautogui():
    """Load pyautogui only when a fallback desktop action actually executes."""
    import pyautogui
    return pyautogui

from .action_plan_schema import Action, ActionPlan
from .cdp_browser import CDPBrowser
from .execution_context import ExecutionContext
from advi.capabilities.desktop.application_launcher import launch as launch_application, resolve_launch_target
from advi.environment.watchtower import EnvironmentWatchtower

logger = logging.getLogger(__name__)


class ActionResult:
    def __init__(
        self,
        action: str,
        success: bool,
        data: Any = None,
        error: str | None = None,
    ) -> None:
        self.action = action
        self.success = success
        self.data = data
        self.error = error

    def __repr__(self) -> str:
        return f"ActionResult(action={self.action!r}, success={self.success!r}, data={self.data!r}, error={self.error!r})"


class FallbackExecutor:
    """
    Generalized Agentic Desktop & Browser Fallback Executor.

    Key Requirements & Architecture:
    1. Target Ownership: Uses ExecutionContext to retain target_tab_id and target_hwnd.
    2. Website Search Inside Website: Search query is typed and submitted INSIDE the page
       via CDP (never using address bar / Ctrl+L unless requested).
    3. Separation of Logical Target & Focus: Web actions run via CDP on target_tab_id.
       Physical keyboard/mouse actions activate and verify target HWND first.
    4. Continuous Execution: No tab loss, tab jumping, or random tab switching.
    5. State Awareness & Idempotency: Prevents duplicate typing ("do-overs") and duplicate tab creation.
    6. Generalized Application Support: Websites (YouTube, Amazon, Reddit, generic) and Desktop Apps (Notepad, etc.).
    """

    def __init__(self, context: ExecutionContext | None = None) -> None:
        self.context = context or ExecutionContext()
        self._chrome_cdp_port = 9222
        self.cdp_browser = CDPBrowser(port=self._chrome_cdp_port)

    # Legacy properties for backward compatibility
    @property
    def _current_window_hwnd(self) -> int | None:
        return self.context.target_hwnd

    @_current_window_hwnd.setter
    def _current_window_hwnd(self, value: int | None) -> None:
        self.context.target_hwnd = value

    @property
    def _chrome_tab_id(self) -> str | None:
        return self.context.target_tab_id

    @_chrome_tab_id.setter
    def _chrome_tab_id(self, value: str | None) -> None:
        self.context.target_tab_id = value

    @property
    def _chrome_tab_url(self) -> str | None:
        return self.context.target_tab_url

    @_chrome_tab_url.setter
    def _chrome_tab_url(self, value: str | None) -> None:
        self.context.target_tab_url = value

    @property
    def _chrome_tab_title(self) -> str | None:
        return self.context.target_tab_title

    @_chrome_tab_title.setter
    def _chrome_tab_title(self, value: str | None) -> None:
        self.context.target_tab_title = value

    @property
    def _chrome_process_id(self) -> int | None:
        return self.context.chrome_process_id

    @_chrome_process_id.setter
    def _chrome_process_id(self, value: int | None) -> None:
        self.context.chrome_process_id = value

    @property
    def _chrome_window_hwnd(self) -> int | None:
        return self.context.chrome_window_hwnd

    @_chrome_window_hwnd.setter
    def _chrome_window_hwnd(self, value: int | None) -> None:
        self.context.chrome_window_hwnd = value

    def execute(self, plan: ActionPlan) -> list[ActionResult]:
        results: list[ActionResult] = []
        for action in plan.actions:
            result = self.execute_action(action)
            results.append(result)
            if not result.success:
                break
        return results

    def execute_action(self, action: Action, context: ExecutionContext | None = None) -> ActionResult:
        if context is not None:
            self.context = context

        handler = getattr(self, f"_execute_{action.action}", None)
        if handler is None:
            return ActionResult(
                action=action.action,
                success=False,
                error=f"Unsupported fallback action: {action.action}",
            )

        try:
            return handler(action)
        except Exception as exc:
            logger.exception("Fallback action execution failed: %s", action.action)
            return ActionResult(
                action=action.action,
                success=False,
                error=str(exc),
            )

    # =========================================================
    # CDP & TAB CONTEXT HELPERS
    # =========================================================

    def _list_chrome_tabs(self) -> list[dict[str, Any]]:
        return self.cdp_browser.list_tabs()

    def _verify_chrome_tab_context(self) -> bool:
        if not self.context.target_tab_id:
            return False

        tab_info = self.cdp_browser.get_tab_info(self.context.target_tab_id)
        if tab_info is None:
            logger.warning("Stored Chrome target_tab_id %s disappeared.", self.context.target_tab_id)
            return False

        self.context.target_tab_url = str(tab_info.get("url", "")) or self.context.target_tab_url
        self.context.target_tab_title = str(tab_info.get("title", "")) or self.context.target_tab_title
        return True

    @staticmethod
    def _list_chrome_window_hwnds() -> set[int]:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        windows: set[int] = set()

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def enum_callback(hwnd, _lparam):
            try:
                hwnd = int(hwnd)
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    return True

                process_id = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                if not process_id.value:
                    return True

                handle = kernel32.OpenProcess(0x1000, False, process_id.value)
                if not handle:
                    return True

                try:
                    size = ctypes.c_ulong(32768)
                    buffer = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        executable = buffer.value.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
                        if executable == "chrome.exe":
                            windows.add(hwnd)
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                pass
            return True

        user32.EnumWindows(EnumWindowsProc(enum_callback), 0)
        return windows

    # =========================================================
    # APPLICATION / FILE LAUNCH & FOCUS
    # =========================================================

    @staticmethod
    def _find_file_window(path: Path) -> int | None:
        """Find a visible Windows window whose title identifies the opened file."""
        if os.name != "nt":
            return None
        try:
            user32 = ctypes.windll.user32
            needle_names = {path.name.casefold(), path.stem.casefold()}
            candidates: list[int] = []
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def callback(hwnd, _lparam):
                hwnd = int(hwnd)
                try:
                    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                        return True
                    length = user32.GetWindowTextLengthW(hwnd)
                    if not length:
                        return True
                    title = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, title, length + 1)
                    normalized = " ".join(title.value.casefold().split())
                    if any(name and name in normalized for name in needle_names):
                        candidates.append(hwnd)
                except Exception:
                    pass
                return True

            user32.EnumWindows(EnumWindowsProc(callback), 0)
            foreground = int(user32.GetForegroundWindow() or 0)
            if foreground in candidates:
                return foreground
            return candidates[0] if candidates else None
        except Exception:
            return None

    def _execute_open_folder(self, action: Action) -> ActionResult:
        path = str(action.parameters.get("path") or action.parameters.get("directory") or action.parameters.get("folder") or action.target or "").strip()
        if not path:
            return ActionResult(action=action.action, success=False, error="Folder path was not specified.")
        target = Path(path).expanduser()
        if not target.is_absolute():
            try:
                from advi.capabilities.files.executor import FileCapability
                target = FileCapability().resolve_path(path)
            except Exception:
                target = Path.cwd() / target
        if not target.exists() or not target.is_dir():
            return ActionResult(action=action.action, success=False, error=f"Folder not found: {target}")
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                opener = shutil.which("xdg-open") or shutil.which("open")
                if not opener:
                    return ActionResult(action=action.action, success=False, error="No operating-system folder opener is available.")
                subprocess.Popen([opener, str(target)], shell=False)
            if os.name != "nt":
                return ActionResult(action=action.action, success=True, data=str(target))
            # Explorer commonly titles a folder by its final component.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                hwnd = self._find_file_window(target)
                if hwnd:
                    self._focus_hwnd(hwnd)
                    return ActionResult(action=action.action, success=True, data={"path":str(target),"hwnd":hwnd})
                time.sleep(0.25)
            return ActionResult(action=action.action, success=False, data=str(target), error=f"The operating system accepted the folder open request, but I could not verify an Explorer window for {target.name}.")
        except Exception as exc:
            return ActionResult(action=action.action, success=False, error=str(exc))

    def _execute_open_file(self, action: Action) -> ActionResult:
        path = str(
            action.parameters.get("path")
            or action.parameters.get("filename")
            or action.target
            or ""
        ).strip()
        if not path:
            return ActionResult(action=action.action, success=False, error="File path was not specified.")

        target = Path(path).expanduser()
        if not target.is_absolute():
            # David fallback commonly receives Desktop/Downloads/Documents
            # friendly paths. Reuse ADVI's canonical Windows path resolver.
            try:
                from advi.capabilities.files.executor import FileCapability
                target = FileCapability().resolve_path(path)
            except Exception:
                target = Path.cwd() / target

        if not target.exists():
            # Fallback requests often contain a friendly filename such as
            # "open verse.pdf" rather than a full path. Resolve it locally before
            # giving up; this keeps David useful without requiring the GUI to guess
            # which folder the user meant.
            try:
                from advi.capabilities.files.executor import FileCapability
                resolver = FileCapability()
                found = resolver._execute_find_file(
                    ToolCall(
                        call_id=f"{getattr(action, 'action', 'open_file')}-resolve",
                        name="find_file",
                        arguments={"name": target.name, "scope": "computer", "limit": 5},
                    )
                )
                candidates = found.output if isinstance(found.output, list) else []
                exact = [Path(str(item)) for item in candidates if Path(str(item)).name.casefold() == target.name.casefold()]
                if len(exact) == 1:
                    target = exact[0]
            except Exception:
                logger.debug("David file-name resolution failed", exc_info=True)
        if not target.exists():
            return ActionResult(action=action.action, success=False, error=f"File not found: {target}")
        if not target.is_file():
            return ActionResult(action=action.action, success=False, error=f"Not a file: {target}")

        try:
            if hasattr(os, "startfile"):
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                opener = shutil.which("xdg-open") or shutil.which("open")
                if not opener:
                    return ActionResult(action=action.action, success=False, error="No operating-system file opener is available.")
                subprocess.Popen([opener, str(target)], shell=False)

            if os.name != "nt":
                return ActionResult(action=action.action, success=True, data=str(target))

            deadline = time.monotonic() + 5.0
            hwnd = None
            while time.monotonic() < deadline:
                hwnd = self._find_file_window(target)
                if hwnd:
                    self._focus_hwnd(hwnd)
                    return ActionResult(action=action.action, success=True, data={"path": str(target), "hwnd": hwnd})
                time.sleep(0.25)

            return ActionResult(
                action=action.action,
                success=False,
                data=str(target),
                error=f"The operating system accepted the request, but I could not verify an open window for {target.name}.",
            )
        except Exception as exc:
            return ActionResult(action=action.action, success=False, error=str(exc))

    def _execute_open_application(self, action: Action) -> ActionResult:
        application = action.parameters.get("application")
        if not application:
            return ActionResult(
                action=action.action,
                success=False,
                error="Application was not specified.",
            )

        app_name = str(application).strip()

        # -----------------------------------------------------
        # Chrome Application Launch
        # -----------------------------------------------------
        if app_name.lower() in {"chrome", "google chrome"}:
            possible_paths = [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]

            chrome_path = next((p for p in possible_paths if p.exists()), None)
            if chrome_path is None:
                return ActionResult(
                    action=action.action,
                    success=False,
                    error="Chrome executable was not found in standard installation paths.",
                )

            try:
                # Check if target tab is already open and valid
                if self.context.is_chrome_target() and self._verify_chrome_tab_context():
                    logger.info("Chrome target tab %s already established.", self.context.target_tab_id)
                    return ActionResult(action=action.action, success=True, data=str(chrome_path))

                target_tab = None
                before_tabs = self._list_chrome_tabs()
                if before_tabs:
                    # Chrome is already running with CDP: create dedicated target tab
                    target_tab = self.cdp_browser.create_new_tab("about:blank")

                if target_tab is None:
                    before_ids = {str(tab.get("id")) for tab in before_tabs}
                    before_window_hwnds = self._list_chrome_window_hwnds()

                    profile_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "ADVI" / "ChromeProfile"
                    profile_dir.mkdir(parents=True, exist_ok=True)

                    chrome_process = subprocess.Popen([
                        str(chrome_path),
                        f"--remote-debugging-port={self._chrome_cdp_port}",
                        f"--user-data-dir={profile_dir}",
                        "--remote-allow-origins=http://127.0.0.1:9222",
                        "--new-window",
                        "about:blank",
                    ])
                    self.context.chrome_process_id = chrome_process.pid

                    # Chrome startup and its CDP endpoint are asynchronous. A fixed
                    # sleep caused intermittent false failures on slower Windows
                    # machines. Poll the endpoint for a bounded interval instead.
                    for _ in range(40):
                        tabs = self._list_chrome_tabs()
                        target_tab = next(
                            (t for t in tabs if str(t.get("id")) not in before_ids),
                            None,
                        )
                        if target_tab is not None:
                            break
                        time.sleep(0.25)

                    for _ in range(40):
                        new_windows = self._list_chrome_window_hwnds() - before_window_hwnds
                        if new_windows:
                            self.context.set_chrome_window(next(iter(new_windows)))
                            break
                        time.sleep(0.25)

                    # Prefer an explicitly created target when CDP is available.
                    # If /json/new is unavailable, a tab that appeared only after
                    # our managed process launch is still attributable to ADVI's
                    # dedicated profile and is therefore safe to bind.
                    dedicated = self.cdp_browser.create_new_tab("about:blank")
                    if dedicated and dedicated.get("id"):
                        target_tab = dedicated

                if target_tab is None:
                    return ActionResult(
                        action=action.action,
                        success=False,
                        error="Chrome launched but target tab ID could not be captured.",
                    )

                self.context.set_target_tab(
                    tab_id=str(target_tab["id"]),
                    url=str(target_tab.get("url", "")),
                    title=str(target_tab.get("title", "")),
                )

                if not self.context.chrome_window_hwnd:
                    hwnds = self._list_chrome_window_hwnds()
                    if hwnds:
                        self.context.set_chrome_window(next(iter(hwnds)))

                if self.context.chrome_window_hwnd:
                    self._focus_hwnd(self.context.chrome_window_hwnd)

                logger.info("Opened Chrome and bound target_tab_id=%s HWND=%s", self.context.target_tab_id, self.context.chrome_window_hwnd)
                return ActionResult(action=action.action, success=True, data=str(chrome_path))

            except Exception as exc:
                logger.exception("Failed to open Chrome application.")
                return ActionResult(action=action.action, success=False, error=str(exc))

        # -----------------------------------------------------
        # Desktop Application Launch (safe executable/shortcut resolution)
        # -----------------------------------------------------
        try:
            watchtower = EnvironmentWatchtower(inventory_enabled=True, full_filesystem_inventory=False)
            watchtower.refresh_applications()
            record = watchtower.find_application(app_name)
            launch_target = resolve_launch_target(app_name, record)
            if launch_target is None:
                return ActionResult(
                    action=action.action,
                    success=False,
                    error=(
                        f"Application '{app_name}' is not in the trusted application catalog "
                        "or installed-application inventory."
                    ),
                )

            process, _launched_via = launch_application(launch_target)
            time.sleep(1.0)

            hwnd = None
            for _ in range(12):
                hwnd = self._find_application_window(app_name)
                if hwnd:
                    break
                time.sleep(0.25)

            if hwnd:
                self.context.set_application(app_name, hwnd=hwnd)
                self._focus_hwnd(hwnd)
            else:
                self.context.set_application(app_name)

            return ActionResult(action=action.action, success=True, data=app_name)
        except Exception as exc:
            logger.exception("Failed to launch application %s", app_name)
            return ActionResult(action=action.action, success=False, error=str(exc))

    @staticmethod
    def _normalize_application_name(application_name: str) -> str:
        return application_name.strip().lower().replace(".exe", "")

    @classmethod
    def _application_matches_window(cls, application_name: str, hwnd: int) -> bool:
        requested = cls._normalize_application_name(application_name)
        aliases = {
            "chrome": {"chrome"},
            "google chrome": {"chrome"},
            "notepad": {"notepad"},
            "edge": {"msedge"},
            "firefox": {"firefox"},
        }
        process_names = aliases.get(requested, {requested})
        user32 = ctypes.windll.user32

        try:
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value:
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, process_id.value)
                if handle:
                    try:
                        buf = ctypes.create_unicode_buffer(32768)
                        size = ctypes.c_ulong(32768)
                        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                            exe = buf.value.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower().replace(".exe", "")
                            if exe in process_names:
                                return True
                    finally:
                        kernel32.CloseHandle(handle)
        except Exception:
            pass

        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                t_lower = title.value.lower()
                if requested in t_lower or any(p in t_lower for p in process_names):
                    return True
        except Exception:
            pass

        return False

    @classmethod
    def _find_application_window(cls, application_name: str) -> int | None:
        user32 = ctypes.windll.user32
        candidates: list[int] = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def enum_callback(hwnd, _lparam):
            try:
                hwnd = int(hwnd)
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    return True
                if cls._application_matches_window(application_name, hwnd):
                    candidates.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(EnumWindowsProc(enum_callback), 0)
        if not candidates:
            return None

        fg = int(user32.GetForegroundWindow() or 0)
        return fg if fg in candidates else candidates[0]

    @staticmethod
    def _focus_hwnd(hwnd: int) -> bool:
        user32 = ctypes.windll.user32
        if not hwnd or not user32.IsWindow(hwnd):
            return False

        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)

            curr_fg = user32.GetForegroundWindow()
            curr_thread = ctypes.windll.kernel32.GetCurrentThreadId()
            fg_thread = user32.GetWindowThreadProcessId(curr_fg, None) if curr_fg else 0

            attached = fg_thread and fg_thread != curr_thread
            if attached:
                user32.AttachThreadInput(fg_thread, curr_thread, True)

            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(fg_thread, curr_thread, False)

            time.sleep(0.15)
            return int(user32.GetForegroundWindow() or 0) == int(hwnd)
        except Exception as exc:
            logger.debug("Focus HWND %s failed: %s", hwnd, exc)
            return False

    def ensure_application_focus(self, application_name: str | None) -> bool:
        """
        Separate logical target ownership from OS focus.

        For Chrome: Activates target_tab_id via CDP and attempts window focus.
        For Desktop Apps: Finds target HWND and attempts window focus.
        """
        if not application_name:
            return True

        app_lower = application_name.strip().lower()
        if app_lower in {"chrome", "google chrome"}:
            if self.context.target_tab_id:
                self.cdp_browser.activate_tab(self.context.target_tab_id)
            if self.context.chrome_window_hwnd:
                self._focus_hwnd(self.context.chrome_window_hwnd)
            return True

        hwnd = self.context.target_hwnd or self._find_application_window(application_name)
        if hwnd:
            self.context.target_hwnd = hwnd
            self._focus_hwnd(hwnd)
            return True

        logger.warning("No window handle found for application %r", application_name)
        return False

    # =========================================================
    # NAVIGATION
    # =========================================================

    def _execute_navigate(self, action: Action) -> ActionResult:
        url = action.parameters.get("url")
        if not url:
            return ActionResult(action=action.action, success=False, error="URL was not specified.")

        url_str = str(url).strip()

        # If Chrome context is active, navigate directly on target_tab_id via CDP
        if self.context.is_chrome_target() and self.context.target_tab_id:
            success = self.cdp_browser.navigate(self.context.target_tab_id, url_str)
            if success:
                self.context.target_tab_url = url_str
                return ActionResult(action=action.action, success=True, data=url_str)
            return ActionResult(action=action.action, success=False, error=f"CDP navigation to {url_str} failed.")

        # Fallback to physical typing into address bar
        if self.context.target_hwnd:
            self._focus_hwnd(self.context.target_hwnd)

        _get_pyautogui().hotkey("ctrl", "l")
        time.sleep(0.2)
        _get_pyautogui().write(url_str, interval=0.02)
        time.sleep(0.2)
        _get_pyautogui().press("enter")
        time.sleep(2.0)

        return ActionResult(action=action.action, success=True, data=url_str)

    # =========================================================
    # SEARCH (CRITICAL REQUIREMENT #2: IN-SITE SEARCH)
    # =========================================================

    def _execute_search(self, action: Action) -> ActionResult:
        query = action.parameters.get("query")
        if not query:
            return ActionResult(action=action.action, success=False, error="Search query was not specified.")

        query_str = str(query).strip()
        target_param = str(action.parameters.get("target", "")).lower()

        # Check if explicitly requested browser address bar search
        is_explicit_address_bar = "address bar" in target_param or "addressbar" in target_param

        if self.context.is_chrome_target() and self.context.target_tab_id and not is_explicit_address_bar:
            # NON-NEGOTIABLE REQUIREMENT: Search INSIDE target website's own search field
            logger.info("Executing IN-SITE website search on tab %s for query %r", self.context.target_tab_id, query_str)
            success = self.cdp_browser.execute_in_site_search(self.context.target_tab_id, query_str)
            if success:
                return ActionResult(action=action.action, success=True, data=query_str)
            logger.warning("CDP in-site search failed on tab %s. Falling back to physical input inside page.", self.context.target_tab_id)

        # Fallback: Address bar or physical interaction
        if self.context.target_hwnd:
            self._focus_hwnd(self.context.target_hwnd)

        if is_explicit_address_bar:
            _get_pyautogui().hotkey("ctrl", "l")
            time.sleep(0.2)
            _get_pyautogui().write(query_str, interval=0.02)
            time.sleep(0.2)
            _get_pyautogui().press("enter")
            time.sleep(2.0)
            return ActionResult(action=action.action, success=True, data=query_str)

        # Physical search fallback
        target = action.parameters.get("resolved_target")
        if self._is_coordinate_target(target):
            self._click_coordinate_target(target)
            time.sleep(0.2)
            _get_pyautogui().hotkey("ctrl", "a")
            _get_pyautogui().write(query_str, interval=0.02)
            _get_pyautogui().press("enter")
            time.sleep(1.5)
            return ActionResult(action=action.action, success=True, data=query_str)

        return ActionResult(action=action.action, success=False, error="In-site search could not locate website search field.")

    # =========================================================
    # MOUSE ACTIONS (CLICK, DOUBLE CLICK, RIGHT CLICK)
    # =========================================================

    def _execute_click(self, action: Action) -> ActionResult:
        target_param = str(action.parameters.get("target", "")).strip()

        # Web action on CDP target tab
        if self.context.is_chrome_target() and self.context.target_tab_id and target_param:
            success = self.cdp_browser.click_relevant_result(self.context.target_tab_id, target_param)
            if success:
                return ActionResult(action=action.action, success=True, data=target_param)

        target = action.parameters.get("resolved_target")
        if target is not None:
            if self._is_coordinate_target(target):
                self._click_coordinate_target(target)
            else:
                target.click_input()
            return ActionResult(action=action.action, success=True)

        x = action.parameters.get("x")
        y = action.parameters.get("y")
        if x is not None and y is not None:
            if self.context.target_hwnd:
                self._focus_hwnd(self.context.target_hwnd)
            _get_pyautogui().click(int(x), int(y))
            return ActionResult(action=action.action, success=True)

        return ActionResult(action=action.action, success=False, error="Click requires a resolved target or coordinates.")

    def _execute_double_click(self, action: Action) -> ActionResult:
        target = action.parameters.get("resolved_target")
        if target is not None:
            if self._is_coordinate_target(target):
                self._click_coordinate_target(target, double=True)
            else:
                target.double_click_input()
            return ActionResult(action=action.action, success=True)

        x, y = action.parameters.get("x"), action.parameters.get("y")
        if x is not None and y is not None:
            if self.context.target_hwnd:
                self._focus_hwnd(self.context.target_hwnd)
            _get_pyautogui().doubleClick(int(x), int(y))
            return ActionResult(action=action.action, success=True)

        return ActionResult(action=action.action, success=False, error="Double-click requires a resolved target or coordinates.")

    def _execute_right_click(self, action: Action) -> ActionResult:
        target = action.parameters.get("resolved_target")
        if target is not None:
            if self._is_coordinate_target(target):
                self._click_coordinate_target(target, right=True)
            else:
                target.right_click_input()
            return ActionResult(action=action.action, success=True)

        x, y = action.parameters.get("x"), action.parameters.get("y")
        if x is not None and y is not None:
            if self.context.target_hwnd:
                self._focus_hwnd(self.context.target_hwnd)
            _get_pyautogui().rightClick(int(x), int(y))
            return ActionResult(action=action.action, success=True)

        return ActionResult(action=action.action, success=False, error="Right-click requires a resolved target or coordinates.")

    # =========================================================
    # KEYBOARD ACTIONS (TYPE TEXT, PRESS KEY, HOTKEY)
    # =========================================================

    def _execute_submit(self, action: Action) -> ActionResult:
        """Submit a targeted UI control without inventing a website-specific selector."""
        target_param = str(action.parameters.get("target", "")).strip()
        if not target_param:
            return ActionResult(action=action.action, success=False, error="Submit requires a semantic target.")

        if self.context.is_chrome_target() and self.context.target_tab_id:
            success = self.cdp_browser.click_relevant_result(
                self.context.target_tab_id,
                target_param,
            )
            if success:
                return ActionResult(action=action.action, success=True, data=target_param)
            return ActionResult(
                action=action.action,
                success=False,
                error=f"Could not submit target '{target_param}' in the owned Chrome tab.",
            )

        target = action.parameters.get("resolved_target")
        if target is not None:
            if self._is_coordinate_target(target):
                self._click_coordinate_target(target)
            else:
                target.click_input()
            return ActionResult(action=action.action, success=True, data=target_param)

        return ActionResult(
            action=action.action,
            success=False,
            error=f"Submit target '{target_param}' could not be resolved.",
        )

    def _execute_type_text(self, action: Action) -> ActionResult:
        value = action.parameters.get("value")
        if value is None:
            return ActionResult(action=action.action, success=False, error="Text value was not specified.")

        val_str = str(value)
        target = action.parameters.get("resolved_target")

        # Guarantee focus to target application before physical typing
        if self.context.target_hwnd:
            self._focus_hwnd(self.context.target_hwnd)

        if target is not None:
            if self._is_coordinate_target(target):
                self._click_coordinate_target(target)
                time.sleep(0.2)
                _get_pyautogui().write(val_str, interval=0.02)
                return ActionResult(action=action.action, success=True, data=val_str)
            else:
                try:
                    target.set_focus()
                    target.type_keys(val_str, with_spaces=True)
                    return ActionResult(action=action.action, success=True, data=val_str)
                except Exception:
                    pass

        _get_pyautogui().write(val_str, interval=0.02)
        return ActionResult(action=action.action, success=True, data=val_str)

    def _execute_press_key(self, action: Action) -> ActionResult:
        key = action.parameters.get("key")
        if not key:
            return ActionResult(action=action.action, success=False, error="Key was not specified.")

        key_name = str(key).lower().strip()

        # Key normalization mapping
        key_map = {
            "enter": "enter",
            "return": "enter",
            "esc": "escape",
            "escape": "escape",
            "tab": "tab",
            "backspace": "backspace",
            "delete": "delete",
            "space": "space",
            "up": "up",
            "down": "down",
            "left": "left",
            "right": "right",
        }
        normalized_key = key_map.get(key_name, key_name)

        if self.context.target_hwnd:
            self._focus_hwnd(self.context.target_hwnd)

        target = action.parameters.get("resolved_target")
        if target is not None:
            if self._is_coordinate_target(target):
                self._click_coordinate_target(target)
                time.sleep(0.15)

        _get_pyautogui().press(normalized_key)
        return ActionResult(action=action.action, success=True, data=normalized_key)

    def _execute_hotkey(self, action: Action) -> ActionResult:
        keys = action.parameters.get("keys")
        if not keys or not isinstance(keys, list):
            return ActionResult(action=action.action, success=False, error="Hotkey keys must be a list.")

        # Key normalization mapping
        key_map = {
            "ctrl": "ctrl",
            "control": "ctrl",
            "alt": "alt",
            "shift": "shift",
            "win": "win",
            "super": "win",
            "cmd": "win",
        }
        normalized_keys = [
            key_map.get(str(k).lower().strip(), str(k).lower().strip())
            for k in keys
        ]

        if self.context.target_hwnd:
            self._focus_hwnd(self.context.target_hwnd)

        target = action.parameters.get("resolved_target")
        if target is not None and self._is_coordinate_target(target):
            self._click_coordinate_target(target)
            time.sleep(0.15)

        _get_pyautogui().hotkey(*normalized_keys)
        return ActionResult(action=action.action, success=True, data=normalized_keys)

    # =========================================================
    # OTHER ACTIONS
    # =========================================================

    def _execute_focus_window(self, action: Action) -> ActionResult:
        if self.context.target_hwnd:
            success = self._focus_hwnd(self.context.target_hwnd)
            return ActionResult(action=action.action, success=success, data=self.context.target_hwnd)
        return ActionResult(action=action.action, success=True)

    def _execute_close_window(self, action: Action) -> ActionResult:
        hwnd = self.context.target_hwnd
        if hwnd:
            self._focus_hwnd(hwnd)
        _get_pyautogui().hotkey("alt", "f4")
        if os.name != "nt" or not hwnd:
            return ActionResult(action=action.action, success=True, data="Window closed.")
        # David is a fallback, but it must still report real GUI state. Verify
        # that the targeted window disappeared before declaring success.
        user32 = getattr(ctypes, "windll", None).user32 if hasattr(ctypes, "windll") else None
        if user32 is None:
            return ActionResult(action=action.action, success=True, data="Window close requested.")
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            try:
                if not bool(user32.IsWindow(int(hwnd))) or not bool(user32.IsWindowVisible(int(hwnd))):
                    self.context.target_hwnd = None
                    return ActionResult(action=action.action, success=True, data="Window closed.")
            except Exception:
                break
            time.sleep(0.2)
        return ActionResult(action=action.action, success=False, error="The window did not close after the fallback close request.")

    def _execute_wait(self, action: Action) -> ActionResult:
        seconds = float(action.parameters.get("seconds", 1))
        time.sleep(seconds)
        return ActionResult(action=action.action, success=True)

    def _execute_scroll(self, action: Action) -> ActionResult:
        amount = int(action.parameters.get("amount", 0))
        _get_pyautogui().scroll(amount)
        return ActionResult(action=action.action, success=True)

    def _execute_finish(self, action: Action) -> ActionResult:
        return ActionResult(action=action.action, success=True, data="Task completed.")

    # =========================================================
    # VISUAL COORDINATE HELPERS
    # =========================================================

    @staticmethod
    def _is_coordinate_target(target: Any) -> bool:
        return isinstance(target, dict) and "x" in target and "y" in target

    @staticmethod
    def _click_coordinate_target(target: dict[str, Any], double: bool = False, right: bool = False) -> None:
        x, y = int(target["x"]), int(target["y"])
        logger.info("Clicking coordinate target at (%s, %s)", x, y)
        if double:
            _get_pyautogui().doubleClick(x, y)
        elif right:
            _get_pyautogui().rightClick(x, y)
        else:
            _get_pyautogui().click(x, y)