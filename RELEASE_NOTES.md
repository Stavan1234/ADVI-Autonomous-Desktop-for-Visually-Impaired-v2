ADVI 0.3.4 — Remaining Log Fixes (2026-09-30)

ADVI 0.3.4 — Remaining Log Fixes (2026-09-30)

- Corrected `list_files` verification for the structured native listing payload.
- Preserved recent folder/file/application aliases across task boundaries.
- Added a local exact-path fast path for opening the last known folder and repairing a wrong-folder correction.
- Kept just-read PDF/file content available as the evidence source for immediate "what is it about?" questions.
- Extended PDF visual sanity verification to every generated page.
- Made simple application-launch responses report the requested application name.
- Validation: full suite passes with 2 expected environment-dependent skips; structured-document smoke check passes for a 10-page PDF and 5-slide PPTX.


## Phase 8 — End-to-End Acceptance & Runtime Diagnostics

- Added bounded per-turn runtime metrics for LLM/provider attempts, tool executions, model-visible observations, local continuations, provider switches, and provider failures.
- Added open-world browser acceptance coverage proving the primary LLM remains responsible for semantic decisions while verified local continuation handles explicit mechanical steps.
- Added desktop acceptance coverage proving an unverified typing action aborts stale follow-up side effects and escalates to targeted perception instead.
- Added acceptance coverage for explicit filename authority, provider schema repair without unnecessary failover, and relevant experience-memory retrieval.
- Added `scripts/analyze_runtime_log.py` to quantify the major failure classes observed in the 2026-09-30 Windows session.
- Added a Windows acceptance checklist for YouTube Music, WhatsApp, Gmail, files, unexpected UI changes, provider failure, and experience-memory validation.

Validation:
- Full `tests/current`: 486 collected, 484 passed, 2 expected environment-dependent skips.
- Python compilation: pass.
- Phase 8 focused acceptance tests: pass.
- Release/package integrity audit: pass.
- Live Windows GUI acceptance remains required on the target Windows machine.
ADVI 0.3.4 — Final warning/cleanup patch (2026-09-30)

- Added an explicit pytest-asyncio development dependency matching the fixture-loop configuration.
- Reworked optional MCP shutdown so asynchronous cleanup is created and awaited only on the worker event loop, eliminating the shutdown race that could leave `_close_async()` unawaited.
- Updated the MCP race regression test to enforce the no-unawaited-coroutine invariant.
- Verified the warning-sensitive MCP/provider suite with warnings enabled: it exits cleanly with no warnings; the full suite reaches 100% with 2 expected skips but this container may remain in third-party teardown afterward.

ADVI 0.3.4

ADVI 0.3.4 — Final Reliability Cleanup (2026-09-29)

- Added deterministic turn-boundary classification for new tasks, continuation, confirmation, cancellation, state queries, and conversation.
- Added a canonical task operation contract with protected user parameters that survive retries and provider/David handoff without semantic rewriting.
- Completion is now evidence-driven: verified intermediate actions cannot be presented as complete compound tasks, and provider narration cannot override execution truth.
- Added deliberate/exploratory execution pacing for complex or unknown work: observe, allow the environment to settle, then re-plan rather than blindly executing stale action chains.
- Local greetings, state queries, task status, capabilities, contacts, and system information use deterministic fast paths where the answer is locally knowable.
- Structured PDF/DOCX/XLSX/PPTX verification now treats known content mismatches as definitive failures while remaining tolerant of native parser text normalization.
- David prepared-email sending re-verifies the exact recipient, subject, and body immediately before the irreversible Send action.
- Fixed oversized provider handoff bounding, duplicate routing code, stale unused state, explicit contact persistence reporting, and Python invalid-escape warnings in the CDP browser module.
- Release packaging excludes secrets, credentials, runtime state, logs, caches, generated build artifacts, and bytecode.

Validation:
- Full test suite: all collected tests passed; 2 expected environment-dependent tests skipped.
- Strict Python compilation/import audit: pass; all 91 importable ADVI modules loaded successfully.
- Capability audit: 7 capabilities, 65 unique runtime actions, all with canonical action contracts.
- Final Windows GUI acceptance remains an on-device validation step; this build environment cannot honestly perform live Windows/Chrome/Gmail/WhatsApp interaction tests.


