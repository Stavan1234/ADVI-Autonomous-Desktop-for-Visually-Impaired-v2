# Developing ADVI

This guide takes a developer from a fresh clone to a running, tested ADVI. For what each part of the code
does, see [ARCHITECTURE.md](ARCHITECTURE.md); for fixes to common problems, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Windows 10 or 11, 64-bit | ADVI drives Windows apps through UI Automation, COM and Win32. It does not run on macOS or Linux. |
| Python **3.13** (64-bit) with the `py` launcher | `pyproject.toml` requires `>=3.13,<3.14`. Developed with 3.13.5. |
| Google Chrome | Web tasks run in ADVI's own Chrome profile (Playwright connects to the installed Chrome; no browser download). |
| Git | To clone. A ZIP download also works. |
| An AI key | At least one of Groq ([console.groq.com/keys](https://console.groq.com/keys)) or Gemini ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)). Both have free tiers. |
| Microphone and speakers | Optional; needed for voice. Typing always works. |
| Tesseract OCR | Optional; used for Telegram and screen reading: `winget install UB-Mannheim.TesseractOCR` |
| Microsoft Word | Optional; used for Word documents and Word-made PDFs. |
| WhatsApp / Telegram Desktop | Optional; signed in once by the user. |

Disk space: about 1.5 GB for `.venv` (PyTorch, used for the memory embeddings, is the largest part) plus
about 320 MB for the offline speech model. Measured on the development machine.

## 2. Install

```powershell
git clone https://github.com/Stavan1234/ADVI-Autonomous-Desktop-for-Visually-Impaired-v2.git ADVI
cd ADVI
.\SETUP_ADVI.bat
```

`SETUP_ADVI.bat` runs `scripts/setup_windows.ps1`, which:
1. checks for Python 3.13 through the `py` launcher;
2. creates `.venv` (it reuses an existing one, so it is safe to rerun);
3. installs ADVI with `pip install -e ".[dev]"`;
4. copies `.env.example` to `.env` unless `.env` already exists;
5. downloads the offline speech model into `assets/whisper` (a failure is only a warning);
6. downloads the small wake-word model (`tiny.en`, about 75 MB);
7. reports whether Piper, Tesseract and Chrome were found;
8. compiles the sources and runs the test suite (the live Groq test is skipped);
9. registers the wake-word listener to start at every Windows sign-in (`python -m advi.wake install`; skipped
   when `ADVI_WAKE=off`).

It stops with a clear message if Python 3.13, the install or the tests fail. Expect 10 to 20 minutes on
a first run, mostly downloads.

Manual equivalent:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env
```

To reproduce the exact package versions ADVI 0.4.0 was tested with, use the lock file instead of the
version ranges in `pyproject.toml`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

## 3. Configure

Open `.env` and set at least one key:

```text
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=...
```

Every other setting is optional and documented in `.env.example`. The ones people change most often:

| Setting | Default | Meaning |
|---|---|---|
| `ADVI_VOICE` | `on` | `off` disables the microphone and the hotkey |
| `ADVI_ECHO_CANCEL` | `on` | WebRTC echo cancellation of ADVI's own voice (`voice/audio_io.py`); `off` only for troubleshooting |
| `ADVI_WAKE` | `on` | The background wake-word listener (`advi/wake`); `off` stops ADVI from starting it and setup from registering it |
| `ADVI_WAKE_MODEL` | `tiny.en` | Local Whisper model for the wake check |
| `ADVI_STT_CLOUD` | `on` | `off` keeps audio on this computer (local Whisper only) |
| `ADVI_STT_MODEL` | `distil-small.en` | Local speech model |
| `ADVI_VISION` | `on` | Gemini screen descriptions; `off` never sends screenshots |
| `ADVI_LOCAL_COMMANDS` | `fallback` | Built-in commands only when the AI is unavailable (`first`, `off`) |
| `ADVI_CONSOLE_LOG_LEVEL` | `WARNING` | Console verbosity; the file log is always detailed |

**Gmail through the API (optional).**
1. In Google Cloud, create a project, enable the Gmail API, and create an OAuth client of type "Desktop app".
2. Save its JSON as `credentials/gmail_credentials.json`.
3. The first Gmail request opens Google's consent page and stores `credentials/gmail_token.json`.

Without this, ADVI uses Gmail through Chrome (the DAVID fallback).

**Several Groq keys.**
- ADVI starts with `GROQ_API_KEY` and moves to the next healthy key on an authentication error, a rate or
  quota limit, a timeout, or a Groq service error. The current task's context is handed over, not resent as a
  new request.
- Keys only add capacity if they belong to different Groq accounts. If several keys share one account, give
  them the same group so a used-up quota isn't retried on each: `GROQ_KEY_GROUPS=shared,shared,shared`.
- Gemini keys are treated as one group (`gemini-default`) unless `GEMINI_KEY_GROUPS` says otherwise.

**MCP servers (optional).** Copy `mcp_servers.json.example` to `mcp_servers.json` and edit it. Local
`stdio` and Streamable HTTP servers are supported. Ask ADVI explicitly to use external tools; the normal
tool surface stays small.

## 4. Run

```powershell
.\run_advi.bat
```

or `.\.venv\Scripts\python.exe -m advi.app`.

**Startup.**
1. ADVI says "Just preparing everything, sir..." while it loads providers, the memory embeddings, the speech
   model and MCP servers in parallel.
2. It runs a startup health check.
3. It prints:

   ```text
   ADVI › Everything is ready, sir. I'm listening; just talk to me. ...
   ```

   followed by the banner and the `You ›` prompt.

**Failures.**
- With no AI key, ADVI prints "No language-model provider is configured or could be initialized." and exits.
- If a blocking health check fails, it prints "ADVI cannot safely enter interactive mode yet." and exits.
- An unexpected startup error prints "ADVI could not finish preparing, so interactive mode was not
  started." Details are in `logs/advi.log`.

**Stopping.** Type `exit` (or `quit`, `sleep`) or say "exit" / "close ADVI". If a task is stuck, ADVI asks it
to stop and force-closes after 8 seconds.

## 5. Test

```powershell
# Full offline suite (no network, no microphone, no real screenshots): about 100 s
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --deselect tests/current/test_groq_smoke.py

# One file, verbose
.\.venv\Scripts\python.exe -m pytest tests/current/test_voice_input.py -v

# Live Groq call (needs GROQ_API_KEY)
.\.venv\Scripts\python.exe -m pytest tests/current/test_groq_smoke.py

# Live Windows smoke test without the AI: desktop access, file round-trip,
# Chrome CDP on a temporary profile, Gmail credential readiness, watcher events
powershell -ExecutionPolicy Bypass -File .\scripts\smoke_windows.ps1
```

`tests/current/conftest.py` keeps the suite safe and offline. Every test:
- deletes through a fake Recycle Bin;
- never touches the clipboard;
- runs with `ADVI_VISION=off`, `ADVI_VOICE=off` and `ADVI_WAKE=off`;
- uses `ADVI_LOCAL_COMMANDS` cleared.

Most tests use small fake providers, engines and devices instead of mocks of internals.

**Live benchmarks** (they spend AI quota and operate the real desktop; see `benchmarks/README.md`):

```powershell
.\.venv\Scripts\python.exe -m benchmarks.desktop.run --split dev                 # scored Desktop Suite
.\.venv\Scripts\python.exe -m benchmarks.desktop.run --tasks capabilities        # 38-task feature check
.\.venv\Scripts\python.exe -m benchmarks.webvoyager.run --split dev --batch 8    # WebVoyager sample
```

Raw results go to `benchmarks/results/` (git-ignored: it holds screenshots and records of your own
mail and files). Summaries belong in `docs/benchmarks/`.

**The wake word.** `python -m advi.wake` runs a supervisor that keeps one listening worker alive
(`src/advi/wake/listener.py`). Useful commands:

```powershell
.\.venv\Scripts\python.exe -m advi.wake status      # installed at sign-in? listening? ADVI running?
.\.venv\Scripts\python.exe -m advi.wake install     # register at sign-in and start now
.\.venv\Scripts\python.exe -m advi.wake uninstall   # unregister and stop it
```

While developing, a running listener holds the microphone only while ADVI is closed. If you change code under
`src/advi/wake/`, run `uninstall` then `install` so the listener restarts with it.

## 6. Where things are written

| Path | Contents | Safe to delete? |
|---|---|---|
| `.venv/` | Python environment | Yes; rerun setup |
| `.env` | Your keys and settings | Only if you want to reconfigure |
| `credentials/` | Gmail OAuth client and token | Deleting the token means signing in again |
| `logs/advi.log` | Detailed log, rotated at 20 MB (3 old files kept) | Yes |
| `logs/wake.log` | Wake listener log (starts, restarts, wake phrases only), rotated at 2 MB | Yes |
| `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `ADVI Wake` | Starts the wake listener at sign-in | Remove with `python -m advi.wake uninstall` |
| `runtime/memory/advi_memory.db` | Long-term memory: facts, contacts, learned chat names | Yes, but ADVI forgets what it learned |
| `runtime/screenshots/`, `runtime/audio/` | OCR captures and temporary speech audio | Yes |
| `runtime/voice_sound_guide_played` | Marker so the sound guide plays only once | Yes; the guide plays again |
| `assets/whisper/` | Offline speech models | Yes; downloaded again on next start |
| `%LOCALAPPDATA%\ADVI\ChromeProfile` | ADVI's own Chrome profile (your web sign-ins for ADVI) | Yes; sign in again |

Everything in this table except `.env.example` is git-ignored.

## 7. Conventions

- **Python 3.13 with `from __future__ import annotations`.** Each module starts with a docstring saying what
  it is for.
- **A fix that comes from a real failure says so in a comment:** the log date and what went wrong. This is
  how the code records why a rule exists. The phase notes in `docs/audits/` collect the larger changes.
- **Every behaviour change gets a test in `tests/current/`.** Use fakes for providers, drivers and devices;
  tests must not need the network, a microphone or a real desktop.
- **The language model only proposes.** New side effects belong in a capability that returns a
  `ToolResult` with honest `verified` / `verification_status`. Anything that sends, deletes mail or is
  otherwise hard to undo must set `requires_confirmation` in `capabilities/registry.py`.
- **Never commit** `.env`, `credentials/`, `logs/`, `runtime/`, `assets/whisper/` or `benchmarks/results/`.
