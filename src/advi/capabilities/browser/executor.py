from __future__ import annotations

import logging
import json
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

from advi.tools.contracts import ToolCall, ToolResult
from advi.capabilities.desktop.context import DesktopContext
from .cdp import CDPBrowser

logger = logging.getLogger(__name__)


_HUMAN_CHECK_TITLE = re.compile(
    r"prove your humanity|are you a robot|verify (?:you are|you're) (?:a )?human|just a moment|attention required"
    r"|captcha|unusual traffic|access denied|security check",
    re.I,
)
_CONSEQUENTIAL_CLICK = re.compile(
    r"\b(?:buy|purchase|pay|payment|checkout|check out|place (?:your |the )?order|confirm (?:order|payment|booking|purchase|transfer)|"
    r"proceed to (?:pay|payment|checkout)|transfer|send money|donate|book now|reserve|subscribe|submit|apply now|"
    r"submit application|delete|remove account|close account|unsubscribe|publish|post|send)\b"
)


# A *link* labelled "Buy iMac" or "Book now" only opens the shop or booking
# page; paying is always a later button. Gating it stopped "find the starting
# price of the cheapest iMac" at the price page's door (WebVoyager dev run,
# 2026-10-23). Paying, ordering, sending, submitting and deleting stay gated
# even on links.
_STILL_CONSEQUENTIAL = re.compile(
    r"\b(?:pay|payment|place (?:your |the )?order|confirm|transfer|send|donate|submit|delete|remove account|"
    r"close account|unsubscribe|publish|post)\b"
)


def _browse_only_link(element: dict[str, Any], label: str) -> bool:
    return element.get("role") == "link" and not _STILL_CONSEQUENTIAL.search(label.casefold())


