from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

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
            logger.warning("CDP list_tabs failed: %s", exc)
            return []

    def create_new_tab(self, url: str = "about:blank") -> dict[str, Any] | None:
        """Create a dedicated target tab in Chrome via CDP /json/new."""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/json/new?{url}",
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

        # If dedicated creation failed, there is no owned target. Do not hijack
        # a user's existing Chrome tab.
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
        """Bring target_tab_id to active tab status within Chrome."""
        try:
            version = self._http_get_json("/json/version")
            ws_url = version.get("webSocketDebuggerUrl")
            if ws_url:
                ws = websocket.create_connection(ws_url, timeout=3)
                try:
                    msg_id = int(time.time() * 1000) % 2147483647
                    ws.send(json.dumps({
                        "id": msg_id,
                        "method": "Target.activateTarget",
                        "params": {"targetId": tab_id},
                    }))
                    ws.recv()
                finally:
                    ws.close()
            return True
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
        """Wait until document.readyState is interactive or complete."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.evaluate_js(tab_id, "document.readyState")
                if state in ("interactive", "complete"):
                    time.sleep(0.5) # Brief stabilization
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

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
                'header input',
                'input'
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
                            tagName: el.tagName
                        };
                    }
                }
            }

            // General fallback: query deep for all visible inputs
            const allInputs = queryDeep('input, textarea');
            for (const el of allInputs) {
                if (!isVisible(el)) continue;
                const type = (el.type || '').toLowerCase();
                if (['button', 'submit', 'hidden', 'checkbox', 'radio', 'image'].includes(type)) continue;

                el.scrollIntoView({ block: 'center' });
                return {
                    found: true,
                    selector: 'input',
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
        for attempt in range(4):
            search_info = self.find_in_site_search_input(tab_id)
            if search_info:
                break
            time.sleep(1.0)

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

            // Re-find element
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
                'div[role="search"] input',
                'input'
            ];

            let inputEl = null;
            for (const sel of siteSelectors) {{
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
                const all = Array.from(document.querySelectorAll('input, textarea'));
                for (const el of all) {{
                    if (!isVisible(el)) continue;
                    const attr = `${{el.id}} ${{el.name}} ${{el.placeholder}} ${{el.getAttribute('aria-label') || ''}}`.toLowerCase();
                    if (attr.includes('search')) {{
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
                time.sleep(2.5)
                self.wait_for_dom_ready(tab_id, timeout=8.0)
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
                'main button',
                'main [role="button"]',
                'button',
                '[role="button"]',
                'main a[href]',
                '#search a[href]',
                'a'
            ];

            let candidates = [];
            for (const sel of resultSelectors) {{
                const els = queryDeep(sel);
                for (const el of els) {{
                    if (isVisible(el)) {{
                        const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('value') || '').trim().toLowerCase();
                        if (text.length > 1) {{
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
                const stopWords = ['video', 'result', 'product', 'item', 'link', 'first', 'the', 'a', 'an', 'click', 'button'];
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
                const r = chosen.el.getBoundingClientRect();
                if (typeof chosen.el.click === 'function') {{
                    chosen.el.click();
                }} else if (chosen.href) {{
                    window.location.href = chosen.href;
                }}

                return {{ success: true, clickedText: chosen.text, href: chosen.href, rect: {{x:r.x,y:r.y,width:r.width,height:r.height}} }};
            }}

            return {{ success: false, reason: "Target result match not found" }};
        }})()
        """

        try:
            res = self.evaluate_js(tab_id, js_click_result)
            if res and isinstance(res, dict) and res.get("success"):
                # Some Gmail/YouTube controls react to native pointer events more
                # reliably than HTMLElement.click(). Replay through CDP when the
                # DOM click produced no immediate navigation/title change.
                try:
                    before = self.evaluate_js(tab_id, "({url:window.location.href,title:document.title})") or {}
                    time.sleep(0.35)
                    post = self.evaluate_js(tab_id, r"""
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
                        const body = String(document.body?.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        const sent = /message sent|sent to/.test(body);
                        const sendControls = queryDeep('button,[role="button"]').filter(visible).filter(b => {
                            const label = `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''} ${b.textContent || ''}`.toLowerCase();
                            return /(^|\s)send(\s|$)|send message/.test(label);
                        }).length;
                        const videos = queryDeep('video').filter(visible);
                        const playing = videos.some(v => !v.paused && v.readyState >= 2);
                        return {
                            url: window.location.href,
                            title: document.title,
                            sent,
                            sendControls,
                            playing
                        };
                    })()
                    """) or {}
                    rect = res.get("rect") if isinstance(res.get("rect"), dict) else None
                    target_lower = str(target_text or '').casefold()
                    semantic_success = False
                    if any(token in target_lower for token in ('send button', 'send message', 'send email')):
                        semantic_success = bool(post.get('sent'))
                    elif any(token in target_lower for token in ('video', 'play', 'watch', 'result')) and 'youtube.com/watch' in str(post.get('url') or '').casefold():
                        semantic_success = bool(post.get('playing'))
                    if rect and before.get("url") == post.get("url") and before.get("title") == post.get("title") and not semantic_success:
                        x = float(rect.get("x", 0)) + float(rect.get("width", 0)) / 2.0
                        y = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2.0
                        self.execute_cdp_command(tab_id, "Input.dispatchMouseEvent", {"type":"mouseMoved","x":x,"y":y,"button":"none"})
                        self.execute_cdp_command(tab_id, "Input.dispatchMouseEvent", {"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1})
                        self.execute_cdp_command(tab_id, "Input.dispatchMouseEvent", {"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1})
                except Exception:
                    post = {}
                logger.info("Clicked result on tab %s for target %r: %s", tab_id, target_text, res)
                time.sleep(1.0)
                self.wait_for_dom_ready(tab_id, timeout=8.0)
                # Do not report false success for consequential Gmail sends or
                # requested YouTube playback when the postcondition is observable.
                try:
                    verify = self.evaluate_js(tab_id, r"""
                    (() => {
                        const body = String(document.body?.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        const sends = Array.from(document.querySelectorAll('button,[role="button"]')).filter(b => {
                            const s=getComputedStyle(b), r=b.getBoundingClientRect();
                            const l=`${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''} ${b.textContent || ''}`.toLowerCase();
                            return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0 && /(^|\s)send(\s|$)|send message/.test(l);
                        }).length;
                        const videos = Array.from(document.querySelectorAll('video')).filter(v => !v.paused && v.readyState >= 2);
                        return {sent:/message sent|sent to/.test(body), sendControls:sends, playing:videos.length > 0, url:window.location.href};
                    })()
                    """) or {}
                except Exception:
                    verify = {}
                if any(token in str(target_text or '').casefold() for token in ('send button', 'send message', 'send email')):
                    if verify.get('sent'):
                        return True
                    return False
                if any(token in str(target_text or '').casefold() for token in ('video', 'play', 'watch', 'result')) and 'youtube.com/watch' in str(verify.get('url') or '').casefold():
                    return bool(verify.get('playing'))
                return True
        except Exception as exc:
            logger.warning("CDP click result failed on tab %s: %s", tab_id, exc)

        return False

    def verify_gmail_compose(self, tab_id: str, *, recipient: str, subject: str, body: str, timeout: float = 8.0) -> bool:
        """Verify Gmail compose UI contains the exact prepared message fields."""
        if not tab_id:
            return False
        deadline = time.monotonic() + max(1.0, timeout)
        script = rf"""
        (() => {{
            const wanted = {{to:{json.dumps(recipient)}, subject:{json.dumps(subject)}, body:{json.dumps(body)}}};
            const norm = v => String(v || '').replace(/\s+/g,' ').trim();
            const visible = el => {{
                if (!el) return false;
                const s=getComputedStyle(el), r=el.getBoundingClientRect();
                return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0;
            }};
            const all = sel => Array.from(document.querySelectorAll(sel)).filter(visible);
            const recipientEls = all('input[name="to"], input[aria-label*="recipient" i], input[role="combobox"], textarea[aria-label*="recipient" i]');
            const subjectEls = all('input[name="subjectbox"], input[aria-label*="subject" i], input[placeholder*="subject" i]');
            const bodyEls = all('[contenteditable="true"], div[aria-label*="message body" i], textarea[name="body"]');
            const url = String(location.href || '');
            const toOk = recipientEls.some(e => norm(e.value).includes(norm(wanted.to))) || decodeURIComponent(url).includes(wanted.to);
            const subjectOk = subjectEls.some(e => norm(e.value) === norm(wanted.subject)) || (wanted.subject === '' && subjectEls.length>0);
            const bodyOk = bodyEls.some(e => norm(e.innerText || e.textContent || e.value).includes(norm(wanted.body)));
            const gmail = /mail\.google\.com/i.test(location.hostname);
            const compose = subjectEls.length>0 && bodyEls.length>0;
            return {{gmail,compose,toOk,subjectOk,bodyOk,url}};
        }})()
        """
        while time.monotonic() < deadline:
            try:
                state = self.evaluate_js(tab_id, script) or {{}}
                if state.get('gmail') and state.get('compose') and state.get('toOk') and state.get('subjectOk') and state.get('bodyOk'):
                    logger.info('Verified Gmail compose on tab %s for %s', tab_id, recipient)
                    return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    def send_gmail_compose(self, tab_id: str, *, timeout: float = 10.0) -> bool:
        """Click Gmail's visible Send control and verify a sent-state/toast."""
        if not tab_id:
            return False
        click_script = r"""
        (() => {
            const visible = el => {
                if (!el) return false;
                const s=getComputedStyle(el), r=el.getBoundingClientRect();
                return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0;
            };
            const label = el => `${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.textContent || ''}`.replace(/\s+/g,' ').trim().toLowerCase();
            const controls = Array.from(document.querySelectorAll('button,[role="button"],div[command]')).filter(visible);
            const candidates = controls.filter(el => {
                const l=label(el);
                return /(^|\s)send(\s|$)|send message/.test(l);
            });
            if (!candidates.length) return {clicked:false, reason:'send_control_not_found'};
            const el=candidates[0];
            el.scrollIntoView({block:'center'});
            el.focus();
            el.click();
            const r=el.getBoundingClientRect();
            return {clicked:true, rect:{x:r.x,y:r.y,width:r.width,height:r.height}};
        })()
        """
        try:
            state = self.evaluate_js(tab_id, click_script) or {}
        except Exception as exc:
            logger.warning('Gmail Send DOM click failed: %s', exc)
            return False
        if not state.get('clicked'):
            return False
        rect = state.get('rect') if isinstance(state.get('rect'), dict) else None
        if rect:
            try:
                x=float(rect.get('x',0))+float(rect.get('width',0))/2
                y=float(rect.get('y',0))+float(rect.get('height',0))/2
                self.execute_cdp_command(tab_id,'Input.dispatchMouseEvent',{'type':'mousePressed','x':x,'y':y,'button':'left','clickCount':1})
                self.execute_cdp_command(tab_id,'Input.dispatchMouseEvent',{'type':'mouseReleased','x':x,'y':y,'button':'left','clickCount':1})
            except Exception:
                pass
        deadline=time.monotonic()+max(1.0,timeout)
        verify_script=r"""
        (() => {
            const body=String(document.body?.innerText || '').replace(/\s+/g,' ').trim().toLowerCase();
            const compose = document.querySelector('[contenteditable="true"], textarea[name="body"]');
            const sent=/message sent|sent to/.test(body);
            return {sent, composeOpen:!!compose, url:location.href};
        })()
        """
        while time.monotonic()<deadline:
            try:
                v=self.evaluate_js(tab_id,verify_script) or {}
                if v.get('sent'):
                    return True
                if not v.get('composeOpen') and 'mail.google.com' in str(v.get('url') or '').casefold():
                    # Compose disappearing is a useful secondary signal when Gmail suppresses the toast.
                    time.sleep(0.4)
                    v2=self.evaluate_js(tab_id,verify_script) or {}
                    if not v2.get('composeOpen'):
                        return True
            except Exception:
                pass
            time.sleep(0.4)
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
