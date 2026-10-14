from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from advi.core.readiness import wait_until, wait_for_stable_value

import websocket

logger = logging.getLogger(__name__)


class CDPBrowser:
    """
    Generalized Chrome DevTools Protocol (CDP) browser automation layer.

    Key Requirements Fulfilled:
    1. Continuous Run & Tab Ownership: Executes directly on target_tab_id via WebSocket,
       never switching tabs or losing context.
    2. Website Search Inside Target Site: Locates in-page search input elements via DOM
       and types/submits directly in-page (never using Ctrl+L or Google).
    3. Independent of OS Focus: Performs DOM manipulation, typing, navigation, and clicks
       via CDP without requiring OS window focus.
    4. State Verification & Idempotency: Inspects URL, DOM readiness, and current input text
       before executing to avoid duplicate actions ("do-overs").
    """

    def __init__(self, port: int = 9222) -> None:
        self.port = port

    def _http_get_json(self, path: str) -> Any:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{self.port}"})
        with urllib.request.urlopen(req, timeout=3.0) as response:
            return json.loads(response.read().decode("utf-8"))

    def list_tabs(self) -> list[dict[str, Any]]:
        """List all open page targets in Chrome."""
        try:
            targets = self._http_get_json("/json/list")
            return [
                t for t in targets
                if t.get("type") == "page" and t.get("id")
            ]
        except Exception as exc:
            # Chrome/CDP is intentionally lazy. A connection refusal simply means
            # Chrome is not attached yet; the browser capability may launch it.
            logger.debug("CDP list_tabs unavailable: %s", exc)
            return []

    def create_new_tab(self, url: str = "about:blank") -> dict[str, Any] | None:
        """Create a dedicated target tab in Chrome via CDP /json/new."""
        try:
            encoded_url = urllib.parse.quote(str(url), safe=":/?=&%#@,+$;~-._!()*'[]")
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/json/new?{encoded_url}",
                headers={"Host": f"127.0.0.1:{self.port}"},
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=3.0) as response:
                res = json.loads(response.read().decode("utf-8"))
                if res and res.get("id"):
                    logger.info("CDP created dedicated target tab id=%s", res["id"])
                    return res
        except Exception as exc:
            logger.warning("CDP create_new_tab via HTTP failed: %s", exc)

        # A failed dedicated-tab creation must not silently return an existing
        # user tab: callers use None to decide whether ownership is safe.
        return None


    def get_tab_info(self, tab_id: str) -> dict[str, Any] | None:
        """Get details for a specific tab_id."""
        tabs = self.list_tabs()
        for tab in tabs:
            if str(tab.get("id")) == str(tab_id):
                return tab
        return None

    def execute_cdp_command(
        self,
        tab_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Execute a CDP method directly on the specified tab's WebSocket debugger."""
        tab_info = self.get_tab_info(tab_id)
        if not tab_info:
            raise RuntimeError(f"CDP tab with id {tab_id} not found.")

        ws_url = tab_info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError(f"No webSocketDebuggerUrl for tab {tab_id}.")

        ws = websocket.create_connection(ws_url, timeout=timeout)
        try:
            msg_id = int(time.time() * 1000000) % 2147483647
            payload = {"id": msg_id, "method": method, "params": params or {}}
            ws.send(json.dumps(payload))

            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = ws.recv()
                if not raw:
                    continue
                resp = json.loads(raw)
                if resp.get("id") == msg_id:
                    if "error" in resp:
                        raise RuntimeError(f"CDP error ({method}): {resp['error']}")
                    return resp.get("result", {})
            raise TimeoutError(f"CDP command {method} timed out on tab {tab_id}.")
        finally:
            ws.close()

    def evaluate_script(self, tab_id: str, expression: str, timeout: float = 5.0) -> Any:
        """Compatibility alias used by BrowserCapability."""
        return self.evaluate_js(tab_id, expression, timeout=timeout)

    def navigate_tab(self, tab_id: str, url: str, timeout: float = 10.0) -> bool:
        """Compatibility alias used by BrowserCapability."""
        return self.navigate(tab_id, url, timeout=timeout)

    def evaluate_js(
        self,
        tab_id: str,
        expression: str,
        await_promise: bool = True,
        return_by_value: bool = True,
        timeout: float = 5.0,
    ) -> Any:
        """Evaluate JavaScript in the target tab context and return the result."""
        result = self.execute_cdp_command(
            tab_id=tab_id,
            method="Runtime.evaluate",
            params={
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": return_by_value,
                "userGesture": True,
            },
            timeout=timeout,
        )
        result_obj = result.get("result", {})
        if "exceptionDetails" in result:
            exc = result["exceptionDetails"]
            raise RuntimeError(f"JS evaluation exception: {exc}")
        return result_obj.get("value")

    def activate_tab(self, tab_id: str) -> bool:
        """Bring a target tab to the foreground and confirm CDP accepted it."""
        try:
            version = self._http_get_json("/json/version")
            ws_url = version.get("webSocketDebuggerUrl")
            if not ws_url:
                return False
            ws = websocket.create_connection(ws_url, timeout=3)
            try:
                msg_id = int(time.time() * 1000) % 2147483647
                ws.send(json.dumps({
                    "id": msg_id,
                    "method": "Target.activateTarget",
                    "params": {"targetId": tab_id},
                }))
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    raw = ws.recv()
                    if not raw:
                        continue
                    response = json.loads(raw)
                    if response.get("id") != msg_id:
                        continue
                    if response.get("error"):
                        logger.warning("Chrome rejected target activation %s: %s", tab_id, response["error"])
                        return False
                    return True
                return False
            finally:
                ws.close()
        except Exception as exc:
            logger.warning("Could not activate CDP target %s: %s", tab_id, exc)
            return False

    def navigate(self, tab_id: str, url: str, timeout: float = 10.0) -> bool:
        """Navigate target tab to a URL and wait for DOM readiness."""
        if not url.startswith(("http://", "https://", "about:", "chrome://")):
            url = "https://" + url

        # State check / Idempotency: if already on URL/domain, skip reload
        try:
            current_url = self.evaluate_js(tab_id, "window.location.href")
            if current_url and current_url.rstrip("/").lower() == url.rstrip("/").lower():
                logger.info("Tab %s is already at requested URL %s", tab_id, url)
                return True
        except Exception:
            pass

        logger.info("CDP navigating tab %s to %s", tab_id, url)
        self.execute_cdp_command(
            tab_id=tab_id,
            method="Page.navigate",
            params={"url": url},
            timeout=timeout,
        )

        return self.wait_for_dom_ready(tab_id, timeout=timeout)

    def wait_for_dom_ready(self, tab_id: str, timeout: float = 10.0) -> bool:
        """Wait for a controlled tab to reach a stable DOM-ready state."""
        def ready_state() -> str | None:
            value = self.evaluate_js(tab_id, "document.readyState")
            if isinstance(value, dict) and "value" in value:
                value = value["value"]
            return str(value) if value else None

        result = wait_until(
            lambda: ready_state() in {"interactive", "complete"},
            timeout=timeout,
            poll_interval=0.15,
            stable_for=0.15,
        )
        if result.ready:
            return True
        return False

    def wait_for_url(
        self,
        tab_id: str,
        *,
        contains: str | None = None,
        changed_from: str | None = None,
        timeout: float = 8.0,
    ) -> str | None:
        """Wait for an owned tab URL to satisfy a local condition."""
        def current() -> str | None:
            info = self.get_tab_info(tab_id) or {}
            return str(info.get("url") or "") or None

        def matches(value: str | None) -> bool:
            if not value:
                return False
            lowered = value.casefold()
            if contains and str(contains).casefold() not in lowered:
                return False
            if changed_from is not None and value == str(changed_from):
                return False
            return True

        def probe() -> str | bool:
            value = current()
            return value if matches(value) else False

        result = wait_until(probe, timeout=timeout, poll_interval=0.15)
        return result.value if result.ready else None

    def wait_for_element(
        self,
        tab_id: str,
        selector: str,
        *,
        timeout: float = 5.0,
        visible: bool = True,
        enabled: bool = True,
        editable: bool = False,
    ) -> dict[str, Any] | None:
        """Wait locally for a DOM element to exist and be actionable."""
        script_template = r"""
        (() => {
            const selector = %s;
            const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            let el = null;
            try { el = document.querySelector(selector); } catch (_) { return {found:false, reason:'invalid_selector'}; }
            if (!el) return {found:false, reason:'not_found'};
            if (%s && !visible(el)) return {found:false, reason:'not_visible'};
            if (%s && ('disabled' in el) && Boolean(el.disabled)) return {found:false, reason:'disabled'};
            const editable = el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && !['button','submit','checkbox','radio','file','hidden','image','reset'].includes((el.type || '').toLowerCase())) || el.isContentEditable || el.getAttribute('role') === 'textbox';
            if (%s && !editable) return {found:false, reason:'not_editable'};
            const rect = el.getBoundingClientRect();
            return {found:true, tag:el.tagName, value:'value' in el ? String(el.value ?? '') : null, text:String(el.textContent ?? '').trim().slice(0,300), focused:document.activeElement === el, editable};
        })()
        """
        script = script_template % (json.dumps(selector), str(bool(visible)).lower(), str(bool(enabled)).lower(), str(bool(editable)).lower())
        def probe() -> dict[str, Any] | bool:
            value = self._unwrap_eval_result(self.evaluate_js(tab_id, script))
            return value if isinstance(value, dict) and value.get("found") else False

        result = wait_until(probe, timeout=timeout, poll_interval=0.12)
        return result.value if result.ready and isinstance(result.value, dict) else None

    @staticmethod
    def _unwrap_eval_result(value: Any) -> Any:
        return value.get("value") if isinstance(value, dict) and set(value.keys()) == {"value"} else value

    def wait_for_stable_tab_state(self, tab_id: str, *, timeout: float = 2.0, stable_for: float = 0.20) -> dict[str, Any] | None:
        """Wait until URL/title/readyState stop changing briefly."""
        def state() -> tuple[str, str, str]:
            info = self.get_tab_info(tab_id) or {}
            return (str(info.get("url") or ""), str(info.get("title") or ""), str(self._unwrap_eval_result(self.evaluate_js(tab_id, "document.readyState")) or ""))
        result = wait_for_stable_value(state, timeout=timeout, poll_interval=0.10, stable_for=stable_for)
        if not result.ready or result.value is None:
            return None
        return {"url": result.value[0], "title": result.value[1], "readyState": result.value[2], "elapsed": result.elapsed}

    def find_in_site_search_input(self, tab_id: str) -> dict[str, Any] | None:
        """
        Locate the target website's own search input field.

        Strictly excludes browser address bars / chrome controls.
        Supports Light DOM and Shadow DOM across YouTube, Amazon, Reddit, Wikipedia, Google, and generic sites.
        """
        js_find_script = """
        (() => {
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const queryDeep = (selector, root = document) => {
                let els = [];
                try {
                    els = Array.from(root.querySelectorAll(selector));
                } catch(e) {}
                const all = Array.from(root.querySelectorAll('*'));
                for (const el of all) {
                    if (el.shadowRoot) {
                        els = els.concat(queryDeep(selector, el.shadowRoot));
                    }
                }
                return els;
            };

            // Specific high-priority selectors for major sites
            const siteSelectors = [
                'input[name="search_query"]', // YouTube
                '#search-input input', // YouTube alternative
                'input[id="twotabsearchtextbox"]', // Amazon
                'input[name="field-keywords"]', // Amazon alternative
                'input[name="q"]', // Google, Reddit, GitHub, etc.
                'reddit-search-large input', // Reddit web component
                'faceplate-search-input input', // Reddit web component
                'shreddit-search-input input', // Reddit web component
                '#header-search-bar input', // Reddit search bar
                '[slot="search-input"] input',
                'input[type="search"]', // Standard HTML5 search input
                'input[placeholder*="search" i]',
                'input[aria-label*="search" i]',
                'input[title*="search" i]',
                'textarea[placeholder*="search" i]',
                'form[action*="search" i] input[type="text"]',
                'form[action*="search" i] input',
                'div[role="search"] input',
                'header input[type="text"]',
            ];

            for (const sel of siteSelectors) {
                const els = queryDeep(sel);
                for (const el of els) {
                    if (isVisible(el)) {
                        const type = (el.type || '').toLowerCase();
                        if (['button', 'submit', 'hidden', 'checkbox', 'radio', 'image'].includes(type)) continue;

                        el.scrollIntoView({ block: 'center' });
                        return {
                            found: true,
                            selector: sel,
                            value: el.value || '',
                            placeholder: el.placeholder || '',
                            id: el.id || '',
                            name: el.name || '',
                            ariaLabel: el.getAttribute('aria-label') || '',
                            title: el.getAttribute('title') || '',
                            tagName: el.tagName
                        };
                    }
                }
            }

            // Conservative generic fallback: require search-like accessible metadata.
            // Never pick an arbitrary text field because it can silently corrupt page state.
            const allInputs = queryDeep('input, textarea');
            for (const el of allInputs) {
                if (!isVisible(el)) continue;
                const type = (el.type || '').toLowerCase();
                if (['button', 'submit', 'hidden', 'checkbox', 'radio', 'image'].includes(type)) continue;
                const attr = `${el.id || ''} ${el.name || ''} ${el.placeholder || ''} ${el.getAttribute('aria-label') || ''}`.toLowerCase();
                if (!attr.includes('search') && !attr.includes('query') && !attr.includes('find')) continue;

                el.scrollIntoView({ block: 'center' });
                return {
                    found: true,
                    selector: null,
                    value: el.value || '',
                    placeholder: el.placeholder || '',
                    id: el.id || '',
                    name: el.name || '',
                    tagName: el.tagName
                };
            }

            return { found: false };
        })()
        """
        try:
            res = self.evaluate_js(tab_id, js_find_script)
            if res and isinstance(res, dict) and res.get("found"):
                logger.info("Found in-site search input on tab %s: %s", tab_id, res)
                return res
        except Exception as exc:
            logger.warning("Error finding search input on tab %s: %s", tab_id, exc)
        return None

    def execute_in_site_search(self, tab_id: str, query: str) -> bool:
        """
        Enter query directly into website's search input and submit.

        Guarantees:
        - Search happens inside the website's search field (NOT address bar / Google).
        - Prevents text duplicate "do-overs" (if text already typed, does not duplicate).
        - Submits via Enter key / Form submit / Search button click.
        """
        search_info = None
        result = wait_until(
            lambda: self.find_in_site_search_input(tab_id) or False,
            timeout=4.0,
            poll_interval=0.20,
        )
        if result.ready and isinstance(result.value, dict):
            search_info = result.value

        if not search_info:
            logger.warning("Could not find in-site search input element on tab %s.", tab_id)
            return False

        js_type_and_submit = f"""
        (() => {{
            const query = {json.dumps(query)};
            const isVisible = (el) => {{
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0;
            }};

            const queryDeep = (selector, root = document) => {{
                let els = [];
                try {{
                    els = Array.from(root.querySelectorAll(selector));
                }} catch(e) {{}}
                const all = Array.from(root.querySelectorAll('*'));
                for (const el of all) {{
                    if (el.shadowRoot) {{
                        els = els.concat(queryDeep(selector, el.shadowRoot));
                    }}
                }}
                return els;
            }};

            // Reuse the exact element identity discovered by the finder first.
            // This prevents a page with multiple search-like fields from silently
            // switching targets between discovery and execution.
            const expectedId = {json.dumps(str(search_info.get("id") or ""))};
            const expectedName = {json.dumps(str(search_info.get("name") or ""))};
            const expectedPlaceholder = {json.dumps(str(search_info.get("placeholder") or ""))};
            const expectedAriaLabel = {json.dumps(str(search_info.get("ariaLabel") or ""))};
            const expectedTitle = {json.dumps(str(search_info.get("title") or ""))};
            let inputEl = null;
            const visibleInput = (el) => {{
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
            }};
            const identityMatches = (el) => {{
                if (!visibleInput(el)) return false;
                if (expectedId && el.id !== expectedId) return false;
                if (expectedName && (el.name || '') !== expectedName) return false;
                if (expectedPlaceholder && (el.placeholder || '') !== expectedPlaceholder) return false;
                if (expectedAriaLabel && (el.getAttribute('aria-label') || '') !== expectedAriaLabel) return false;
                if (expectedTitle && (el.getAttribute('title') || '') !== expectedTitle) return false;
                return true;
            }};

            if (expectedId) {{
                const byId = document.getElementById(expectedId);
                if (identityMatches(byId)) inputEl = byId;
                if (!inputEl) {{
                    // Do not build a CSS selector from the id here: this script is
                    // itself an outer Python f-string and that previously caused a
                    // Python NameError on CSS. Search by DOM identity instead.
                    inputEl = queryDeep('input, textarea').find((el) => el.id === expectedId && identityMatches(el)) || null;
                }}
            }}
            if (!inputEl && (expectedName || expectedPlaceholder || expectedAriaLabel || expectedTitle)) {{
                inputEl = queryDeep('input, textarea').find(identityMatches) || null;
            }}

            // Re-find element if the original node was replaced during page settling.
            const siteSelectors = [
                'input[name="search_query"]',
                '#search-input input',
                'input[id="twotabsearchtextbox"]',
                'input[name="field-keywords"]',
                'input[name="q"]',
                'input[type="search"]',
                'input[placeholder*="search" i]',
                'input[aria-label*="search" i]',
                'textarea[placeholder*="search" i]',
                'form[action*="search" i] input',
                'div[role="search"] input'
            ];

            if (!inputEl) for (const sel of siteSelectors) {{
                const els = queryDeep(sel);
                for (const el of els) {{
                    if (isVisible(el)) {{
                        const type = (el.type || '').toLowerCase();
                        if (['button', 'submit', 'hidden', 'checkbox', 'radio', 'image'].includes(type)) continue;
                        inputEl = el;
                        break;
                    }}
                }}
                if (inputEl) break;
            }}

            if (!inputEl) {{
                const all = queryDeep('input, textarea');
                for (const el of all) {{
                    if (!isVisible(el)) continue;
                    const attr = `${{el.id}} ${{el.name}} ${{el.placeholder}} ${{el.getAttribute('aria-label') || ''}}`.toLowerCase();
                    if (attr.includes('search') || attr.includes('query') || attr.includes('find')) {{
                        inputEl = el;
                        break;
                    }}
                }}
            }}

            if (!inputEl) return {{ success: false, reason: "Element no longer visible" }};

            // Auto-dismiss cookie/consent popups if present
            try {{
                const consentBtns = queryDeep('button, a').filter(el => {{
                    const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').toLowerCase();
                    return (t.includes('accept all') || t.includes('i agree') || t.includes('accept the use')) && isVisible(el);
                }});
                if (consentBtns.length > 0) {{
                    consentBtns[0].click();
                }}
            }} catch(e) {{}}

            // Focus and clear input element
            inputEl.focus();
            inputEl.scrollIntoView({{ block: 'center' }});

            // Check if value already equals query (Idempotency)
            if (inputEl.value !== query) {{
                try {{
                    inputEl.select();
                    document.execCommand('insertText', false, query);
                }} catch(e) {{}}

                if (inputEl.value !== query) {{
                    inputEl.value = query;
                    inputEl.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    inputEl.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}

            // Dispatch Enter key events on inputEl with composed: true for Polymer/Shadow DOM
            const enterOpts = {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, charCode: 13, bubbles: true, cancelable: true, composed: true }};
            inputEl.dispatchEvent(new KeyboardEvent('keydown', enterOpts));
            inputEl.dispatchEvent(new KeyboardEvent('keypress', enterOpts));
            inputEl.dispatchEvent(new KeyboardEvent('keyup', enterOpts));

            // Find search submit button or form
            let form = inputEl.form || inputEl.closest('form');
            let submitBtn = document.querySelector('#search-icon-legacy, button[type="submit"], button[aria-label*="Search" i], button[title*="Search" i]');

            if (submitBtn && isVisible(submitBtn)) {{
                try {{
                    submitBtn.focus();
                    submitBtn.click();
                }} catch(e) {{}}
            }} else if (form) {{
                if (typeof form.requestSubmit === 'function') {{
                    try {{ form.requestSubmit(); }} catch(e) {{ form.submit(); }}
                }} else {{
                    form.submit();
                }}
            }}

            return {{ success: true, typed: query, value: inputEl.value }};
        }})()
        """

        try:
            res = self.evaluate_js(tab_id, js_type_and_submit)
            # Send native CDP virtual Enter keypress
            try:
                self.execute_cdp_command(tab_id, "Input.dispatchKeyEvent", {"type": "rawKeyDown", "windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter", "text": "\r"})
                self.execute_cdp_command(tab_id, "Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter"})
            except Exception:
                pass

            if res and isinstance(res, dict) and res.get("success"):
                logger.info("Executed in-site search on tab %s for query %r: %s", tab_id, query, res)
                # Wait for search results navigation
                self.wait_for_dom_ready(tab_id, timeout=8.0)
                self.wait_for_stable_tab_state(tab_id, timeout=2.0, stable_for=0.15)
                return True
        except Exception as exc:
            logger.warning("CDP in-site search evaluation failed on tab %s: %s", tab_id, exc)

        return False

    def click_relevant_result(self, tab_id: str, target_text: str) -> bool:
        """
        Locate and click the requested result element on the page.

        Supports targets such as:
        - "Baby Shark video", "first video", "first search result", "first product"
        - Exact matching or substring matching text in result cards/links.
        """
        js_click_result = f"""
        (() => {{
            const target = {json.dumps(target_text.lower())};

            const isVisible = (el) => {{
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
            }};

            const queryDeep = (selector, root = document) => {{
                let els = [];
                try {{
                    els = Array.from(root.querySelectorAll(selector));
                }} catch(e) {{}}
                const all = Array.from(root.querySelectorAll('*'));
                for (const el of all) {{
                    if (el.shadowRoot) {{
                        els = els.concat(queryDeep(selector, el.shadowRoot));
                    }}
                }}
                return els;
            }};

            const isFirstRequested = target.includes('first') || target.includes('1st');

            // Site-specific candidate selectors
            const resultSelectors = [
                'ytd-video-renderer a#video-title', // YouTube
                '#contents ytd-video-renderer a',
                'a.yt-simple-endpoint.ytd-video-renderer',
                'div[data-component-type="s-search-result"] h2 a', // Amazon
                'div.s-main-slot div[data-component-type="s-search-result"] a.a-link-normal',
                'a[data-testid="post-title"]', // Reddit
                'a[shreddit-post-link]',
                'div.search-results a',
                'main a[href]',
                '#search a[href]',
                'a'
            ];

            let candidates = [];
            for (const sel of resultSelectors) {{
                const els = queryDeep(sel);
                for (const el of els) {{
                    if (isVisible(el)) {{
                        const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
                        if (text.length > 2) {{
                            candidates.push({{ el: el, text: text, href: el.href || '' }});
                        }}
                    }}
                }}
                if (candidates.length > 0 && isFirstRequested) break;
            }}

            if (candidates.length === 0) return {{ success: false, reason: "No result elements found" }};

            let chosen = null;

            if (isFirstRequested) {{
                chosen = candidates[0];
            }} else {{
                // Extract meaningful search terms (remove stop words)
                const stopWords = ['video', 'result', 'product', 'item', 'link', 'first', 'the', 'a', 'an', 'click'];
                const terms = target.split(/[^a-z0-9]+/).filter(w => w.length > 1 && !stopWords.includes(w));

                for (const c of candidates) {{
                    if (terms.length === 0 || terms.every(t => c.text.includes(t))) {{
                        chosen = c;
                        break;
                    }}
                }}

                if (!chosen && terms.length > 0) {{
                    for (const c of candidates) {{
                        if (terms.some(t => c.text.includes(t))) {{
                            chosen = c;
                            break;
                        }}
                    }}
                }}

                if (!chosen) chosen = candidates[0];
            }}

            if (chosen && chosen.el) {{
                chosen.el.scrollIntoView({{ block: 'center' }});
                chosen.el.focus();

                // Direct click or navigation
                if (typeof chosen.el.click === 'function') {{
                    chosen.el.click();
                }} else if (chosen.href) {{
                    window.location.href = chosen.href;
                }}

                return {{ success: true, clickedText: chosen.text, href: chosen.href }};
            }}

            return {{ success: false, reason: "Target result match not found" }};
        }})()
        """

        try:
            res = self.evaluate_js(tab_id, js_click_result)
            if res and isinstance(res, dict) and res.get("success"):
                logger.info("Clicked result on tab %s for target %r: %s", tab_id, target_text, res)
                self.wait_for_dom_ready(tab_id, timeout=8.0)
                self.wait_for_stable_tab_state(tab_id, timeout=2.0, stable_for=0.15)
                return True
        except Exception as exc:
            logger.warning("CDP click result failed on tab %s: %s", tab_id, exc)

        return False

    def verify_action_state(
        self,
        tab_id: str,
        expected_url_contains: str | None = None,
        expected_title_contains: str | None = None,
    ) -> bool:
        """Verify tab page state (URL, title, DOM loading)."""
        try:
            info = self.evaluate_js(tab_id, "({ url: window.location.href, title: document.title, state: document.readyState })")
            if not info or not isinstance(info, dict):
                return False

            url = (info.get("url") or "").lower()
            title = (info.get("title") or "").lower()

            if expected_url_contains and expected_url_contains.lower() not in url:
                logger.warning("State check failed: expected URL containing %r, got %r", expected_url_contains, url)
                return False

            if expected_title_contains and expected_title_contains.lower() not in title:
                logger.warning("State check failed: expected title containing %r, got %r", expected_title_contains, title)
                return False

            return True
        except Exception as exc:
            logger.warning("CDP verify_action_state failed on tab %s: %s", tab_id, exc)
            return False
