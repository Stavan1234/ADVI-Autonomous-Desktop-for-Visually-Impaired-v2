# ADVI: Architecture Review, Industry Patterns, and Overhaul Roadmap

**Review scope:** ADVI 0.3.4 source snapshot with runtime fingerprint `6eeb98fa9f0b9d7d`; runtime log `advi(20261013-170548).log`; current primary source under `src/advi/`.

**Purpose:** Explain the observed failure modes, compare relevant industry patterns, and lay out a staged architecture roadmap. This is an architectural review; it does not imply changes have been implemented.

---

## Executive summary

ADVI is not merely PyAutoGUI with OCR. The inspected implementation already has multiple layers:

- Windows UI Automation (`pywinauto`, `backend="uia"`) as the first choice for native desktop target resolution.
- A screenshot/Tesseract OCR fallback for visible text and coordinate candidates.
- A custom Chrome DevTools Protocol (CDP) browser adapter with URL, DOM-ready, element, and stable-state waits.
- A `TaskState`/`AgentSession` orchestration layer, dynamically selected tool sets, tool contracts and guard, tool runner, execution engine, verifier, local continuation controller, perception controller, and watchdog.
- A Windows environment watchtower for Start Menu/Start Apps/registry/AppX app inventory, process polling, bounded filesystem metadata indexing, and filesystem change events.
- SQLite-backed memory, lexical retrieval, optional semantic reranking, and structured experience lessons.
- Native Gmail API operations, which are among the clearest successful end-to-end workflows in the log.

The failure pattern is **coordination across boundaries**: request intent does not consistently map to the right tool surface; target and path information can change before execution; GUI actions sometimes succeed as input events without proving the intended state transition; stateful workflows issue repeated low-level actions without progress; and a failed provider call can prevent final narration even after a local operation has succeeded.

The best next architecture is a **layered hybrid agent**: use direct APIs for supported workflows, semantic DOM/Playwright-style interaction for browser work, Windows UI Automation for native controls, and OCR/vision/coordinates as a last-mile fallback. Wrap common workflows in typed application-specific adapters and explicit state machines. Keep the LLM responsible for language understanding and ambiguous decisions, but let deterministic code own resource identity, action preconditions, waits, postconditions, retry limits, confirmation, and evidence.

No architecture can guarantee every arbitrary GUI task will work. Industry benchmarks show that long-horizon computer-use tasks remain difficult, and vendor implementations use different trade-offs. The purpose of this roadmap is to make ADVI's success and failure modes measurable, bounded, and explainable.

## 1. Root-cause analysis grounded in ADVI's log and code

### 1.1 What the log says quantitatively

The companion forensic report lists 80 user prompts across four sessions. Its counts are **log-event counts**, not an overall completion-rate calculation:

- 41 task-created events.
- 213 ToolRunner starts and 212 matching exits; one tool start lacks a matching exit in the recorded trace.
- 89 Groq token-per-minute limit events across 22 turns.
- 13 Groq quota-exhaustion events across 9 turns.
- 23 Groq authentication-failure events across 11 turns.
- 8 Gemini 20-second timeout events across 7 turns.
- 13 provider complaints that the requested tool was not in `request.tools`, across 6 turns.
- 41 `content_mismatch` verification records across 3 document-creation tasks (not 41 unique bad files).
- 77 `uncertain` and 20 `failed` verification-status records.

These numbers show both external service failures and local coordination problems. They do **not** support a statistically valid end-to-end success rate because there is no fixed test denominator and some events are repeated within a single task.

### 1.2 Request -> tool surface mismatch

**Code locations:** `src/advi/agent/session.py`, `src/advi/agent/provider_session.py`, `src/advi/tools/registry.py`, `src/advi/tools/guard.py`.

The primary agent narrows tools according to the current prompt and task context. Consequently, a tool existing in the registry does not imply that it is present in a specific provider request. The log repeatedly shows a model attempting a call omitted from that request. Provider-native sessions also have their own tool-surface lifecycle, and the source includes logic to refresh/recreate native sessions when the tool declarations change. The observed failures indicate that, despite that code, the tool contract is not consistently coherent end-to-end under all turn/failover paths.

This is not a GUI perception failure: a request rejected as “tool was not in request.tools” never reaches the underlying desktop or file handler.

A related symptom appears in the multi-turn PDF request on 13 October. ADVI asks what a PDF should contain, receives a topic, then treats the topic as a normal writing request. After the user clarifies that it should be saved as a PDF, the model claims PDF creation is unavailable even though the capability exists and had worked earlier. The pending output-format intent is not preserved robustly across the turns.

**Diagnosis:** Tool availability, pending task intent, schema validation, and provider session/tool declarations behave like loosely synchronized systems rather than one authoritative contract.

### 1.3 Target identity and protected arguments diverge

**Code locations:** `src/advi/agent/session.py`, `src/advi/agent/contracts.py`, `src/advi/core/verification.py`, `src/advi/capabilities/files/executor.py`.

