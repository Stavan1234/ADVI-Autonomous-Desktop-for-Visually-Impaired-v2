# ADVI — Windows Setup (reference)

Most people only need the quick start in `README.md`. This page is the detailed reference.

## Requirements

- Windows 11 64-bit
- Python 3.13.x
- Google Chrome
- An API key for at least one supported LLM provider
- Tesseract OCR (recommended): needed for Telegram and screen reading; `winget install UB-Mannheim.TesseractOCR`

## Install

From the ADVI repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The normal setup installs semantic-memory support. At startup, ADVI preloads the embedding model before interactive chat begins; if that preload is unavailable, ADVI keeps lexical memory retrieval available and does not start a model loader during an interactive turn.

Copy `.env.example` to `.env` and configure at least one provider. Do not commit `.env` or Gmail credentials.

For the normal full setup, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

## Groq keys

Put your keys in `.env`:

```text
GROQ_API_KEY=...
GROQ_API_KEY_2=...
GROQ_API_KEY_3=...
```

ADVI starts with `GROQ_API_KEY` and automatically fails over to the next healthy Groq
key when a key returns an authentication error, rate limit/quota error, timeout,
connection failure, or temporary Groq service error. Failover preserves the current
task handoff instead of resending the same user message as a fresh task.

Optional quota grouping is controlled by `GROQ_KEY_GROUPS`. Example:

```text
GROQ_KEY_GROUPS=groq-1,groq-2,groq-3
```

For keys that share one Groq project/quota scope, use the same group for those keys:

```text
GROQ_KEY_GROUPS=shared,shared,shared
```

The three keys do not automatically create three independent quotas. Groq rate limits
can be shared by the organization/project that owns the keys, so separate keys only
provide independent failover where their underlying quota scopes are actually separate.

## Gmail

Place your OAuth client credentials at:

```text
credentials/gmail_credentials.json
```

ADVI creates `credentials/gmail_token.json` after the normal Google authorization flow. Gmail is optional; the application can run without it.

## Speech output

The `assets/` folder is optional in the current source archive. When Piper assets are present, ADVI uses them; when they are absent, ADVI falls back to Windows built-in speech output where available.

Expected files:

```text
assets/piper/piper.exe
assets/models/en_US-lessac-medium.onnx
assets/models/en_US-lessac-medium.onnx.json
```

You can override these paths with `ADVI_PIPER_EXE` and `ADVI_PIPER_MODEL`.

## Run

After installation, start ADVI with:

```powershell
.\run_advi.bat
```

Or:

```powershell
.\.venv\Scripts\python.exe -m advi.app
```

## Verify

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m pytest
```

For actual Windows capability validation, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\smoke_windows.ps1
```


## MCP (optional)

MCP support is installed with the normal dependency set. To configure external
MCP servers, copy `mcp_servers.json.example` to `mcp_servers.json` and edit the
server definitions. Supported transports in this release are local `stdio` and
Streamable HTTP. Ask ADVI explicitly to use MCP/external tools; the normal model
tool surface stays small.
