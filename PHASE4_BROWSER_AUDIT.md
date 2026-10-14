# ADVI 0.3.4: Phase 4 Audit, Professional Browser Automation (2026-10-14)

## What the log showed (turns 27–33)

- **WhatsApp works through ADVI**: "send hello to siyu" was sent and verified. The "New Chapter" send was
  delivered, but the task was marked uncertain (wording only).
- **Browser:**
  1. "open chrome" called `open_application("Chrome")`, which opened the **personal** Chrome. That profile
     shows the profile picker and has no automation port, so it isn't the browser ADVI controls.
  2. `navigate("miruro.to")` reached `https://www.miruro.to/` but was recorded as a *definitive failure*
     (exact host/scheme comparison), twice.
  3. `inspect_web_page` returned **zero elements** on Miruro, so the model could not search or click.
  4. Every attempt also lost time to provider failovers (`groq-1` rejected, per-minute limits on the others).

## Research: how robust browser agents work

- **Snapshot → refs → act.** Playwright MCP ([snapshots](https://playwright.dev/mcp/snapshots),
  [interaction tools](https://playwright.dev/mcp/tools/interaction)) and browser-use
  ([DOM processing](https://tessl.io/registry/tessl/pypi-browser-use/files/docs/dom-processing.md)) give the
  model a compact text list of interactive elements with short refs. Refs are valid for one snapshot only.
- **Actionability.** Robust automation waits for the target to be visible, stable, enabled and able to
  receive events, and fails with a reason rather than "succeeding" blindly. Playwright does this for every
  action.
- **Dedicated profile.** Since Chrome 136, remote debugging is ignored on the default profile; automation
  needs its own `--user-data-dir` ([Chrome blog](https://developer.chrome.com/blog/remote-debugging-port),
  [Browserless guide](https://www.browserless.io/blog/chrome-remote-debugging)).

## What changed

- **Engine:** Playwright 1.64 (added to `pyproject.toml`), driving the **installed Chrome**
  (`channel="chrome"`) with ADVI's own profile at `%LOCALAPPDATA%\ADVI\ChromeProfile`. If an ADVI Chrome
  is already running on port 9222 it attaches; otherwise it launches one. The port stays open, so the
  existing CDP helpers (`play_media`, the David fallback) drive the same tab.
- **`capabilities/browser/pw_session.py`:**
  - The snapshot extracts visible interactive elements in **every frame, including cross-origin player
    iframes** (refs like `f1e2`), and includes shadow roots.
  - It's sent as compact lines (`e22 link "Play Daily Lives of High School Boys" -> /watch/…`), ordered
    fields → iframe elements → page content → navigation chrome.
  - 40 elements fit the model-facing output budget. The JSON form had been truncated, and the player refs
    were cut.
  - Actions go through Playwright locators, which wait automatically. Afterwards ADVI waits for the DOM to
    stop changing (single-page apps render late), adopts new tabs, and reports URL/page change, media
    playback and a fresh snapshot.
- **Existing tools upgraded in place** (no extra tokens, legacy paths kept as fallbacks):
  - `inspect_web_page` returns the ref snapshot;
  - `click_web_element` / `type_web_element` take a ref as `selector` (`type_web_element` gains `submit`);
  - `navigate` / `search` use Playwright with proper waits.
  - Playwright engages only with the real CDP client, so tests with injected fakes never launch a browser.
- **`open_browser`** opens ADVI's browser. `open_application("Chrome" / "browser")` is redirected to it,
  so the personal profile picker no longer appears.
- **Navigation verification** compares sites, not exact strings: `miruro.to` → `www.miruro.to` passes. Only
  a tab stuck on a blank or error page counts as a definitive failure.
- **`play_media` on YouTube Music** matches by word overlap ("Overflow (Live) - Transformation Worship" for
  "Overflow Transformation Worship") and requires real playback. The old exact-title matcher remains as
  fallback.

## Live verification on this machine (real sites, ADVI's tool path, no LLM)

| Step | Result | Time |
|---|---|---|
| open_browser | verified | 4.8 s |
| navigate miruro.to | verified (www redirect accepted) | 3.2 s |
| search "Daily life of high school boys" | verified, results URL | 2.5 s |
| inspect → `e22 link "Play Daily Lives of High School Boys"` | 40 elements | 0.0 s |
| click e22 → episode 1 watch page | verified | 1.9 s |
| inspect → `f1e2 button "Play"` (cross-origin `strm.cx` iframe) | | 0.1 s |
| click f1e2 | **media playing** (time 4–6 s) | 2.8 s |
| play_media YouTube "Never Say Never" | verified playback | 10.0 s |
| play_media YouTube Music "Overflow" | verified playback | 9.1 s |

Playback was paused after each test.

## Still open

- **LLM capacity is now the main limit.** A site task needs about 4–6 model rounds, and each snapshot is
  about 600 tokens. Fix the rejected `groq-1` key; consider a provider tier with higher per-minute limits.
- Sign-in walls and CAPTCHAs are left to the user, by design.

## Validation

- `tests/current/test_phase4_browser.py` (8 tests). Full suite: 594 passed (live Groq smoke test deselected).