In the `speech.pdf` case, the original call record contains the exact intended path inside `Desktop\\ADVI-TEST\\speech.pdf`. The execution layer subsequently receives the literal phrase `a pdf called speech.pdf` as the path, creates a file on the Desktop under that unintended name, and fails content verification. This is strong evidence of a target/argument integrity failure before the operation completes.

Elsewhere, recent file/folder references sometimes work and sometimes trigger new searches in the wrong scope. The main issue is not that ADVI has no artifact concept; the release has recent-artifact aliases and argument protection. It is that those mechanisms do not guarantee every continuation uses the exact verified identity in every provider/task transition.

**Diagnosis:** Task intent, resolved entities, canonical target paths, and executed arguments need a single source of truth with explicit provenance. Natural language should select the path once; subsequent layers should not reconstruct it from prose.

### 1.4 UI action completion is confused with task completion

**Code locations:** `src/advi/capabilities/desktop/executor.py`, `src/advi/capabilities/desktop/perception.py`, `src/advi/core/perception_controller.py`, `src/advi/core/verification.py`, `src/advi/core/execution_watchdog.py`.

ADVI already has meaningful execution verification. The verifier distinguishes `verified`, `uncertain`, and `failed`, and it intentionally avoids marking a successful action as failed unless it finds evidence of failure. That distinction is correct in principle.

However, the log shows that click/type/key operations can succeed as input operations while the higher-level goal remains unverified. The 13 October WhatsApp workflow issues many desktop actions while observations continue to show the chat-list/search UI, and it eventually reaches the safe tool-call limit without evidence that the intended chat received a message. The final YouTube Music workflow repeatedly types a URL and presses Enter while screen text remains associated with Google Search/Chrome profile content; no target page or playback state is verified.

**Diagnosis:** The agent does not consistently turn observation into a decisive state transition. It can mistake “the input event was dispatched” for “the intended application state now exists,” then continue with actions whose preconditions have not been proven.

### 1.5 Screenshots/OCR exist, but ADVI is not a screenshot-grounded VLM agent

**Code locations:** `src/advi/capabilities/desktop/perception.py`, `src/advi/capabilities/desktop/vision.py`, `src/advi/core/ocr.py`, and `src/advi/fallback/vision.py`.

The native desktop perception order is **Windows UI Automation first, vision fallback second**. Chrome browser actions also have a CDP fast path. The vision layer really does call `pyautogui.screenshot()` and saves a PNG under `runtime/screenshots/`; Tesseract extracts text and bounding boxes locally. Therefore it is inaccurate to describe the whole current architecture as raw coordinate clicking alone.

It is also inaccurate to imply the primary model currently receives raw screenshot images for direct visual grounding. The inspected native path primarily exposes OCR-derived text/targets and UI/DOM observations to the agent; it is not a configured screenshot-to-VLM grounding loop comparable to a screenshot-native model such as UI-TARS or commercial computer-use agents.

OCR is useful for text and rough coordinates, but it does not provide the semantic roles, hierarchy, enabled state, focus state, or supported action patterns that UI Automation and browser DOM expose. It struggles with icons, overlapping elements, animations, scaling, low contrast, and visually similar targets. These limits are structural, not something an OCR prompt can fully remove.

### 1.6 Browser automation is a custom CDP adapter, not Playwright

**Code locations:** `src/advi/capabilities/browser/cdp.py`, `src/advi/capabilities/browser/executor.py`, and the Chrome fast path in `desktop/perception.py`.

ADVI uses CDP/WebSocket with custom search-field discovery, navigation, result clicking, URL waits, DOM-ready waits, element waits, stable-tab-state polling, and playback verification. This is stronger than coordinate-only browser automation, but it is a home-built browser interaction layer rather than Playwright's locator/actionability machinery.

The log's browser problems therefore do not prove CDP itself is a poor idea. They show that the target/session identity, model-visible tool surface, transition conditions, and outcome verifier are not consistently aligned. Mixing browser actions into a desktop interaction route can also mean the model receives desktop controls when the current task needs browser-specific DOM controls.

**Diagnosis:** One browser controller must own the browser session, target tab, semantic element references, waiting, action execution, and verification. It must not allow a model to silently switch between unrelated Chrome tabs or between desktop and browser tool surfaces.

### 1.7 The watchdog is a brake, not recovery intelligence

**Code location:** `src/advi/core/execution_watchdog.py`.

The watchdog bounds task duration, number of actions, failure count, and identical action repetition. That is useful defensive behavior. It can stop a runaway loop but it cannot itself infer a better target or choose a recovery strategy. In the WhatsApp task, exhausting the action budget is a containment outcome, not a recovered task.

**Diagnosis:** Loop prevention exists; a uniform, evidence-based recovery state machine remains the missing behavioral layer.

### 1.8 System awareness is deliberately scoped, not a full-disk search index