## Batch 1 — Application identity & live session foundation (2026-09-29)
- Application discovery records now retain AppID/AUMID, package family, runtime type, and a stable identity key.
- Installed Chromium PWAs discovered through Start Apps are classified separately from generic packaged applications.
- Desktop application launch no longer uses fuzzy window-title matching to decide whether an application is already open or which window was launched. This prevents cases such as a Word document named “WhatsApp Case Study” being mistaken for WhatsApp.
- Classic applications can reuse their canonical executable identity even when their Windows Start Apps record does not expose an executable path.
- Launch/session tracking now records launcher PID separately from the PID that owns the visible application window, covering Windows host-process models such as ApplicationFrameHost.
- Desktop context now carries a lightweight application session with explicit lifecycle states (launching, stabilizing, ready, blocked dialog, closed, etc.) and identity metadata.
- Desktop target validation updates the live session when the window disappears, becomes hidden, encounters a same-process dialog, or becomes ready/focused.
- Fixed the desktop UI Automation perception runtime import defect that was repeatedly raising `NameError: name 'os' is not defined`.
- Added regression coverage for application identity, PWA classification, session state, and launch-title safety.

Final engineering cleanup for the current operator runtime.

Highlights
- Voice/TTS preflight happens before startup progress speech.
- Semantic memory is loaded during startup and third-party model-loader output is suppressed from the interactive console.
- Normal turns receive only an intent-selected tool surface; MCP is explicit and David remains the isolated fallback. Known action contracts remain routable even during transient capability-readiness gaps, while execution still enforces real runtime availability.
- Groq supports three configured key routes with per-route health/cooldown state and structured failover. Shared quota groups can be configured when keys actually share a quota scope.
- Provider timeouts poison and defer teardown of the timed-out session instead of closing live SDK state from another thread.
- Tool results, task state, artifacts, and provider handoffs remain bounded.
- Desktop targets, documents, browser tabs, files, email, OCR, and MCP each have deterministic native execution boundaries.
- Browser actions use ADVI-owned CDP tabs, exact DOM selectors first, bounded semantic element matching, open-shadow-root traversal, real pointer-event fallback, and explicit post-action verification. YouTube playback follows navigate/search/result selection/playback verification rather than stopping at the search page.
- Natural-language filesystem routing covers common human variants such as new folder, mkdir, erase/remove, set file content, and folder/file mutation phrases without widening into unrelated tool families.
- Application discovery includes built-ins, PATH executables, Windows app inventory, shortcuts, registry-derived executables, and Windows AppsFolder targets.
- Filesystem operations include create/read/write/search/content-search/copy/move/rename/delete and common document readers.
- Gmail supports read/draft/update/send/reply and verified outcomes, with David fallback where native Gmail is unavailable.
- Session/task lifecycle includes awaiting-input, confirmation, verified recovery, cancellation, and preserved progress after provider failure.
- Release packaging excludes secrets, runtime state, logs, caches, credentials, and egg-info/build artifacts.

Validation
- Full current test suite: 318 collected, 316 passed, 2 environment-dependent skips.
- Python source compilation: pass.
- Strict warning-as-error run: targeted lifecycle warnings were fixed; the full warning-as-error harness is not used as the release gate because it can stall on long-lived third-party teardown paths.
- Native action audit: 62 registered actions, 62 handlers, 62 canonical contracts; no missing handlers or contracts.
- Shell/destructive command audit: no `shell=True` or `os.system()` usage in runtime/tests.
- All importable ADVI modules: pass.
- Final Windows acceptance remains an on-device validation step because this development environment is not Windows.

- Corrected the OCR Python dependency floor to a published `pytesseract` release (`>=0.3.13`); the previous `0.3.30` requirement was invalid.


Current hardening (2026-09-28)
- Structured .pdf/.docx/.xlsx/.pptx creation and saving use native document libraries and publish atomically after parser validation; structured files are never silently written as UTF-8 text.
- Explicit page/slide counts in the user's request are propagated even when the model omits the optional count argument.
- YouTube play/watch requests use the verified first result candidate immediately after search, preventing redundant re-search/inspection loops; repeated user turns start fresh executable tasks.
- A plain application launch exposes only the semantic open_application action; discovery/GUI primitives remain available for explicit discovery or interaction tasks.
- Never report memory/file/email/application success without an actual successful tool result.
- Windows environment-path simulation and MCP async cleanup edge cases were fixed, and SQLite resources are closed deterministically.

## Phase 6 — LLM Tool Contract Hardening

- Browser tool surfaces now keep required one-step interaction dependencies coherent.
- Deterministic provider request/schema errors no longer trigger provider failover.
- Missing-tool repair is bounded to one same-provider recovery attempt per user turn.
- Provider traces record the model-visible tool surface for diagnosis.
