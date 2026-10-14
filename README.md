# ADVI

**Autonomous Desktop for Visually Impaired — Engineering Release 0.3.4**

ADVI is a Windows desktop agent whose design goal is to let an LLM reason about user requests while deterministic local software controls the real machine safely and verifies what actually happened.

## Core principle

> The LLM owns cognition. Local software owns reality.

The model decides what to do. Local software validates, executes, observes, verifies, enforces safety, and reports evidence.

## Current runtime

```text
User
  -> AgentSession / LLM
  -> ToolCall
  -> ToolGuard / ToolRunner
  -> ExecutionEngine
  -> Capability
  -> REAL WORLD
  -> Observation / Verification
  -> ToolResult
  -> LLM continuation or final response
```

The native runtime has one execution owner. Capabilities do not invent conversational outcomes; the execution/verification layer owns truth about what actually happened.

Desktop interaction maintains an exact target window (HWND/PID/title when available), re-establishes focus before keyboard/mouse input, and exposes current window/dialog state so unexpected foreground changes do not silently redirect input.

Dynamic runtime context is bounded before it reaches an LLM provider. Provider calls have an interactive deadline and failover is limited to errors another provider can plausibly recover from.

David is kept behind an explicit fallback boundary for unsupported or irregular GUI work. Its internal planner/execution vocabulary remains isolated from the native path.

## Capability surface

ADVI currently exposes native support for desktop/window observation and recovery, OCR, browser/CDP control and form interaction, filesystem create/read/write/append/replace/delete/copy/move/rename/folder operations plus filename and content search, common document readers (PDF/DOCX/XLSX/PPTX), document save/save-as/close lifecycle, Gmail read/draft/update/send/reply flows, durable memory/contact lookup, TTS, and David fallback.

OCR is optional at the OS level because Tesseract itself must be installed on Windows. Piper voice assets are also optional; Windows built-in speech is used as a fallback when Piper is unavailable.

## Memory

ADVI separates core durable memory, associative recall, and deeper explicit memory operations. Long-term storage is local SQLite-based. Semantic retrieval is optional.

## Browser

The browser runtime uses an ADVI-owned CDP context. Exact tab ownership is retained across navigation and verification.

## David

David is a supported, isolated fallback subsystem. The primary AgentSession can delegate genuinely unsupported desktop/browser tasks to `david_fallback`, and can use the same path when a native capability such as Gmail is unavailable or environment-blocked. David keeps its own intent/planning/execution internals behind the adapter boundary. Consequential delegated actions remain subject to the same approval and verification expectations as native execution.

## Testing

Build-environment checkpoint:

```text
python -m compileall -q src
python -m pytest
```

The default suite covers the trusted current runtime. Legacy compatibility and the historical foundation gate are separate explicit validation targets.

The default suite covers `tests/current`. It validates contracts, task lifecycle, provider handoff, browser ownership, filesystem semantics, memory, desktop state handling, and capability/verifier consistency. The live Windows acceptance gate remains separate because this development environment is not Windows.

## Setup

On Windows, the quickest supported setup is:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

See `SETUP_WINDOWS.md` for manual installation and `docs/WINDOWS_VALIDATION.md` for the real Windows capability gate.

Voice input / speech-to-text is not part of the current production console. Speech output prefers Piper when its optional runtime/model assets are present, with Windows built-in System.Speech used as a fallback when Piper assets are absent. Visual OCR uses Tesseract; ADVI automatically checks common Tesseract installation locations. Image files can also be read through OCR, and common PDF, Word, Excel, and PowerPoint documents have native readers.

## Release status

Source release: **0.3.4**. The release package contains source, current tests, setup scripts, and Windows validation documentation, while intentionally excluding local secrets, OAuth credentials, runtime databases, logs, caches, and generated build artifacts. Semantic-memory embeddings are preloaded during startup, quietly and before interactive chat; interactive turns never start a model loader or stream dependency logs into the console.

## Architecture status

The retired brain/planner/action-plan orchestration has been removed from the product runtime. David remains the supported isolated fallback and owns its remaining legacy Action/ActionPlan vocabulary under `advi.fallback`.


## Groq key failover

ADVI supports up to three Groq API keys through `GROQ_API_KEY`, `GROQ_API_KEY_2`,
and `GROQ_API_KEY_3` (or `GROQ_API_KEYS`). The existing provider manager treats each
key as a separate route and automatically moves to the next healthy key after an
authentication failure, 429/rate-limit response, timeout, connection failure, or
Groq 498/5xx service error. The current task handoff is reused during a switch, so
the user request is not blindly sent twice.

`GROQ_KEY_GROUPS` optionally maps keys to a shared quota scope. Leave it empty for
independent key-level routing. When multiple keys belong to the same Groq project or
other shared quota scope, assign them the same group so ADVI will avoid wasting retries
against sibling keys that cannot recover the same quota failure.

## MCP integration
ADVI can connect to configured Model Context Protocol servers through `mcp_servers.json`.
Copy `mcp_servers.json.example` to `mcp_servers.json`, configure one or more stdio or
Streamable HTTP servers, and explicitly ask ADVI to use MCP tools. Ordinary turns do not
receive the MCP tool surface.