**Code location:** `src/advi/environment/watchtower.py`.

ADVI's environment watchtower discovers applications using sources such as Start Menu shortcuts, Windows `Get-StartApps`, AppX packages, and uninstall registry keys. It tracks running process names and polls on a configured interval. It also indexes filesystem metadata under bounded roots and listens for file events. In the reviewed implementation, full-filesystem inventory is disabled by default; the code indexes a limited set of user roots such as Desktop, Documents, and Downloads, with a cap on entries per root. Targeted filename search can query Windows Search before falling back to filesystem search.

This is not intended to work like Everything's whole-volume NTFS name/path index. Higher privileges do not automatically provide a fast global index or make UI state more reliable. Admin rights should not be the default solution; applications running elevated may require matching privilege to inspect, but routine ADVI operation should remain least-privilege.

**Diagnosis:** App discovery, running-process detection, open-window inspection, folder metadata indexing, and content indexing are separate concepts but are sometimes treated as one “system awareness” capability.

### 1.9 Memory stores useful data, but it is not a substitute for live task state

**Code locations:** `src/advi/memory/retriever.py`, `src/advi/memory/experience.py`, `src/advi/memory/long_term.py`, `src/advi/core/context_router.py`.

The memory system supports SQLite-backed durable facts, lexical search, optional semantic reranking, recent candidates, and structured execution lessons. These are useful for personalization and lessons across tasks. In the log, a memory write can be persisted and verified, yet the final response is garbled/uncertain; persistent data storage and task completion are separate concerns.

Memory should not be used to infer live things that must be observed now, such as which WhatsApp account is logged in, which Chrome tab is active, whether playback is running, or the exact current path of a file. Those belong in fresh application/task state.

### 1.10 Provider instability is an amplifier, not the sole root cause

The log includes rate limits, quota exhaustion, credential failures, and interactive timeouts. The first starts have no configured Groq/Gemini keys; the next starts find a three-key Groq pool. During later workflows some requests time out or fail with authentication/quota errors.

These are real availability constraints. They explain why a task may stop between actions even if local work already occurred. They do not explain the incorrect path argument, unverified UI transitions, malformed tool argument types, or contradictory claims about whether PDF creation is supported.

---

## 2. What current agent systems and industry patterns actually do

There is no single industry-standard perception stack. Current systems use different approaches depending on whether they target a browser, a native desktop, or an unconstrained visual interface. The transferable principle is to select the strongest available interface for each surface and provide one coherent action/verification loop.

### 2.1 PyAutoGUI: reliable low-level input, weak semantic targeting

PyAutoGUI dispatches mouse and keyboard input and can capture screenshots. Coordinates do not tell the automation why a control is there, whether it is enabled, whether focus belongs to it, or whether another UI element is covering it. Absolute positions can break with display scaling, window movement, browser zoom, layout changes, pop-ups, or different screen sizes.

It remains useful for:

- keyboard shortcuts that are stable and unambiguous;
- fallback interactions when the application exposes no useful accessibility/API surface;
- small portions of a visual workflow after a target has been grounded;
- screenshot capture.

It should not be ADVI's default method for every field, button, list or browser result. Sending a click is not evidence that the target action happened.

### 2.2 Playwright/DOM: preferred for web pages when the browser can be controlled

Playwright locators describe elements semantically, for example by role, label, text, or a stable test ID. Its documented actionability checks wait for the target to be unique, visible, stable, enabled, and able to receive events; actions fail on timeout rather than being treated as successful just because a click was sent. citeturn129554search0turn212311search6

This makes browser work less fragile than coordinate targeting, although it is not infallible. Web apps can use shadow DOM, iframes, virtualized lists, inaccessible custom widgets, or labels that change. Dynamic lists must still be awaited, and overly specific CSS selectors can be brittle. A DOM action can also succeed technically while the business outcome is wrong, so postconditions are still required.

ADVI has a custom CDP backend, which already provides some waits. The recommendation is **not to run a second browser controller alongside it**. First standardize the interface and state ownership. Then either keep CDP and add semantic locators/actionability around it, or migrate the adapter to Playwright behind the same interface after a parity test suite exists.

### 2.3 Windows UI Automation: semantic native desktop access

Microsoft UI Automation exposes desktop elements in a tree and defines properties, control types, control patterns, and events. These enable automation to inspect or invoke controls through their supported semantics rather than coordinates. It is used by accessibility technology and automated testing. citeturn158633view3turn129554search2

For ADVI's Windows target, UIA should be the normal native-app path:

- identify the top-level window by process identity plus HWND/title and application profile;
- inspect a bounded subtree beneath that window, not the entire desktop tree every turn;
- choose controls by automation ID, control type, accessible name, value, enabled/focused state, and parent scope;
- use supported control patterns (Invoke, Value, Selection, Expand/Collapse, etc.) where available;
- wait for relevant UIA state/events where possible and verify the resulting value/state.

