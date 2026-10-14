# ADVI: Root Cause Analysis and Architecture Overhaul

**Inputs:** `logs/ADVI_Runtime_Log_Forensic_Analysis.md`, `logs/advi.log` (69,148 lines, 30 Sep – 13 Oct 2026), and the `src/advi` source tree (v0.3.4).
**Date:** 2026-10-14

---

## 0. Summary

ADVI already has most of the parts that good desktop agents use: UIA access through pywinauto, a CDP browser driver, video-playback checks (`capabilities/browser/executor.py:871`), registry and AppX app inventory (`environment/watchtower.py:475,891`), a postcondition verifier, and a native Gmail path that works every time. It fails because of **how those parts are wired together**:

1. **The model sees OCR text, not UI elements.** `read_screen` returns a Tesseract text dump. The model gets no element IDs, roles, or states, so it cannot point at "the search box" or "the Anurodh chat row". The code uses UIA internally to resolve targets, but never hands UIA to the model as its observation.
2. **The LLM drives every keystroke.** "Send Hello to Anurodh on WhatsApp" took **25 tool calls** (`find_application → click → read_screen → open_application → click → read_screen → hotkey → read_screen → inspect_window → type_text → read_screen → …`). Every call is a free-tier Groq/Gemini round trip, so rate limits end up killing the task. The Gmail path works because it is a **skill**: 3 deterministic calls with typed slots.
3. **Regex-based tool hiding.** `_select_model_tools` (`agent/session.py:302`) uses keyword regexes to decide which tools the model may see on each turn. That one function explains the "I can't create PDFs", "I don't have a storage tool", and "laptop status" failures, and all 13 `tool not in request.tools` rejections.
4. **Regex-extracted "protected parameters" overwrite correct model arguments.** At `agent/session.py:4120-4135`, the model correctly produced `Desktop\ADVI-TEST\speech.pdf`, and the code replaced it with the raw phrase `"a pdf called speech.pdf"` taken from the utterance.
5. **No progress detection.** In the WhatsApp run, `read_screen` returned roughly the same chat-list text 8 times, and the loop kept going until the hard tool-call cap. Nothing asked "did the state change since my last action?"
6. **Task state leaks between turns.** The WhatsApp turn began with `last_tool='click_web_element' task_status='uncertain'` carried over from the previous YouTube task, which steered tool selection toward the browser.
7. **The verifier calls deterministic successes "uncertain".** Folder created, file opened, memory persisted: all ended as `uncertain`. The user hears a hedge even when the action clearly worked.
8. **Control words depend on the LLM.** "stop" got no response during quota exhaustion.

The fix is not "better OCR". The fix is to **change what the model sees** (a structured accessibility tree), **change what the model controls** (skills and intents, not clicks), and **change what the loop measures** (state deltas and postconditions).

---

## 1. Root Cause Analysis

### 1.1 Why PyAutoGUI + OCR is failing

| Property | PyAutoGUI + Tesseract (current observation) | UIA tree (Windows) | DOM / CDP accessibility tree (browser) |
|---|---|---|---|
| Element identity | None. Only text fragments and pixel boxes | `AutomationId`, `Name`, `ControlType`, runtime ID | Node IDs, roles, accessible names, selectors |
| State (focused, selected, enabled, value) | Not observable | `HasKeyboardFocus`, `IsEnabled`, `ValuePattern.Value`, `SelectionItem.IsSelected` | `document.activeElement`, `aria-*`, `.value`, `video.paused` |
| Off-screen / scrolled content | Invisible | Often present (virtualized lists are partial) | Present |
| Robust to theme, DPI, font, dark mode | No. Tesseract degrades sharply on dark UIs, small fonts, and emoji-heavy chat lists | Yes | Yes |
| Action precision | Coordinates that go stale after any layout shift | `InvokePattern.Invoke()`, `ValuePattern.SetValue()` with no coordinates | `element.click()`, CDP `Input.insertText` |
| Verification | Re-OCR and hope the text differs | Read the property you just changed | Evaluate a JS predicate |
| Cost per observation | 1–3 s OCR plus a large noisy text blob into the LLM | 50–300 ms, compact | 20–100 ms, compact |

