# ADVI: Autonomous Desktop for the Visually Impaired

ADVI is a voice assistant for Windows, built for people who can't see the screen. You say (or type) what you
want in plain English. ADVI does it on the computer and tells you the result out loud. For example:

> "Wake up ADVI." … "Send hello to Mom on WhatsApp." … "Write a short speech on courage in Word and save it
> on the desktop." … "Play Believer on YouTube." … "Organize my downloads." … "Is the internet connected?"

**How ADVI works:**
- An AI model (Groq or Google Gemini) understands the request.
- ADVI's own code does the work on the computer and **checks that it really happened** before saying "done".
- Anything that sends a message or email, or deletes mail, **always asks you first**.

**Contents**
1. [Quick start](#1-quick-start)
2. [Already have ADVI? Updating](#2-already-have-advi-updating)
3. [What you need](#3-what-you-need)
4. [Using ADVI by voice](#4-using-advi-by-voice)
5. [Things to try](#5-things-to-try)
6. [Safety and privacy](#6-safety-and-privacy)
7. [When the AI is unavailable](#7-when-the-ai-is-unavailable)
8. [Troubleshooting](#8-troubleshooting)
9. [Working on ADVI as a team](#9-working-on-advi-as-a-team)

---

## 1. Quick start

About 10–20 minutes the first time, mostly downloads.

1. **Install Python 3.13 (64-bit) and Google Chrome.** See [section 3](#3-what-you-need).
2. **Get the code into a short folder,** for example `C:\ADVI`:
   ```powershell
   git clone https://github.com/Stavan1234/ADVI-Autonomous-Desktop-for-Visually-Impaired-v2.git C:\ADVI
   ```
   - Keep the path short. Windows limits file paths, and one of ADVI's packages (PyTorch) needs the folder
     path to stay under about 100 characters.
   - A ZIP extracted inside Downloads is usually too long. Setup checks this and tells you what to do.
3. **Double-click `SETUP_ADVI.bat`.** It:
   - creates a private Python environment (`.venv`) and installs everything;
   - creates your `.env` settings file;
   - downloads the offline speech and wake-word models (about 400 MB);
   - runs the test suite (about 970 tests);
   - turns on the wake word.

   It ends with `SETUP COMPLETE`, or with a clear message saying what to fix.
4. **Add an AI key.** Open `.env` in Notepad and paste at least one key:
   ```text
   GROQ_API_KEY=gsk_your_key_here
   GEMINI_API_KEY=your_gemini_key_here
   ```
   - Both are free:
     - Groq: [console.groq.com/keys](https://console.groq.com/keys);
     - Gemini: [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
   - One key is enough. With both, ADVI switches automatically when one runs out.
   - **Never commit or share `.env`.** Each person uses their own keys.
5. **Start ADVI:** double-click `run_advi.bat`, or just say **"wake up ADVI"**.
   - ADVI says *"Just preparing everything…"*, then *"Everything is ready, sir. I'm listening…"* (about
     10–20 seconds, longer on the very first start).
   - Then talk to it, or type in its window.
6. **First-time sign-ins**, each once:
   - **Chrome:** ADVI uses its **own** Chrome profile and never touches your personal one. The first time it
     opens Chrome, sign in to Gmail and YouTube in that window.
   - **WhatsApp / Telegram Desktop:** open the app and sign in.
   - **Gmail through the Google API** (optional, more reliable than the browser): see
     [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#3-configure). Without it, ADVI uses Gmail in Chrome.

To close ADVI, say or type **"sleep"** or **"exit"**. To bring it back, say **"wake up ADVI"**.

## 2. Already have ADVI? Updating

```powershell
git pull
```

Then start ADVI as usual (`run_advi.bat` or "wake up ADVI"). If the update needs new packages or models,
ADVI installs them by itself:
- it says *"ADVI was updated. Installing the new parts once…"*;
- it takes a few minutes, then starts;
- it happens only once per update, and later starts are not slowed down.

Your `.env` and everything ADVI remembered are kept.

**Updating from 0.3.x to 0.4.0** adds always-on voice, the wake word, and echo cancellation (ADVI no longer
hears itself). The wake word is switched on during that first start. If you don't want it, put
`ADVI_WAKE=off` in `.env` before starting. See [RELEASE_NOTES.md](RELEASE_NOTES.md) for everything that
changed.

## 3. What you need

| Requirement | Why | How to get it |
|---|---|---|
| Windows 10 or 11, 64-bit | ADVI controls Windows apps | |
| **Python 3.13**, 64-bit | Runs ADVI | [python.org/downloads](https://www.python.org/downloads/). Keep **"py launcher"** selected |
| **Google Chrome** | Websites, YouTube, Gmail in the browser | `winget install Google.Chrome` |
| **An AI key** (at least one) | The "brain" | Groq or Gemini, both free (see above) |
| Microphone and speakers | Talking to ADVI | Windows Settings > Privacy & security > Microphone > allow desktop apps |
| Tesseract OCR *(recommended)* | Telegram, reading text on screen | `winget install UB-Mannheim.TesseractOCR` |
| Microsoft Word *(optional)* | Word documents | Works even when Office shows "Product Activation Failed" |
| WhatsApp / Telegram Desktop *(optional)* | Messaging | Microsoft Store; sign in once |

**Disk space:** about 2 GB (the Python environment, about 1.5 GB, plus the speech models).

## 4. Using ADVI by voice

ADVI listens from the moment it's ready. **Just talk; no key is needed.**

| You want to… | Do this |
|---|---|
| Start ADVI | Say **"wake up ADVI"** (or "hey ADVI"). It works after a restart too, once you're signed in to Windows |
| Ask something | Just say it. ADVI waits about a second after you stop; if you pause mid-sentence ("send a message to Siyu saying…") it waits a little longer |
| Interrupt ADVI | Just start talking: it pauses and listens. Say **"stop"** to make it stop. The hotkey (below) also stops it talking |
| Cancel a running task | Say **"stop"** or **"cancel"** |
| Fix a name or message before sending | "It's S I Y U, not C U", "send it to Siyu instead", "change the message to see you at 5". ADVI asks again |
| Hear what the sounds mean | "What do the sounds mean?" |
| Close ADVI | **"Sleep"**, "exit" or "close ADVI" (spoken or typed) |

**ADVI's sounds** are all from one soft, bell-like family:

| Sound | Meaning |
|---|---|
| rising "ti-ting" | I'm ready, your turn to talk |
| tiny tick | I can hear you speaking |
| two falling notes | got it, working on it |
| slow, quiet breathing tone | still working |
| short falling notes | stopped or interrupted |
| two low notes | I didn't catch that, please say it again |
| rising arpeggio | ADVI is starting ("wake up ADVI") |
| falling arpeggio | going to sleep (ADVI closes) |

**Good to know:**
- **ADVI doesn't hear itself.** It removes its own voice from the microphone (echo cancellation, as in Chrome,
  Meet and phone assistants). If a little of its voice still gets through, it notices and carries on talking.
- **While music or a video plays,** plain media commands work ("pause", "next", "volume down"). For anything
  else, start with its name ("ADVI, …"), so song lyrics never become commands.
- **The hotkey** is Ctrl + Alt + Space, or Ctrl + Alt + Z if another program already uses that; ADVI says
  which at startup. While ADVI runs, it stops ADVI talking. While ADVI is closed, it starts ADVI.
- **Speech recognition** uses Whisper on Groq when online (under a second). Offline, it uses a model on this
  computer (2–3 seconds; ADVI tells you once). `ADVI_STT_CLOUD=off` keeps your voice on this computer only.
- **The wake word** is a small background program that starts at Windows sign-in. It recognises the phrase on
  this computer only, and nothing is sent online. Check it with:
  ```powershell
  .venv\Scripts\python.exe -m advi.wake status
  ```
  Turn it off with `uninstall` instead of `status`, and set `ADVI_WAKE=off` in `.env`.
- **Typing always works** too, at the same time as voice.

## 5. Things to try

Phrase things naturally; these are only ideas.

| Area | Examples |
|---|---|
| Apps | "open notepad and type hello from ADVI" · "open calculator", then "close calculator" · "open word" |
| Web and music | "play Believer by Imagine Dragons" · "pause" · "next song" · "volume up" · "search wireless earbuds on Amazon" · "look up Mount Everest on Wikipedia" |
| Documents | "write a short speech on courage in Word and save it on desktop" · "make a Word document with a table of the months and save it as a PDF" · "make a PowerPoint about ADVI" |
| Files | "what's in my downloads?" · "organize my downloads", then "undo" · "move all PDF files from downloads to a folder called PDF" · "find my resume" · "delete test.txt from desktop" (Recycle Bin) |
| Messages (asks first) | "send hello to Mom on WhatsApp" · "message Rahul on WhatsApp saying see you at 5" · "send hi to Saved Messages on Telegram" |
| Email (asks first) | "send an email to someone@example.com saying the meeting is at 5" · "check my inbox" · "any unread emails?" · "reply to that saying thanks" · "delete that email" (Trash) |
| Screen (needs a Gemini key) | "what's on my screen?" · "is there an error on the screen?" |
| Questions | "what can you do?" · "what time is it?" · "battery level" · "is the internet connected?" · "status of my laptop" |

## 6. Safety and privacy

**What ADVI will and won't do:**
- **Messages and emails are never sent without your "yes".** "No", "don't send it" and corrections never
  send.
- **Deleting files goes to the Recycle Bin**, never permanently. Bulk moves can be undone ("undo"). Saving
  never overwrites a file.
- **Buy, pay, submit and delete buttons on websites** need your confirmation. ADVI never solves CAPTCHAs.
- When you ask ADVI to close something, it closes only that, not other windows.

**What leaves your computer:**
- Your requests and the information ADVI needs to answer them go to the AI service (Groq or Gemini).
- Your voice goes to Groq for transcription, unless `ADVI_STT_CLOUD=off`.
- Screenshots go to Gemini only when you ask what's on screen, or at most twice per task when ADVI is stuck.
  `ADVI_VISION=off` turns this off.

**What stays local:** the wake word, your `.env`, ADVI's memory and its logs (`logs/`).

## 7. When the AI is unavailable

If every AI key fails (daily quota used up, no internet, or a bad key), ADVI still handles simple
**built-in commands**:
- opening apps, websites and folders;
- playing, pausing and skipping media;
- moving and organizing files;
- time, battery and internet status;
- messages and email, which still ask first.

It tells you when it does this, and anything unclear waits for the AI. `ADVI_LOCAL_COMMANDS=first` in `.env`
uses the built-in commands first, to save AI quota.

**Free-tier tip:** extra Groq keys only add quota if they come from **different Groq accounts**.

## 8. Troubleshooting

| Problem | What to do |
|---|---|
| `SETUP FAILED … Python 3.13 was not found` | Install Python 3.13 64-bit from python.org (with the py launcher), then run setup again |
| Setup says the project path is too long | Move the folder to a short path like `C:\ADVI` and run setup again |
| "No language-model provider is configured" | Add a key to `.env` (Quick start, step 4) |
| "The AI service's daily quota is used up" | Wait for the reset (the message says when), or add a key from another Groq account, or a Gemini key |
| "Voice input is off because…" | Allow desktop apps to use the microphone (Windows privacy settings), check a microphone is connected, and restart ADVI |
| "Wake up ADVI" does nothing | Run `.venv\Scripts\python.exe -m advi.wake status`; if it isn't listening, use `install` instead of `status`. Say the phrase as one short sentence. Details are in `logs/wake.log` |
| ADVI starts by itself | It heard something like "hey ADVI". `logs/wake.log` shows what it heard. Turn the wake word off with `uninstall` |
| Telegram can't find chats | Install Tesseract (`winget install UB-Mannheim.TesseractOCR`) and sign in to Telegram Desktop |
| Office shows "Product Activation Failed" | Nothing to do: ADVI closes the pop-up and still writes documents |
| A website asks you to prove you're human | Complete it yourself in ADVI's Chrome window, then ask again |
| Anything else | See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) and the end of `logs/advi.log` |

## 9. Working on ADVI as a team

**Before you push:**
1. Work on a branch: `git checkout -b fix/whatsapp-focus`.
2. Add or update a test in `tests/current/` for every behaviour change.
3. Run the full test suite (offline, about 100 seconds):
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --deselect tests/current/test_groq_smoke.py
   ```
4. Try the change in ADVI itself when it affects what users hear or see.
5. Open a pull request saying what was wrong, what changed, and how you checked it.

**New package?** Add it to `pyproject.toml`. Teammates get it automatically the next time they start ADVI
after `git pull`.

**Never commit** `.env`, `credentials/`, `logs/`, `runtime/`, `assets/whisper/` or `benchmarks/results/`. They
hold keys, personal data and large downloads, and `.gitignore` already excludes them. Use `example.com`
addresses and made-up names in tests.

| Document | What it covers |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | The rules that keep ADVI trustworthy (verification, confirmation, honesty) |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Setup in detail, configuration, tests, benchmarks, where files are written |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How ADVI is built, with diagrams |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Problems seen in practice and their fixes |
| [docs/ADVI_TECHNICAL_REPORT.md](docs/ADVI_TECHNICAL_REPORT.md) | The full technical report: design, testing, limitations |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | What changed in each version |
| `docs/audits/` | Engineering notes for each development phase |

**Where things are in the code:**

| Folder | What is in it |
|---|---|
| `src/advi/agent/` | The conversation and task loop (`session.py`), built-in commands, name handling |
| `src/advi/capabilities/` | Everything that touches the computer: desktop, browser, files, Office, WhatsApp/Telegram, Gmail, system |
| `src/advi/voice/` | Listening, echo cancellation, speech recognition, sounds, hotkey |
| `src/advi/wake/` | The background wake-word listener |
| `src/advi/providers/` | Groq and Gemini clients with automatic failover |
| `src/advi/fallback/` | "DAVID": the separate fallback that works through the browser |
| `benchmarks/` | Desktop Suite, capability sweep, WebVoyager sample |
| `tests/current/` | The test suite |

**Optional extras:**
- A Piper neural voice in `assets/` (see `assets/README.md`); otherwise Windows' built-in voice is used.
- MCP servers in `mcp_servers.json` (copy `mcp_servers.json.example`).
- A Picovoice Porcupine wake-word engine (see `.env.example`).