UIA is not universal. Some custom-rendered, canvas, game, remote-desktop, or badly labeled controls expose little useful structure. Keep OCR/vision as a fallback, and use MSAA/other access paths for applicable legacy controls.

### 2.4 Screenshot-native agents: Anthropic computer use, OpenAI CUA, and UI-TARS

Anthropic's computer-use tool is a client-side loop: the model requests screenshot/mouse/keyboard operations, the application executes them, returns per-action results and screenshots, and the model continues. For browser-only tasks Anthropic distinguishes its browser-use tool from full desktop computer use. Its docs recommend sandboxing, careful treatment of untrusted screen/page contents, and human confirmation for meaningful consequences. citeturn158633view0

OpenAI's Computer-Using Agent (CUA)/Operator announcement describes screenshot-based GUI understanding with mouse/keyboard action, multi-step reasoning, and self-correction. Current computer-use docs also describe both a structured computer tool and code-execution integrations such as Playwright or PyAutoGUI, emphasizing bounded execution, checking actual outcomes, treating screen contents as untrusted, and confirming consequential actions. citeturn376024search6turn376024search7

UI-TARS is a different research approach: a native GUI agent model trained to perceive screenshots and produce GUI actions, with large-scale screenshot/action data, grounding, reasoning, and reflective traces. It is not “Tesseract plus an ordinary text LLM.” The paper reports OSWorld and AndroidWorld results under its evaluation settings; those reported numbers should not be treated as directly comparable across benchmark versions or as a guarantee for arbitrary personal-computer tasks. citeturn224020academia120turn224020search5

**Lesson for ADVI:** A VLM can help when semantic APIs fail, but adding a VLM is not a replacement for state tracking, selectors, waiting, safety, or verification. A vision-only approach trades semantic structure for flexibility; a DOM/UIA approach trades universal coverage for more reliable targets when the structure is available. ADVI should combine them as fallbacks, not choose one for everything.

### 2.5 Benchmarks show the problem is hard—but do not excuse avoidable errors

OSWorld provides computer tasks in real desktop/web applications, with task setup and execution-based evaluation across operating systems. Its initial paper reported large gaps between human and model performance and highlighted GUI grounding and operational knowledge as weaknesses. citeturn224020academia121

WebArena provides reproducible, realistic website environments and long-horizon tasks. Its original evaluation reported a 14.41% end-to-end success rate for its best GPT-4-based agent versus 78.24% for humans in that benchmark setup. Those figures are historical research results rather than today's universal SOTA, but they demonstrate how difficult open-ended, multi-step web tasks are. citeturn212311academia132

WorkArena/BrowserGym add structured knowledge-work tasks and common benchmark environments, which are useful patterns for building ADVI's own regression tasks. citeturn224020search6turn224020search7

### 2.6 ReAct is useful, but an unbounded ReAct loop is not a workflow engine

ReAct interleaves reasoning and action so a model can observe the consequences of actions and adjust its next step. It provides an important conceptual pattern for interactive agents. citeturn129554academia120

But ADVI should not ask an LLM to independently coordinate every small, deterministic step in every common workflow. Each extra provider call adds latency, token cost, quota exposure, an opportunity for tool-surface mismatch, and a chance to lose state. Keep language-model reasoning for understanding, ambiguity and recovery; place predictable subflows in deterministic local state machines.

---

## 3. Recommended technical architecture for ADVI

### 3.1 Target architecture

```text
User / voice input
        |
        v
Request & context manager
  - parse intent and output format
  - preserve pending task and user-approved targets
        |
        v
TaskState + AppResolver
  - task ID, current step, status, app profile
  - window/tab/resource identity, paths, evidence
        |
        +---------------------------+
        |                           |
        v                           v
Application workflow adapter    General agent planner
  - Gmail workflow              - unusual/new tasks
  - WhatsApp workflow           - ambiguous interpretation
  - media workflow              - decide fallback
  - file/document workflow
        |                           |
        +-------------+-------------+
                      v
            Typed Tool Manifest
    schemas + preconditions + risk + postconditions
                      |
                      v
               Guard / Consent
                      |
                      v
              Domain Executor
   API > Browser DOM/CDP/Playwright > Windows UIA
                         > MSAA/other native access
                         > OCR/VLM/coordinates fallback
                      |
                      v
          State-based wait & verification
                      |
             +--------+---------+
             |                  |
          verified          mismatch/unknown
             |                  |
             v                  v
     TaskState advance      bounded Recovery FSM
             |              re-observe, classify, retry once,
             |              switch safe method, ask user, or stop
             v                  |
        Progress/response <-----+
  text + non-blocking TTS queue
```

The LLM chooses semantics and deals with uncertainty; deterministic code owns real-world state, operation contracts, waits, verification, retry budgets and side-effect control.

### 3.2 Build one canonical tool contract

Every model-callable operation should be declared in a canonical manifest with:

