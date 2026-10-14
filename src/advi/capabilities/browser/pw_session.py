"""Playwright-backed browser session: snapshot with refs, actions with actionability waits.

Why: the hand-written CDP layer dispatched clicks/keys without waiting for the
target to be attached, visible, stable, enabled and able to receive events, and
`inspect_web_page` returned zero elements on Miruro (runtime log, 2026-10-14).
Professional browser agents converge on two ideas, both used here:

* Snapshot -> refs (Playwright MCP, browser-use): the model sees a compact list
  of visible interactive elements, each with a short ref, and acts by ref. Refs
  belong to one snapshot; acting on a stale ref is refused.
* Actionability (Playwright): every action auto-waits until the element can
  really receive it, and fails with a reason instead of "succeeding" blindly.

The session drives ADVI's own Chrome profile (never the user's default profile;
Chrome 136+ refuses remote debugging there anyway) and keeps the CDP port open
so the existing CDP-based helpers (play_media, David fallback) keep working.

Playwright's sync API is thread-bound: use one instance from one thread.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_LIMIT = 40  # the model-facing serializer keeps ~40 list items
ACTION_TIMEOUT_MS = 8000
NAV_TIMEOUT_MS = 25000
ACTIONS = ("click", "fill", "type", "press", "select", "check", "uncheck", "hover", "scroll_into_view")

# Runs in every frame. Tags visible interactive elements with data-advi-ref and
# returns a compact description of each. Shadow roots are traversed.
_EXTRACT_JS = r"""
({prefix, limit}) => {
  for (const old of document.querySelectorAll('[data-advi-ref]')) old.removeAttribute('data-advi-ref');
  const SEL = 'a[href],button,input:not([type=hidden]),textarea,select,summary,video,audio,[contenteditable=""],[contenteditable=true],'
    + '[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[role=checkbox],[role=radio],[role=switch],'
    + '[role=textbox],[role=searchbox],[role=combobox],[role=slider],[onclick],[tabindex]:not([tabindex="-1"])';
  const out = [];
  const seen = new Set();
  const vw = window.innerWidth, vh = window.innerHeight;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return null;
    return r;
  };
  const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map(id => { const n = document.getElementById(id); return n ? textOf(n) : ''; }).join(' ').trim(); if (t) return t; }
    if (el.id) { const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (lab && textOf(lab)) return textOf(lab); }
    const t = textOf(el);
    if (t) return t;
    return el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('alt')
      || (el.querySelector('img[alt]') || {}).alt || el.getAttribute('name') || '';
  };
  const roleOf = (el) => {
    const r = el.getAttribute('role'); if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'input') { const t = (el.type || 'text').toLowerCase(); return ({checkbox:'checkbox', radio:'radio', submit:'button', button:'button', search:'searchbox', range:'slider'})[t] || 'textbox'; }
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'video' || tag === 'audio') return tag;
    if (el.isContentEditable) return 'textbox';
    return tag === 'button' || tag === 'summary' ? 'button' : 'generic';
  };
  const collect = (root) => {
    let nodes = [];
    try { nodes = Array.from(root.querySelectorAll(SEL)); } catch (e) {}
    for (const el of nodes) {
      if (seen.has(el)) continue; seen.add(el);
      const r = visible(el); if (!r) continue;
      const role = roleOf(el);
      let name = nameOf(el).slice(0, 90);
      if (!name && role === 'generic') continue;
      const item = {role, name, inView: r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw, y: Math.round(r.top + window.scrollY), el,
                    input: ['textbox', 'searchbox', 'combobox'].includes(role),
                    chrome: !!el.closest('nav,header,footer,aside,[role=navigation],[role=banner],[role=contentinfo]')};
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') item.value = (el.value || '').slice(0, 80);
      if (el.tagName === 'SELECT') item.value = (el.options[el.selectedIndex] || {}).text || '';
      if (el.type === 'checkbox' || el.type === 'radio' || el.getAttribute('aria-checked')) item.checked = !!(el.checked || el.getAttribute('aria-checked') === 'true');
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') item.disabled = true;
      if (el.tagName === 'A') { const h = el.getAttribute('href') || ''; if (h && !h.startsWith('javascript')) item.href = h.slice(0, 80); }
      if (el.tagName === 'VIDEO' || el.tagName === 'AUDIO') { item.playing = !el.paused && el.readyState >= 2; item.time = Math.round(el.currentTime || 0); }
      out.push(item);
    }
    for (const host of root.querySelectorAll('*')) if (host.shadowRoot) collect(host.shadowRoot);
  };
  collect(document);
  // Inputs first, then what is on screen, then page content before nav/header/footer chrome.
  out.sort((a, b) => (b.input - a.input) || (b.inView - a.inView) || (a.chrome - b.chrome) || (a.y - b.y));
  const kept = out.slice(0, limit);
  kept.forEach((item, i) => { item.ref = prefix + (i + 1); item.el.setAttribute('data-advi-ref', item.ref);
                              delete item.el; delete item.y; delete item.input; delete item.chrome; });
  const dialog = Array.from(document.querySelectorAll('[role=dialog]:not([aria-hidden=true]),dialog[open],[aria-modal=true]'))
                      .find(d => visible(d));
  return {elements: kept, total: out.length, dialog: dialog ? textOf(dialog).slice(0, 120) : null,
          scroll: {y: Math.round(window.scrollY), max: Math.max(0, document.documentElement.scrollHeight - vh)}};
}
"""


@dataclass
class Snapshot:
    number: int
    url: str
    title: str
    elements: list[dict[str, Any]]
    frames: dict[str, Any] = field(default_factory=dict)  # ref prefix -> frame
    truncated: bool = False
    dialog: str | None = None
    scroll: dict[str, int] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        key = [(e.get("role"), e.get("name"), e.get("value"), e.get("checked"), e.get("playing")) for e in self.elements]
        return hashlib.sha1(json.dumps([self.url, key], default=str).encode("utf-8")).hexdigest()[:12]

    def lines(self) -> list[str]:
        """One compact line per element, most useful first.

        JSON objects for 60 elements were ~6.6k chars; the model-facing output
        cap (5k) cut the tail, which is where iframe players were. Inputs, then
        iframe elements, then the rest of the page.
        """
        def line(e: dict[str, Any]) -> str:
            text = f'{e["ref"]} {e.get("role", "")} "{str(e.get("name", ""))[:60]}"'
            if e.get("value"):
                text += f' value="{str(e["value"])[:40]}"'
            for flag in ("checked", "disabled", "playing"):
                if e.get(flag):
                    text += f" [{flag}]"
            if e.get("inView") is False:
                text += " (offscreen)"
            href = str(e.get("href") or "")
            if href:
                text += " -> " + (href[:40] + "…" if len(href) > 40 else href)
            return text

        inputs = [e for e in self.elements if e.get("role") in ("textbox", "searchbox", "combobox") and not e["ref"].startswith("f")]
        framed = [e for e in self.elements if e["ref"].startswith("f")]
        rest = [e for e in self.elements if e not in inputs and e not in framed]
        return [line(e) for e in inputs + framed + rest]

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "snapshot": self.number, "url": self.url, "title": self.title,
            "elements": self.lines(), "truncated": self.truncated,
        }
        if self.dialog:
            data["dialog"] = self.dialog
        if self.scroll.get("max"):
            data["scroll"] = self.scroll
        return data


@dataclass
class ActResult:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class PlaywrightBrowser:
    def __init__(self, *, profile_dir: Path | None = None, cdp_port: int = 9222) -> None:
        self.profile_dir = Path(profile_dir or Path(os.environ.get("LOCALAPPDATA", ".")) / "ADVI" / "ChromeProfile")
        self.cdp_port = cdp_port
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self._counter = 0
        self.latest: Snapshot | None = None

    # --- lifecycle -----------------------------------------------------------
    def _cdp_alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/version", timeout=1.5) as response:
                return response.status == 200
        except Exception:
            return False

    def ensure_page(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        if self._context is None:
            from playwright.sync_api import sync_playwright

            self._pw = self._pw or sync_playwright().start()
            if self._cdp_alive():
                # An ADVI Chrome from earlier is still running on our port.
                self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
                self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            else:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._context = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    channel="chrome",
                    headless=False,
                    no_viewport=True,
                    ignore_default_args=["--enable-automation"],
                    args=[f"--remote-debugging-port={self.cdp_port}", "--no-first-run", "--no-default-browser-check",
                          # The legacy CDP websocket client sends an Origin header.
                          f"--remote-allow-origins=http://127.0.0.1:{self.cdp_port},http://localhost:{self.cdp_port}",
                          "--start-maximized"],
                )
            self._context.set_default_timeout(ACTION_TIMEOUT_MS)
            self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            self._context.on("page", self._adopt_new_page)
        pages = [p for p in self._context.pages if not p.is_closed()]
        blank = next((p for p in pages if p.url in ("about:blank", "chrome://newtab/")), None)
        self.page = blank or self._context.new_page()
        return self.page

    def _adopt_new_page(self, page) -> None:
        # Links that open a new tab: follow them, as a person would.
        self.page = page

    def bring_to_front(self) -> None:
        try:
            self.ensure_page().bring_to_front()
        except Exception:
            logger.debug("bring_to_front failed", exc_info=True)

    def close(self) -> None:
        for closer in (getattr(self._browser, "close", None) if self._browser else None,
                       getattr(self._context, "close", None) if self._context and not self._browser else None,
                       getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        self._pw = self._browser = self._context = self.page = None

    # --- navigation ----------------------------------------------------------
    def settle(self, page=None, *, network_ms: int = 3000) -> None:
        page = page or self.page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=network_ms)
        except Exception:
            pass  # long-polling sites never go idle; DOM-ready is enough

    def wait_dom_stable(self, page=None, *, timeout: float = 4.0, quiet: float = 0.5) -> bool:
        """Wait until the page stops adding/removing elements (SPA results render late)."""
        page = page or self.page
        deadline = time.monotonic() + timeout
        last, stable_since = None, time.monotonic()
        while time.monotonic() < deadline:
            try:
                count = page.evaluate("() => document.getElementsByTagName('*').length")
            except Exception:
                count = None
            now = time.monotonic()
            if count != last:
                last, stable_since = count, now
            elif now - stable_since >= quiet:
                return True
            time.sleep(0.15)
        return False

    def goto(self, url: str) -> dict[str, Any]:
        page = self.ensure_page()
        target = url if "://" in url or url.startswith("about:") else f"https://{url}"
        response = page.goto(target, wait_until="domcontentloaded")
        self.settle(page)
        self.wait_dom_stable(page)
        return {"url": page.url, "title": page.title(), "status": response.status if response else None}

    def back(self) -> dict[str, Any]:
        page = self.ensure_page()
        page.go_back(wait_until="domcontentloaded")
        self.settle(page)
        return {"url": page.url, "title": page.title()}

    # --- snapshot ------------------------------------------------------------
    def snapshot(self, *, limit: int = SNAPSHOT_LIMIT) -> Snapshot:
        page = self.ensure_page()
        self._counter += 1
        elements: list[dict[str, Any]] = []
        frames: dict[str, Any] = {}
        truncated = False
        dialog = None
        scroll: dict[str, int] = {}
        frame_list = page.frames[:6]
        # Reserve room for child frames: video players live in iframes and were
        # crowded out when the main page alone filled the budget.
        child_reserve = min(20, 8 * (len(frame_list) - 1))
        for index, frame in enumerate(frame_list):
            prefix = "e" if index == 0 else f"f{index}e"
            budget = (limit - child_reserve) if index == 0 else (limit - len(elements))
            if budget <= 0:
                truncated = True
                break
            try:
                data = frame.evaluate(_EXTRACT_JS, {"prefix": prefix, "limit": budget})
            except Exception:
                continue  # detached or still-navigating frames are skipped
            frames[prefix] = frame
            elements.extend(data.get("elements") or [])
            truncated = truncated or data.get("total", 0) > len(data.get("elements") or [])
            if index == 0:
                dialog = data.get("dialog")
                scroll = data.get("scroll") or {}
        self.latest = Snapshot(self._counter, page.url, page.title(), elements, frames, truncated, dialog, scroll)
        return self.latest

    # --- act -----------------------------------------------------------------
    def _locate(self, ref: str):
        snap = self.latest
        if snap is None:
            raise LookupError("No snapshot yet. Take a browser snapshot first.")
        ref = str(ref or "").strip()
        if not any(e.get("ref") == ref for e in snap.elements):
            raise LookupError(f"'{ref}' is not in snapshot {snap.number}. Take a new snapshot and use a current ref.")
        prefix = ref.rstrip("0123456789")
        frame = snap.frames.get(prefix)
        if frame is None:
            raise LookupError(f"The frame for '{ref}' is gone. Take a new snapshot.")
        return frame.locator(f'[data-advi-ref="{ref}"]').first

    def act(self, ref: str, action: str, text: str | None = None) -> ActResult:
        action = str(action or "click").strip().casefold()
        if action not in ACTIONS:
            return ActResult(False, f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.")
        try:
            locator = self._locate(ref)
        except LookupError as exc:
            return ActResult(False, str(exc))
        before = self.latest
        page = self.ensure_page()
        pages_before = len(self._context.pages) if self._context else 1
        label = next((e.get("name") for e in before.elements if e.get("ref") == ref), "") if before else ""
        try:
            if action == "click":
                locator.click()
            elif action == "fill":
                locator.fill(text or "")
            elif action == "type":
                locator.click()
                locator.press_sequentially(text or "", delay=20)
            elif action == "press":
                locator.press(text or "Enter")
            elif action == "select":
                locator.select_option(label=text or "")
            elif action == "check":
                locator.check()
            elif action == "uncheck":
                locator.uncheck()
            elif action == "hover":
                locator.hover()
            elif action == "scroll_into_view":
                locator.scroll_into_view_if_needed()
        except Exception as exc:
            # Playwright explains why: hidden, covered by an overlay, detached, disabled...
            reason = str(exc).splitlines()[0][:240]
            return ActResult(False, f"Could not {action} '{label or ref}': {reason}", {"ref": ref, "action": action})

        new_tab = bool(self._context and len(self._context.pages) > pages_before)
        self.settle(self.page, network_ms=2000)
        self.wait_dom_stable(self.page, timeout=3.0)
        details: dict[str, Any] = {"ref": ref, "action": action, "target": label, "new_tab": new_tab}
        if action in ("fill", "type"):
            try:
                value = locator.input_value(timeout=1500)
            except Exception:
                try:
                    value = locator.inner_text(timeout=1500)
                except Exception:
                    value = None
            details["value_after"] = value
        after = self.snapshot()
        details["url_changed"] = before is not None and after.url != before.url
        details["page_changed"] = before is not None and after.digest != before.digest
        details["snapshot"] = after.payload()
        if action in ("fill", "type"):
            ok = details.get("value_after") is None or (text or "").strip().casefold() in str(details["value_after"]).casefold()
        elif action in ("hover", "scroll_into_view"):
            ok = True
        else:
            ok = bool(details["url_changed"] or details["page_changed"] or new_tab)
        message = f"{action} on '{label or ref}'." + ("" if ok else " Nothing visibly changed.")
        return ActResult(ok, message, details)

    def media_state(self) -> dict[str, Any]:
        """Is any frame playing media? Used to verify play requests."""
        page = self.ensure_page()
        for frame in page.frames:
            try:
                state = frame.evaluate("""() => {
                  const m = Array.from(document.querySelectorAll('video,audio'));
                  const p = m.find(x => !x.paused && x.readyState >= 2);
                  return {count: m.length, playing: !!p, time: p ? Math.round(p.currentTime) : 0};
                }""")
            except Exception:
                continue
            if state.get("playing"):
                return {**state, "frame_url": frame.url}
        return {"playing": False}


_shared: PlaywrightBrowser | None = None


def shared_browser() -> PlaywrightBrowser:
    global _shared
    if _shared is None:
        _shared = PlaywrightBrowser()
    return _shared