**Basic limits of pixel-and-coordinate action (PyAutoGUI):**
- It is **open-loop**. `click(x, y)` only reports that the OS accepted a mouse event. It cannot tell whether the click landed on the intended element, whether a popup stole focus, or whether the window moved. This is why `click` "succeeded" in the log while the UI never changed.
- Coordinates come from a stale observation. Any animation, toast, or lazy-loaded list (WhatsApp's chat list is virtualized and re-sorts by recency) invalidates them.
- Focus is implicit. `type_text` sends keystrokes to whatever has focus, and there is no check that focus is in the WhatsApp message composer rather than the search box. The log shows exactly this: the name and "Hello" were typed, and the screen still showed the search UI.
- DPI scaling on Windows 11 (125%/150%) breaks coordinate mapping between screenshots, pywinauto rectangles, and PyAutoGUI unless the process is declared per-monitor-DPI-aware.

**Basic limits of Playwright/CDP:**
- It only works inside a browser (or Electron/WebView2 apps that expose a debugging port, which you cannot force on third-party apps).
- It is blocked by OS-level dialogs, file pickers, and permission prompts.
- Canvas- and WebGL-rendered content has no DOM semantics.
- Logins, CAPTCHAs, and bot detection can show up. For a personal assistant, a **persistent user profile** avoids most of these. You already use CDP attach, which is the right choice.

**Limits of OCR as a perception engine:** OCR is a *last-resort reader*, not a perception engine. It produces unstructured text with no reading order guarantees, no affordances, and no state. Feeding the result into the LLM ("here are 1,200 characters of WhatsApp chrome, decide what to click") asks the model to do grounding that the OS already did for screen readers.

> ADVI is "Autonomous Desktop for **Visually Impaired**". The same UIA tree that NVDA and Narrator read is the most reliable perception channel on Windows. **If a screen reader can operate an app, ADVI can operate it the same way.** Build on that.

### 1.2 Orchestration defects in this codebase (with evidence)

| # | Defect | Location | Log evidence | Impact |
|---|---|---|---|---|
| D1 | Per-turn tool set chosen by keyword regex | `agent/session.py:302` `_select_model_tools` | Prompts 11, 41–47, 66, 69, 76: "no tool available"; 13 `not in request.tools` rejections | Capability denials; the model invents "I can't" |
| D2 | Protected parameters override model args | `agent/session.py:4120-4135`, `agent/contracts.py:122` | Prompt 15: `path` became `"a pdf called speech.pdf"` | Wrong file written; content_mismatch |
| D3 | Observation = OCR text | `capabilities/desktop/perception.py:60`, `vision.py:103` | WhatsApp and YouTube Music: repeated identical OCR | Model cannot ground or verify |
| D4 | LLM-per-primitive loop | `agent/session.py` loop, cap at `:2569` | 25 calls (WhatsApp), 27 calls (YT Music) | Rate limits, timeouts, tool-cap exhaustion |
| D5 | No state-delta / no-progress detector | (absent) | Unchanged `read_screen` after type+Enter, ×8 | Infinite-style loops |
| D6 | Task context bleeds across turns | `respond.enter` trace shows prior `last_tool` | WhatsApp turn started as browser continuation | Wrong routing |
| D7 | Verifier returns `uncertain` for deterministic ops | `core/verification.py` | 77 `uncertain` records, many on successful file/folder ops | Hedged replies; final status disagrees with reality |
| D8 | Control intents need the LLM | (absent local intent handler) | Prompts 17–22 ("stop", "shut up") | No stop during provider outage |
| D9 | Two parallel stacks | `capabilities/desktop/*` vs `fallback/*` (perception ~1.2k lines each, two CDP drivers) | David fallback fails differently from the native path | Double maintenance; behaviour drift |
| D10 | Strong skills hidden inside primitives | YouTube play/verify logic buried in `_execute_search` / `_ensure_youtube_playing` | Model hand-drives click → inspect → press `k` | The good code is bypassed |

D1, D2 and D6 are **regex-intent heuristics overriding the model**. D3, D4 and D5 are **the wrong abstraction level for perception and action**. Fixing only the second group will not stop the "I can't create PDFs" class of failure.

---

## 2. Industry Benchmarks: How Leading Agents Solve This

### 2.1 Perception approaches

| System | Observation | Action space | Main idea for ADVI |
|---|---|---|---|
| **Anthropic Computer Use** | Screenshots only, downscaled (≈XGA/WXGA) | `screenshot`, `left_click(x,y)`, `type`, `key`, `scroll`, `wait`, `zoom` | The model is *trained* for pixel grounding. It screenshots after every action and self-verifies. Anthropic recommends low resolution and asking the model to verify each step. Pure vision works **only with a grounding-trained VLM**. Tesseract plus a general LLM is not equivalent. |
| **OpenAI Operator / CUA** | Screenshots in a sandboxed browser | Mouse/keyboard | Hands control to the user for logins/CAPTCHAs and asks for confirmation before consequential actions (send, purchase). |
| **OSWorld** (benchmark) | Compares screenshot, a11y tree, screenshot+a11y, and Set-of-Mark | pyautogui code | The original paper found **a11y tree or screenshot+a11y beat raw screenshots** for general models, and the main failure modes were **GUI grounding** and **operational knowledge**. Humans score ~72%; agents were far below that at launch. |
| **WebArena / VisualWebArena** | Accessibility tree with element IDs (+ SoM screenshots) | `click [id]`, `type [id] [text]` | Agents act on **IDs from the observation**, never on coordinates or free-text labels. |
| **Set-of-Mark prompting** (Microsoft) | Screenshot with numbered boxes over detected elements | "click 17" | Turns grounding into multiple choice. This is how you combine vision with structure. |
| **OmniParser** (Microsoft) | Pure-vision parser: icon/element detector + captioner → structured element list | Feeds SoM | The fallback for apps with poor accessibility (canvas, games, some Electron apps). |
| **Microsoft UFO / UFO²** (Windows) | **UIA control tree + screenshot**, with hybrid vision detection for controls UIA misses | UIA control actions **plus native app APIs** (COM for Office, etc.) | The closest reference architecture for ADVI: HostAgent picks the app, AppAgent operates it, **API first, GUI second**. |
| **UI-TARS / UI-TARS-1.5** (ByteDance) | Screenshot-only, end-to-end native GUI model | Unified action space, explicit "thought" plus reflection | Strong open-weights grounding model. Usable as a *grounding service* ("where is X?") that returns coordinates, not as the whole brain. |
| **Agent S / S2** (Simular) | Screenshot + a11y tree, with specialized grounding experts | Hierarchical manager → worker | Separating **planning from grounding** and using experience memory both improve success. |
| **Adept ACT-1 / Fuyu** | Screenshots / DOM | Learned actions | Historical. It showed the "action transformer" idea, but Adept moved away from a general agent product. |
| **Playwright MCP / browser-use** | **Accessibility snapshot with `ref` IDs** (Playwright MCP) or indexed interactive DOM elements (browser-use) | `click(ref)`, `type(ref, text)` | Cheap, deterministic browser perception with no screenshots needed. |

### 2.2 The pattern that all mature agents converge on

```
            ┌──────────────────────── Perception Router ─────────────────────────┐
            │ 1. Native API / app-specific adapter      (Gmail API, COM, URIs)   │
            │ 2. Structured tree with element IDs       (CDP AX/DOM, UIA)        │
            │ 3. Vision grounding with Set-of-Mark      (OmniParser / VLM)       │
            │ 4. OCR                                    (read-only text fallback)│
            └────────────────────────────────────────────────────────────────────┘
             Unified observation = {app, window, focus, elements[id, role, name,
                                    state, bbox, source], screenshot_ref?, hash}
```

- **Grounding is a separate service from planning.** The planner says "click the chat titled Anurodh". A grounder resolves that to a stable element (UIA runtime ID, DOM node, or SoM box) and returns *one* target or *ambiguous* with candidates.
- **Actions target element IDs, not coordinates.** Coordinates only come from the vision tier, and are re-grounded immediately before the click.
- **Every action has a postcondition** that is checked against the *next* observation (focus moved, value changed, URL changed, element appeared or disappeared).

### 2.3 Workflow execution (Q2.1): skills vs. generic GUI

Production agents use **both, in layers**:

1. **Tier A: Native APIs / deep links / URIs.** Gmail API, Outlook Graph, `spotify:` URIs, `https://www.youtube.com/results?search_query=`, `whatsapp://send?phone=<E164>&text=<msg>` (WhatsApp Desktop registers this protocol and opens the chat with the text prefilled), `ms-settings:` pages, `shell:AppsFolder\<AUMID>`. Deterministic, quick, and verifiable.
2. **Tier B: Scripted app skills** (UFO's "app agent" / RPA-style). A Python routine per high-frequency app that uses UIA/CDP with known selectors, built-in waits, and postconditions. It exposes one **typed tool** to the LLM: `whatsapp_send_message(contact, text)`.
3. **Tier C: Generic GUI loop** (observe → ground → act → verify) for the long tail. This is where the LLM drives step by step.

The LLM's job is **intent and slot filling**, plus choosing Tier C only when no skill matches. Your Gmail flow already proves this works: 3 calls, verified, every time. WhatsApp should look the same.

### 2.4 App-state verification (Q2.2)

State-of-the-art agents treat app state as a **precondition check run by code before the action**, not something the LLM infers:

| Precondition | How to check deterministically on Windows |
|---|---|
| Installed? | AppX: `Get-AppxPackage *WhatsAppDesktop*` (you already enumerate AppX in `watchtower.py:475`); Win32: registry Uninstall keys (`watchtower.py:891`) |
| Running? | `psutil` process name (`WhatsApp.exe` / `WhatsApp.Root.exe`) + top-level window by AUMID/class |
| Foreground & ready? | `GetForegroundWindow` == target hwnd **and** a known landmark exists in UIA (e.g., the search edit control) |
| Logged in? | Landmark probe: chat list present ⇒ logged in; QR-code / "Link with phone" text ⇒ logged out. Web: `canvas[aria-label*="Scan"]` vs `#pane-side` |
| Desktop vs Web? | Policy, not a guess: prefer Desktop if installed **and** logged in; otherwise Web in a persistent Chrome profile if logged in; otherwise ask the user to link the device |

Return these as a typed `AppState` (`not_installed | not_running | starting | logged_out | ready | blocked_by_dialog`) and branch in code.

### 2.5 Progressive voice feedback (Q2.3)

The pattern is an **event bus with a non-blocking narrator**:

- Skills and the loop emit structured events: `step_started("Opening WhatsApp")`, `step_done`, `needs_confirmation`, `blocked(reason)`.
- A narrator task consumes them on its own thread/queue, debounces (do not speak events under ~700 ms apart; merge or skip them), and speaks only **subgoal boundaries**, never individual clicks.
- Speech must be **barge-in interruptible**. The voice front end watches for a local wake/stop phrase and cancels both TTS and the executor through a shared `CancellationToken`.
- "Stop", "cancel", "wait", "yes", and "no" are handled by a **local intent matcher before any LLM call** (fixes D8).
- For visually impaired users, also use short **earcons** (tick for step done, tone for waiting on the user) so progress is audible without long speech.

### 2.6 Failure detection and self-correction (Q4)

What leading systems do:

- **Postconditions per action** (WebArena/OSWorld evaluators, UFO's per-step validation): did the expected property change?
- **State hashing / no-progress detection.** Hash the normalized observation (focused element, window title, URL, key landmarks). If an action leaves the hash unchanged and its postcondition is unmet, that is a *failed action*, even if the OS accepted the input.
- **Loop detection.** The same (action, target) pair repeated, or an A→B→A→B oscillation, triggers a mandatory replan.
- **Budgets per subgoal**, not just per task. For example, "open chat" gets 4 actions. When exhausted, escalate to replanning, then to a different tier, then to the user.
- **Reflection** (Reflexion; UI-TARS reflective traces; Agent S): on failure, the model gets a compact *diff* (expected vs. observed) and must pick a *different* strategy. Retrying the same thing is not allowed.
- **Plan-and-execute with checkpoints.** A planner produces subgoals with success criteria, an executor attempts each one, and a verifier gates progression. Replans happen at the subgoal level only.

### 2.7 Undo / reversibility (Q4.3)

Honest industry answer: **GUI side effects are generally not undoable.** A sent message, a submitted form, or a played track cannot be rolled back. Practice therefore relies on *preventing* irreversible mistakes and making the reversible ones transactional:

| Action class | Strategy |
|---|---|
| Irreversible external (send message, send email, delete remote, purchase) | **Confirm-before-commit**: show and speak the exact payload ("Send 'Hello' to Anurodh on WhatsApp?"). This is already done for Gmail; apply it everywhere. |
| Local filesystem writes | **Journaled operations with inverses**: create → delete (to Recycle Bin), move → move back, overwrite → keep a pre-image in `runtime/undo/`. Never hard-delete; use `send2trash`. |
| App UI navigation (open window, navigate tab) | "Compensating actions": close what was opened, `history.back()`, restore the previous foreground window |
| System settings | Snapshot the prior value (registry/`ms-settings` state) before changing; offer an undo |
| Unknown | Treat as irreversible and confirm |

Claude Code's file checkpoints, Operator's confirmation gates, and UFO's "safeguard" of sensitive actions all follow this model.

---

## 3. System Awareness (Q3)

### 3.1 How fast indexers work

- **Everything / WizTree** don't walk directories. They read the **NTFS Master File Table** directly (needs admin), building a full index of millions of entries in seconds. Then they follow the **USN Change Journal** for real-time updates.
- **Practical options for ADVI, in order:**
  1. **Use Everything's SDK / `es.exe` CLI** if Everything is installed. Sub-10 ms queries over the whole disk, no admin needed at query time. This is by far the highest-leverage choice.
  2. **Windows Search index** via `SystemIndex` (ADODB/OLE DB `SELECT System.ItemPathDisplay FROM SystemIndex WHERE …`). Built in, already indexes the user's libraries.
  3. **Your own scoped index** (SQLite + FTS5) of user folders (Desktop, Documents, Downloads, Music, Pictures, Videos, OneDrive), kept fresh with `watchdog` (already a dependency) or USN journal reads. Store path, name, extension, kind (audio/video/doc), size, and mtime.
- The prompt 71 failure ("GODS" search timed out walking the user scope) is exactly what an index removes.

### 3.2 App and window inventory

You already have a good AppX + registry enumerator in `watchtower.py`. Add:
- **Start Menu shortcuts** (`%ProgramData%\Microsoft\Windows\Start Menu\Programs`, `%AppData%\…\Start Menu\Programs`). These are the names users actually say.
- **Alias table**: `{"whatsapp": AUMID 5319275A.WhatsAppDesktop_…!App, "word": WINWORD.EXE, …}` with fuzzy matching. Persist it, refresh it on startup and on AppX install events.
- **Live window list** via `EnumWindows` + process name + AUMID (`SHGetPropertyStoreForWindow`) on a ~1 s refresh, cached.

### 3.3 Exposing awareness to the LLM without flooding context

The standard pattern is **summaries in context, details behind tools**:

- **Always in the system/context block (≤ ~300 tokens):** foreground app and window title, a list of open windows (titles only, max ~10), the active browser tab URL/title, battery, current time, "recent artifacts" (last 5 created/opened files with full paths), and the current task's pending slots.
- **Retrieval tools for everything else:** `find_files(query, kind?, scope?, limit=10)`, `list_dir(path, limit=50)`, `find_app(name)`, `system_status(fields=[battery, storage, cpu, memory, network])`. Results are **top-k, ranked, and paginated**, never raw dumps.
- **Artifact registry** for references like "that PDF" and "that folder": every created or opened path is pushed onto a typed stack (`kind`, `path`, `created_at`, `turn_id`). Resolve anaphora against it **in code** before the LLM sees the request. That fixes prompts 6, 7, 16, and 71.

---

## 4. Recommended Architecture for ADVI

### 4.1 Target architecture

```
 Voice In ──► Local Control Intents (stop/cancel/yes/no/repeat) ──┐
   │                                                               │ (bypass LLM)
   ▼                                                               ▼
 Router LLM (one call): intent + slots, or "generic_task"   ──►  Executor
   │                                                               │
   ├─ Skill match ──► Skill (Tier A/B): preconditions → steps → postconditions
   │                     │  emits progress events ─────────────────┼─► Narrator (TTS queue, barge-in)
   │                     └─ on failure ──► Tier C with context ────┤
   │                                                               │
   └─ generic_task ──► Tier C Loop: Observe → Ground → Act → Verify → (Reflect/Replan)
                           │
                           ▼
                 Unified Perception Engine
                 (CDP AX/DOM │ UIA tree │ SoM+VLM grounding │ OCR)
                           │
                 System Awareness Service (app index, window list, file index, artifact stack)
                 Action Journal (reversible ops, confirmation gates)
```

### 4.2 Roadmap

Ordered by **impact per unit of effort**, based on the log.

#### Phase 0: Stop the bleeding (1–3 days, no new subsystems)

1. **Delete regex tool gating (D1).** Replace `_select_model_tools` with a **stable tool surface**: about 20–30 tools with good descriptions, the same on every turn. Cache that prefix. Groq and Gemini both handle this size well. If you must shrink it, select by *active skill domain* chosen by the router LLM, never by keyword regex on the utterance.
2. **Make protected parameters validators, not overriders (D2).** Only *explicitly confirmed* values (an email recipient after "yes") may override. Otherwise, if the model's argument differs from the extracted value, keep the model's value and log the difference. Paths must always be normalized through `Path.resolve()` and checked: the parent exists, and the parent equals the referenced folder from the artifact stack.
3. **Reset task state per new user goal (D6).** A new imperative utterance that isn't a follow-up ("try again", "that", "it", "yes") starts a fresh `TaskState` with `last_tool=None`.
4. **Local control intents (D8).** Regex/keyword handling for `stop|cancel|wait|shut up|never mind|yes|no|repeat` before any provider call.
5. **Verifier semantics (D7).** Deterministic ops with an observable postcondition (file exists with size > 0, folder window open, SQLite row present, Gmail `SENT` label) return `verified`, not `uncertain`. Reserve `uncertain` for GUI actions without an observable delta.
6. **Honest capability narration.** Add one system-prompt line plus a check: the model may never claim a capability is missing if it exists in the registry. If a needed tool isn't exposed, that is a routing bug to log, not a user-facing "I can't".

#### Phase 1: Skills for the high-frequency workflows (1–2 weeks)

Define a `Skill` contract:

```python
class Skill(Protocol):
    name: str                      # "whatsapp.send_message"
    slots: type[BaseModel]         # pydantic: contact: str, text: str
    reversible: bool               # False → requires confirmation
    def preconditions(self, env) -> AppState: ...
    def plan(self, slots) -> list[Step]: ...        # deterministic steps
    def verify(self, env, slots) -> Verification: ...
```

Each `Step` has `action`, `target` (selector/UIA query), `postcondition`, `timeout`, and `progress_message`.

**First skills to build, chosen from the log:**

| Skill | Implementation | Verification |
|---|---|---|
| `whatsapp.send_message(contact, text)` | Resolve the contact to a phone number (Google People API, already authed, or a local alias table) → `whatsapp://send?phone=…&text=…` → wait for the composer → confirm with the user → press Enter. Fallback: UIA search box → type → select the exact-match row → composer. | Last outgoing message bubble text == `text` (UIA/OCR on the conversation pane) |
| `whatsapp.open_chat(contact)` | Same, without sending | Conversation header name == contact |
| `media.play(query, service=youtube|ytmusic|local)` | **Promote the existing** `_youtube_play_first_result` / `_ensure_youtube_music_playing` into one tool. URL-based search (`/results?search_query=`, `music.youtube.com/search?q=`) | `video.paused === false && currentTime` increasing (already implemented at `browser/executor.py:871`) |
| `files.create_document(kind, path, content_spec)` | One call; path resolved against the artifact stack | File exists, page count, text contains the title |
| `files.find / open / read` | Backed by the index (Phase 3) | Path exists; opened window title contains the filename |
| `system.status(fields)` | psutil + `shutil.disk_usage` | n/a (read-only) |
| `email.*` | Already good; keep it | Gmail `SENT` |

This removes roughly 90% of the LLM round trips in the log's failing tasks: WhatsApp goes from 25 calls to about 2 (router + confirmation), and YouTube Music from 27 to 1.

#### Phase 2: Unified perception engine for Tier C (2–3 weeks)

1. **New observation contract** (replace `read_screen` as the model-facing observation):

```json
{
  "app": "WhatsApp", "window": "WhatsApp", "hwnd": 13042, "source": "uia",
  "focused": "e12",
  "elements": [
    {"id": "e7",  "role": "Edit",     "name": "Search or start a new chat", "state": ["focusable"]},
    {"id": "e12", "role": "ListItem", "name": "Anurodh, Hello, 9:41 PM", "state": ["selected"]},
    {"id": "e30", "role": "Edit",     "name": "Type a message", "state": ["focusable"]}
  ],
  "truncated": false, "state_hash": "a91f…"
}
```

   - Prune to **interactive and landmark controls only** (Edit, Button, ListItem, Hyperlink, MenuItem, TabItem, CheckBox, plus headers). Cap at about 60–80 elements, ranked by proximity to the focus or task terms.
   - IDs are session-scoped handles mapped to UIA `RuntimeId` / CDP `backendNodeId`.
2. **Browser source**: CDP `Accessibility.getFullAXTree` or a browser-use-style interactive-element extraction. You already attach via CDP; add an AX-snapshot mode to `inspect_web_page`.
3. **Vision tier**: only when the UIA/AX element count is below a threshold or the needed target isn't found. Run OmniParser or a grounding VLM (UI-TARS-1.5-7B locally, or a hosted VLM), draw **Set-of-Mark** numbers on a downscaled screenshot, and return the same `elements[]` shape with `source: "vision"` and bboxes.
4. **OCR**: keep it only for *reading content* (e.g., reading a message aloud, a PDF image), never for grounding.
5. **Actions take element IDs**: `click(id)`, `set_text(id, text)` (UIA `ValuePattern.SetValue` → fallback focus + `type`), `invoke(id)`, `select(id)`, `scroll(id, dir)`. Coordinates are only allowed for `source: "vision"` elements.
6. **Make the process per-monitor-DPI-aware** at startup (`SetProcessDpiAwarenessContext(-4)`) so UIA rects, screenshots, and PyAutoGUI coordinates agree.
7. **Merge `fallback/perception.py` and `capabilities/desktop/perception.py`** (D9). Keep one engine and one CDP driver.

> WhatsApp Desktop on Windows is now a WebView2-hosted app. Chromium exposes its web content to UIA when an accessibility client queries it, so the chat list, search box, and composer usually appear as named UIA elements. Verify this on your machine with Accessibility Insights for Windows or `inspect.exe`. If coverage is poor, the skill should prefer the `whatsapp://` URI route or WhatsApp Web via CDP, where the DOM is fully accessible.

#### Phase 3: System awareness service (1 week)

1. **File index**: Everything SDK/`es.exe` if present → Windows Search `SystemIndex` → your own SQLite FTS5 index of user folders, kept fresh by `watchdog`. Classify each entry by `kind` from its extension (audio, video, document, image, archive).
2. **App index**: AppX + Uninstall registry + Start Menu `.lnk` + alias table, persisted to SQLite and refreshed at startup and on change.
3. **Window tracker**: a 1 s cached `EnumWindows` snapshot with process, AUMID, title, and foreground flag.
4. **Artifact stack** in `TaskState`/session memory: every created or opened file or folder, with typed resolution of "that PDF", "that folder", or "the one you just made".
5. **Context block** (≤300 tokens) injected each turn, as described in §3.3.

#### Phase 4: Execution loop with verification, recovery, and reversibility (1–2 weeks)

```
for subgoal in plan:                                  # planner emits subgoals + success criteria
    budget = subgoal.budget  (e.g., 4 actions)
    while not subgoal.satisfied(obs):
        if budget == 0 or loop_detector.tripped():  → reflect()  → replan(subgoal) | switch_tier | ask_user
        action = llm.next_action(obs, subgoal)        # targets element IDs
        if action.irreversible: await confirm(action) # voice yes/no via local intent
        journal.record_pre_state(action)
        execute(action)
        obs2 = perceive(wait_for_settle=True)          # UIA/DOM "idle" or 300–800 ms settle
        if obs2.state_hash == obs.state_hash and not action.postcondition(obs2):
            mark_failed(action, reason="no_state_change")
        obs = obs2; budget -= 1
    narrator.emit(step_done(subgoal))
```

- **No-progress detector**: same `state_hash` after an action whose postcondition is unmet counts as a failure. Two consecutive no-progress actions force reflection.
- **Loop detector**: a ring buffer of `(action, target_id)`. Repetition or oscillation trips it.
- **Reflection prompt**: "Expected X, observed Y (diff). Previous strategies tried: [...]. Choose a *different* strategy or report blocked." The output must name the new strategy.
- **Escalation ladder**: retry with re-grounding → alternate tier (skill ↔ generic, Desktop ↔ Web) → ask the user one specific question → stop with a precise account.
- **Final report generated from the journal, not from the LLM's memory**: "Opened WhatsApp ✓, opened Anurodh's chat ✓, message not sent: composer didn't accept text." This fixes the "tool succeeded but the reply says uncertain" mismatch.
- **Action journal**: every state-changing op records its inverse. `undo` pops the journal (files: trash/restore from pre-image; windows: close; browser: back). Irreversible ops are flagged and gated by confirmation.

#### Phase 5: Provider resilience (in parallel)

- Your free-tier Groq/Gemini limits are an **amplifier**: 89 TPM rate-limit records and 13 quota exhaustions. Phases 1–2 cut calls per task by 5–10×, which is the real fix.
- Additionally: use **one** provider per turn (don't fail over mid-tool-loop with a different tool contract), use prompt caching for the stable tool surface, and enforce a token budget on observations (the pruned element list).
- Consider a local small model (e.g., via Ollama) for the router and local intent classification, so "stop" and simple routing work offline.

### 4.3 Evaluation harness (do this alongside Phase 0)

You have no denominator right now (as the forensic report notes). Build a small **replayable task suite**, in the style of OSWorld/WebArena:

- 30–50 tasks taken from this log: "send Hello to Anurodh on WhatsApp", "play Overflow on YouTube Music", "create speech.pdf in ADVI-TEST", "open that pdf", "battery and storage", "play any audio from Music".
- Each task has a **programmatic checker** (file exists with content, `video.paused === false`, last WhatsApp bubble text, etc.).
- Track success rate, mean LLM calls per task, mean wall time, and the false-"I can't" rate. Run it on every release. `scripts/analyze_runtime_log.py` can feed it.

---

## 5. Direct answers to your questions

**1.1 PyAutoGUI vs Playwright limits:** see §1.1. Pixel actions are open-loop and identity-less. DOM/a11y actions are identity-based and verifiable but limited to what exposes a tree.
**1.2 Reliable perception on dynamic layouts:** act on element identities from a structured tree, re-observe after every action, and use vision grounding (SoM) only as a fallback. Pure-vision agents (Computer Use, UI-TARS) work because their models are *trained* for grounding, not because of OCR.
**1.3 Unified perception:** a tiered router (API → CDP/UIA tree → SoM vision → OCR) that emits one observation schema with element IDs and a state hash (§2.2, §4.2 Phase 2).
**2.1 Generic GUI vs schemas:** typed skills and APIs for high-frequency apps, with generic GUI only for the long tail (§2.3).
**2.2 App-state verification:** deterministic precondition probes returning a typed `AppState` (§2.4).
**2.3 Voice feedback:** an event bus, a non-blocking narrator at subgoal boundaries, barge-in, and local control intents (§2.5).
**3.1 Fast inventory:** MFT/USN-based indexers (Everything SDK) or Windows Search, plus AppX/registry/Start Menu app indexes and a cached window tracker (§3).
**3.2 Exposing awareness:** a compact summary block plus top-k retrieval tools and an artifact stack for anaphora (§3.3).
**4.1 Detecting failure:** postconditions, state-hash deltas, loop detection, and subgoal budgets (§2.6).
**4.2 Replan/self-correct:** plan-and-execute with subgoal criteria, reflection on an expected-vs-observed diff, and an escalation ladder (§4.2 Phase 4).
**4.3 Undo:** only for journaled local operations. External side effects are prevented with confirm-before-commit rather than undone (§2.7).