- name and version;
- typed JSON schema and required slots;
- execution domain (browser, native desktop, filesystem, Gmail, media, memory, system);
- preconditions and postconditions;
- side-effect/risk class;
- idempotency classification;
- timeout and retry policy;
- verification adapter;
- optional compensation/undo procedure;
- progress-event labels.

The same manifest should drive provider declarations, argument validation, dispatch, logging, and evaluation. Log the manifest hash for each model request. Reject or repair a provider's undeclared-tool call at the boundary and record it as a contract error; never send it into an unrelated handler or silently infer missing arguments. Rebuild provider-native sessions when their tool manifest changes, and test that failover sessions preserve the same task state and tool availability.

### 3.3 Use higher-level workflow adapters for common tasks

Generic `click`, `type_text`, and `press_key` tools remain valuable for open-ended use. They should not be the primary orchestration interface for tasks ADVI handles frequently.

For example, model-facing `whatsapp_send_message` (name illustrative) should accept structured inputs:

```json
{
  "surface": "desktop | web | auto",
  "contact_name": "Anurodh",
  "message_text": "Hello",
  "confirm_if_ambiguous": true
}
```

Its internal state machine would:

1. Resolve the user's preferred/available WhatsApp surface.
2. Confirm that the correct app/window/tab is active.
3. Determine if the account is logged in using surface-specific UI indicators.
4. Locate the contact and inspect candidate identity; ask if ambiguous.
5. Open the intended chat and verify its header/identity.
6. Focus the message composer by semantic element, type the exact text, and verify the composer value.
7. Apply the configured confirmation policy for external sending.
8. Send once, then verify a sent-message bubble or stronger state evidence.
9. Return structured evidence, not simply “click succeeded.”

If the account is logged out, the workflow should report that state and wait for the user to sign in; it should not attempt to bypass authentication. If no supported API exists for the user's account type, UI automation is the path. Meta's WhatsApp Cloud API is a business-messaging API requiring business assets and a registered business number; it should not be assumed to be a universal API for ordinary personal-account chats. citeturn150533search1turn150533search4

Use the same pattern for media playback, email, document creation and other common workflows: typed slots at the LLM boundary, a deterministic local executor internally, and a final postcondition.

### 3.4 Make app resolution and app state explicit

An application should have an `AppProfile` with fields such as:

```text
canonical_id
preferred_surface: native | browser | user_choice
launch_recipe / app identity
process names / package or AppID
window identity rules
browser tab ownership rule
ready-state predicates
logged-in-state predicates
login / consent / CAPTCHA blocking indicators
key controls and semantic locator strategies
postconditions for common actions
```

The app state should not be a single boolean `open` flag. Use an explicit enum, for example:

```text
NOT_INSTALLED -> INSTALLED -> LAUNCHING -> WINDOW_READY
                                      |-> BLOCKED_BY_DIALOG
                                      |-> LOGIN_REQUIRED
                                      |-> READY_AUTHENTICATED
                                      |-> ERROR
```

“Process running,” “window visible,” “control ready,” and “user authenticated” are different facts and must be verified separately. On Windows, the current watchtower already uses Start Menu/Start Apps/AppX/registry sources; enhance the data model and state checks rather than replacing them with process-name guessing.

### 3.5 Use an evidence hierarchy for perception

Recommended order for actions:

1. **Supported application/API operation** — strongest when it is the official, authorized API for the task.
2. **Browser semantic DOM via one owned Playwright/CDP adapter** — element role/label/text, exact match count, visible/enabled/editable status, element and URL waits, DOM postconditions.
3. **Windows UI Automation** — window/process match, scoped control tree, automation ID/name/role/value/focus/enabled state, UIA control patterns/events.
4. **Legacy accessibility interface** — MSAA or other supported accessibility interfaces for controls not adequately exposed through UIA.
5. **Local screenshot + OCR** — extract visible text and bounding boxes; include OCR confidence and screenshot/window identity. Useful for fallback target grounding, not as proof of a consequential result.
6. **Multimodal visual model or coordinates** — only when supported and justified; model must receive the actual image if it is expected to reason visually. Use short action-observation loops and map any rescaled coordinates back to actual display dimensions.

Every observation should be typed and timestamped:

```text
Observation {
  surface_id / app_id / hwnd / tab_id
  captured_at / age_ms
  evidence_source: api | dom | uia | msaa | ocr | vlm
  target candidates: role, name, id, bounds, enabled, focused, value
  current state predicates
  confidence and known limitations
}
```

Do not expose the entire UI tree or all OCR output to the LLM. Scope the observation to the active target and current workflow step, summarize the state delta, and preserve only the evidence needed for the next decision.

### 3.6 Search and system awareness: metadata index, not giant prompt dumps

ADVI's current watchtower is deliberately bounded. It inventories common user roots, observes configured directories, polls processes, and can query Windows Search for targeted filenames. For an instant file index comparable to Everything, there are three reasonable implementation options:

