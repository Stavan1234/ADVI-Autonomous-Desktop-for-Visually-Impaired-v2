# ADVI: Autonomous Desktop for the Visually Impaired

ADVI is a voice-first assistant for Windows. You type (or dictate) what you want in plain English and ADVI
does it on the computer and speaks the result. For example, it can open apps and websites, play music,
write Word documents, manage files, and send WhatsApp, Telegram or Gmail messages.

An AI model (Groq or Google Gemini) understands the request. ADVI's own code does the work on the computer
and **checks that it really happened** before saying "done". Anything that sends a message or email always
asks you first.

---

## 1. What you need

| Requirement | Why | Get it |
|---|---|---|
| Windows 10/11, 64-bit | ADVI controls Windows apps | Not applicable |
| **Python 3.13** (64-bit) | Runs ADVI | [python.org/downloads](https://www.python.org/downloads/). Tick **"Add python.exe to PATH"** and keep **"py launcher"** selected |
| **Google Chrome** | Websites, YouTube, Gmail through the browser | `winget install Google.Chrome` |
| **A free AI key** (at least one) | The "brain" | Groq: [console.groq.com/keys](https://console.groq.com/keys). Gemini: [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| Tesseract OCR *(recommended)* | Telegram messages, reading the screen | `winget install UB-Mannheim.TesseractOCR` |
| Microsoft Office *(optional)* | Writing Word documents | Works even when Office shows "Product Activation Failed" |
| WhatsApp / Telegram Desktop *(optional)* | Messaging | Microsoft Store; sign in once |

## 2. Install (about 5–10 minutes)

1. Download or clone this folder, for example to `C:\ADVI`.
2. Double-click **`SETUP_ADVI.bat`**.
   It creates a private Python environment (`.venv`), installs everything, creates your `.env` file, and runs
   the test suite. At the end it tells you whether Tesseract and Chrome were found.
3. Open **`.env`** in Notepad and paste your key(s):
   ```text
   GROQ_API_KEY=gsk_your_key_here
   GEMINI_API_KEY=your_gemini_key_here
   ```
   One key is enough. Having both Groq and Gemini means ADVI can switch when one runs out.
4. Double-click **`run_advi.bat`**. When ADVI says *"Everything is ready"*, type a request and press Enter.
   Type `exit` to quit.

> **Free-tier tip:** free AI keys have a daily limit. Extra Groq keys only help if they come from
> **different Groq accounts**; keys from the same account share one quota. When every key is used up, ADVI
> still runs simple commands without AI (see section 5).

### First-time sign-ins

- **Browser:** ADVI uses its own Chrome profile, so it never touches your personal Chrome. The first time it
  opens Chrome, sign in to Gmail and YouTube in that window once.
- **WhatsApp / Telegram:** open the desktop app and sign in once.
- **Gmail through the Google API (optional):** put your OAuth client file at `credentials/gmail_credentials.json`.
  Without it, ADVI sends Gmail through the browser instead ("David").

## 3. Things to try

Start simple, then mix them up. Phrase things naturally; the examples are only ideas.

**Apps**
- `open notepad`
- `open notepad and type hello from advi`
- `open calculator` and then `close calculator`
- `open word`, `open excel`, `open settings`

**Websites and music**
- `open youtube`
- `play believer by imagine dragons`
- `pause`, `resume`, `next song`, `volume up`, `mute`
- `play lofi on youtube music`
- `search wireless earbuds on amazon`
- `search iphone 15 on flipkart`
- `look up mount everest on wikipedia`
- `search python tips on reddit`
- `show me pizza near me on google maps`
- `open gmail`

**Writing documents (Microsoft Word)**
- `write a short speech on courage in word and save it on desktop`
- `create a word document about healthy breakfast ideas with headings and a table, save it as a pdf in documents`
- `make a word document with 5 study tips for exams and save it in documents/School`

**Files**
- `open my downloads folder`
- `create a folder called Projects on desktop`
- `organize my downloads` and then `undo`
- `move all pdf files from downloads to a folder called PDF`
- `find my resume`
- `what files are on my desktop?`
- `delete test.txt from desktop` (it goes to the Recycle Bin)

**Messages and email.** ADVI always asks "Should I send it?" first; say `yes` or `no`.
- `send hello to Mom on whatsapp`
- `message Rahul on whatsapp saying see you at 5`
- `send hi to Saved Messages on telegram`
- `send an email to someone@example.com saying the meeting is at 5`
- `send an email to someone@example.com using david fallback saying good evening` (Gmail through the browser)

**Questions and system**
- `what can you do`
- `what is my battery level`
- `what time is it`
- `tell me a joke`
- `stop` (stops what is playing or running)
- `cancel` (cancels the current task)

## 4. Safety

- **Messages and emails are never sent without your "yes".** Natural answers like "yes send it" or "sure" work.
  "no", "don't send it" and "send it to someone else instead" never send.
- **Deleting goes to the Recycle Bin**, never permanent. If the Recycle Bin isn't available, ADVI refuses
  to delete.
- **Bulk file moves can be undone:** say `undo`.
- **Saving never overwrites** an existing file; ADVI adds "(2)" to the name instead.
- **Buy, pay, submit and delete buttons on websites** need your confirmation before ADVI clicks them.
- ADVI **never solves CAPTCHAs** or "prove you're human" pages; it tells you to do that yourself.

## 5. When the AI is unavailable

ADVI always asks the AI first. If every AI key fails (daily quota used up, no internet, or a bad key), ADVI
falls back to a small set of **built-in commands** that need no AI:

- opening apps, websites and folders
- searching a site
- play, pause, skip and volume
- organizing, moving and undoing files
- WhatsApp, Telegram and email (still with confirmation)

It tells you when it did this. Anything unclear or multi-step waits for the AI; ADVI never guesses.

You can change this in `.env`:

```text
ADVI_LOCAL_COMMANDS=fallback   # default: built-in commands only when the AI is unavailable
ADVI_LOCAL_COMMANDS=first      # built-in commands first (saves AI quota)
ADVI_LOCAL_COMMANDS=off        # never use built-in commands
```

## 6. Troubleshooting

| Problem | What to do |
|---|---|
| `SETUP FAILED … Python 3.13 was not found` | Install Python 3.13 64-bit from python.org (with the py launcher), then run the setup again |
| "the AI service's daily quota is used up" | Wait for the reset (the message says when), or add a key from another Groq account, or a Gemini key |
| "Telegram isn't signed in" | Open Telegram Desktop and sign in |
| Telegram works but cannot find chats | Install Tesseract (`winget install UB-Mannheim.TesseractOCR`) |
| Office shows "Product Activation Failed" | Nothing to do: ADVI closes the activation pop-up and still writes documents |
| A website asks you to prove you're human | Complete the check in the ADVI Chrome window, then ask again |
| Gmail opens but asks you to sign in | Sign in once in the Chrome window ADVI opened |
| Something else | Look at `logs/advi.log` (a new log is written each run) |

## 7. For developers

```powershell
.\.venv\Scripts\python.exe -m pytest --deselect tests/current/test_groq_smoke.py   # ~650 tests, ~90 s
.\.venv\Scripts\python.exe -m pytest tests/current/test_groq_smoke.py              # live Groq call (needs a key)
```

How a request flows:

```text
you -> AgentSession -> AI picks a tool -> ToolGuard/ToolRunner -> ExecutionEngine -> capability
    -> the real computer -> verification -> result -> AI reply (spoken)
```

| Folder | What is in it |
|---|---|
| `src/advi/agent/` | Conversation and task loop (`session.py`), built-in commands (`local_intents.py`) |
| `src/advi/capabilities/` | `desktop/` (apps, windows), `browser/` (Playwright + Chrome), `files/`, `apps/` (WhatsApp, Telegram), `office/` (Word) |
| `src/advi/fallback/` | "David": Gmail and other tasks through the browser |
| `src/advi/providers/` | Groq and Gemini clients with automatic failover |
| `tests/current/` | Test suite |
| `docs/` | Architecture notes, the capability and prompt catalog, and `docs/audits/` (development history) |

Optional extras:
- Piper neural voice files in `assets/` (see `assets/README.md`); otherwise Windows' built-in voice is used.
- MCP servers via `mcp_servers.json` (copy `mcp_servers.json.example`).

Never commit `.env`, `credentials/`, `logs/` or `runtime/`; `.gitignore` already excludes them.
