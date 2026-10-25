# ADVI: an evidence-checked voice assistant for operating Windows without sight

**Technical report.**
- Version: ADVI 0.4.0.
- Report date: 2026-10-25.
- Repository: `Stavan1234/ADVI-Autonomous-Desktop-for-Visually-Impaired-v2`, branch `main`. The report
  describes the working tree prepared for the commit after `dc46e96`.
- Status: working document. It describes the code as it stands. It is not a paper, and none of its results
  have been independently reproduced.

Companion documents:
- [ARCHITECTURE.md](ARCHITECTURE.md): diagrams (referred to below as Figures 1–8).
- [DEVELOPMENT.md](DEVELOPMENT.md): setup and testing.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- `docs/audits/`: per-phase engineering notes, which are the primary evidence for Section 6.

---

## Abstract

ADVI is a Windows desktop assistant for blind and visually impaired users. A person speaks or types a
request ("send hello to Siyu on WhatsApp", "make a Word table of the months", "is there an unread email
from my bank?"). ADVI carries it out by operating real applications, then answers aloud.
- A hosted language model (Groq `openai/gpt-oss-120b`, with Google Gemini as backup) interprets the request
  and chooses among 88 typed tools.
- Deterministic Python code executes each tool through Windows UI Automation, COM, Playwright-controlled
  Chrome, the file system and the Gmail API.
- That code then checks the result before ADVI reports it.

The central design rule is a division of labour. The model reasons, and the program owns execution state,
resources and evidence. Concretely:
- tool calls are validated against contracts;
- actions that send messages, forward or trash mail, or delete memories cannot run without an explicit
  "yes", enforced by a fingerprinted approval check in `ToolGuard`;
- the final answer is reconciled against recorded tool outcomes, so the model cannot claim a send or a
  completion that did not happen.

Version 0.4.0 adds:
- always-on voice interaction: VAD endpointing, online/offline Whisper, interruption by voice, and a small,
  teachable set of audio cues;
- name correction and learning for messaging;
- additional Gmail operations;
- a last-resort screen description service;
- an evaluation harness.

The evaluation harness comprises a 23-task Desktop Suite (9 tasks adapted from Windows Agent Arena), a
38-task live capability sweep, and a WebVoyager sample. These benchmarks were used mainly to find and fix
defects. The report keeps the fixing rounds separate from any score, and it does not claim a WebVoyager
result.

## 1. Introduction and motivation

Screen readers make the contents of a screen audible, but the user still has to navigate it element by
element. Multi-step tasks become long sequences of keyboard commands across unfamiliar interfaces: reply
to the newest email, find a file and attach it, play a particular song, build a table in Word. Large
language models make a different interaction possible. The user states the goal, and software performs
the steps.

For a user who cannot see the screen, that delegation is only useful if the result is reported truthfully.
A sighted user can glance at the screen and notice that a message went to the wrong chat. A blind user
depends entirely on what the assistant says. ADVI is therefore built around one question for every
action: what evidence does the program have that this really happened, and does the spoken answer stay
within that evidence?

## 2. Problem scope and intended use

**In scope:**
- **Platform:** Windows 10/11 on an ordinary laptop. Development used an Intel i5-8265U with 16 GB RAM and no
  GPU.
- **Input:** typed text, or speech through an always-on microphone loop.
- **Output:** printed text and synthesized speech (Piper when present, otherwise Windows SAPI).
- **Tasks:**
  - opening and closing applications;
  - files and folders (create, move, copy, rename, delete to the Recycle Bin, search, organize with
    undo);
  - Word documents and PDFs, Excel and PowerPoint files;
  - web browsing in a dedicated Chrome profile;
  - media playback (YouTube, local files);
  - WhatsApp and Telegram Desktop messages;
  - Gmail (read, draft, send, reply, forward, trash, archive, mark);
  - system questions;
  - a long-term memory of user facts.

**Out of scope or unsupported:**
- other operating systems;
- solving CAPTCHAs or "prove you're human" challenges (ADVI tells the user instead);
- purchases and payments, which ADVI gates and does not complete on its own;
- running without any AI provider beyond a small set of built-in commands.

ADVI depends on external AI services (Groq, Gemini). Their free tiers impose daily and per-minute limits
that visibly shape behaviour (Sections 7.2 and 15).

## 3. Objectives and design principles

1. **The model reasons; the program owns reality.**
   - The model proposes tool calls and writes prose.
   - Task state, which resources are open, what was verified, what is pending confirmation, and what
     ADVI may say about outcomes all live in Python objects: `TaskState`, `ToolResult`, and
     `AgentSession` fields.
2. **Verify before reporting.**
   - Capabilities return a `ToolResult` with `verified` and `verification_status`.
   - `ToolVerifier` (`core/verification.py`) adds independent checks for many tools.
   - `AgentSession._authoritative_task_response()` replaces prose that contradicts the evidence.
3. **Ask before anything hard to undo.** Sends, mail forwarding and trashing, and memory deletion require
   confirmation. File deletions go to the Recycle Bin and are refused if it is unavailable. Bulk file moves
   can be undone.
4. **Prefer structure to pixels.**
   - Desktop perception uses the UI Automation tree first, then OCR, and only then a screenshot description.
   - Vision never supplies click coordinates and is never accepted as proof of success.
5. **Degrade honestly.** When providers fail, ADVI runs only unambiguous built-in commands, and otherwise
   says that the request needs the AI and why.
6. **Keep the fallback separate.** DAVID, the older planner/executor stack, is an isolated subsystem
   reached through one tool, not part of the primary path.
7. **Fix failures at their cause.** Rules that came from observed failures carry a comment naming the log
   or benchmark run that motivated them, so the reasoning survives in the code.

Section 7.4 assesses how completely each principle is enforced.

## 4. Development history and implemented work

The repository has three commits:
- `06d0fc4` "Initial commit of completely overhauled codebase";
- `936b037`;
- `dc46e96` (2026-10-14).

Everything after 2026-10-14 is uncommitted and enters with the commit this report accompanies. The
per-phase engineering notes in `docs/audits/` record each batch: the triggering log or test, the cause
found, the change and the tests. The table below summarises them. Dates are those written in the notes.
Where a note gives no date, none is invented.

| Phase (note) | Date | Problem addressed | Main changes (files) | Evidence recorded in the note |
|---|---|---|---|---|
| Architecture overhaul (`ADVI_Agent_Architecture_Overhaul.md`, `ADVI_Architecture_Review_and_Roadmap.md`) | before 2026-10-14 | Root-cause analysis of an earlier runtime log; roadmap | Design documents; basis of the native `ToolCall` / `ExecutionEngine` path | Analysis document |
| LOGFIX, LOGFIX2 | 2026-09-30 | PDF/PPT/folder defects in a runtime log | Verification payload fixes, artifact aliases | Unit tests; full suite at the time |
| Phase 0: integrity | 2026-10-14 | Routing denied capabilities ("not in request.tools") | `request_tools` meta-tool (`tools/catalog.py`), task/tool/path integrity | Mapped to log prompts |
| Phase 1: skills | 2026-10-14 | First app skills | WhatsApp skill (`capabilities/apps/`) | Log of 16 prompts |
| Phase 2: perception and readiness | 2026-10-14 | Acting before the UI was ready; first WhatsApp trial | `core/readiness.py`, structured perception | Log turns 17–22 |
| Phase 3: controller and progress | 2026-10-14 | Typing never reached WhatsApp; silent waits | `core/execution_controller.py`, spoken progress | Live experiments, log turns 23–26 |
| Phase 4: browser | 2026-10-14 | `open_application("Chrome")` opened the personal profile | Playwright over CDP in ADVI's own profile | Log turns 27–33 |
| Phase 5: core reliability; experience memory | 2026-10-14 | Status honesty, local apps, files | Experience lessons in SQLite (`memory/experience.py`) | 30-turn log |
| Phase 6: contracts; quota and local commands | 2026-10-14 | Tool-schema defects causing failover storms; daily quota | Tool dependency clusters; `local_intents.py`; quota diagnosis | Log turns 31–45 |
| Phase 7: DAVID and live sweep | 2026-10-14 | DAVID e-mail flow, Word under "activation failed" | `word_create_document` via COM; single-confirmation DAVID Gmail | Live runs on the developer's PC |
| Phase 8: acceptance; WhatsApp and honesty | 2026-10-14 / log of 2026-10-21 | End-to-end acceptance, unbacked "sent" claims | Runtime metrics; `_no_unbacked_send_claim`; spoken-chat restoration | Unit and acceptance tests |
| Phase 9: vision | not dated in note | A screenshot module supplied by the developer | `look_at_screen`, `screen_describer.py`, automatic looks capped at 2 per task | `test_phase9_vision.py`; live comparison of vision models |
| Phase 10: session fixes | 2026-10-23 | Evening session issues (reading, media, Telegram header, JSON repair) | Browser main-text reader, media polling, profile facts | Log lines cited in the note |
| Benchmarks | 2026-10-23 | Need for honest measurement | `benchmarks/` (Desktop Suite, WebVoyager harness) | `docs/benchmarks/` |
| Phase 11: voice input | 2026-10-24 | Voice prototype from the developer | `voice/` push-to-talk, local Whisper; then dual online/offline recognition | Model timing measurements (Section 14.5) |
| Phase 12: always-on voice | 2026-10-24 | First live voice session: key needed per sentence, names misheard, rigid confirmations | `VoiceConversation`, name learning, correction at confirmation, sleep handling; echo-cancellation evaluation | Live session log; measurements; tests |
| Phase 13: reliability and turn-taking | 2026-10-24 | Second live session: cut-off speech, no turn cue, stop and exit ignored during a 4-minute search | Endpointing, cues, cancellation flag, email tools, WhatsApp focus | Live log; Desktop Suite regression |
| Phase 14: voice UX and capability sweep | 2026-10-24/25 | Third live session: dropped utterances, unclear sounds, Markdown tables in Word | Onset-based floor decision, earcon redesign, Word/Excel tables, 38-task sweep with fixes | Live log; sweep records |
| Phase 15: echo, sleep, wake word | 2026-10-25 | Fourth live session: ADVI answered its own voice; sleep must close ADVI; wake word must start ADVI when closed | `voice/audio_io.py` (duplex stream + WebRTC AEC), sentence-by-sentence speech, `wake/` (listener process, supervisor, start at sign-in) | AEC measurement; wake-chain test on clips; live launch/close cycles (Section 14.8) |
| 0.4.0 onboarding (this report) | 2026-10-25 | Repository hygiene and reproducibility | `.gitignore`, `.env.example`, lock file, setup checks, docs | Fresh-copy installation (Section 13, Appendix C) |

The notes for Phases 0–8 were written before this report and are taken as recorded. Their claims were
not re-run during report preparation, except where they are exercised by the current test suite.

## 5. System overview

ADVI runs as one Python 3.13 process started by `run_advi.bat` (`python -m advi.app`).

**Startup** (`app.py: main`, Figure 8):
1. Speech output is prepared first, so the first progress message is audible.
2. The runtime is started: settings, the SQLite memory and the file-system watcher.
3. Providers, capabilities and the agent session are built.
4. Slow preparations run in parallel: provider sessions, the embedding model, experience memory, the Gmail
   token, MCP servers and the speech model.
5. A startup health validator runs. ADVI blocks on runtime, provider or task-state failures, and continues
   with a warning on degraded optional parts.
6. Voice and the input loop are started.

**Running.** The main loop takes typed lines and spoken transcripts from one queue (`InputHub`) and handles
them one at a time with `AgentSession.respond()`.

**Shutdown.** The session is consolidated into memory by the model (skipped when providers are
rate-limited) and every registered resource is closed.

## 6. Architecture and component responsibilities

Figures 1 (context) and 2 (components) in [ARCHITECTURE.md](ARCHITECTURE.md) show the structure.
Responsibilities:

| Layer | Component | Responsibility |
|---|---|---|
| Input/output | `io/console.py` `InputHub` | One queue for typed and spoken input; typed exit is handled on the input thread so it works mid-task |
| | `io/output.py` `OutputManager` | Printing (with one print lock), speech queueing, barge-in (`silence()`), "is ADVI speaking", recent speech for echo detection, short spoken confirmations |
| | `io/tts.py` `PiperTTS` | Piper synthesis when assets exist; otherwise a persistent PowerShell SAPI worker; `stop()` for interruptions |
| Voice | `voice/conversation.py` `VoiceConversation` | Listen loop, floor decision, echo filtering, sleep/wake/exit, music gate, cues |
| | `voice/recorder.py`, `transcriber.py`, `earcons.py`, `hotkey.py`, `system_audio.py` | Microphone and VAD, online/offline recognition, audio cues, global hotkey, output level meter |
| Agent | `agent/session.py` `AgentSession` | Turn handling, local fast paths, tool selection, the model loop, confirmation and correction, recovery, final-answer reconciliation |
| | `agent/provider_session.py` `ProviderSessionManager` | Conversation state per provider, failover across keys and providers, handoff of task context, short-limit waiting |
| | `agent/local_intents.py`, `agent/names.py` | Built-in commands without the model; spelled names, sound-alike matching, corrections |
| Tool boundary | `tools/` `ToolRegistry`, `ToolGuard`, `ToolRunner` | Model-facing tool specs; validation and confirmation enforcement; execution entry |
| | `core/execution_engine.py`, `core/action_contracts.py`, `core/verification.py` | Contract normalisation, dispatch to capabilities, independent verification |
| | `core/execution_strategy.py`, `execution_controller.py`, `execution_watchdog.py`, `perception_controller.py` | Pacing (one side effect per round for deliberate tasks), deterministic follow-up steps, loop limits, observation policy |
| Capabilities | `capabilities/registry.py` and 11 capabilities | Everything that changes or reads the computer (Section 7.3) |
| Memory | `memory/` | SQLite facts, contacts and learned chat names; `BAAI/bge-small-en-v1.5` retrieval; experience lessons; consolidation |
| Fallback | `fallback/` (DAVID) | Isolated intent/planner/executor stack with its own perception and Chrome driver |
| Environment | `environment/` | Known Windows folders (OneDrive-aware); watchdog-based file events used as a search cache |
| Diagnostics | `core/logging.py`, `core/pipeline_trace.py`, `core/health.py` | Rotating detailed log, structured traces with secret redaction, health reports |

## 7. Detailed implementation of the major subsystems

### 7.1 The agent loop (`AgentSession`)

`respond()` handles one user turn. The order matters, because each early step exists so that the model
is not asked something the program can answer exactly:

1. **Pending state.** A pending confirmation, a pending "spell the name" question, a pending DAVID e-mail
   send, a pending proposal, or "try again" after a failed send.
2. **Deterministic answers.**
   - "stop" and "cancel";
   - time and date (always from the PC clock);
   - failure diagnosis ("why couldn't you…", answered from the last recorded failure);
   - "why are you slow?" (answered from the speech-recognition state).
3. **Model turn.**
   - **Tool selection:** `_select_model_tools()` picks a small tool set from keywords and task state.
     The model can ask for more through the `request_tools` meta-tool.
   - **Context:** `ContextRouter` builds the context: local time, user profile, voice status, the pending
     action spelled out when the user is replying to one, and the full previous answer when the user
     refers back to it ("put this speech into a PDF").
4. **Tool calls.**
   - A call that requires confirmation is not executed. It becomes `pending_confirmation`, and ADVI
     speaks a short question.
   - Other calls are normalised (paths under another Windows profile remapped, a search naming a known
     site routed to that site, desktop scrolling during browsing kept in the browser) and executed.
   - A guard refuses to close an application the user did not ask to close.
   - For deliberate task families (file workflows, documents) only one state-changing call runs per model
     round, so the model sees the new state before its next decision. Independent creations of different
     items are allowed to run together.
5. **Reconciliation.** `_finalize_task_status()` derives COMPLETED / UNCERTAIN / FAILED from recorded
   outcomes. `_authoritative_task_response()` and `_no_unbacked_send_claim()` replace prose that the
   evidence does not support. When only the model's closing sentence was lost after a verified step,
   ADVI reports the step itself.

The module is large (about 6,200 lines). Its size reflects many small, log-driven rules accumulated over
the phases; Section 15 lists this as a maintainability cost.

### 7.2 Providers and failover

**Providers.** `GroqProvider` and `GeminiProvider` implement `LLMProvider`. Up to three keys each are
configured, in the order groq-1..3, then gemini-1..3.

**Failover.** `ProviderSessionManager` keeps per-provider conversation state. On an authentication error,
rate or quota limit, timeout or service error it moves to the next provider and hands over a bounded
summary of the task (`agent/session_handoff.py`). That summary is bounded to 7,000 characters; its nesting
depth limit was raised in 0.4.0 after it hid file listings from the model.

**Quota handling.**
- Keys can be grouped by quota scope, so an exhausted shared quota is not retried on each key.
- A per-minute limit shorter than 25 s is waited out once, with a spoken notice.
- A daily limit is reported with the provider's own retry time.

### 7.3 Tool boundary and capabilities

| Element | What it does |
|---|---|
| Tool specs | 88 tools are declared in `capabilities/registry.py` (`_ACTION_SEMANTICS`), each with a side-effect class and a verification method. Each has a matching parameter contract in `core/action_contracts.py`. |
| `ToolGuard.decision()` | Validates arguments against the contract-derived schema. For tools marked `requires_confirmation` it refuses unless the exact call (a SHA-256 fingerprint of name and arguments) was approved within 300 seconds. |
| Approval flow | The approval is recorded only when the user's answer is a confirmation (`_resolve_pending_confirmation`) and is consumed on use. |
| Confirmation-gated tools | `email_send`, `email_reply`, `email_forward`, `email_trash`, `memory_forget`, `whatsapp_send_message`, `telegram_send_message`. |
| Browser gate | Clicks on links whose text means pay, order, confirm, send, submit or delete return `requires_confirmation`. Shop and booking links that only open pages do not. |
| `ExecutionEngine` | Normalises the call, finds the capability, executes it, and passes the result to `ToolVerifier`. A `create_file` result that fails verification has its new file removed so a retry can reuse the name. |

The capabilities:

| Capability (registry name) | Class / module | Mechanism |
|---|---|---|
| `desktop_control` | `DesktopCapability` (`desktop/executor.py`) | Application launch (built-in names, Office paths, shortcuts, `shell:AppsFolder`), window focus and close, keys and typing (pywinauto, pyautogui, clipboard), UIA observation, OCR, `look_at_screen` |
| `desktop_ui`, `messaging` | `DesktopUiCapability`, `MessagingCapability` (`apps/`) | WhatsApp and Telegram skills over a UIA driver: search, open chat, verify the chat title, compose, send through the Send button, verify the composer cleared. Telegram reads chat state by OCR |
| `browser_control` | `BrowserCapability` (`browser/`) | Playwright attached to Chrome over CDP; numbered element snapshots; main-text reading; site search; media control checked against page media elements |
| `file_management` | `FileCapability` (`files/`) | Create (txt, docx, xlsx, pptx, pdf), read (including docx/xlsx/pptx/pdf), move/copy/rename, Recycle-Bin deletion, search by name and content, bulk moves and organising with undo, zip, hide, play local media |
| `office` | `OfficeCapability` (`office/`) | `word_create_document`: a hidden, private Word instance via COM builds styled documents (headings, lists, real tables, page numbers), saves .docx or exports .pdf, reads the file back, and dismisses activation pop-ups when opening it for the user |
| `email` | `GmailCapability` (`gmail/`, `integrations/gmail/`) | Gmail API with the `gmail.modify` scope: read, drafts, send, reply, forward, trash, archive, mark; checked against message IDs and labels |
| `memory` | `MemoryCapability` | Remember, recall, forget (gated), contacts |
| `system` | `SystemCapability` | Battery, CPU, memory, disk, OS information |
| `mcp` | `MCPCapability`, `integrations/mcp/` | Optional external tools over stdio or Streamable HTTP |
| `david_fallback` | `DavidFallbackCapability` (`fallback/`) | Delegation to DAVID (Section 7.7) |

### 7.4 How firmly the principles are enforced

| Principle | Enforcement | Strength |
|---|---|---|
| Confirmation before sends and mail deletion | `ToolGuard` refuses gated tools without a matching recent approval; covered by tests in several phases | Enforced in code |
| No claims beyond evidence | `_authoritative_task_response()`, `_no_unbacked_send_claim()`, `_no_unbacked_memory_claim()` | Enforced for sends, completion status and memory claims. The model's wording is otherwise trusted, for example in summaries of pages it read. |
| Verification of effects | Per-tool verification in capabilities and `ToolVerifier` | Varies by tool: strong for files, Word, Gmail and WhatsApp (read-back, IDs, composer state); weaker for keystroke-based actions such as media keys outside the browser, which report "key sent" |
| Recycle Bin instead of permanent deletion | `FileCapability._trash` refuses if the bin is unavailable | Enforced |
| Close only what was asked | `_close_target_refusal()` (0.4.0) | Enforced for explicit close requests; a close inside a larger task is not checked against the request |
| DAVID isolation | Reached only through the `david_fallback` tool, offered conditionally | Enforced by construction; DAVID's own success is trusted without primary verification |

### 7.5 Perception and vision

**Desktop perception.**
- **UIA first.** `perception.py` reads the UI Automation tree of the target window.
- **OCR when the tree is too sparse.** When fewer than four interactive elements are found, or the screen
  seems stalled, `vision.py` OCRs the window with Tesseract.
- **Last resort.** `screen_describer.py` sends the window, or for explicit requests the full screen, to
  Gemini (`gemini-3.1-flash-lite` by default) and returns a description in words.
- **Limits.** Automatic looks are capped at two per task and pass the window title for grounding. A circuit
  breaker pauses vision after 503/504 or quota errors.

**Browser perception** uses Playwright snapshots of interactive elements with numbered references, plus a
main-content text reader.

### 7.6 Memory

| Store | Contents |
|---|---|
| `LongTermMemory` (`runtime/memory/advi_memory.db`) | Facts, a user profile, contacts with e-mail addresses, learned chat names and misheard aliases (`chat_names`, 0.4.0), and experience lessons |
| `MemoryRetriever` | Embeds memories with `BAAI/bge-small-en-v1.5` (sentence-transformers) and falls back to lexical retrieval if the model can't load |
| `SessionConsolidator` | Summarises a session into memories at shutdown |
| `ExperienceMemory` | Stores reusable recovery lessons that are shown to the model, never executed directly |

### 7.7 DAVID fallback

DAVID (`src/advi/fallback/`) is the older planner/executor architecture, kept as an isolated subsystem. It
has its own:
- intent and planner prompts (`fallback/prompts/`);
- execution loop, executor and perception;
- vision;
- Chrome DevTools driver.

`DavidFallbackCapability` exposes it as the single tool `david_fallback`, which receives the original user
request, a reason, and optionally protected parameters. `AgentSession` offers this tool to the model only
when one of these holds:
1. the user explicitly asks ("use David", "fallback");
2. a native tool from a defined recoverable set (about thirty desktop, browser and file tools) failed in the
   current task;
3. a Gmail API action failed with a fallback recommendation (for example, no OAuth credentials).

DAVID's result returns as an ordinary `ToolResult`. A successful DAVID loop is marked verified on DAVID's
own authority (`verification_details.source = "david_execution_loop"`), not checked by the primary
verifier. DAVID Gmail sends keep a single confirmation step: the draft is prepared and checked first, and
only the send needs the user's "yes". The current code makes no other connection between the two
architectures.

### 7.8 Voice

Figure 6 shows the voice flow.

| Component | How it works |
|---|---|
| Capture and playback | `DuplexAudio` (`voice/audio_io.py`): one sounddevice stream (16 kHz mono, 30 ms blocks) plays all of ADVI's speech and cues and records the microphone in the same callback. WebRTC's audio processing module (AEC3, noise suppression, high-pass; from `livekit`) removes ADVI's own voice using what was played as the reference. The cleaned audio is cut into 32 ms frames with a 0.3 s pre-roll. Without `livekit`, the raw microphone is used. |
| End of speech | Silero VAD (bundled with faster-whisper) is run frame by frame with hysteresis. A sentence ends after 1.2 s of silence, or 2 s once the user has spoken for 3 s. A sentence that ends mid-thought ("... saying", a trailing comma) gets a further 2.5 s window, and the parts are joined. |
| Limits | No speech within 6 s closes the recording quietly; 30 s is the maximum length |
| Recognition | `DualTranscriber` uses Groq `whisper-large-v3-turbo` when reachable. Otherwise, or after any cloud error, it uses a preloaded local faster-whisper model (`distil-small.en`, int8, CPU). Cool-downs avoid repeated timeouts, the user is told once about the slower mode, and both paths filter Whisper's known silence hallucinations. |
| Floor decision | The user "starts talking" after 0.09 s of continuous speech (`Recorder.on_speech_start`). With echo cancellation on, ADVI pauses at that moment (barge-in), so the rest of the sentence is clean; if the transcript turns out to be its own leaked voice or noise it resumes where it paused, otherwise the answer ends. A transcript that is still mostly ADVI's last 20 s of speech is ignored as a second guard. Without echo cancellation, one or two words that ADVI also said ("yes" during "say yes or no") stop ADVI and hand the floor back, so a leaked "yes" cannot confirm an action. |
| Music gate | While other audio plays (read from Windows' output peak meter through comtypes, excluding ADVI's own sounds), only sentences starting with "ADVI" and plain media commands count |
| Local commands | Sleep and exit (both close ADVI completely), the sound guide, and "wake up ADVI" while running are handled without the model |
| Audio cues | `earcons.py` synthesises eight short cues in one timbre and one pentatonic scale. They are played through sounddevice so they never cut off speech, and they are taught by a spoken guide on first use and on request. |
| Hotkey | A hidden global hotkey (`RegisterHotKey`) interrupts ADVI's speech, falling back to another key combination when the first is taken. While ADVI is closed, the wake listener holds the same key and starts ADVI with it. |
| Speech output | With the duplex stream, SAPI or Piper synthesise each sentence into memory while the previous one plays; stopping is immediate. File paths are spoken as "poem.txt in Downloads". |

**Echo cancellation.** A first attempt, `echoff` (WebRTC AEC with Windows' loopback as the reference),
was rejected because the loopback delivers nothing while the PC is silent (Section 14.6). The fourth live
session showed that the word-matching filter alone could not work: ADVI's own voice came back misheard
("The protein works solid.") and was answered. 0.4.0 therefore owns its playback and uses WebRTC AEC3 with
the exact played signal as the reference, the technique of browser and phone voice assistants. Measurements
are in Section 14.6.

### 7.10 Wake word

The wake word runs in a separate background process (`src/advi/wake/`), so it works while ADVI is closed:
- **Start-up:** at every Windows sign-in (the per-user `Run` registry key; no administrator rights), and also
  by ADVI at start-up, so it is already listening when "sleep" closes ADVI. It can't listen before sign-in.
- **Process model:** a supervisor holds a named mutex (one copy only) and restarts the listening worker after
  a crash, with growing pauses. ADVI holds its own named mutex while it runs; the listener then releases the
  microphone and the hotkey.
- **Detection** follows the two-stage design of on-device assistants:
  - a cheap, always-on first stage: Silero VAD finds utterances with 0.3–4 s of speech;
  - an accurate second stage, only for those: Whisper `tiny.en` on this PC, biased to "ADVI";
  - a phrase matcher (`wake/phrase.py`) that knows Whisper's spellings of the name, excludes "advice", and
    accepts only short utterances.
- **Optional engine:** Picovoice Porcupine, a dedicated keyword spotter, used when the user configures an
  AccessKey and an "ADVI" keyword file.
- **Starting ADVI:** a wake sound plays, then `run_advi.bat` starts in its own window. A 120 s grace period
  prevents a second copy while ADVI starts.
- **Recovery:** a microphone that stops delivering audio is reopened.
- **Privacy:** heard text is never logged unless it was the wake phrase.

### 7.9 Recovery

Figure 7 shows the recovery paths.

- **Provider failure:**
  - failover across keys and providers;
  - a short wait for per-minute limits;
  - then built-in commands, or a plain explanation.
- **Tool failure:**
  - a recovery observation (UIA, OCR, at most two vision looks) is given to the model;
  - a call rejected for a bad parameter before it ran no longer fails the task;
  - a failed confirmed send can be retried exactly with "try again";
  - a chat that cannot be found leads to a request to spell the name.
- **Stopping:**
  - a voice "stop" or typed exit sets a process-wide cancellation flag (`core/cancel.py`);
  - long file searches check that flag, and the agent loop checks it between steps;
  - exit force-closes after 8 seconds if a tool does not return.

## 8. Feature inventory

Status vocabulary:
- **Implemented, tested live** means it was exercised against real applications on the development machine
  (with the run named).
- **Implemented, unit-tested** means it is covered by automated tests with fakes only.
- **Partial** means it works with stated gaps.
- **Planned** means it is not implemented.

| Feature | Purpose | Implementation | Main dependencies | Status | Evidence |
|---|---|---|---|---|---|
| Typed and spoken requests through one queue | Input | `io/console.py`, `app.py` | stdlib threading | Implemented, tested live | Live sessions in Phases 11–14 |
| Always-on voice with endpointing | Hands-free use | `voice/` | sounddevice, faster-whisper (Silero VAD) | Implemented, tested live; 0.4.0 floor-decision fix not yet re-tested live | Phase 12–14 notes; `test_phase12_voice_always_on.py` |
| Online/offline speech recognition | Works without internet | `voice/transcriber.py` | groq, faster-whisper, ctranslate2 | Implemented, measured | Section 14.5 |
| Audio cues and sound guide | Turn-taking without sight | `voice/earcons.py` | numpy, sounddevice | Implemented, unit-tested; perceived quality unvalidated | `test_phase12_voice_always_on.py` |
| Interruption by voice | Stop long answers | `voice/conversation.py`, `voice/audio_io.py` | livekit | Implemented with echo cancellation; barge-in at speech onset not yet tried live with the user's voice | Phase 15 note; `test_phase15_echo_and_wake.py` |
| Echo cancellation of ADVI's own voice | ADVI must not answer itself | `voice/audio_io.py` | livekit (WebRTC AEC3), sounddevice | Implemented, measured on the dev laptop's speakers and mic; live startup checked | Section 14.6 |
| Sleep closes ADVI | Full stop on "sleep" | `voice/conversation.py`, `app.py` | none | Implemented, tested live (typed "sleep"); spoken path unit-tested | Phase 15 note |
| Wake word | Start ADVI by voice, even when closed | `wake/` | faster-whisper (tiny.en), Silero VAD; optional pvporcupine | Implemented; launch, supervisor and hand-over tested live through the hotkey; detection tested on synthetic clips; the spoken phrase not yet tried with the user's voice; Porcupine not tried live | Section 14.8 |
| Start at Windows login | No manual launch | `wake/autostart.py` | winreg | Implemented (the wake listener starts at sign-in; ADVI starts on the wake word) | Run key written and listener started, 2026-10-25 |
| Open/close/focus applications | Desktop control | `desktop/` | pywinauto, pywin32 | Implemented, tested live | Capability sweep r1 (Notepad, Calculator) |
| Close-target guard | Safety | `agent/session.py` | none | Implemented, tested live | Sweep re-run `capabilities-r1-fixes2/3` |
| Files: create/move/copy/rename/delete/search/organize/undo/zip/hide | File management | `files/` | stdlib, Windows shell | Implemented, tested live | Desktop Suite; sweep |
| Remove empty folders | File management | `files/executor.py` | Recycle Bin | Implemented, tested live | Regression re-run 5/5 |
| Read docx/xlsx/pptx/pdf | Answer questions about files | `files/executor.py` | python-docx, openpyxl, python-pptx, PyMuPDF | Implemented, tested live (docx, xlsx) | Sweep: `cap-read-docx`, `cap-excel-read` |
| Word documents with real tables | Documents | `office/word.py` | pywin32 (Word COM) | Implemented, tested live | Sweep: three Word tasks |
| Excel / PowerPoint / PDF creation | Documents | `files/executor.py` | openpyxl, python-pptx, PyMuPDF | Implemented, tested live | Sweep |
| Browser: open, search, read, click (gated) | Web tasks | `browser/` | playwright, Chrome | Implemented, tested live | Sweep (5 sites); WebVoyager dev r1 (5 tasks) |
| YouTube and local media playback | Media | `browser/`, `files/` | playwright, Windows default player | Implemented, tested live | Sweep |
| WhatsApp messages | Messaging | `apps/whatsapp.py` | pywinauto UIA | Implemented, tested live | Phase 12–13 live sends; focus fix from Phase 13 unit-tested only |
| Telegram messages | Messaging | `apps/telegram.py` | UIA, Tesseract | Implemented, tested live in earlier phases | Phase 7 and 10 notes |
| Name learning and corrections | Misheard names | `agent/names.py`, `memory/long_term.py` | difflib | Implemented, tested live once (spelled correction), unit-tested | Phase 12–13 notes |
| Gmail read/draft/send/reply | E-mail | `gmail/` | Google API client | Implemented, tested live | Sweep (inbox, unread, send to confirmation); live session sends |
| Gmail forward/trash/archive/mark | E-mail | `gmail/executor.py` | Google API client | Implemented, unit-tested; trash tested live up to confirmation | Sweep `cap-email-trash-confirm` |
| Confirmation and correction of pending actions | Safety | `agent/session.py`, `tools/guard.py` | none | Implemented, unit-tested, used live | Phase 12–13 tests |
| Built-in commands without AI | Degraded mode | `agent/local_intents.py` | none | Implemented, unit-tested; live offline use in Phase 6 | Phase 6, 12 notes |
| Screen description | Last-resort perception | `desktop/screen_describer.py` | google-genai | Implemented, tested live | Phase 9 note |
| Long-term memory and experience lessons | Personalisation | `memory/` | sqlite3, sentence-transformers | Implemented, unit-tested | Phase 5 note |
| DAVID fallback | Recovery | `fallback/` | own CDP driver | Implemented; live use recorded in Phase 7 (Gmail through Chrome) | Phase 7 note |
| MCP client | External tools | `integrations/mcp/` | mcp | Implemented, unit-tested; not exercised live in recent phases | Tests |
| Benchmarks | Evaluation | `benchmarks/` | none beyond ADVI | Implemented, used | Section 14 |

## 9. Libraries, frameworks, tools and services

Usage below was verified by tracing imports in `src/` (an AST scan during report preparation), not only
by reading `pyproject.toml`. All runtime packages are declared there, and exact tested versions are in
`requirements-lock.txt`.

| Package | Role in ADVI | Where used | Kind | Licence |
|---|---|---|---|---|
| groq | Brain (`gpt-oss-120b`) and online speech recognition | `providers/groq.py`, `voice/transcriber.py` | Required (or Gemini) | Apache-2.0 |
| google-genai | Backup brain; screen description | `providers/gemini.py`, `desktop/screen_describer.py` | Optional (or Groq) | Apache-2.0 |
| google-api-python-client, google-auth-oauthlib | Gmail API and OAuth | `integrations/gmail/` | Optional at runtime | Apache-2.0 |
| playwright | Browser automation over CDP in the installed Chrome | `capabilities/browser/` | Required for web tasks | Apache-2.0 |
| websocket-client | Direct CDP for DAVID and helper paths | `browser/cdp.py`, `fallback/cdp_browser.py` | Required | Apache-2.0 |
| pywinauto | UI Automation access and input | `desktop/`, `apps/`, `fallback/` | Required (Windows) | BSD-3 |
| pywin32 | Word COM automation, window and process helpers | `office/word.py`, `desktop/` | Required (Windows) | PSF |
| comtypes | Output peak meter (IAudioMeterInformation) | `voice/system_audio.py` | Required (Windows) | MIT |
| pyautogui | Keyboard and mouse fallback | `desktop/`, `fallback/` | Required | BSD-3 |
| pytesseract (+ Tesseract binary) | OCR of windows | `desktop/vision.py`, `core/ocr.py`, `fallback/` | Optional (system install) | Apache-2.0 |
| Pillow | Screenshots and images | `desktop/`, `core/`, `fallback/` | Required | MIT-CMU |
| faster-whisper (CTranslate2, onnxruntime, Silero VAD) | Offline speech recognition; VAD; wake check | `voice/`, `wake/` | Required for voice | MIT |
| livekit (WebRTC audio processing module) | Echo cancellation of ADVI's own voice | `voice/audio_io.py` | Required for echo cancellation; voice falls back to the raw mic without it | Apache-2.0 |
| pvporcupine | Optional keyword spotter for the wake word | `wake/engines.py` | Optional, not installed by default; needs a Picovoice AccessKey | Proprietary (free personal tier) |
| sounddevice | Microphone capture; cue playback | `voice/` | Required for voice | MIT |
| numpy | Audio buffers; embeddings | `voice/`, `memory/` | Required | BSD-3 |
| sentence-transformers (PyTorch) | Memory embeddings (`BAAI/bge-small-en-v1.5`) | `memory/retriever.py` | Required; lexical fallback if the model fails to load | Apache-2.0 |
| python-docx, openpyxl, python-pptx | Office file read/write and read-back | `files/`, `core/verification.py` | Required | MIT |
| PyMuPDF | PDF creation, reading and checks | `files/`, `core/` | Required | **AGPL-3.0 or commercial** |
| watchdog | File-system events for the environment watcher | `environment/watchtower.py` | Required | Apache-2.0 |
| psutil | Processes, battery, disk | many | Required | BSD-3 |
| pydantic | DAVID plan and intent schemas | `fallback/` | Required | MIT |
| mcp | MCP client | `integrations/mcp/` | Optional at runtime | MIT |
| python-dotenv | `.env` loading | `core/config.py` | Required | BSD-3 |
| tzdata | Time-zone data for `zoneinfo` on Windows | `agent/provider_session.py` (indirect) | Required on Windows | Apache-2.0 |
| pytest, pytest-asyncio | Tests | `tests/` | Development | MIT / Apache-2.0 |

**System prerequisites:**
- Windows 10/11 64-bit;
- Python 3.13 with the `py` launcher;
- Google Chrome;
- optional Tesseract OCR, Microsoft Word, WhatsApp/Telegram Desktop, microphone, and Piper voice assets.

**External services:**
- Groq API (LLM and speech);
- Google Gemini API;
- Gmail API (optional);
- Hugging Face (one-time model downloads);
- optional MCP servers.

**Licensing note.** The repository currently has no licence file. PyMuPDF is distributed under AGPL-3.0
(or a commercial licence), which matters if ADVI is distributed or offered as a service. The upstream
WebVoyager task file is MIT-licensed, and its notice is kept in `benchmarks/webvoyager/upstream/LICENSE`.

## 10. Data flow and execution lifecycle

Figure 3 traces one request.

**Data path.**
1. Text (typed, or transcribed locally or by Groq) enters `AgentSession`.
2. The model receives the request and bounded context: time, profile, relevant memories, task state, and
   the results of earlier tools in the turn. Model-facing tool results are bounded to about 12,000
   characters.
3. Tool arguments pass through contract normalisation, `ToolGuard`, the capability and verification.
4. Results flow back to the model as structured payloads.

**Recorded locally.**
- The detailed log (`logs/advi.log`, rotating at 20 MB, three backups) holds structured traces of each
  stage. `core/pipeline_trace.py` redacts tokens, keys and OAuth secrets.
- Memories are stored in SQLite.

## 11. Desktop and browser interaction

Figures 4 and 5.

**WhatsApp skill** (an example of a self-verifying skill):
1. Bring the window to the front: a plain `SetForegroundWindow`, then an Alt-key unlock, then attaching
   input threads.
2. Search for the chat and read its UIA rows.
3. Click the unique match and confirm the open chat from the composer's accessible name (emoji-insensitive).
4. Type through two independent routes, verifying the composer text.
5. Invoke the Send button through UIA.
6. Require that the composer empties.

Every failed stage returns a specific message.

**Word.** Documents are built in a hidden Word instance rather than typed key by key. That proved more
reliable and also works when Office shows "Product Activation Failed" (Phase 7 note).

**Browser.** All work happens in a dedicated Chrome profile (`%LOCALAPPDATA%\ADVI\ChromeProfile`), never
the user's own. Playwright attaches over CDP on port 9222.

## 12. Configuration and environment

All configuration is through `.env`, documented line by line in `.env.example`. At least one of
`GROQ_API_KEY` / `GEMINI_API_KEY` is required; everything else has a default.

**Changes in 0.4.0:**
- **Added:** `ADVI_VOICE`, `ADVI_STT_CLOUD`, `ADVI_STT_CLOUD_MODEL`, `ADVI_STT_MODEL`, `ADVI_VOICE_LANGUAGE`,
  `ADVI_VOICE_HOTKEY`, `ADVI_VISION`, `ADVI_VISION_MODEL`.
- **Newly documented:** `TESSERACT_CMD`, `ADVI_MCP_CONFIG`.
- **Removed from the template:** `ADVI_CALL_TRACE` and `ADVI_TERMINAL_TRACE`, which no code reads.
- **Dependencies:** added `faster-whisper` and `sounddevice`; `pywin32` and `comtypes` are now declared
  explicitly (previously they arrived through pywinauto).

**Upgrading from 0.3.4** means rerunning `SETUP_ADVI.bat`. The existing `.env` is kept, and the new
settings default sensibly.

## 13. Setup and reproducibility

**What setup does.** `SETUP_ADVI.bat` runs `scripts/setup_windows.ps1`, which:
1. checks Python 3.13 and the project path length;
2. creates `.venv` and installs with `pip install -e ".[dev]"`;
3. creates `.env`;
4. pre-downloads the offline speech model;
5. reports the optional tools found;
6. compiles the sources;
7. runs the test suite.

**Fresh-copy verification (2026-10-25).** The files that would be committed were copied into new folders
(no `.venv`, `.env`, models or credentials) and the setup was run there.

| Step | Result |
|---|---|
| A 123-character folder path, Windows long paths disabled | **Failed:** pip could not write a PyTorch header file. This is a real onboarding defect: a GitHub ZIP extracted into Downloads gives a path of about 135 characters. |
| Fix | Setup now checks the path length and the `LongPathsEnabled` setting first, and stops in seconds with instructions |
| Re-run in the long path | Stopped early with the new message (verified) |
| Clean install in a 46-character path containing a space | **Passed** in 9.5 minutes (exit code 0): packages, `.env` created, offline speech model, Tesseract and Chrome found, 891 tests passed (Appendix C) |

`requirements-lock.txt` pins the 128 packages of the tested environment.

## 14. Testing methodology and results

### 14.1 Automated tests

`tests/current/` has 83 test files. On 2026-10-25, the command
`.venv\Scripts\python.exe -m pytest -p no:cacheprovider --deselect tests/current/test_groq_smoke.py`
gave **963 passed** (one live Groq test deselected) in about 95–100 s on the development machine.

`conftest.py` keeps every test offline and safe:
- a fake Recycle Bin, and no clipboard;
- vision and voice disabled;
- the built-in command mode cleared.

Behaviour is tested through fakes of providers, drivers and devices. Tests cover:
- the guard and confirmation flow;
- corrections, name resolution and learning;
- endpointing, echo and floor logic, and earcon shape;
- online/offline recognition switching;
- Office output structure (including that tables are real tables);
- benchmark harness logic.

### 14.2 Desktop Suite (23 tasks, execution-checked)

**Setup.** The split was fixed by seed before any run: 9 dev tasks and 14 test tasks. Each task runs in its
own sandbox folder and is scored by checking the real end state.

**Dev rounds:** 6/9 → 8/9 → 9/9, with general fixes after each round (`docs/benchmarks/DESKTOP_RESULTS.md`).

**Test split (`test-v1`).** Only 5 of 14 test tasks were run (3 passes, 2 failures, plus one run lost to a
provider outage) before fixes were made at the developer's request from failures in scored tasks. The
test split is therefore no longer a held-out measure. This is disclosed in the results file.

**Full regression run, 2026-10-24** (all 23 tasks, regression only, not a score): **20/23**.
- The three failures were remove-empty, notepad-draft and notepad-count, each traced to a defect: no
  emptiness tool, a Notepad restored tab with a wrong user profile path, and the controller opening a file
  as a folder.
- After fixes, those three plus two tasks whose answers were mis-worded were re-run: **5/5**.

### 14.3 Capability sweep (38 live tasks)

The 38 tasks cover:
- **Office:** Word, Excel, PowerPoint, PDF;
- **files:** read, open/close, create, rename, copy, delete, content search, count, largest file,
  recommend, organise;
- **media:** video and audio play and close, YouTube play and pause;
- **web:** Google, Amazon, Flipkart, GitHub, a Wikipedia question;
- **e-mail:** inbox, unread count, and send and trash up to the confirmation;
- **system:** time, disk, Calculator.

They were written in the same session in which the fixes were made, so they demonstrate features. They do
not measure generalisation.

**First full pass:** 31/38. The defects found included:
- **Safety:** an attempt to close an unrelated browser window.
- **Handoff:** a failover handoff that hid file details from the model.
- **Wrong site:** "search Amazon" carried out on Google.
- **Overridden answers:** inbox answers replaced by an over-broad "unbacked send" guard; the time question
  refused by the model.
- **Media:** files not found ("tone.wav", "video clip").

**After fixes:** every failed or mis-worded task was re-run.
- `capabilities-r1-fixes`: **15/15**.
- `capabilities-r1-fixes2`: **2/3**. The video task failed on a new phrasing ("video clip").
- `capabilities-r1-fixes3`: **2/2** after the fix.

Every one of the 38 capabilities therefore passed at least once on the final code. The whole sweep was not
re-run end to end afterwards.

### 14.4 WebVoyager (Emergence maintained version)

The harness instantiates all 535 tasks safely (no `eval`). The sample is fixed: 30 dev and 45 test tasks.

One dev batch was run: **5 tasks, unjudged.**
- Three ended at ADVI's step limit (repeated searches, a stale element reference, a site without search).
- One answer was overwritten by a status message.
- One "Buy" link that only opens a page was gated as a purchase.

These were fixed as general rules. **No WebVoyager score exists**; the test split has not been run.

### 14.5 Speech recognition measurements

Measured on the development laptop (i5-8265U, no GPU) with Windows-synthesised command recordings:

| Engine | Time per command | Notes |
|---|---|---|
| Local large-v3-turbo (int8) | 12–15 s | Whisper encodes a fixed 30 s window, so short commands cost the same; `chunk_length` did not help |
| Local small.en | ~3 s | |
| Local distil-small.en | ~2.8 s (benchmark), ~2 s in the full pipeline | Chosen default; spelled the test name correctly |
| Local base.en | ~1 s | More name errors |
| Groq whisper-large-v3-turbo | 0.45–0.76 s | End to end, including upload |
| Local fallback after a simulated outage | 2.5–2.8 s | About 2.5 s extra once, for the first refused connection |

A comma-separated vocabulary prompt made distil-small.en loop ("Open, Note, Open, ..."), so no prompt is
used.

### 14.6 Echo cancellation evaluation

`echoff` (WebRTC AEC with a WASAPI loopback reference) was tested with synthetic speech played through the
speakers and nobody talking.

| Measurement | Result |
|---|---|
| Frames delivered in 8 s of system silence | 0 (the loopback is silent, so always-on listening would go deaf) |
| Reported suppression | 0.6 dB (its quality check never became ready) |
| Raw microphone, 45 s of loud speaker speech | One false utterance |

The library was removed. A 40-second startup test in a quiet room produced no false commands.

**WebRTC AEC3 with ADVI's own playback as the reference (0.4.0).** 29 s of SAPI speech was played through
the laptop speakers through one duplex stream while nobody talked:

| Signal | Silero speech frames | Utterances | Whisper on the whole recording |
|---|---|---|---|
| Raw microphone | 53 | 1 | ADVI's sentences, including its greeting |
| After AEC (delay 210 ms = reported in + out latency) | 0 | 0 | a short hallucination on 30 s of near-silence, which the VAD gate never sends to Whisper |

A synthetic user command added at −30 to −18 dBFS on top of ADVI's speech kept 37–59 speech frames after
AEC, so interruption works. Recognition while both voices overlap was weaker, which is why ADVI stops
speaking at the onset of the user's speech. In two live starts afterwards, ADVI spoke its long greeting
with the microphone open and nothing was heard.

### 14.7 Live use

The developer ran three recorded voice sessions (Phases 12–14) and several earlier typed sessions. They
surfaced most of the voice and messaging defects fixed in 0.4.0. These sessions are qualitative evidence,
not measurements. The final 0.4.0 voice changes (onset-based floor decision, redesigned cues, short spoken
confirmations) have not yet been tried in a live session.

### 14.8 Wake word

These measurements used Windows' two voices (Hazel, Zira), 6 wake phrases and 17 other phrases each, all
synthetic:

| Configuration | Wake phrases found | Other phrases that started ADVI | Time per check |
|---|---|---|---|
| tiny.en + first matcher | 8/12 | 1/34 | 0.34 s |
| tiny.en biased to "ADVI" + first matcher | 12/12 | 1/34 ("Look at V") | 0.40 s |
| base.en biased + first matcher | 10/12 | 1/34 | 0.91 s |
| Full chain (Recorder + Silero + tiny.en biased + final matcher, clips streamed as frames with noise) | **12/12** | **0/34** | about 0.4 s |

- **Development set:** the final matcher rule (a split name like "at V" needs "wake" or a greeting) was made
  after seeing these clips, so the last row confirms the fix but doesn't measure accuracy on new speech or
  on the user's voice.
- **Resources:** idle, the worker used 2.6% of one core and 202 MB.

**Live checks (2026-10-25):**

| Check | Result |
|---|---|
| ADVI started through the listener's hotkey | ADVI's process existed 2 s after the key. "Listening" came after 54 s on the first, cold launch and 12.2 s on the second |
| Typed "sleep" | Closed ADVI in 5–6 s, twice, and the listener took back the microphone at once |
| A killed worker | Restarted by the supervisor in 7 s |

**Not verified live:** the wake phrase was played through the laptop's own speakers and never reached the
listener, because the laptop microphone's echo suppression removes its own speaker output. The spoken wake
phrase still has to be tried with a human voice.

## 15. Known limitations

- **Windows only.** Much of the code depends on UI Automation, COM and Win32.
- **Dependence on hosted models.**
  - Free tiers impose rolling daily token limits and per-minute limits. A day of testing exhausted all
    three Groq keys more than once.
  - The Gemini backup's free brain quota is small and shared across keys.
  - Without providers, only built-in commands work.
- **Voice.**
  - The wake word was tested on synthetic voices and through the hotkey, not yet with the user's voice. It
    can't listen before Windows sign-in.
  - Echo cancellation covers ADVI's own sounds only; music from other apps is handled by the "ADVI, ..." gate.
  - Behaviour was validated by one developer on one laptop.
  - Offline recognition takes 2–3 s per command on that CPU.
- **App skills depend on current app versions.** WhatsApp and Telegram skills depend on the apps' current
  accessibility trees and window titles. Telegram relies on OCR.
- **Verification is uneven across tools** (Section 7.4). DAVID's results are trusted, not independently
  verified.
- **Evaluation.**
  - The Desktop Suite test split is contaminated by fixes (disclosed).
  - The capability sweep was authored alongside the fixes.
  - No WebVoyager score.
- **Maintainability.**
  - `agent/session.py` is about 6,200 lines of accumulated rules.
  - No linter, formatter or type checker is configured.
- **Personal data in committed tests.** Committed test files from earlier phases contain the developer's
  own e-mail address and a contact's address as test data; they were already published.

## 16. Security, privacy and operations

**What leaves the computer:**
- request text, conversation context and tool results (which can include file contents ADVI read, or e-mail
  content) go to Groq or Gemini;
- microphone audio goes to Groq when `ADVI_STT_CLOUD=on`;
- window screenshots go to Gemini for screen descriptions (`ADVI_VISION=on`, at most two automatic looks per
  task);
- Gmail API calls go to Google.

**What stays local:**
- `.env` and `credentials/`;
- the SQLite memory;
- the detailed log (which contains request text and tool arguments);
- the Chrome profile;
- downloaded models.

All of these are git-ignored, as are raw benchmark results.

**Safeguards:**
- confirmation for sends and mail deletion, enforced in `ToolGuard`;
- Recycle-Bin-only file deletion;
- the close-target guard;
- gated payment and submit clicks;
- no CAPTCHA solving;
- a separate browser profile;
- secret redaction in traces.

**Operations.** Typed or spoken exit stops ADVI, even mid-task. Logs rotate.

## 17. Design trade-offs

| Decision | Alternatives considered | Rationale |
|---|---|---|
| Hosted LLM brain | Local models | The laptop has no GPU; tool-use quality and latency needed a large hosted model. The cost is dependence on quotas and the network. |
| Structure-first perception, vision in words only | Screenshot-driven agents | Accessibility trees are cheaper, deterministic and verifiable. Pixel coordinates from a model are not checkable. |
| One state-changing step per round for deliberate tasks | Executing whole model plans | The model sees the result before choosing the next step; independent creations are exempted to avoid loops |
| Word via hidden COM instance | Typing into Word | Far fewer model rounds, robust to focus changes and to the "activation failed" state |
| Dual speech recognition with a preloaded local model | Cloud only; local Turbo | Offline use is essential for a blind user. Local Turbo was too slow without a GPU, so a smaller local model with a one-time "slower" notice was chosen. |
| Floor decided at speech onset, plus word-level echo filter | Half duplex; WebRTC AEC | Half duplex prevented interruption. The AEC library stalled on silence and gave negligible suppression on this hardware. |
| Few, taught earcons in one timbre | Spoken prompts; many sounds | Spoken prompts are slow and get recorded; Google's guidance is to use few, consistent earcons and to teach them |
| DAVID as an isolated tool | Merging architectures | Preserves a working fallback without letting its planner influence the primary path |

## 18. Future work

These items are not implemented:
- measuring the wake word's false wake-ups over days of real room audio, and its detection rate on several
  speakers; the optional Porcupine engine with a trained "ADVI" keyword;
- a Desktop Suite v2 with new, unseen test tasks for a clean score;
- running and human-judging the WebVoyager test sample;
- splitting `agent/session.py` by concern;
- adding a linter and a type checker;
- choosing a licence;
- replacing committed personal test addresses with placeholders;
- re-validating the 0.4.0 voice changes in live sessions with blind users.

## 19. Conclusion

ADVI 0.4.0 is a working Windows voice assistant whose central property is that the program, not the
model, decides what counts as done.
- **Enforced in code:** confirmation for sends and mail deletion, reconciliation of answers with
  evidence, and conservative deletion.
- **Tested live:** most capabilities were exercised against real applications on the development machine,
  through an execution-checked task suite and a feature sweep.
- **Not yet established:** an unbiased benchmark score, broader voice validation with its intended users,
  and independence from hosted model quotas.

The engineering record in `docs/audits/` makes each rule traceable to the failure that motivated it, which
should help the next contributors change it safely.

## 20. References

Papers (copies of the first two were consulted locally):
- He, H. et al. *WebVoyager: Building an End-to-End Web Agent with Large Multimodal Models.* Zhejiang
  University, Tencent AI Lab, Westlake University.
- Abuelsaad, T. et al. *Agent-E: From Autonomous Web Navigation to Foundational Design Principles in Agentic
  Systems.* Emergence AI, July 2024.
- Emergence AI. *EmergenceWebVoyager* (task set and tooling, MIT licence).
  https://github.com/EmergenceAI/EmergenceWebVoyager (commit `e988bb1`, licence retrieved 2026-10-25).

Guidance and evaluated software:
- Google. *Earcons.* Conversation design guidelines for Google Assistant.
  https://developers.google.com/assistant/conversation-design/earcons
- *echoff*: WebRTC echo cancellation for voice agents (evaluated and not adopted).
  https://github.com/koljab/echoff
- WebRTC audio processing (AEC3) through LiveKit's Python SDK: https://github.com/livekit/python-sdks
- Picovoice Porcupine (optional wake-word engine): https://github.com/Picovoice/porcupine

Software used (from installed package metadata):
- faster-whisper: https://github.com/SYSTRAN/faster-whisper
- playwright-python: https://github.com/Microsoft/playwright-python
- pywinauto: https://github.com/pywinauto/pywinauto
- sentence-transformers: https://www.SBERT.net
- groq-python: https://github.com/groq/groq-python
- python-genai: https://github.com/googleapis/python-genai
- sounddevice: https://python-sounddevice.readthedocs.io/
- PyMuPDF: https://pymupdf.readthedocs.io/
- Model Context Protocol SDK: https://modelcontextprotocol.io

## Appendix A: module inventory

Line counts are from 2026-10-25.

| Package | Modules (lines) |
|---|---|
| `agent/` | `session.py` (~6,200), `provider_session.py` (944), `local_intents.py` (340), `contracts.py` (254), `session_handoff.py` (220), `names.py` (174), `turns.py` (168) |
| `capabilities/` | `desktop/executor.py` (2,916), `files/executor.py` (2,341), `browser/executor.py` (2,276), `desktop/perception.py` (1,120), `desktop/vision.py` (989), `browser/cdp.py` (684), `registry.py` (665), `gmail/executor.py` (454), `browser/pw_session.py` (388), `apps/uia.py` (372), `apps/whatsapp.py` (349), `memory/executor.py` (352), `apps/ui_observer.py` (253), `apps/telegram.py` (241), `files/pdf_layout.py` (241), `office/word.py` (220), `desktop/screen_describer.py` (206), `files/bulk.py` (198) |
| `core/` | `verification.py` (1,035), `action_contracts.py` (501), `execution_strategy.py` (296), `pipeline_trace.py` (286), `context_router.py` (238), `execution_controller.py` (190), `execution_engine.py` (167), `health.py` (164), `perception_controller.py` (145), `runtime.py` (144), `config.py` (137) |
| `voice/` | `conversation.py` (370), `transcriber.py` (269), `recorder.py` (228), `earcons.py` (120), `listener.py` (113), `system_audio.py` (103), `hotkey.py` (90) |
| `memory/` | `long_term.py` (1,388), `retriever.py` (295), `consolidator.py` (273), `experience.py` (224) |
| `fallback/` (DAVID) | `perception.py` (993), `planner_service.py` (991), `vision.py` (865), `executor.py` (829), `cdp_browser.py` (744), `execution_loop.py` (240), `service.py` (202) |
| `providers/` | `gemini.py` (773), `groq.py` (608) |
| `io/`, `tools/`, `environment/`, `integrations/` | `tts.py` (315), `output.py` (313), `tools/contracts.py` (268), `tools/guard.py` (193), `environment/watchtower.py` (930), `integrations/gmail/service.py` (709), `integrations/mcp/manager.py` (298) |

## Appendix B: commands

```powershell
.\SETUP_ADVI.bat                                                                        # install
.\run_advi.bat                                                                          # run
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --deselect tests/current/test_groq_smoke.py   # tests
powershell -ExecutionPolicy Bypass -File .\scripts\smoke_windows.ps1                    # live Windows smoke test
.\.venv\Scripts\python.exe -m benchmarks.desktop.run --tasks capabilities --name <run>  # capability sweep
python scripts\build_release.py                                                         # source ZIP (excludes secrets, models, results)
```

## Appendix C: fresh-install record

All of these runs were on 2026-10-25, on the development laptop with Windows 11 and Python 3.13.5. The fresh copies held only the files that would be committed. There was no `.venv`, `.env`, model, credential or log.

| Check | Command | Result |
|---|---|---|
| Install in a 123-character path | `.\SETUP_ADVI.bat` | Failed: pip could not write a PyTorch header (Windows path limit; `LongPathsEnabled` = 0) |
| Same path after adding the path guard | `.\SETUP_ADVI.bat` | Stopped within seconds with the long-path instructions (exit code 1) |
| Install in `%TEMP%\advi fresh\ADVI` (46 characters, contains a space) | `.\SETUP_ADVI.bat` | Exit code 0 in 9.5 minutes. `.env` created; offline speech model downloaded; Piper reported missing (SAPI fallback); Tesseract and Chrome found; sources compiled; 891 tests passed |
| First launch with no key | `.\run_advi.bat` (voice off) | Printed "No language-model provider is configured or could be initialized." and shut down cleanly. The launcher returns exit code 0 in this case |
| Full suite on the final working tree (before Phase 15) | `python -m pytest -p no:cacheprovider --deselect tests/current/test_groq_smoke.py` | 891 tests, 0 failures, 0 errors, 0 skipped, 85.8 s |
| Full suite after Phase 15 (development checkout) | same | 963 passed |

Seen but harmless: Hugging Face prints a warning that symlinks aren't supported in `assets/whisper`, and PyMuPDF warns that the `fitz` import is deprecated.

Not verified from a fresh copy: a launch *with* keys, voice input, Gmail OAuth, and the live app features. Those were checked in the development checkout (Section 14).