- **Preferred pragmatic option:** optionally integrate Everything's supported command-line/IPC interface if the user already has it installed and opts in. It is designed for very fast name/path search on fixed NTFS volumes and uses the NTFS USN Change Journal to keep its index current. citeturn158633view7turn224020search1
- **Native Windows option:** use Windows Search's existing index for scoped filename/metadata queries; fall back when an excluded/unindexed location returns no result.
- **ADVI-managed option:** maintain a local SQLite metadata index, bootstrap it with a bounded per-root crawl and update it using watchdog/`ReadDirectoryChangesW`; consider USN journal support for whole-volume NTFS coverage only if the implementation cost and permissions are justified. Microsoft documents the USN journal as a way to track file/directory changes without repeatedly crawling the entire volume. citeturn376024search0turn376024search3

Do not index contents by default. Keep metadata such as normalized name/path, extension, size, modified time, root scope, stable file identity if available, and indexing freshness. Content should be read only after the target is resolved and the user/task needs it.

The LLM should get a query-oriented result, not a full inventory dump:

```json
{
  "query": "1023146_Stavan.pdf",
  "scope": "Downloads",
  "matches": [{"name":"1023146_Stavan.pdf","path":"...","kind":"file"}],
  "complete": true,
  "index_age_ms": 230
}
```

If the index is incomplete, say so explicitly. “No results found” and “search did not complete” must remain separate outcomes.

### 3.7 Self-correction should be a bounded state machine

Do not let the agent replan indefinitely from a full transcript. A recovery FSM should classify the failure into categories such as:

- target not found;
- wrong/changed window or tab;
- element exists but is not actionable;
- UI not ready;
- login/permission/dialog blocks progress;
- postcondition not met;
- provider/transport unavailable;
- invalid/missing tool schema;
- operation completed but confirmation evidence is unknown.

Use a deterministic response per class:

1. Capture the actual failure and current-state delta.
2. Check whether the side effect may already have happened.
3. If no side effect occurred, observe/resolve the exact target again.
4. Wait on a specific condition rather than a fixed arbitrary sleep.
5. Retry once with an alternative safe interaction mechanism if available.
6. Re-verify the postcondition.
7. If state still does not progress, ask one targeted question, request user assistance, or stop honestly.

Have a strict action/time/provider-call budget. The existing watchdog remains valuable, but the workflow FSM must record which state it reached and why recovery was attempted. The key stop condition is **no meaningful state change with no new evidence**, not simply “the same tool has been called twice.”

### 3.8 Undo/redo: use compensation, not blind Ctrl+Z

A universal Undo button does not exist across Windows applications. Use explicit reversibility classes:

- **Reversible/compensatable:** create a new uniquely named file, move, rename, duplicate, or edit with a stored before-image/hash. Record the original and destination paths, file identity, timestamps and exact operation.
- **Conditionally reversible:** overwrite/edit existing content, multi-file operations, or form entry. Capture a backup/transaction log first and verify no other actor changed the resource.
- **External irreversible side effects:** send email/message, submit a post/form, make a purchase, or delete without backup. Require the applicable consent gate and precondition checks. If completion is uncertain, query status before retrying. After sending, do not pretend the action can be undone; explain what happened and offer a corrective follow-up.

Before every non-idempotent action, persist an operation journal containing the intended target, planned effect, approval, idempotency key or unique operation identity, and outcome. On timeout, query the external/system state before repeating, to avoid duplicate messages or files.

### 3.9 Voice feedback must be asynchronous and evidence-based

The current Piper path in `src/advi/io/tts.py` invokes Piper to synthesize a WAV and plays it through Windows audio; Windows SAPI is a fallback. Piper synthesis has an 8-second subprocess timeout, and playback through `winsound.PlaySound` is synchronous. If progress messages are spoken inline, this can stall or serialize execution behind audio playback.

Add an event channel independent of planning and execution:

```text
TaskStarted -> StepStarted -> StepVerified -> UserConfirmationNeeded
            -> StepFailed/Recovery -> TaskCompleted/TaskStopped
```

A non-blocking TTS queue speaks concise, deduplicated stage messages. Emit a statement like “Opening WhatsApp” when the intent is accepted, “WhatsApp is open; checking sign-in” once that state is observed, “I found two matching contacts” when the ambiguity is real, and “Message sent” only after the sent state is verified. Do not call the LLM merely to write each progress sentence. The execution controller must continue working while audio plays, and a stop/cancel action must interrupt pending low-value speech and halt the next state-changing action.

### 3.10 Security and consent are part of the architecture

- Run with least privilege. UIA tools may need same-or-higher privilege to control an elevated app; do not launch ADVI as administrator by default.
- Treat web pages, email bodies, document text, OCR, and other environmental content as untrusted input, not instructions overriding the user.
- Ask for confirmation for consequential actions or unresolved target ambiguity.
- Keep the tool manifest allowlisted and each operation scoped to the active app/account/resource.
- Keep screenshots/logs local by default, redact secrets, and define retention limits.
- Separate user intent, model-suggested arguments, and user-approved arguments in task state.