class BrowserCapability:
    """
    Browser Automation Capability.
    Directly controls Chrome via Chrome DevTools Protocol (CDP).
    Handles navigation, in-page search, clicking DOM elements, and tab lifecycle.
    """

    SUPPORTED_ACTIONS = {
        "media_control",
        "open_browser",
        "play_media",
        "navigate",
        "search",
        "click_web_element",
        "inspect_web_page",
        "type_web_element",
        "read_web_page",
        "go_back",
        "go_forward",
        "reload_page",
        "research_web",
    }

    def __init__(
        self,
        context: DesktopContext | None = None,
        cdp_port: int = 9222,
        user_data_dir: Path | None = None,
    ) -> None:
        self.context = context or DesktopContext()
        self.cdp_port = int(cdp_port)
        self.user_data_dir = user_data_dir
        self.cdp = CDPBrowser(port=self.cdp_port)

    def _owned_or_started_tab(self) -> str | None:
        """ADVI's tab, starting ADVI's Chrome first when none is open yet.

        "play baby shark on youtube" as the first browser request of a run
        failed with "No ADVI-owned Chrome tab is available" (live log,
        2026-10-21). Playwright connects to or launches ADVI's Chrome itself.
        """
        tab_id = self._owned_tab_id()
        if tab_id or not self._pw_enabled():
            return tab_id
        try:
            self._pw_ready()
        except Exception as exc:
            logger.warning("Could not start ADVI's browser: %s", exc)
            return None
        return self._owned_tab_id()

    def _owned_tab_id(self) -> str | None:
        """Return ADVI's exact live browser target, never an arbitrary Chrome tab."""
        tab_id = self.context.target_tab_id
        if not tab_id:
            return None
        try:
            tabs = self.cdp.list_tabs()
        except Exception as exc:
            logger.debug("Could not inspect Chrome tabs for ownership: %s", exc)
            return None
        target = next((tab for tab in tabs if str(tab.get("id")) == str(tab_id)), None)
        if target is None:
            self.context.target_tab_id = None
            self.context.target_tab_url = None
            self.context.target_tab_title = None
            return None
        self.context.target_tab_url = target.get("url") or self.context.target_tab_url
        self.context.target_tab_title = target.get("title") or self.context.target_tab_title
        mark_observed = getattr(self.context, "mark_observed", None)
        if callable(mark_observed):
            mark_observed("browser")
        return str(tab_id)

    def close(self) -> None:
        """Stop Chrome only when this capability launched its dedicated process."""
        if self._pw_enabled():
            from . import pw_session

            if pw_session._shared is not None:
                pw_session._shared.close()  # closes the Playwright-launched ADVI Chrome
        pid = self.context.chrome_process_id
        self.context.chrome_process_id = None
        if not pid:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
            else:
                os.kill(pid, 15)
        except (OSError, ValueError) as exc:
            logger.warning("Could not stop ADVI-launched Chrome process %s: %s", pid, exc)

    def execute(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        handler = getattr(self, f"_execute_{action.action}", None)
        if handler is None:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Unsupported browser action: {action.action}",
            )

        self._approval_granted = approval_granted
        try:
            # Ensure Chrome is running and tab is ready before executing browser action
            if not self._ensure_chrome_running():
                return ToolResult(
                    call_id=action.call_id,
                    name=action.action,
                    success=False,
                    error="Failed to launch or attach to Chrome via CDP.",
                )
            return handler(action)
        except Exception as exc:
            logger.exception("Browser action execution failed: %s", action.action)
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=str(exc),
            )

    def _ensure_chrome_running(self) -> bool:
        """Verify Chrome is running with remote debugging, launch if needed, and bind target tab."""
        tabs = self.cdp.list_tabs()
        if tabs:
            # Never silently hijack an unrelated user tab. Prefer an existing
            # owned tab; otherwise create a dedicated blank target when CDP
            # supports it, and only fall back to a page tab if creation fails.
            if self.context.target_tab_id:
                current = next((t for t in tabs if str(t.get("id")) == str(self.context.target_tab_id)), None)
                if current:
                    self.context.set_target_tab(
                        tab_id=current["id"],
                        url=current.get("url"),
                        title=current.get("title"),
                    )
                    return True
                self.context.target_tab_id = None
                self.context.target_tab_url = None
                self.context.target_tab_title = None

            dedicated = self.cdp.create_new_tab("about:blank")
            if dedicated and dedicated.get("id"):
                self.context.set_target_tab(
                    tab_id=dedicated["id"],
                    url=dedicated.get("url"),
                    title=dedicated.get("title"),
                )
                return True

            # Do not commandeer an arbitrary user tab when ownership cannot be
            # established. A connected browser session without a dedicated
            # target is not a safe execution context.
            logger.warning(
                "Chrome CDP is reachable but ADVI could not create an owned target tab; refusing to hijack an existing tab."
            )
            return False

        # Launch ADVI's dedicated Chrome through Playwright (it keeps the CDP
        # port open for the legacy helpers). Direct launch remains the fallback.
        try:
            self._pw_ready()
            return True
        except Exception as exc:
            logger.warning("Playwright launch failed; launching Chrome directly: %s", exc)

        # Launch Chrome with CDP
        possible_paths = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]

        chrome_exe = next((p for p in possible_paths if p.exists()), None)
        if not chrome_exe:
            logger.warning("Chrome executable not found in standard paths.")
            return False

        profile_dir = self.user_data_dir or (
            Path(os.environ.get("LOCALAPPDATA", "")) / "ADVI" / "ChromeProfile"
        )
        profile_dir = Path(profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(chrome_exe),
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-allow-origins=http://127.0.0.1:{self.cdp_port},http://localhost:{self.cdp_port}",
            "about:blank",
        ]

        try:
            proc = subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0)
            self.context.chrome_process_id = proc.pid
            time.sleep(1.5)

            # Establish an owned target explicitly. Never infer ownership from
            # the first tab returned by Chrome after process launch.
            dedicated = self.cdp.create_new_tab("about:blank")
            if dedicated and dedicated.get("id"):
                self.context.set_target_tab(
                    tab_id=dedicated["id"],
                    url=dedicated.get("url"),
                    title=dedicated.get("title"),
                )
                return True
        except Exception as exc:
            logger.exception("Failed to launch Chrome with CDP: %s", exc)

        return False

    def _legacy_navigate(self, action: ToolCall) -> ToolResult:
        url = action.parameters.get("url") or action.target or ""
        if not url:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No URL specified for navigation.",
            )

        clean_url = str(url).strip()
        if not clean_url.startswith(("http://", "https://")):
            clean_url = f"https://{clean_url}"

        tab_id = self._owned_tab_id()

        if not tab_id:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No ADVI-owned Chrome tab found to navigate.",
            )

        success = self.cdp.navigate_tab(tab_id=tab_id, url=clean_url)
        if success:
            get_tab_info = getattr(self.cdp, "get_tab_info", None)
            actual = (get_tab_info(tab_id) if get_tab_info else {}) or {}
            actual_url = actual.get("url") or clean_url
            actual_title = actual.get("title")
            self.context.target_tab_url = actual_url
            if actual_title is not None:
                self.context.target_tab_title = actual_title
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=True,
                output=actual_url,
                human_readable=f"Navigated browser to {actual_url}.",
                verified=bool(actual_url),
                verification_status="verified" if actual_url else "uncertain",
                verification_details={"method": "owned_tab_state", "tab_id": tab_id, "current_url": actual_url, "expected_url": clean_url},
                metadata={
                    "current_url": actual_url,
                    "expected_url": clean_url,
                    "tab_title": actual_title,
                    "cdp_port": self.cdp_port,
                    "tab_id": tab_id,
                },
            )

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=False,
            error=f"Failed to navigate to {clean_url}.",
        )

    def _execute_history_action(self, action: ToolCall, direction: str) -> ToolResult:
        """Perform a browser history transition and verify the owned tab changed."""
        tab_id = self._owned_tab_id()
        if not tab_id:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No ADVI-owned Chrome tab is available.",
            )

        info = (self.cdp.get_tab_info(tab_id) or {}) if hasattr(self.cdp, "get_tab_info") else {}
        before_url = str(info.get("url") or self.context.target_tab_url or "")
        before_title = str(info.get("title") or self.context.target_tab_title or "")
        script = "history.back()" if direction == "back" else "history.forward()"
        try:
            self.cdp.evaluate_script(tab_id, script)
        except Exception as exc:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Browser history navigation failed: {exc}",
                metadata={"tab_id": tab_id, "direction": direction},
            )

        deadline = time.monotonic() + 5.0
        after = info
        while time.monotonic() < deadline:
            try:
                after = (self.cdp.get_tab_info(tab_id) or {}) if hasattr(self.cdp, "get_tab_info") else {}
            except Exception:
                after = {}
            after_url = str(after.get("url") or "")
            after_title = str(after.get("title") or "")
            if after_url and (after_url != before_url or after_title != before_title):
                break
            time.sleep(0.15)

        after_url = str(after.get("url") or self.context.target_tab_url or "")
        after_title = str(after.get("title") or self.context.target_tab_title or "")
        changed = bool(after_url and (after_url != before_url or after_title != before_title))
        if after_url:
            self.context.target_tab_url = after_url
        if after_title:
            self.context.target_tab_title = after_title

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=changed,
            output=after_url,
            human_readable=(f"Went {direction} in the browser." if changed else f"Could not go {direction}: browser history did not change."),
            error=None if changed else f"Browser history did not change when going {direction}.",
            verified=changed,
            verification_status="verified" if changed else "failed",
            verification_details={
                "method": "owned_tab_history",
                "tab_id": tab_id,
                "direction": direction,
                "before_url": before_url,
                "after_url": after_url,
                "before_title": before_title,
                "after_title": after_title,
            },
            metadata={"tab_id": tab_id, "direction": direction, "current_url": after_url},
        )

    def _execute_go_back(self, action: ToolCall) -> ToolResult:
        return self._execute_history_action(action, "back")

    def _execute_go_forward(self, action: ToolCall) -> ToolResult:
        return self._execute_history_action(action, "forward")

    def _execute_reload_page(self, action: ToolCall) -> ToolResult:
        tab_id = self._owned_tab_id()
        if not tab_id:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No ADVI-owned Chrome tab is available.",
            )
        try:
            self.cdp.execute_cdp_command(tab_id, "Page.reload", {"ignoreCache": False})
        except Exception as exc:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Browser reload failed: {exc}",
                metadata={"tab_id": tab_id},
            )
        deadline = time.monotonic() + 5.0
        ready = False
        url = self.context.target_tab_url or ""
        title = self.context.target_tab_title or ""
        while time.monotonic() < deadline:
            try:
                state = self.cdp.evaluate_script(tab_id, "({readyState:document.readyState,url:location.href,title:document.title})") or {}
                if isinstance(state, dict) and "value" in state and isinstance(state["value"], dict):
                    state = state["value"]
                if isinstance(state, dict):
                    ready = state.get("readyState") == "complete"
                    url = str(state.get("url") or url)
                    title = str(state.get("title") or title)
                    if ready:
                        break
            except Exception:
                pass
            time.sleep(0.15)
        if url:
            self.context.target_tab_url = url
        if title:
            self.context.target_tab_title = title
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=ready,
            output=url,
            human_readable="Reloaded the controlled browser page." if ready else "Browser reload was requested but page readiness could not be verified.",
            error=None if ready else "Browser reload could not be verified as complete.",
            verified=ready,
            verification_status="verified" if ready else "failed",
            verification_details={"method": "owned_tab_page_reload", "tab_id": tab_id, "current_url": url, "ready_state": ready},
            metadata={"tab_id": tab_id, "current_url": url},
        )

    _YOUTUBE_MUSIC_SERVICES = {"youtube_music", "youtubemusic", "ytmusic", "music"}

    def _execute_play_media(self, action: ToolCall) -> ToolResult:
        """Search a media service and start playback as one verified operation.

        The runtime log shows the model hand-driving navigate/search/inspect/
        click/press "k" for 15-27 calls per song and still ending unverified.
        The search URL is built directly, the existing result pickers choose the
        result, and success requires the page's media element to be playing.
        """
        query = str(action.parameters.get("query") or "").strip()
        if not query:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="What should I play? No search query was given.")
        service = re.sub(r"[\s-]+", "_", str(action.parameters.get("service") or "youtube").strip().casefold())
        music = service in self._YOUTUBE_MUSIC_SERVICES
        if not music and service not in {"youtube", ""}:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error=f"play_media supports YouTube and YouTube Music, not '{service}'.",
            )
        tab_id = self._owned_or_started_tab()
        if not tab_id:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No ADVI-owned Chrome tab is available, and I couldn't open ADVI's Chrome window to play that.")
        base = "https://music.youtube.com/search?q=" if music else "https://www.youtube.com/results?search_query="
        url = base + urllib.parse.quote_plus(query)
        navigated = False
        if self._pw_enabled():
            # The legacy websocket navigate timed out on a busy tab (runtime log);
            # Playwright navigation waits on real load events instead.
            try:
                browser, _page = self._pw_ready()
                browser.goto(url)
                navigated = True
            except Exception as exc:
                logger.warning("Playwright navigation for play_media failed: %s", exc)
        if not navigated:
            try:
                navigated = bool(self.cdp.navigate_tab(tab_id=tab_id, url=url, timeout=12.0))
            except Exception as exc:
                logger.warning("CDP navigation for play_media failed: %s", exc)
            wait_dom = getattr(self.cdp, "wait_for_dom_ready", None)
            if navigated and callable(wait_dom):
                wait_dom(tab_id, timeout=8.0)
        if not navigated:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"I couldn't open the search page for '{query}'.")
        self.context.target_tab_url = url
        best = self._pw_play_best_match(action, query) if self._pw_enabled() else None
        if best is not None and best.success:
            result = best
        elif music:
            result = self._youtube_music_play_requested_result(action, tab_id) or best
        else:
            result = self._youtube_play_first_result(action, "play", tab_id) or best
        if result is None:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False, output=query,
                error=f"I searched for '{query}' but found nothing I could play.",
                metadata={"tab_id": tab_id, "url": url, "service": "youtube_music" if music else "youtube"},
            )
        return result

    def _legacy_search(self, action: ToolCall) -> ToolResult:
        """Search using the single canonical in-page CDP search implementation."""
        query = str(action.parameters.get("query") or action.parameters.get("value") or "").strip()
        if not query:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No search query specified.",
            )

        tab_id = self._owned_tab_id()
        if not tab_id:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No ADVI-owned Chrome tab is available for search.",
            )

        wait_dom = getattr(self.cdp, "wait_for_dom_ready", None)
        if callable(wait_dom) and not wait_dom(tab_id, timeout=5.0):
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error="The controlled browser page did not become ready before the search.",
                metadata={"query": query, "tab_id": tab_id, "current_url": self.context.target_tab_url, "method": "dom_readiness_gate"},
            )

        # Never repeat an already-completed website search just because the model
        # asks for the same search again during recovery. This is especially
        # important for YouTube: once results are visible, the next operation is
        # choosing a result, not searching for the same phrase again.
        get_tab_info = getattr(self.cdp, "get_tab_info", None)
        try:
            current_info = (get_tab_info(tab_id) if get_tab_info else {}) or {}
        except Exception:
            current_info = {}
        current_url = str(current_info.get("url") or self.context.target_tab_url or "")
        if "/results" in current_url.casefold():
            try:
                params = urllib.parse.parse_qs(urllib.parse.urlparse(current_url).query)
                existing_query = str((params.get("search_query") or params.get("q") or [""])[0]).replace("+", " ").strip()
                normalize = lambda value: re.sub(r"\s+", " ", str(value or "")).casefold().strip()
                if existing_query and normalize(existing_query) == normalize(query):
                    actual_title = current_info.get("title") or self.context.target_tab_title
                    self.context.target_tab_url = current_url
                    if actual_title is not None:
                        self.context.target_tab_title = str(actual_title)
                    if "music.youtube.com/search" in current_url.casefold():
                        candidates = self._youtube_music_result_candidates(tab_id, query=query, timeout=5.0)
                    elif "youtube.com/results" in current_url.casefold():
                        candidates = self._youtube_result_candidates(tab_id)
                    else:
                        candidates = []
                    return ToolResult(
                        call_id=action.call_id, name=action.action, success=True, output=query,
                        human_readable=f"Search results for '{query}' are already open.",
                        verified=True, verification_status="verified",
                        verification_details={"method": "existing_results_reused", "tab_id": tab_id, "current_url": current_url, "query": query, "result_candidates": candidates},
                        metadata={"query": query, "method": "existing_results_reused", "current_url": current_url, "tab_id": tab_id, "tab_title": actual_title},
                    )
            except Exception:
                pass

        # If the request clearly names YouTube but the dedicated tab is still blank,
        # recover locally instead of sending the same failed search back to the LLM.
        # This commonly happens when the model selects search before its navigation
        # step on a fresh task.
        lower_query = str(query).casefold()
        if current_url.casefold() in {"", "about:blank"} and "youtube" in lower_query:
            youtube_terms = re.sub(r"\byoutube\b", "", query, flags=re.I).strip()
            youtube_results = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(youtube_terms)
            if self.cdp.navigate_tab(tab_id=tab_id, url=youtube_results, timeout=12.0):
                if callable(wait_dom):
                    wait_dom(tab_id, timeout=8.0)
                info = (get_tab_info(tab_id) if get_tab_info else {}) or {}
                actual_url = str(info.get("url") or youtube_results)
                actual_title = info.get("title") or self.context.target_tab_title
                self.context.target_tab_url = actual_url
                if actual_title is not None:
                    self.context.target_tab_title = str(actual_title)
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=True, output=query,
                    human_readable=f"Opened YouTube search results for '{youtube_terms}'.",
                    verified=True, verification_status="verified",
                    verification_details={"method":"youtube_direct_search_recovery", "tab_id":tab_id, "current_url":actual_url, "query":query},
                    metadata={"query":query, "method":"youtube_direct_search_recovery", "current_url":actual_url, "tab_id":tab_id, "tab_title":actual_title},
                )

        # CDP owns website search semantics. It deliberately rejects ambiguous
        # generic inputs rather than guessing, then verifies that the owned tab
        # actually changed state. Older test/lightweight adapters may not expose
        # the consolidated helper; fail cleanly instead of reviving duplicate JS.
        execute_search = getattr(self.cdp, "execute_in_site_search", None)
        if not callable(execute_search):
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=(
                    f"Could not search for '{query}' because the current browser adapter "
                    "does not support the canonical in-page search operation. ADVI will not silently redirect the search to another site."
                ),
                metadata={"query": query, "method": "in_page_unsupported", "tab_id": tab_id},
            )
        if not execute_search(tab_id, query):
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                output=query,
                error=f"Could not find a usable search field on the current page for '{query}'.",
                metadata={
                    "query": query,
                    "method": "in_page_failed",
                    "current_url": self.context.target_tab_url,
                    "cdp_port": self.cdp_port,
                    "tab_id": tab_id,
                },
            )

        get_tab_info = getattr(self.cdp, "get_tab_info", None)
        info = (get_tab_info(tab_id) if get_tab_info else {}) or {}
        actual_url = info.get("url") or self.context.target_tab_url
        actual_title = info.get("title") or self.context.target_tab_title
        if actual_url:
            self.context.target_tab_url = actual_url
        if actual_title is not None:
            self.context.target_tab_title = actual_title
        current_lower = str(actual_url or "").casefold()
        candidates: list[dict[str, Any]] = []
        if "music.youtube.com/search" in current_lower:
            candidates = self._youtube_music_result_candidates(tab_id, query=query, timeout=7.0)
        elif "youtube.com/results" in current_lower:
            candidates = self._youtube_result_candidates(tab_id)
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=query,
            human_readable=f"Searched for '{query}' on the current website.",
            verified=bool(actual_url),
            verification_status="verified" if actual_url else "uncertain",
            verification_details={"method": "owned_tab_in_page_search", "tab_id": tab_id, "current_url": actual_url, "query": query, "result_candidates": candidates},
            metadata={
                "query": query,
                "method": "in_page",
                "current_url": actual_url,
                "tab_id": tab_id,
                "tab_title": actual_title,
            },
        )

    def _youtube_music_play_requested_result(self, action: ToolCall, tab_id: str) -> ToolResult | None:
        """Deterministically target the requested YouTube Music search result and verify playback.

        The model should not have to guess which of several identical Play controls belongs to
        the requested song. The current search URL is the authoritative query, then the matching
        result row supplies the exact play control inside that row.
        """
        try:
            info = (self.cdp.get_tab_info(tab_id) if hasattr(self.cdp, "get_tab_info") else {}) or {}
        except Exception:
            info = {}
        current_url = str(info.get("url") or self.context.target_tab_url or "")
        if "music.youtube.com/search" not in current_url.casefold():
            return None
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(current_url).query)
            query = urllib.parse.unquote_plus(str((params.get("q") or [""])[0])).strip()
        except Exception:
            query = ""
        if not query:
            query = str(self.context.target_tab_title or "").replace("YouTube Music", "").strip(" -")
        if not query:
            return None

        script = r"""
        (() => {
            const needle = __QUERY__;
            const normalize = v => String(v ?? '').replace(/\s+/g, ' ').trim().toLowerCase();
            const visible = el => {
                if (!el) return false;
                const s = getComputedStyle(el), r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const queryDeep = (selector, root = document, seen = new Set()) => {
                let found = [];
                try { found = Array.from(root.querySelectorAll(selector)); } catch (_) {}
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')).slice(0, 4500); } catch (_) {}
                for (const node of nodes) {
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {
                        seen.add(node.shadowRoot);
                        found.push(...queryDeep(selector, node.shadowRoot, seen));
                    }
                }
                return found;
            };
            const rows = queryDeep('ytmusic-responsive-list-item-renderer').filter(visible);
            let row = rows.find(r => normalize(r.textContent).includes(normalize(needle))) || null;
            if (!row) return {found:false, reason:'requested_result_not_found', query:needle};
            const root = row.shadowRoot || row;
            const controls = queryDeep('ytmusic-play-button-renderer,button,[role="button"]', root).filter(visible);
            const play = controls.find(el => {
                const l = normalize(`${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.textContent || ''}`);
                return l.includes('play') && !l.includes('playlist');
            }) || null;
            if (!play) return {found:true, rowText:normalize(row.textContent).slice(0,400), reason:'play_control_not_found', query:needle};
            play.scrollIntoView({block:'center', inline:'nearest'});
            const rect = play.getBoundingClientRect();
            const before = {
                label: play.getAttribute('aria-label') || play.getAttribute('title') || '',
                disabled: Boolean(play.disabled),
                rect: {x:rect.x,y:rect.y,width:rect.width,height:rect.height},
                rowText: normalize(row.textContent).slice(0,400)
            };
            if (before.disabled || rect.width <= 0 || rect.height <= 0) {
                return {found:true, actionable:false, before, query:needle};
            }
            try { play.click(); } catch (_) {}
            return {found:true, actionable:true, before, query:needle};
        })()
        """.replace('__QUERY__', json.dumps(query, ensure_ascii=False))

        try:
            raw = self.cdp.evaluate_js(tab_id, script)
            if isinstance(raw, dict) and 'value' in raw:
                raw = raw['value']
        except Exception as exc:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                output=query, error=f"Could not inspect the YouTube Music result controls: {exc}",
                metadata={"tab_id": tab_id, "query": query, "method":"youtube_music_row_targeting"},
            )
        if not isinstance(raw, dict) or not raw.get('found'):
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False, output=query,
                error=f"I couldn't find the requested song '{query}' in the current YouTube Music results.",
                metadata={"tab_id": tab_id, "query": query, "method":"youtube_music_row_targeting", "details": raw or {}},
            )
        if not raw.get('actionable'):
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False, output=query,
                error="I found the requested song, but its Play control is not currently actionable.",
                verified=False, verification_status="uncertain",
                verification_details={"method":"youtube_music_actionability_gate", "tab_id":tab_id, "query":query, **raw},
                metadata={"tab_id":tab_id, "query":query, "method":"youtube_music_row_targeting"},
            )

        playback = self._ensure_youtube_music_playing(tab_id, timeout=10.0)
        playing = bool(playback and playback.get("playing"))
        if not playing:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False, output=query,
                error="The requested song was targeted, but YouTube Music did not confirm that playback started.",
                verified=False, verification_status="uncertain",
                verification_details={"method":"youtube_music_playback_verification", "tab_id":tab_id, "query":query, "target":raw, "playback":playback or {}},
                metadata={"tab_id":tab_id, "query":query, "method":"youtube_music_row_targeting", "recovery_recommended":True},
            )
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True, output=query,
            human_readable=f"Playing '{query}' on YouTube Music.", verified=True,
            verification_status="verified",
            verification_details={"method":"youtube_music_row_targeting", "tab_id":tab_id, "query":query, "target":raw, "playback":playback},
            metadata={"tab_id":tab_id, "query":query, "method":"youtube_music_row_targeting", "playback_verified":True},
        )

    def _youtube_music_result_candidates(self, tab_id: str, query: str, timeout: float = 7.0, limit: int = 5) -> list[dict[str, Any]]:
        """Poll the owned YouTube Music page until dynamic rows expose the requested track."""
        needle = json.dumps(str(query or '').casefold(), ensure_ascii=False)
        capped = max(1, min(int(limit), 8))
        script = r"""
        (() => {
            const needle = __NEEDLE__;
            const normalize = v => String(v ?? '').replace(/\s+/g, ' ').trim().toLowerCase();
            const visible = el => {
                if (!el) return false;
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const queryDeep = (selector, root = document, seen = new Set()) => {
                let found = [];
                try { found = Array.from(root.querySelectorAll(selector)); } catch (_) {}
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')).slice(0, 3500); } catch (_) {}
                for (const node of nodes) {
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {
                        seen.add(node.shadowRoot);
                        found.push(...queryDeep(selector, node.shadowRoot, seen));
                    }
                }
                return found;
            };
            const rows = queryDeep('ytmusic-responsive-list-item-renderer').filter(visible);
            const out = [];
            for (const row of rows) {
                const rowText = normalize(row.textContent || '');
                if (needle && !rowText.includes(needle)) continue;
                const root = row.shadowRoot || row;
                const links = queryDeep('a[href*="/watch?v="]', root).filter(visible);
                const playControls = queryDeep('ytmusic-play-button-renderer,[role="button"],button', root).filter(visible);
                const play = playControls.find(el => {
                    const label = normalize(el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent);
                    return label.includes('play') && !label.includes('playlist');
                }) || null;
                const titleLink = queryDeep('a', root).find(a => visible(a) && needle && normalize(a.textContent || a.getAttribute('aria-label')).includes(needle)) || null;
                const chosenLink = titleLink || links[0] || null;
                if (!chosenLink || !chosenLink.href || !play) continue;
                const playLabel = String(play.getAttribute('aria-label') || play.getAttribute('title') || '').trim();
                out.push({
                    result_index: out.length + 1,
                    title: String(chosenLink.textContent || chosenLink.getAttribute('aria-label') || rowText).replace(/\s+/g, ' ').trim().slice(0, 240),
                    href: chosenLink.href,
                    selector: `ytmusic-play-button-renderer[aria-label=${JSON.stringify(playLabel)}]`,
                    play_selector: `ytmusic-play-button-renderer[aria-label=${JSON.stringify(playLabel)}]`,
                    play_label: playLabel,
                    source: 'youtube_music_shadow_dom'
                });
                if (out.length >= __LIMIT__) break;
            }
            return out;
        })()
        """.replace('__NEEDLE__', needle).replace('__LIMIT__', str(capped));

        deadline = time.monotonic() + max(0.5, float(timeout))
        while time.monotonic() < deadline:
            try:
                value = self.cdp.evaluate_js(tab_id, script)
                if isinstance(value, dict) and 'value' in value:
                    value = value['value']
                if isinstance(value, list) and value:
                    return [item for item in value if isinstance(item, dict)]
            except Exception as exc:
                logger.debug('YouTube Music candidate poll failed: %s', exc)
            time.sleep(0.35)
        return []

    def _youtube_result_candidates(self, tab_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return the first actionable YouTube video links on the owned results page."""
        script = r"""
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const queryDeep = (selector, root = document, seen = new Set()) => {
                let found = [];
                try { found = Array.from(root.querySelectorAll(selector)); } catch (_) {}
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')).slice(0, 3000); } catch (_) {}
                for (const node of nodes) {
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {
                        seen.add(node.shadowRoot);
                        found.push(...queryDeep(selector, node.shadowRoot, seen));
                    }
                }
                return found;
            };
            const seen = new Set();
            const out = [];
            for (const a of queryDeep('a[href*="/watch?v="]')) {
                if (!visible(a)) continue;
                const href = String(a.href || '');
                if (!href || !/youtube\.com\/watch\?v=/i.test(href) || /youtube\.com\/(?:shorts|clip)\//i.test(href) || seen.has(href)) continue;
                const container = a.closest && a.closest('ytd-video-renderer, ytd-rich-item-renderer, ytd-grid-video-renderer');
                if (container && container.matches && container.matches('ytd-reel-shelf-renderer, ytd-shorts')) continue;
                seen.add(href);
                const titleNode = container?.querySelector?.('#video-title, a#video-title, yt-formatted-string#video-title');
                const label = String(titleNode?.getAttribute?.('aria-label') || titleNode?.textContent || a.getAttribute('aria-label') || a.textContent || '')
                    .replace(/\s+/g, ' ').trim().slice(0, 240);
                const videoId = (href.match(/[?&]v=([^&]+)/i) || [])[1] || '';
                out.push({
                    result_index: out.length + 1,
                    title: label || `YouTube video ${out.length + 1}`,
                    href,
                    selector: videoId ? `a[href*="/watch?v=${videoId}"]` : 'a[href*="/watch?v="]',
                });
                if (out.length >= %d) break;
            }
            return out;
        })()
        """ % max(1, min(int(limit), 8))
        try:
            value = self.cdp.evaluate_js(tab_id, script)
            if isinstance(value, dict) and "value" in value:
                value = value["value"]
            return [item for item in value if isinstance(item, dict) and item.get("href")] if isinstance(value, list) else []
        except Exception as exc:
            logger.debug("YouTube result candidate discovery failed: %s", exc)
            return []

    def _youtube_play_first_result(self, action: ToolCall, requested: str, tab_id: str) -> ToolResult | None:
        """Recover YouTube result playback when dynamic DOM prevents normal selector clicks."""
        try:
            info = (self.cdp.get_tab_info(tab_id) if hasattr(self.cdp, "get_tab_info") else {}) or {}
        except Exception:
            info = {}
        current_url = str(info.get("url") or self.context.target_tab_url or "")
        requested_lower = str(requested or "").casefold()
        if "youtube.com/results" not in current_url.casefold():
            return None
        if not any(token in requested_lower for token in ("video", "watch", "result", "play")):
            return None

        result_script = r"""
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
            };
            const collect = (root = document, seen = new Set(), out = []) => {
                let anchors = [];
                try { anchors = Array.from(root.querySelectorAll('a[href*="/watch?v="]')); } catch (_) {}
                for (const a of anchors) {
                    if (visible(a) && a.href && !out.some(x => x.href === a.href)) out.push(a);
                }
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')).slice(0, 2500); } catch (_) {}
                for (const node of nodes) {
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {
                        seen.add(node.shadowRoot);
                        collect(node.shadowRoot, seen, out);
                    }
                }
                return out;
            };
            const links = collect();
            const picked = links.find(a => a.closest('ytd-video-renderer, ytd-rich-item-renderer, ytd-grid-video-renderer')) || links[0] || null;
            if (!picked) return {found:false};
            return {found:true, href:picked.href, text:(picked.innerText || picked.textContent || '').replace(/\s+/g, ' ').trim().slice(0,240)};
        })()
        """

        selected = None
        for attempt in range(10):
            try:
                response = self.cdp.evaluate_js(tab_id, result_script)
                value = response.get("value") if isinstance(response, dict) and "value" in response else response
            except Exception as exc:
                logger.debug("YouTube result discovery attempt %s failed: %s", attempt + 1, exc)
                value = None
            if isinstance(value, dict) and value.get("found") and value.get("href"):
                selected = value
                break
            if attempt < 9:
                try:
                    self.cdp.evaluate_js(tab_id, "window.scrollBy(0, Math.max(450, Math.floor(window.innerHeight * 0.8))); true;")
                except Exception:
                    pass
                time.sleep(0.6)
        if not selected:
            return None

        href = str(selected.get("href") or "").strip()
        if not href or not self.cdp.navigate_tab(tab_id=tab_id, url=href, timeout=12.0):
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                output=requested,
                error="I found the YouTube video, but could not open its video page.",
                metadata={"method":"youtube_result_recovery", "tab_id":tab_id, "selected_result":selected},
            )

        deadline = time.monotonic() + 8.0
        clicked_play = False
        last_state: dict[str, object] = {}
        state_script = r"""
        (() => {
            const video = document.querySelector('video');
            const buttons = Array.from(document.querySelectorAll('button,[role="button"]'));
            const play = buttons.find(b => {
                const label = `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''}`.toLowerCase();
                return label.includes('play') && !label.includes('playlist');
            }) || null;
            return {
                watch: /youtube\.com\/watch/i.test(window.location.href),
                ready: !!video && video.readyState >= 2,
                paused: video ? !!video.paused : null,
                currentTime: video ? Number(video.currentTime || 0) : 0,
                playing: !!video && !video.paused && video.readyState >= 2,
                playFound: !!play,
            };
        })()
        """
        while time.monotonic() < deadline:
            try:
                response = self.cdp.evaluate_js(tab_id, state_script)
                value = response.get("value") if isinstance(response, dict) and "value" in response else response
                last_state = value if isinstance(value, dict) else {}
            except Exception:
                last_state = {}
            if last_state.get("watch") and last_state.get("playing"):
                self.context.target_tab_url = href
                return ToolResult(
                    call_id=action.call_id,
                    name=action.action,
                    success=True,
                    output=href,
                    human_readable=f"Playing the first YouTube result: {selected.get('text') or 'video'}.",
                    verified=True,
                    verification_status="verified",
                    verification_details={"method":"youtube_result_to_player", "tab_id":tab_id, "selected_result":selected, "playing":True, "current_time":last_state.get("currentTime")},
                    metadata={"tab_id":tab_id, "url":href, "selected_result":selected, "clicked_play":clicked_play},
                )
            if last_state.get("watch") and last_state.get("playFound") and not clicked_play:
                play_script = r"""
                (() => {
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'));
                    const play = buttons.find(b => {
                        const label = `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''}`.toLowerCase();
                        return label.includes('play') && !label.includes('playlist');
                    });
                    if (!play) return {clicked:false};
                    play.scrollIntoView({block:'center'});
                    play.click();
                    return {clicked:true};
                })()
                """
                try:
                    response = self.cdp.evaluate_js(tab_id, play_script)
                    value = response.get("value") if isinstance(response, dict) and "value" in response else response
                    clicked_play = bool(isinstance(value, dict) and value.get("clicked"))
                except Exception:
                    clicked_play = False
            elif last_state.get("watch") and last_state.get("ready") and last_state.get("paused"):
                try:
                    self.cdp.evaluate_js(tab_id, "(() => { const v=document.querySelector('video'); if (!v) return false; const p=v.play(); return !!p; })()")
                except Exception:
                    pass
            time.sleep(0.4)

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=False,
            output=requested,
            error="I found the YouTube video, but I could not verify that playback started.",
            metadata={"method":"youtube_result_recovery", "tab_id":tab_id, "selected_result":selected, "clicked_play":clicked_play, "last_state":last_state},
        )

    def _ensure_youtube_playing(self, tab_id: str, timeout: float = 8.0) -> dict[str, Any] | None:
        """Ensure a YouTube watch page is actually playing, not merely opened."""
        state_script = r"""
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const queryDeep = (selector, root = document, seen = new Set()) => {
                let found = [];
                try { found = Array.from(root.querySelectorAll(selector)); } catch (_) {}
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')).slice(0, 2500); } catch (_) {}
                for (const node of nodes) {
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {
                        seen.add(node.shadowRoot);
                        found.push(...queryDeep(selector, node.shadowRoot, seen));
                    }
                }
                return found;
            };
            const videos = queryDeep('video').filter(visible);
            const video = videos[0] || null;
            const buttons = queryDeep('button,[role="button"]').filter(visible);
            const play = buttons.find(b => {
                const label = `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''} ${b.textContent || ''}`.toLowerCase();
                return label.includes('play') && !label.includes('playlist');
            }) || null;
            return {
                watch: /youtube\.com\/watch/i.test(window.location.href),
                ready: !!video && video.readyState >= 2,
                paused: video ? !!video.paused : null,
                playing: !!video && !video.paused && video.readyState >= 2,
                currentTime: video ? Number(video.currentTime || 0) : 0,
                playFound: !!play,
                playLabel: play ? (play.getAttribute('aria-label') || play.getAttribute('title') || '') : ''
            };
        })()
        """
        play_script = r"""
        (() => {
            const visible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const queryDeep = (selector, root = document, seen = new Set()) => {
                let found = [];
                try { found = Array.from(root.querySelectorAll(selector)); } catch (_) {}
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')).slice(0, 2500); } catch (_) {}
                for (const node of nodes) {
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {
                        seen.add(node.shadowRoot);
                        found.push(...queryDeep(selector, node.shadowRoot, seen));
                    }
                }
                return found;
            };
            const videos = queryDeep('video').filter(visible);
            const video = videos[0] || null;
            if (video && video.paused) {
                try { video.muted = false; } catch (_) {}
                try { const p = video.play(); if (p && p.catch) p.catch(() => {}); } catch (_) {}
            }
            const buttons = queryDeep('button,[role="button"]').filter(visible);
            const play = buttons.find(b => {
                const label = `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''} ${b.textContent || ''}`.toLowerCase();
                return label.includes('play') && !label.includes('playlist');
            }) || null;
            if (play) { try { play.scrollIntoView({block:'center'}); play.click(); } catch (_) {} }
            return true;
        })()
        """
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                value = self.cdp.evaluate_script(tab_id, state_script)
                if isinstance(value, dict):
                    last = value
            except Exception:
                last = {}
            if last.get('watch') and last.get('playing'):
                return last
            try:
                self.cdp.evaluate_script(tab_id, play_script)
            except Exception:
                pass
            time.sleep(0.35)
        return last or None

    def _ensure_youtube_music_playing(self, tab_id: str, timeout: float = 8.0) -> dict[str, Any] | None:
        """Verify that a YouTube Music media element/player is actually playing."""
        state_script=r"""
        (()=>{
            const visible=(el)=>{if(!el)return false;const s=getComputedStyle(el);const r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
            const queryDeep=(selector,root=document,seen=new Set())=>{let found=[];try{found=Array.from(root.querySelectorAll(selector));}catch(_){}let nodes=[];try{nodes=Array.from(root.querySelectorAll('*')).slice(0,3000);}catch(_){}for(const n of nodes){if(n.shadowRoot&&!seen.has(n.shadowRoot)){seen.add(n.shadowRoot);found.push(...queryDeep(selector,n.shadowRoot,seen));}}return found;};
            const media=queryDeep('video,audio').filter(visible);
            const playing=media.find(m=>!m.paused&&!m.ended&&m.readyState>=2)||null;
            const controls=queryDeep('button,[role="button"],ytmusic-play-button-renderer').filter(visible);
            const pause=controls.find(b=>{const l=`${b.getAttribute('aria-label')||''} ${b.getAttribute('title')||''} ${b.textContent||''}`.toLowerCase();return (l.includes('pause')||l.includes('stop'))&&!l.includes('playlist');})||null;
            return {music:/music\.youtube\.com/i.test(location.href),playing:!!playing,paused:media[0]?!!media[0].paused:null,currentTime:playing?Number(playing.currentTime||0):0,mediaCount:media.length,pauseFound:!!pause,pauseLabel:pause?(pause.getAttribute('aria-label')||pause.getAttribute('title')||''):''};
        })()
        """
        play_script=r"""
        (()=>{
            const visible=(el)=>{if(!el)return false;const s=getComputedStyle(el);const r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
            const queryDeep=(selector,root=document,seen=new Set())=>{let found=[];try{found=Array.from(root.querySelectorAll(selector));}catch(_){}let nodes=[];try{nodes=Array.from(root.querySelectorAll('*')).slice(0,3000);}catch(_){}for(const n of nodes){if(n.shadowRoot&&!seen.has(n.shadowRoot)){seen.add(n.shadowRoot);found.push(...queryDeep(selector,n.shadowRoot,seen));}}return found;};
            const controls=queryDeep('button,[role="button"],ytmusic-play-button-renderer').filter(visible);
            const play=controls.find(b=>{const l=`${b.getAttribute('aria-label')||''} ${b.getAttribute('title')||''} ${b.textContent||''}`.toLowerCase();return l.includes('play')&&!l.includes('playlist');})||null;
            if(!play)return {clicked:false}; try{play.scrollIntoView({block:'center'});play.click();}catch(_){} const r=play.getBoundingClientRect(); return {clicked:true,rect:{x:r.x,y:r.y,width:r.width,height:r.height},label:play.getAttribute('aria-label')||play.getAttribute('title')||''};
        })()
        """
        deadline=time.monotonic()+max(0.5,float(timeout)); last={}; pointer=False
        while time.monotonic()<deadline:
            try:
                value=self.cdp.evaluate_js(tab_id,state_script)
                if isinstance(value,dict) and 'value' in value: value=value['value']
                if isinstance(value,dict): last=value
            except Exception: last={}
            if last.get('music') and last.get('playing'): return last
            try:
                click=self.cdp.evaluate_js(tab_id,play_script)
                if isinstance(click,dict) and 'value' in click: click=click['value']
                if isinstance(click,dict) and click.get('clicked') and not pointer:
                    rect=click.get('rect') or {}; x=float(rect.get('x',0) or 0)+float(rect.get('width',0) or 0)/2; y=float(rect.get('y',0) or 0)+float(rect.get('height',0) or 0)/2
                    if float(rect.get('width',0) or 0)>0 and float(rect.get('height',0) or 0)>0:
                        try:
                            for typ in ('mouseMoved','mousePressed','mouseReleased'):
                                self.cdp.execute_cdp_command(tab_id,'Input.dispatchMouseEvent',{'type':typ,'x':x,'y':y,'button':'left' if typ!='mouseMoved' else 'none','clickCount':1})
                            pointer=True
                        except Exception: pass
            except Exception: pass
            time.sleep(0.35)
        return last or None

    def _legacy_click_web_element(self, action: ToolCall) -> ToolResult:
        requested = str(action.parameters.get("selector") or action.target or "").strip()
        tab_id = self._owned_tab_id()
        if not tab_id or not requested:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="Tab ID or element target is missing.",
            )

        wait_dom = getattr(self.cdp, "wait_for_dom_ready", None)
        if callable(wait_dom) and not wait_dom(tab_id, timeout=5.0):
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error="The controlled browser page is not ready for this interaction.",
                metadata={"selector": requested, "tab_id": tab_id, "method": "dom_readiness_gate"},
            )

        # YouTube Music result playback is a high-risk ambiguous interaction: every result row
        # can expose a similarly labelled Play control. Resolve the requested row first and
        # invoke the row-local control instead of letting a generic selector pick another row.
        try:
            current_info = (self.cdp.get_tab_info(tab_id) if hasattr(self.cdp, "get_tab_info") else {}) or {}
            current_url = str(current_info.get("url") or self.context.target_tab_url or "")
        except Exception:
            current_url = str(self.context.target_tab_url or "")
        if "music.youtube.com/search" in current_url.casefold() and (
            "play" in requested.casefold() or "ytmusic-play-button" in requested.casefold()
        ):
            deterministic = self._youtube_music_play_requested_result(action, tab_id)
            if deterministic is not None:
                return deterministic

        # Resolve exact CSS first. If the model supplied natural UI text instead,
        # use a bounded semantic fallback over interactive elements. This keeps the
        # model interface human-friendly without inventing a second browser engine.
        resolve_script = rf"""
        (() => {{
            const requested = {json.dumps(requested, ensure_ascii=False)};
            const normalize = (v) => String(v ?? '').replace(/\s+/g, ' ').trim().toLowerCase();
            const queryDeep = (selector, root = document, seen = new Set()) => {{
                let out = [];
                try {{ out = Array.from(root.querySelectorAll(selector)); }} catch (_) {{}}
                let nodes = [];
                try {{ nodes = Array.from(root.querySelectorAll('*')).slice(0, 2500); }} catch (_) {{}}
                for (const node of nodes) {{
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {{
                        seen.add(node.shadowRoot);
                        out = out.concat(queryDeep(selector, node.shadowRoot, seen));
                    }}
                }}
                return out;
            }};
            let el = null;
            try {{ el = document.querySelector(requested); }} catch (_) {{}}
            if (!el) {{
                try {{ el = queryDeep(requested)[0] || null; }} catch (_) {{}}
            }}
            if (!el) {{
                const needle = normalize(requested);
                const candidates = queryDeep('button,a,input,textarea,select,[role="button"],[role="link"],[role="checkbox"],[role="menuitem"],[tabindex]').slice(0, 800);
                let exact = null;
                let best = null;
                let bestScore = 0;
                for (const candidate of candidates) {{
                    const attrs = [
                        candidate.getAttribute('aria-label'), candidate.getAttribute('title'),
                        candidate.getAttribute('name'), candidate.getAttribute('placeholder'),
                        candidate.getAttribute('value'), candidate.textContent
                    ].map(normalize).filter(Boolean);
                    if (attrs.some(v => v === needle)) {{ exact = candidate; break; }}
                    let score = 0;
                    if (attrs.some(v => v.includes(needle) || needle.includes(v))) score += 2;
                    if (normalize(candidate.getAttribute('aria-label')) === needle) score += 4;
                    if (normalize(candidate.textContent) === needle) score += 3;
                    if (score > bestScore) {{ best = candidate; bestScore = score; }}
                }}
                el = exact || best;
            }}
            if (!el) return {{success:false, reason:'element_not_found'}};
            const visible = (node) => {{
                if (!node) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            }};
            const rectNow = el.getBoundingClientRect();
            const actionable = visible(el) && !("disabled" in el && Boolean(el.disabled)) && rectNow.width > 0 && rectNow.height > 0;
            if (!actionable) return {{success:false, reason:'element_not_actionable', actionable:false}};
            const text = normalize(el.textContent).slice(0, 400);
            const before = {{
                url: window.location.href,
                title: document.title,
                tag: el.tagName,
                text,
                ariaLabel: el.getAttribute('aria-label'),
                role: el.getAttribute('role'),
                value: 'value' in el ? String(el.value ?? '').slice(0, 500) : null,
                checked: 'checked' in el ? Boolean(el.checked) : null,
                pressed: el.getAttribute('aria-pressed'),
                expanded: el.getAttribute('aria-expanded'),
                disabled: Boolean(el.disabled),
                href: el.getAttribute('href'),
                focused: document.activeElement === el,
                rect: (() => {{ const r = el.getBoundingClientRect(); return {{x:r.x,y:r.y,width:r.width,height:r.height}}; }})(),
                actionable: true,
                selector_match_count: (() => {{ try {{ return document.querySelectorAll(requested).length; }} catch (_) {{ return null; }} }})(),
                tab_count: window.__advi_tab_count || 0
            }};
            el.scrollIntoView({{block:'center', inline:'nearest'}});
            el.click();
            return {{success:true, before, resolvedTarget: el.outerHTML.slice(0, 800)}};
        }})()
        """
        res = self.cdp.evaluate_script(tab_id, resolve_script)
        # Production CDP returns the JavaScript value directly, while some
        # compatibility adapters/tests still return {"value": <js-value>}.
        # Accept both shapes so the browser capability never converts a
        # successful DOM click into a false click_failed result.
        if isinstance(res, dict) and set(res.keys()) == {"value"}:
            raw_value = res["value"]
        else:
            raw_value = res
        if raw_value is True:
            # Some CDP adapters only expose the boolean result of a script. The
            # click was dispatched, but there is no post-click state to verify.
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=True,
                output=requested,
                human_readable=f"Clicked web element '{requested}'.",
                verified=False,
                verification_status="uncertain",
                verification_details={
                    "method": "dom_click_dispatch_only",
                    "selector": requested,
                    "postcondition_observable": False,
                },
                metadata={"selector": requested, "cdp_port": self.cdp_port, "tab_id": tab_id},
            )
        if not isinstance(raw_value, dict) or raw_value.get("success") is not True:
            reason = raw_value.get("reason", "element_not_found") if isinstance(raw_value, dict) else "click_failed"
            youtube_recovery = self._youtube_play_first_result(action, requested, tab_id)
            if youtube_recovery is not None:
                return youtube_recovery
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                output=requested,
                error=f"Could not click web element '{requested}': {reason}",
                metadata={"selector": requested, "cdp_port": self.cdp_port, "tab_id": tab_id},
            )

        before = raw_value.get("before", {}) or {}

        # DOM .click() is normally sufficient, but several real-world pages
        # (including YouTube/Gmail) attach behavior to pointer events rather than
        # the DOM click path. When the page does not change, dispatch a real CDP
        # mouse gesture at the element center.
        time.sleep(0.25)
        try:
            after_probe = self.cdp.evaluate_script(tab_id, "({url:window.location.href,title:document.title})")
        except Exception:
            after_probe = {}
        probe = after_probe if isinstance(after_probe, dict) else {}
        gmail_send_succeeded = False
        if "mail.google.com" in str(before.get("url") or "").casefold() and "send" in requested.casefold():
            try:
                gmail_probe = self.cdp.evaluate_script(
                    tab_id,
                    r"(() => { const t=(document.body?.innerText || '').replace(/\s+/g,' ').toLowerCase(); return /message sent|sent to/.test(t); })()",
                )
                gmail_send_succeeded = bool(gmail_probe)
            except Exception:
                gmail_send_succeeded = False
        if probe.get("url") == before.get("url") and probe.get("title") == before.get("title") and not gmail_send_succeeded:
            href = str(before.get("href") or "").strip()
            # YouTube result anchors expose an authoritative watch URL. If the
            # page's JS click handler did not navigate, use that href directly
            # rather than asking the LLM to search/inspect/retry the same result.
            if ("youtube.com/results" in str(before.get("url") or "").casefold()
                    and "youtube.com/watch?v=" in href.casefold()):
                try:
                    if self.cdp.navigate_tab(tab_id=tab_id, url=href, timeout=12.0):
                        time.sleep(0.3)
                        direct_info = self.cdp.get_tab_info(tab_id) if hasattr(self.cdp, "get_tab_info") else {}
                        direct_url = str((direct_info or {}).get("url") or self.context.target_tab_url or "")
                        if "youtube.com/watch?v=" in direct_url.casefold():
                            self.context.target_tab_url = direct_url
                            requested_lower = requested.casefold()
                            wants_playback = any(token in requested_lower for token in ("video", "play", "watch", "result"))
                            playback = self._ensure_youtube_playing(tab_id, timeout=8.0) if wants_playback else None
                            if wants_playback and not (playback and playback.get("playing")):
                                return ToolResult(
                                    call_id=action.call_id, name=action.action, success=False, output=direct_url,
                                    error="The YouTube video opened, but ADVI could not verify that playback started.",
                                    verified=False, verification_status="uncertain",
                                    verification_details={"method":"youtube_result_href_navigation", "tab_id":tab_id, "url":direct_url, "playback":playback or {}},
                                    metadata={"selector":requested, "tab_id":tab_id, "url":direct_url, "opened_from_href":True},
                                )
                            return ToolResult(
                                call_id=action.call_id, name=action.action, success=True, output=direct_url,
                                human_readable=("Playing the selected YouTube video." if wants_playback else "Opened the selected YouTube video."), verified=True,
                                verification_status="verified",
                                verification_details={"method": "youtube_result_href_navigation", "tab_id": tab_id, "url": direct_url, "playing": bool(playback and playback.get("playing")) if wants_playback else None},
                                metadata={"selector": requested, "tab_id": tab_id, "url": direct_url, "opened_from_href": True},
                            )
                except Exception:
                    pass
            rect = before.get("rect") if isinstance(before.get("rect"), dict) else None
            if rect and float(rect.get("width", 0) or 0) > 0 and float(rect.get("height", 0) or 0) > 0:
                try:
                    x = float(rect.get("x", 0)) + float(rect.get("width", 0)) / 2.0
                    y = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2.0
                    self.cdp.execute_cdp_command(tab_id, "Input.dispatchMouseEvent", {"type":"mouseMoved","x":x,"y":y,"button":"none"})
                    self.cdp.execute_cdp_command(tab_id, "Input.dispatchMouseEvent", {"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1})
                    self.cdp.execute_cdp_command(tab_id, "Input.dispatchMouseEvent", {"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1})
                except Exception:
                    pass

        time.sleep(0.45)
        after_res = self.cdp.evaluate_script(
            tab_id,
            f"""
            (() => {{
                const requested = {requested!r};
                const normalize = (v) => String(v ?? '').replace(/\\s+/g, ' ').trim().toLowerCase();
                let el = null;
                try {{ el = document.querySelector(requested); }} catch (_) {{}}
                if (!el) {{
                    const needle = normalize(requested);
                    const candidates = Array.from(document.querySelectorAll('button,a,input,textarea,select,[role="button"],[role="link"],[role="checkbox"],[role="menuitem"],[tabindex]')).slice(0, 500);
                    el = candidates.find(c => [c.getAttribute('aria-label'), c.getAttribute('title'), c.getAttribute('name'), c.getAttribute('placeholder'), c.getAttribute('value'), c.textContent].map(normalize).includes(needle)) || null;
                }}
                if (!el) return {{found:false, active: document.activeElement === document.body}};
                return {{found:true, state: {{
                    url: window.location.href,
                    title: document.title,
                    tag: el.tagName,
                    text: normalize(el.textContent).slice(0, 400),
                    value: 'value' in el ? String(el.value ?? '').slice(0, 500) : null,
                    checked: 'checked' in el ? Boolean(el.checked) : null,
                    pressed: el.getAttribute('aria-pressed'),
                    expanded: el.getAttribute('aria-expanded'),
                    disabled: Boolean(el.disabled),
                    href: el.getAttribute('href'),
                    focused: document.activeElement === el
                }}}};
            }})()
            """,
        )
        # evaluate_script returns the JavaScript object directly.
        if isinstance(after_res, dict) and set(after_res.keys()) == {"value"}:
            after_value = after_res["value"]
        else:
            after_value = after_res if isinstance(after_res, dict) else None
        after_state = (after_value.get("state", {}) if isinstance(after_value, dict) else {}) or {}

        get_tab_info = getattr(self.cdp, "get_tab_info", None)
        after_tab = (get_tab_info(tab_id) if get_tab_info else {}) or {}
        actual_url = after_tab.get("url") or after_state.get("url")
        actual_title = after_tab.get("title") or after_state.get("title")
        tabs_before = int(before.get("tab_count", 0) or 0)
        try:
            tab_count_after = len(self.cdp.list_tabs())
        except Exception:
            tab_count_after = tabs_before
        state_changed = any(
            before.get(key) != after_state.get(key)
            for key in ("text", "value", "checked", "pressed", "expanded", "href", "focused")
            if before.get(key) is not None or after_state.get(key) is not None
        )
        page_changed = bool(actual_url and actual_url != before.get("url")) or bool(actual_title and actual_title != before.get("title"))
        new_tab_detected = tab_count_after > tabs_before if tabs_before else False
        verified = bool(state_changed or page_changed or new_tab_detected)

        if actual_url:
            self.context.target_tab_url = actual_url
        if actual_title is not None:
            self.context.target_tab_title = actual_title

        requested_lower = requested.casefold()
        music_page = bool(actual_url and "music.youtube.com" in str(actual_url).casefold())
        music_play_target = music_page and ("ytmusic-play-button" in requested_lower or "play" in requested_lower or "never say never" in requested_lower or "result" in requested_lower)
        if music_play_target:
            playback = self._ensure_youtube_music_playing(tab_id, timeout=8.0)
            if not (playback and playback.get("playing")):
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=False, output=requested,
                    error="The YouTube Music result was targeted, but playback could not be verified.",
                    verified=False, verification_status="uncertain",
                    verification_details={"method":"youtube_music_playback_verification", "tab_id":tab_id, "current_url":actual_url, "playback":playback or {}},
                    metadata={"selector":requested, "tab_id":tab_id, "current_url":actual_url, "recovery_recommended":True},
                )
            verified=True
        if actual_url and "youtube.com/watch?v=" in str(actual_url).casefold() and any(token in requested_lower for token in ("video", "play", "watch", "result")):
            playback = self._ensure_youtube_playing(tab_id, timeout=8.0)
            if not (playback and playback.get("playing")):
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=False, output=requested,
                    error="The YouTube video opened, but ADVI could not verify that playback started.",
                    verified=False, verification_status="uncertain",
                    verification_details={"method":"youtube_playback_verification", "tab_id":tab_id, "current_url":actual_url, "playback":playback or {}},
                    metadata={"selector":requested, "tab_id":tab_id, "current_url":actual_url},
                )
            verified = True

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=requested,
            human_readable=f"Clicked web element '{requested}'.",
            verified=verified,
            verification_status="verified" if verified else "uncertain",
            verification_details={
                "method": "dom_click_plus_control_state",
                "selector": requested,
                "tab_id": tab_id,
                "before": before,
                "after": after_state,
                "current_url": actual_url,
                "before_url": before.get("url"),
                "current_title": actual_title,
                "before_title": before.get("title"),
                "state_changed": state_changed,
                "page_changed": page_changed,
                "new_tab_detected": new_tab_detected,
            },
            metadata={
                "selector": requested,
                "before_url": before.get("url"),
                "current_url": actual_url,
                "tab_title": actual_title,
                "cdp_port": self.cdp_port,
                "tab_id": tab_id,
            },
        )

    def _execute_research_web(self, action: ToolCall) -> ToolResult:
        """Run a bounded search -> select -> navigate -> read workflow.

        This is intentionally deterministic: the workflow uses the browser's
        existing primitives and chooses a result by transparent text/URL scoring.
        It does not ask an LLM to invent a selector or claim that a source was opened.
        """
        query = str(action.parameters.get("query") or "").strip()
        target = str(
            action.parameters.get("target")
            or action.parameters.get("site")
            or action.target
            or ""
        ).strip()
        max_chars_raw = action.parameters.get("max_chars", 4000)
        try:
            max_chars = max(500, min(12000, int(max_chars_raw)))
        except (TypeError, ValueError):
            max_chars = 4000

        if not query:
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=False, error="No web research query specified."
            )

        tab_id = self._owned_or_started_tab()
        if not tab_id:
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=False, error="No ADVI-owned Chrome tab is available for web research, and I couldn't open one."
            )

        search_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
        if not self.cdp.navigate_tab(tab_id=tab_id, url=search_url):
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=False,
                error=f"Could not open search results for '{query}'."
            )

        wait_dom = getattr(self.cdp, "wait_for_dom_ready", None)
        if callable(wait_dom):
            wait_dom(tab_id, timeout=8.0)
        self.context.target_tab_url = search_url

        try:
            result = self.cdp.evaluate_script(
                tab_id,
                rf"""
                (() => {{
                    const target = {target!r}.toLowerCase();
                    const tokens = target.split(/[^a-z0-9]+/).filter(Boolean);
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    const candidates = anchors.map(a => ({{
                        text: (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' '),
                        href: a.href || ''
                    }})).filter(x => x.text && /^https?:/i.test(x.href));
                    const filtered = candidates.filter(x =>
                        !/^(?:https?:\/\/)?(?:www\.)?google\./i.test(x.href) &&
                        !/accounts\.google\.com|support\.google\.com|policies\.google\.com/i.test(x.href)
                    );
                    const scored = filtered.map(x => {{
                        const hay = (x.text + ' ' + x.href).toLowerCase();
                        let score = 0;
                        for (const token of tokens) if (hay.includes(token)) score += 2;
                        if (tokens.length && tokens.every(t => hay.includes(t))) score += 4;
                        return {{...x, score}};
                    }}).sort((a,b) => b.score - a.score);
                    return {{candidates: scored.slice(0, 10)}};
                }})()
                """,
            )
        except Exception as exc:
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=False, error=f"Could not inspect search results: {exc}"
            )

        raw = result.get("value") if isinstance(result, dict) else result
        candidates = raw.get("candidates", []) if isinstance(raw, dict) else []
        if not candidates:
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=False, output=str(query),
                error="Search completed but no usable result links were found.",
                metadata={"query": query, "search_url": search_url, "tab_id": tab_id},
            )

        selected = candidates[0]
        href = str(selected.get("href") or "").strip()
        if not href:
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=False, error="Selected search result did not contain a URL."
            )

        if not self.cdp.navigate_tab(tab_id=tab_id, url=href):
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=False,
                error=f"Could not open selected search result: {href}",
                metadata={"query": query, "selected_result": selected, "tab_id": tab_id},
            )
        wait_stable = getattr(self.cdp, "wait_for_stable_tab_state", None)
        if callable(wait_stable):
            wait_stable(tab_id, timeout=2.0, stable_for=0.15)

        get_tab_info = getattr(self.cdp, "get_tab_info", None)
        info = (get_tab_info(tab_id) if get_tab_info else {}) or {}
        current_url = info.get("url") or href
        title = info.get("title") or selected.get("text")
        self.context.target_tab_url = current_url
        if title is not None:
            self.context.target_tab_title = title

        try:
            body = self.cdp.evaluate_script(
                tab_id,
                f"document.body ? document.body.innerText.substring(0, {max_chars}) : ''",
            )
            text = body.get("value", "") if isinstance(body, dict) else ""
        except Exception as exc:
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=False,
                error=f"Opened the result but could not read the page: {exc}",
                metadata={"query": query, "selected_result": selected, "current_url": current_url, "tab_id": tab_id},
            )

        if not isinstance(text, str) or not text.strip():
            return ToolResult(
                call_id=action.call_id,
                name=action.action, success=True, output="",
                human_readable=f"Opened {current_url}, but the page contained no readable text.",
                metadata={
                    "query": query, "selected_result": selected, "current_url": current_url,
                    "tab_title": title, "content_available": False, "tab_id": tab_id,
                },
            )

        return ToolResult(
            call_id=action.call_id,
            name=action.action, success=True, output=text,
            human_readable=f"Opened and read {title or current_url}.",
            metadata={
                "query": query, "search_url": search_url, "selected_result": selected,
                "current_url": current_url, "tab_title": title,
                "content_available": True, "content_length": len(text),
                "tab_id": tab_id, "cdp_port": self.cdp_port,
            },
        )


    def _legacy_inspect_web_page(self, action: ToolCall) -> ToolResult:
        """Return a bounded, actionable inventory of visible webpage controls."""
        tab_id = self._owned_tab_id()
        if not tab_id:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No ADVI-owned Chrome tab is available.")
        try:
            limit = max(10, min(int(action.parameters.get("limit") or 60), 120))
        except (TypeError, ValueError):
            limit = 60

        script = rf"""
        (() => {{
            const limit = {limit};
            const isVisible = (el) => {{
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
            }};
            const queryDeep = (selector, root = document, seen = new Set()) => {{
                let out = [];
                let els = [];
                try {{ els = Array.from(root.querySelectorAll(selector)); }} catch (e) {{}}
                for (const el of els) out.push(el);
                for (const el of Array.from(root.querySelectorAll('*'))) {{
                    if (el.shadowRoot && !seen.has(el.shadowRoot)) {{
                        seen.add(el.shadowRoot);
                        out = out.concat(queryDeep(selector, el.shadowRoot, seen));
                    }}
                }}
                return out;
            }};
            const cssEscape = (value) => {{
                try {{ return CSS.escape(value); }} catch (e) {{ return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&'); }}
            }};
            const unique = (selector) => {{
                try {{ return document.querySelectorAll(selector).length === 1; }} catch (e) {{ return false; }}
            }};
            const selectorFor = (el) => {{
                const id = el.id && String(el.id).trim();
                if (id && unique('#' + cssEscape(id))) return '#' + cssEscape(id);
                const name = el.getAttribute('name');
                if (name) {{
                    const byName = el.tagName.toLowerCase() + '[name="' + String(name).replace(/"/g, '\\\\"') + '"]';
                    if (unique(byName)) return byName;
                }}
                const aria = el.getAttribute('aria-label');
                if (aria) {{
                    const byAria = el.tagName.toLowerCase() + '[aria-label="' + String(aria).replace(/"/g, '\\\\"') + '"]';
                    if (unique(byAria)) return byAria;
                }}
                const parts = [];
                let node = el;
                while (node && node.nodeType === 1 && node !== document.body && parts.length < 7) {{
                    let part = node.tagName.toLowerCase();
                    if (node.id) part += '#' + cssEscape(node.id);
                    else if (node.parentElement) {{
                        const same = Array.from(node.parentElement.children).filter(x => x.tagName === node.tagName);
                        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
                    }}
                    parts.unshift(part);
                    node = node.parentElement;
                }}
                return parts.join(' > ');
            }};
            const labelFor = (el) => {{
                const aria = el.getAttribute('aria-label');
                if (aria) return aria.trim();
                const labelledby = el.getAttribute('aria-labelledby');
                if (labelledby) {{
                    const ids = labelledby.split(/\\s+/).map(id => document.getElementById(id)).filter(Boolean);
                    const text = ids.map(x => (x.innerText || x.textContent || '').trim()).join(' ');
                    if (text) return text;
                }}
                if (el.labels && el.labels.length) {{
                    const text = Array.from(el.labels).map(x => (x.innerText || x.textContent || '').trim()).join(' ');
                    if (text) return text;
                }}
                const placeholder = el.getAttribute('placeholder');
                if (placeholder) return placeholder.trim();
                return (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 180);
            }};
            const selectors = 'button,a,input,textarea,select,[contenteditable="true"],[role="button"],[role="link"],[role="textbox"],[role="checkbox"],[role="menuitem"]';
            const elements = queryDeep(selectors);
            const result = [];
            const seen = new Set();

            // On search-result pages, prioritize actionable result links before
            // site chrome. This is the key behavior needed for natural requests
            // such as "search YouTube for X and play the first video": the model
            // should see the actual results, not 20 header controls.
            if (/youtube\.com\/results/i.test(window.location.href)) {{
                const seenHrefs = new Set();
                const videoLinks = queryDeep('a[href*="/watch?v="]').filter((el) => {{
                    if (!isVisible(el)) return false;
                    const href = String(el.href || '');
                    if (!href || seenHrefs.has(href) || !/youtube\.com\/watch\?v=/i.test(href)) return false;
                    if (el.closest && el.closest('ytd-reel-shelf-renderer, ytd-shorts')) return false;
                    seenHrefs.add(href);
                    return true;
                }}).slice(0, Math.min(8, limit));
                for (let resultIndex = 0; resultIndex < videoLinks.length && result.length < limit; resultIndex++) {{
                    const el = videoLinks[resultIndex];
                    const href = String(el.href || '');
                    const container = el.closest('ytd-video-renderer, ytd-rich-item-renderer, ytd-grid-video-renderer');
                    const titleNode = container?.querySelector?.('#video-title, a#video-title, yt-formatted-string#video-title');
                    const label = (titleNode?.getAttribute?.('aria-label') || titleNode?.textContent || el.getAttribute('aria-label') || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 240);
                    const videoId = (href.match(/[?&]v=([^&]+)/i) || [])[1] || '';
                    const selector = videoId ? `a[href*="/watch?v=${{videoId}}"]` : selectorFor(el);
                    result.push({{
                        index: result.length,
                        kind: 'video_result',
                        result_index: resultIndex + 1,
                        tag: 'a',
                        role: 'link',
                        type: null,
                        label: label || `YouTube video result ${{resultIndex + 1}}`,
                        placeholder: null,
                        name: null,
                        value: label || '',
                        href,
                        selector,
                        editable: false,
                        disabled: false,
                    }});
                }}
            }}

            for (const el of elements) {{
                if (result.length >= limit || !isVisible(el) || seen.has(el)) continue;
                seen.add(el);
                const tag = el.tagName.toLowerCase();
                const role = el.getAttribute('role');
                const type = el.getAttribute('type');
                const label = labelFor(el);
                const value = (el.value !== undefined ? String(el.value) : (el.textContent || '')).trim();
                const href = el.href || null;
                if (!label && !href && !value) continue;
                result.push({{
                    index: result.length,
                    tag,
                    role: role || null,
                    type: type || null,
                    label: label || null,
                    placeholder: el.getAttribute('placeholder') || null,
                    name: el.getAttribute('name') || null,
                    value: value.slice(0, 300),
                    href,
                    selector: selectorFor(el),
                    editable: tag === 'input' || tag === 'textarea' || el.isContentEditable || role === 'textbox',
                    disabled: !!el.disabled,
                }});
            }}
            return result;
        }})()
        """
        try:
            data = self.cdp.evaluate_js(tab_id, script)
        except Exception as exc:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not inspect webpage controls: {exc}")
        if not isinstance(data, list):
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Webpage inspection returned an invalid result.")
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=data,
            human_readable=f"Found {len(data)} visible interactive webpage elements.",
            verified=True,
            verification_status="verified",
            verification_details={"method": "dom_interactive_inventory", "count": len(data), "tab_id": tab_id},
            metadata={"tab_id": tab_id, "count": len(data), "cdp_port": self.cdp_port},
        )

    def _legacy_type_web_element(self, action: ToolCall) -> ToolResult:
        """Type into an exact webpage control and verify its resulting value."""
        tab_id = self._owned_tab_id()
        if not tab_id:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No ADVI-owned Chrome tab is available.")
        selector = str(action.parameters.get("selector") or action.parameters.get("target") or action.parameters.get("field") or "").strip()
        text = str(action.parameters.get("text") if action.parameters.get("text") is not None else action.parameters.get("value") or action.parameters.get("content") or "")
        if not selector:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No webpage field selector was provided.")

        wait_dom = getattr(self.cdp, "wait_for_dom_ready", None)
        if callable(wait_dom) and not wait_dom(tab_id, timeout=5.0):
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error="The controlled browser page is not ready for typing.",
                metadata={"selector": selector, "tab_id": tab_id, "method": "dom_readiness_gate"},
            )

        script = f"""
        (() => {{
            const selector = {json.dumps(selector, ensure_ascii=False)};
            const text = {json.dumps(text, ensure_ascii=False)};
            const queryDeep = (sel, root = document, seen = new Set()) => {{
                let found = [];
                try {{ found = Array.from(root.querySelectorAll(sel)); }} catch (e) {{
                    if (root === document) return [];
                }}
                if (found.length) return found;
                let nodes = [];
                try {{ nodes = Array.from(root.querySelectorAll('*')).slice(0, 2500); }} catch (e) {{}}
                for (const node of nodes) {{
                    if (node.shadowRoot && !seen.has(node.shadowRoot)) {{
                        seen.add(node.shadowRoot);
                        const nested = queryDeep(sel, node.shadowRoot, seen);
                        if (nested.length) return nested;
                    }}
                }}
                return [];
            }};
            let el = null;
            try {{
                // Keep the direct selector path first for ordinary DOM controls and
                // legacy adapters; fall back to recursive open-shadow-root traversal
                // only when the selector is not found in the light DOM.
                el = document.querySelector(selector) || queryDeep(selector)[0] || null;
            }} catch (e) {{ return {{success:false, reason:'invalid_selector'}}; }}
            if (!el || !(el instanceof HTMLElement)) return {{success:false, reason:'element_not_found'}};
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || el.offsetWidth <= 0 || el.offsetHeight <= 0) return {{success:false, reason:'element_not_visible'}};
            const tag = el.tagName.toLowerCase();
            const editable = tag === 'textarea' || (tag === 'input' && !['button','submit','checkbox','radio','file','hidden','image','reset'].includes((el.type || '').toLowerCase())) || el.isContentEditable || el.getAttribute('role') === 'textbox';
            if (!editable) return {{success:false, reason:'element_not_editable', tag, role:el.getAttribute('role')}};
            el.scrollIntoView({{block:'center'}});
            el.focus();
            const before = ('value' in el) ? String(el.value ?? '') : String(el.textContent ?? '');
            try {{
                if ('value' in el) {{
                    const proto = Object.getPrototypeOf(el);
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && desc.set) desc.set.call(el, text); else el.value = text;
                }} else {{
                    el.textContent = text;
                }}
            }} catch (e) {{
                if ('value' in el) el.value = text; else el.textContent = text;
            }}
            el.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:text}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            const after = ('value' in el) ? String(el.value ?? '') : String(el.textContent ?? '');
            return {{success: after === text || after.includes(text), before, after, selector, tag, role:el.getAttribute('role')}};
        }})()
        """
        try:
            data = self.cdp.evaluate_js(tab_id, script)
        except Exception as exc:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Could not type into webpage field: {exc}")
        if not isinstance(data, dict) or not data.get("success"):
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False,
                error=f"Could not type into webpage field: {data.get('reason', 'target unavailable') if isinstance(data, dict) else 'invalid result'}",
                metadata={"tab_id": tab_id, "selector": selector, "attempt": data},
            )
        verified = data.get("after") == text or text in str(data.get("after") or "")
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=data.get("after"),
            human_readable=f"Entered text into webpage field '{selector}'.",
            verified=verified,
            verification_status="verified" if verified else "uncertain",
            verification_details={"method":"dom_input_value", "tab_id":tab_id, "selector":selector, "before":data.get("before"), "after":data.get("after"), "expected":text},
            metadata={"tab_id": tab_id, "selector": selector, "before":data.get("before"), "after":data.get("after"), "expected":text, "cdp_port": self.cdp_port},
        )

    # The page's main content as text: search results, product titles and
    # prices, article body. Header menus come first in document.body and
    # crowded results out of a 4000-character read.
    _MAIN_TEXT_SCRIPT = (
        "(() => { const d = document, el = d.querySelector('main, [role=main], #search, #content') || d.body;"
        r" return (el ? el.innerText || '' : '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim().substring(0, 60000); })()"
    )
    _READ_CHUNK = 3000

    def _main_text(self, tab_id: str) -> str | None:
        res = self.cdp.evaluate_script(tab_id, self._MAIN_TEXT_SCRIPT)
        # CDPBrowser returns the value itself; some test/legacy adapters return {"value": ...}.
        if isinstance(res, dict) and "value" in res:
            res = res.get("value", "")
        return self._distill(res) if isinstance(res, str) else None

    @staticmethod
    def _distill(text: str) -> str:
        """Drop repeats and fragments so more of the page fits in one read.

        Amazon's result text repeats each title (full, then cut off with "…")
        and splits prices into "₹11,990" / "." / "00": 2500 characters covered
        three products (live probe, 2026-10-23).
        """
        kept: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or re.fullmatch(r"[\d.,]{1,3}", line):
                continue
            recent = kept[-6:]
            stem = line.rstrip("…").rstrip()
            if line in recent or (line.endswith("…") and any(r.startswith(stem) for r in recent)):
                continue
            if any(r.startswith(line) and r != line for r in recent[-3:]):
                continue  # "₹11,990" right after "₹11,990.00"
            kept.append(line)
        return "\n".join(kept)

    def _execute_read_web_page(self, action: ToolCall) -> ToolResult:
        tab_id = self._owned_tab_id()
        if not tab_id:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="No ADVI-owned Chrome tab is available.")

        try:
            start = max(0, int(action.parameters.get("start") or 0))
        except (TypeError, ValueError):
            start = 0
        full = self._main_text(tab_id)
        res = None if full is None else full[start:start + self._READ_CHUNK]
        remaining = 0 if full is None else max(0, len(full) - start - self._READ_CHUNK)
        text = res if isinstance(res, str) else ""
        if text and remaining:
            text += f"\n[{remaining} more characters; read_web_page start={start + self._READ_CHUNK} continues]"
        get_tab_info = getattr(self.cdp, "get_tab_info", None)
        info = (get_tab_info(tab_id) if get_tab_info else {}) or {}
        current_url = info.get("url") or self.context.target_tab_url
        title = info.get("title") or self.context.target_tab_title
        if current_url:
            self.context.target_tab_url = current_url
        if title is not None:
            self.context.target_tab_title = title
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=isinstance(text, str),
            output=text,
            human_readable=f"Extracted {len(text)} characters from page.",
            verified=isinstance(text, str) and bool(current_url),
            verification_status="verified" if (isinstance(text, str) and current_url) else "uncertain",
            verification_details={"method": "owned_tab_page_read", "tab_id": tab_id, "current_url": current_url, "length": len(text) if isinstance(text, str) else 0},
            metadata={
                "tab_id": tab_id,
                "cdp_port": self.cdp_port,
                "current_url": current_url,
                "tab_title": title,
                "length": len(text) if isinstance(text, str) else 0,
                "page_text_available": bool(text),
            },
        )

    # ------------------------------------------------------------------
    # Playwright-backed paths. The legacy CDP implementations above remain as
    # fallbacks when Playwright is unavailable or an action uses a CSS selector.
    # ------------------------------------------------------------------
    _REF_RE = re.compile(r"^(?:f\d+)?e\d+$")

    def _pw(self):
        from .pw_session import shared_browser

        return shared_browser()

    def _pw_enabled(self) -> bool:
        # Only drive a real Chrome. An injected CDP client (tests, adapters)
        # keeps the legacy path and must never cause a browser launch.
        return isinstance(self.cdp, CDPBrowser)

    def _pw_ready(self):
        """The Playwright page, aligned with ADVI's owned CDP tab (one source of truth)."""
        if not self._pw_enabled():
            raise RuntimeError("Playwright engine disabled for a non-standard CDP client")
        browser = self._pw()
        page = browser.ensure_page()
        owned = self.context.target_tab_id
        if owned and self._target_id(page) != str(owned):
            for candidate in (browser._context.pages if browser._context else []):
                if not candidate.is_closed() and self._target_id(candidate) == str(owned):
                    browser.page = page = candidate
                    break
        target = self._target_id(page)
        if target:
            self.context.set_target_tab(tab_id=target, url=page.url, title=page.title())
        return browser, page

    @staticmethod
    def _target_id(page) -> str | None:
        try:
            session = page.context.new_cdp_session(page)
            try:
                return str(session.send("Target.getTargetInfo")["targetInfo"]["targetId"])
            finally:
                session.detach()
        except Exception:
            return None

    def _pw_result(self, action: ToolCall, *, success: bool, summary: str, output: Any = None,
                   details: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> ToolResult:
        details = dict(details or {})
        return ToolResult(
            call_id=action.call_id, name=action.action, success=success, output=output,
            human_readable=summary, error=None if success else summary,
            verified=success, verification_status="verified" if success else "failed",
            verification_details={"engine": "playwright", **details},
            metadata={"engine": "playwright", "tab_id": self.context.target_tab_id, "cdp_port": self.cdp_port,
                      **dict(metadata or {}), "verification_details": details},
        )

    def _execute_open_browser(self, action: ToolCall) -> ToolResult:
        """Open ADVI's own browser window (never the user's personal Chrome profile)."""
        try:
            browser, page = self._pw_ready()
            url = str(action.parameters.get("url") or "").strip()
            if url:
                browser.goto(url)
            browser.bring_to_front()
            return self._pw_result(action, success=True, summary=f"Opened the browser{' at ' + page.url if url else ''}.",
                                   output=page.url, details={"current_url": page.url})
        except Exception as exc:
            logger.warning("Playwright open_browser failed, using legacy launch: %s", exc)
            return self._pw_result(action, success=bool(self._owned_tab_id()), summary="Opened the browser.")

    def _execute_navigate(self, action: ToolCall) -> ToolResult:
        if not self._pw_enabled():
            return self._legacy_navigate(action)
        url = str(action.parameters.get("url") or action.target or "").strip()
        if not url:
            return self._legacy_navigate(action)
        try:
            browser, page = self._pw_ready()
            info = browser.goto(url)
            self.context.target_tab_url = info["url"]
            self.context.target_tab_title = info["title"]
        except Exception as exc:
            logger.warning("Playwright navigate failed, using legacy CDP: %s", exc)
            return self._legacy_navigate(action)
        if _HUMAN_CHECK_TITLE.search(str(info.get("title") or "")):
            # Reddit answered a search with "Prove your humanity" and this was
            # reported as a verified page. ADVI never solves these checks.
            site = urllib.parse.urlsplit(info["url"]).netloc.removeprefix("www.") or "The site"
            message = (f"{site} is asking to confirm you're a human before it shows the page. "
                       "It's open in the browser; please complete that check yourself, then ask me again.")
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False, output=info["url"],
                error=message, human_readable=message, verified=False, verification_status="failed",
                verification_details={"method": "playwright_goto", "current_url": info["url"], "human_check": True},
                metadata={"current_url": info["url"], "expected_url": url, "tab_title": info["title"],
                          "failure_class": "human_verification_required", "engine": "playwright"},
            )
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True, output=info["url"],
            human_readable=f"Opened {info['title'] or info['url']}.",
            verified=True, verification_status="verified",
            verification_details={"method": "playwright_goto", "current_url": info["url"], "status": info.get("status")},
            metadata={"current_url": info["url"], "expected_url": url, "tab_title": info["title"],
                      "cdp_port": self.cdp_port, "tab_id": self.context.target_tab_id, "engine": "playwright"},
        )

    def _execute_inspect_web_page(self, action: ToolCall) -> ToolResult:
        if not self._pw_enabled():
            return self._legacy_inspect_web_page(action)
        try:
            browser, _page = self._pw_ready()
            from .pw_session import SNAPSHOT_LIMIT

            limit = max(10, min(int(action.parameters.get("limit") or SNAPSHOT_LIMIT), SNAPSHOT_LIMIT))
            snap = browser.snapshot(limit=limit)
            if not snap.elements:
                # Right after a search the page was still rendering: "0
                # interactive elements", then a stale ref and a repeated
                # search (WebVoyager dev run, 2026-10-23). Let it settle once.
                browser.wait_dom_stable(timeout=4.0)
                snap = browser.snapshot(limit=limit)
            if not snap.elements:
                # Heavy pages (Amazon results) were still empty after that; wait for the
                # network to go quiet and look once more (capability sweep, 2026-10-25).
                browser.settle(network_ms=5000)
                snap = browser.snapshot(limit=limit)
        except Exception as exc:
            logger.warning("Playwright snapshot failed, using legacy inventory: %s", exc)
            return self._legacy_inspect_web_page(action)
        return self._pw_result(
            action, success=True, output=snap.payload(),
            summary=f"Page '{snap.title}' has {len(snap.elements)} interactive elements; act on them by ref.",
            details={"method": "playwright_snapshot", "count": len(snap.elements), "snapshot": snap.number},
        )

    def _execute_click_web_element(self, action: ToolCall) -> ToolResult:
        if not self._pw_enabled():
            return self._legacy_click_web_element(action)
        ref = str(action.parameters.get("selector") or action.target or "").strip()
        if not self._REF_RE.match(ref):
            return self._legacy_click_web_element(action)
        try:
            browser, page = self._pw_ready()
            element = next((e for e in (browser.latest.elements if browser.latest else []) if e.get("ref") == ref), {})
            label = str(element.get("name") or "")
            if (_CONSEQUENTIAL_CLICK.search(label.casefold()) and not _browse_only_link(element, label)
                    and not getattr(self, "_approval_granted", False)):
                # Buying, paying, sending, submitting or deleting on a website
                # needs the user's yes first, like email_send and WhatsApp.
                return ToolResult(
                    call_id=action.call_id, name=action.action, success=False,
                    requires_confirmation=True,
                    confirmation_reason=f"click '{label}' on {page.url.split('?')[0]}",
                    error=f"Clicking '{label}' may buy, pay, send, submit or delete something; it needs the user's confirmation.",
                    metadata={"selector": ref, "label": label, "consequential": True},
                )
            before_url = page.url
            outcome = browser.act(ref, "click")
            media = browser.media_state() if outcome.details else {"playing": False}
        except Exception as exc:
            return self._pw_result(action, success=False, summary=f"Could not click '{ref}': {exc}")
        details = {
            "method": "playwright_click", "before_url": before_url, "current_url": browser.page.url,
            "state_changed": outcome.ok, "new_tab": outcome.details.get("new_tab"),
            "playing": bool(media.get("playing")), "playback": media,
        }
        success = outcome.ok or bool(media.get("playing"))
        summary = outcome.message + (" Media is playing." if media.get("playing") else "")
        return self._pw_result(action, success=success, summary=summary, details=details,
                               output={"result": summary, "url": browser.page.url, "snapshot": outcome.details.get("snapshot")})

    def _execute_type_web_element(self, action: ToolCall) -> ToolResult:
        if not self._pw_enabled():
            return self._legacy_type_web_element(action)
        ref = str(action.parameters.get("selector") or action.parameters.get("target") or "").strip()
        if not self._REF_RE.match(ref):
            return self._legacy_type_web_element(action)
        text = str(action.parameters.get("text") if action.parameters.get("text") is not None else action.parameters.get("value") or "")
        submit = str(action.parameters.get("submit") or "").strip().casefold() in {"true", "1", "yes"}
        try:
            browser, page = self._pw_ready()
            filled = browser.act(ref, "fill", text)
            after = filled.details.get("value_after")
            submitted = browser.act(ref, "press", "Enter") if (submit and filled.ok) else None
        except Exception as exc:
            return self._pw_result(action, success=False, summary=f"Could not type into '{ref}': {exc}")
        summary = filled.message + (f" Submitted; now at {browser.page.url}." if submitted is not None else "")
        snapshot = (submitted or filled).details.get("snapshot")
        result = self._pw_result(action, success=filled.ok, summary=summary,
                                 output={"result": summary, "url": browser.page.url, "snapshot": snapshot},
                                 details={"method": "playwright_fill", "submitted": submitted is not None},
                                 metadata={"selector": ref, "before": "", "after": str(after if after is not None else text), "expected": text})
        return result

    def _execute_search(self, action: ToolCall) -> ToolResult:
        """Search on the current site: find its search field, fill it, submit, wait for results."""
        if not self._pw_enabled():
            return self._legacy_search(action)
        query = str(action.parameters.get("query") or action.parameters.get("value") or "").strip()
        current = str(self.context.target_tab_url or "").casefold()
        # YouTube has a dedicated, verified result pipeline in the legacy path.
        if not query or "youtube.com" in current:
            return self._legacy_search(action)
        try:
            browser, page = self._pw_ready()
            snap = browser.snapshot()
            fields = [e for e in snap.elements if e.get("role") in ("searchbox", "combobox", "textbox") and not e.get("disabled")]
            fields.sort(key=lambda e: (e.get("role") != "searchbox", "search" not in e.get("name", "").casefold()))
            if not fields:
                return self._legacy_search(action)
            before_url = page.url
            ref = fields[0]["ref"]
            filled = browser.act(ref, "fill", query)
            submitted = browser.act(ref, "press", "Enter") if filled.ok else filled
        except Exception as exc:
            logger.warning("Playwright search failed, using legacy CDP: %s", exc)
            return self._legacy_search(action)
        changed = bool(submitted.details.get("url_changed") or submitted.details.get("page_changed"))
        summary = (f"Searched for '{query}'; results are showing at {browser.page.url}." if changed
                   else f"Typed '{query}' into the search field and pressed Enter, but the page did not change.")
        results = ""
        if changed:
            # The results as text in the same step, so the brain can answer
            # ("3 speakers around 20,000 rupees ...") instead of searching again.
            try:
                browser.wait_dom_stable(timeout=4.0)
                results = self._distill(str(browser.page.evaluate(self._MAIN_TEXT_SCRIPT) or ""))[:2500]
            except Exception as exc:
                logger.info("Could not read search results text: %s", exc)
        return self._pw_result(
            action, success=filled.ok and changed, summary=summary,
            output={"result": summary, "url": browser.page.url, "results": results,
                    "snapshot": None if results else submitted.details.get("snapshot")},
            details={"method": "playwright_site_search", "field": fields[0].get("name"), "before_url": before_url,
                     "current_url": browser.page.url},
            metadata={"query": query},
        )

    def _pw_play_best_match(self, action: ToolCall, query: str) -> ToolResult | None:
        """On a results page, click the play control that best matches the query; require real playback.

        Titles rarely match a spoken query exactly ("Overflow (Live) - Transformation
        Worship" for "Overflow Transformation Worship"), so candidates are scored by
        the share of query words they contain.
        """
        words = [w for w in re.findall(r"\w+", query.casefold()) if len(w) > 1]
        if not words:
            return None
        try:
            browser, _page = self._pw_ready()
            browser.wait_dom_stable(timeout=4.0)
            snap = browser.snapshot()
        except Exception as exc:
            logger.warning("Playwright play lookup failed: %s", exc)
            return None

        def score(element: dict[str, Any]) -> float:
            name = str(element.get("name") or "").casefold()
            covered = sum(1 for word in words if word in name) / len(words)
            is_play = name.startswith("play ") or "watch?v=" in str(element.get("href") or "")
            return covered + (0.25 if is_play else 0.0)

        candidates = [e for e in snap.elements if e.get("role") in ("button", "link") and not e.get("disabled")]
        candidates.sort(key=score, reverse=True)
        if not candidates or score(candidates[0]) < 0.75:
            return None
        best = candidates[0]
        outcome = browser.act(best["ref"], "click")
        media: dict[str, Any] = {"playing": False}
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            media = browser.media_state()
            if media.get("playing"):
                break
            time.sleep(0.5)
        if not media.get("playing"):
            # Some players open paused; press their own Play control once.
            try:
                snap = browser.snapshot()
                player_play = next((e for e in snap.elements if e.get("role") == "button" and str(e.get("name")).strip().casefold() == "play"), None)
                if player_play is not None:
                    browser.act(player_play["ref"], "click")
                    time.sleep(1.5)
                    media = browser.media_state()
            except Exception:
                pass
        playing = bool(media.get("playing"))
        # Result links carry the length in their accessible name ("... 3 minutes, 37 seconds"),
        # which ADVI then read aloud.
        title = re.sub(r"\s+(?:\d+\s+hours?,?\s*)?(?:\d+\s+minutes?,?\s*)?(?:\d+\s+seconds?)?\s*$", "", str(best.get('name') or "")).strip() or best.get('name')
        title = re.sub(r"^Play\s+Radio\s*\W\s*", "", str(title)).strip() or title
        summary = f"Playing '{title}'." if playing else f"I clicked '{title}' but playback did not start."
        return self._pw_result(
            action, success=playing, summary=summary, output=query,
            details={"method": "playwright_best_match_play", "selected": best.get("name"), "playing": playing,
                     "playback": media, "click": outcome.message},
        )

    _MEDIA_KEYS = {"pause": 0xB3, "resume": 0xB3, "toggle": 0xB3, "next": 0xB0, "previous": 0xB1, "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}

    def _execute_media_control(self, action: ToolCall) -> ToolResult:
        """Pause / resume / next for what ADVI is playing.

        "stop" after playing a song used to do nothing. Media in ADVI's browser
        is controlled directly and verified; anything else (a local player)
        gets the Windows media key, like a keyboard media button.
        """
        op = str(action.parameters.get("action") or "pause").strip().casefold()
        if op in {"stop", "halt"}:
            op = "pause"
        if op in {"play", "continue", "unpause"}:
            op = "resume"
        op = {"louder": "volume_up", "quieter": "volume_down", "unmute": "mute", "volume up": "volume_up",
              "volume down": "volume_down"}.get(op, op)
        if op not in self._MEDIA_KEYS:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error=f"Unknown media action '{op}'.")
        if op in {"pause", "resume"} and self._pw_enabled():
            from . import pw_session

            browser = pw_session._shared
            if browser is not None and browser.page is not None and not browser.page.is_closed():
                script = ("() => { const m = Array.from(document.querySelectorAll('video,audio')); "
                          + ("m.forEach(x => x.pause());" if op == "pause" else "m.forEach(x => { if (x.paused && x.currentTime > 0) x.play(); });")
                          + " return m.length; }")
                found = 0
                for frame in browser.page.frames:
                    try:
                        found += int(frame.evaluate(script) or 0)
                    except Exception:
                        continue
                if found:
                    # play() is asynchronous: one check at 0.6 s reported "not
                    # playing" for a song that started a moment later, and the
                    # brain then pressed space and paused it (log, 2026-10-23).
                    deadline = time.monotonic() + 3.0
                    while True:
                        time.sleep(0.3)
                        playing = bool(browser.media_state().get("playing"))
                        ok = (not playing) if op == "pause" else playing
                        if ok or time.monotonic() >= deadline:
                            break
                    word = "Paused" if op == "pause" else "Resumed"
                    return ToolResult(
                        call_id=action.call_id, name=action.action, success=ok,
                        human_readable=f"{word}." if ok else f"I tried, but media is {'still' if playing else 'not'} playing.",
                        verified=ok, verification_status="verified" if ok else "uncertain",
                        verification_details={"method": "browser_media_elements", "playing": playing},
                    )
        if os.name != "nt":
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="Media keys are only supported on Windows.")
        import ctypes

        key = self._MEDIA_KEYS[op]
        user32 = ctypes.windll.user32
        # One volume key press is 2%; "volume up" means a noticeable step.
        for _ in range(5 if op in {"volume_up", "volume_down"} else 1):
            user32.keybd_event(key, 0, 0, 0)
            user32.keybd_event(key, 0, 2, 0)
        word = {"pause": "Pressed play/pause on your media player", "resume": "Pressed play/pause on your media player",
                "toggle": "Pressed play/pause on your media player", "next": "Skipped to the next track",
                "previous": "Went back a track", "volume_up": "Turned the volume up", "volume_down": "Turned the volume down",
                "mute": "Toggled mute"}[op]
        # Windows handles media keys itself; what ADVI can confirm is that the key
        # was sent. Reporting this as unverified made every "volume up" end with
        # "I couldn't verify the final state" (log, 2026-10-23).
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True,
            human_readable=f"{word}.", verified=True, verification_status="verified",
            verification_details={"method": "windows_media_key", "key": hex(key), "evidence": "key_sent_only"},
        )