Anthropic's computer-use documentation specifically warns about prompt injection from pages/images and recommends restricted environments/privileges plus human confirmation for meaningful consequences. OpenAI's computer-use guidance similarly recommends isolation, treating screen content as untrusted, confirming consequential operations, and bounding and verifying runs. citeturn158633view0turn376024search7

---

## 4. Staged implementation roadmap

The order matters. Improve execution contracts before replacing perception models.

### Phase 0 — Baseline and observability

- Freeze a source fingerprint and build identifier in every run.
- Establish a corpus of common prompts from the current log.
- For each tool call, log: task ID, step ID, tool-manifest hash, selected domain, target/window/tab ID, arguments (with sensitive data redacted), precondition result, start/end time, postcondition, failure class, retry count, and final status.
- Add timeline fields to every event so sequences can be reconstructed without manually joining ambiguous output lines.
- Do not change success criteria during this measurement phase.

**Exit criteria:** every prompt can be traced from request through tool declaration, action, evidence and final response; there are no unmatched tool-start/exit records in a deterministic harness.

### Phase 1 — Tool contract and task-state integrity

- Make the canonical tool manifest the source of truth for provider schemas and local dispatch.
- Test every tool-surface transition, provider failover, pending confirmation and resumed turn.
- Store output-format requests, selected app surface, resolved contact/file/path, and pending clarification as typed task slots.
- Preserve canonical target IDs/paths as structured state; never pass natural-language fragments where canonical paths are expected.
- Add idempotency/effect classification before retry logic.

**Exit criteria:** no “tool not in request.tools” call escapes as a generic failure; the PDF clarification sequence retains the requested output format and exact path; provider failure does not silently erase task state.

### Phase 2 — Application adapters and deterministic workflow state machines

Prioritize workflows visible in the log:

1. Gmail draft/send (native API; retain the strongest current path).
2. WhatsApp contact/chat/message.
3. YouTube/YouTube Music search and playback.
4. Find/open/read/create document and folder workflows.
5. Local media search/play.

Each adapter should return typed stages and evidence. Keep generic click/type tools available for novel tasks, but route known high-frequency tasks through the appropriate adapter.

**Exit criteria:** each workflow has automated tests for success, logged-out state, missing app, ambiguous contact, missing result, stale UI, provider interruption, and already-performed side effect.

### Phase 3 — Unified state-aware perception

- Standardize a `SurfaceAdapter` API for `observe`, `resolve_target`, `act`, `wait_for`, and `verify`.
- Browser adapter owns one Chrome session/profile and exact tab IDs. Standardize on the current CDP adapter first; evaluate Playwright migration behind the same interface only after tests exist.
- Native adapter queries a bounded UIA tree rooted at the verified window, including role/name/automation ID/focus/value/enabled state.
- OCR is fallback-only for controls unavailable via semantic interfaces. Keep screenshot identity, bounds and OCR confidence in the result.
- Add a vision-language model only if the selected model accepts images, and test whether it improves targets UIA/DOM/OCR cannot resolve. Do not route all tasks through screenshots by default.

**Exit criteria:** every state-changing step is preceded by valid target evidence and followed by a relevant state predicate or explicit uncertainty; wrong-surface interactions are detected before typing/sending.

### Phase 4 — Fast system inventory and search

- Keep app inventory sources separate (Start Apps/AppX/registry/shortcuts) and distinguish installed vs running vs visible vs ready vs authenticated.
- Instrument current crawl and Windows Search latencies and hit rates.
- Optionally integrate Everything's CLI/IPC; otherwise build a scoped SQLite metadata index updated by events and targeted reconciliation.
- Add freshness/completeness fields; never present an incomplete search as a complete negative result.

**Exit criteria:** named file searches have recorded latency, root scope, index freshness and completeness; a specific application query distinguishes not installed from not discovered or installed-but-not-running.

### Phase 5 — Recovery, compensation and voice events

- Implement bounded recovery classes and explicit transition limits.
- On uncertain side effects, inspect state before retrying.
- Add operation journal/compensations for reversible file tasks; forbid pretend-undo for external side effects.
- Decouple progress announcements into an asynchronous, interruptible queue.

**Exit criteria:** no repeated identical state-changing actions without new evidence; the user receives accurate progress and a final report that separates completed steps, failed steps, and uncertain outcomes.

### Phase 6 — Benchmark and acceptance

Use a repeatable ADVI task suite inspired by OSWorld/WebArena/BrowserGym's reproducible tasks and execution-based evaluators. Begin with 30–50 representative scenarios and repeat each several times under a recorded environment; include positive, negative, ambiguous, login-required and provider-failure cases.

Measure:

- end-to-end task success;
- correct target rate;
- false-success rate;
- rate of unresolved uncertain outcomes;
- recovery success rate;
- duplicate side-effect rate;
- median and p95 latency;
- model calls and tool calls per task;
- provider-unavailable degradation;
- user confirmations per task.

Run both mocked deterministic tests and live Windows tests. Report desktop OS version, display scaling/resolution, app versions, browser version/profile, model/provider IDs, network conditions and exact test prompts. Separate unit-test pass rate from real application workflow success.

**Exit criteria:** the team can compare releases against the same corpus and tell whether a change improved end-to-end task outcomes instead of merely increasing tool-call success.

---

## 5. Practical acceptance checklist

### Desktop application launch

- Correct application identity resolved from inventory.
- Launch target uses AppID/package/executable/shortcut, never model-generated shell text.
- Visible target window belongs to requested application.
- Ready and logged-in state are verified separately.
- No typing occurs until the intended editable control is focused or the action uses a verified semantic target.

### WhatsApp send

- Correct surface selected (native or web).
- Account state and blocking dialogs checked.
- Contact candidate uniquely identified or clarification requested.
- Correct chat header confirmed before composer entry.
- Exact message text verified in composer.
- Confirmation policy applied before transmission.
- Exactly one send attempt; query state before retry on timeout.
- Sent bubble/status evidence captured.

### YouTube playback

- Owned browser tab and site verified.
- Search query performed once.
- Results list inspected through DOM/accessibility data where possible.
- Target video identified by text/metadata and opened.
- Watch page URL and player state verified; a click result alone is insufficient.

### File and document operations

- Scope and canonical target path persisted from resolution onward.
- Search result completeness and uniqueness known.
- Correct output path is checked before writing.
- Document content is validated by parsing the generated artifact, not merely checking file existence.
- Open/read follows the exact artifact path; duplicate or existing files are handled idempotently.

### Provider and tool failures

- API credentials/authentication, TPM limits, daily quotas, network errors and schema errors have distinct categories.
- A provider failure after a successful operation cannot cause that operation to run a second time blindly.
- The final response distinguishes “not attempted,” “partially completed,” “completed and verified,” and “outcome unknown.”

---

## 6. Recommended priorities in one sentence each

1. **First:** fix task/tool/path integrity and instrument the exact failure boundaries.
2. **Second:** wrap WhatsApp and media workflows in deterministic app-specific adapters.
3. **Third:** make browser/native perception return typed observations and require relevant postconditions.
4. **Fourth:** improve file/app indexing only within a measured scope, without putting system dumps into the LLM context.
5. **Fifth:** implement bounded recovery, side-effect compensation, and asynchronous progress speech.
6. **Finally:** measure the same real Windows task suite at every release; do not call the system reliable because its unit tests pass.

---

## References

1. Anthropic, *Computer use tool documentation*, tool loop, screenshots, security and confirmation. https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
2. OpenAI, *Computer use*, environment integration, screenshots/tool calls, safe execution and verification. https://developers.openai.com/api/docs/guides/tools/computer-use
3. Microsoft, *UI Automation*, native UI elements and accessibility framework. https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiauto-win32
4. Microsoft, *UI Automation Specification*, tree, properties, control patterns and events. https://learn.microsoft.com/en-us/windows/win32/winauto/ui-automation-specification
5. Microsoft Playwright documentation, *Auto-waiting and actionability*. https://playwright.dev/docs/actionability
6. Y. Qin et al., *UI-TARS: Pioneering Automated GUI Interaction with Native Agents*, arXiv:2501.12326. https://arxiv.org/abs/2501.12326
7. T. Xie et al., *OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments*, arXiv:2404.07972. https://arxiv.org/abs/2404.07972
8. S. Zhou et al., *WebArena: A Realistic Web Environment for Building Autonomous Agents*, arXiv:2307.13854. https://arxiv.org/abs/2307.13854
9. S. Yao et al., *ReAct: Synergizing Reasoning and Acting in Language Models*, arXiv:2210.03629. https://arxiv.org/abs/2210.03629
10. Voidtools, *Everything Indexes* and *Options*, NTFS/USN indexing. https://www.voidtools.com/en-us/support/everything/indexes/
11. Microsoft, *Change Journals*, NTFS USN change-journal concepts. https://learn.microsoft.com/en-us/windows/win32/fileio/change-journals
12. Meta, *WhatsApp Cloud API* official Business Platform collection. https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api
13. ServiceNow, *BrowserGym* and WorkArena benchmark ecosystem. https://github.com/servicenow/browsergym and https://github.com/ServiceNow/WorkArena

## Scope and caveats

This review is based on the matching ADVI source fingerprint and the supplied runtime log, plus the linked public documentation and research papers. Vendor product capabilities and benchmarks evolve; figures from individual papers describe their stated evaluation environments and are not direct apples-to-apples comparisons. Proposed modules and state-machine behavior above are recommendations, not claims that they already exist in ADVI.
