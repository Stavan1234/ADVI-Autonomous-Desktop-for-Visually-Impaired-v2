# Troubleshooting ADVI

Every entry here is a problem that was actually seen: in development logs, in live testing, or while
verifying the setup from a fresh copy. The detailed log is `logs/advi.log` (newest entries at the end).

## Setup

| Symptom | Cause | Fix |
|---|---|---|
| `Python Launcher 'py' was not found` or `Python 3.13 was not found` | Python 3.13 isn't installed, or was installed without the launcher | Install Python 3.13 64-bit from python.org with "py launcher" ticked, then rerun `SETUP_ADVI.bat` |
| `The ADVI folder path is N characters long` | Windows' 260-character path limit. PyTorch installs files up to ~145 characters deep inside `.venv`, so the project folder must be at most ~100 characters unless long paths are enabled. Extracting the GitHub ZIP into Downloads gives about 135. | Move the folder to a short path such as `C:\ADVI`, or enable long paths (administrator PowerShell, then restart): `New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force` |
| `ERROR: Could not install packages due to an OSError ... torch\include\...` | The same path limit, hit inside pip (setups from before 0.4.0 had no early check) | As above |
| `'SETUP_ADVI.bat' is not recognized` when started from a terminal | The terminal has `NoDefaultCurrentDirectoryInExePath` set, so commands in the current folder need a path | Run `.\SETUP_ADVI.bat`, or double-click it in Explorer |
| `ADVI regression tests failed` at the end of setup | A test failed in this environment | Run the test command from [DEVELOPMENT.md](DEVELOPMENT.md#5-test) to see which one; please report it |
| `Offline speech model: NOT DOWNLOADED` | No internet during setup | Nothing to do; ADVI downloads it on its first start. Typing works without it. |
| First start takes a minute or more | One-time downloads: the memory embedding model (`BAAI/bge-small-en-v1.5`) and, if setup couldn't, the speech model | Later starts are faster |

## Starting

| Symptom | Cause | Fix |
|---|---|---|
| `No language-model provider is configured or could be initialized.` | `.env` has no AI key, or no `.env` | Put `GROQ_API_KEY=` and/or `GEMINI_API_KEY=` in `.env` |
| `ADVI virtual environment not found. Run SETUP_ADVI.bat first.` | Setup didn't finish | Run `SETUP_ADVI.bat` and read its last error |
| `ADVI cannot safely enter interactive mode yet.` | A blocking startup health check failed (runtime, provider or task state) | See `logs/advi.log` for which check failed |
| `Voice input is off because I can't reach a microphone.` | No microphone, or Windows blocks apps from using it | Settings > Privacy & security > Microphone: allow desktop apps. Typing still works. |
| `Voice input is off because no speech recognition could be loaded.` | No offline model and no Groq key for online recognition | Add a Groq key, or let the model download once with internet |
| "Control Alt Space is already used by another program" / ADVI picks Ctrl+Alt+Z | Another app (on the dev machine: Flow Launcher / PowerToys) owns the hotkey | Use the key ADVI announces, or set `ADVI_VOICE_HOTKEY` in `.env` |

## While running

| Symptom | Cause | Fix |
|---|---|---|
| "the AI service's daily quota is used up ... try again in about N minutes" | Free Groq keys have a token budget per rolling day; keys from one account share it | Wait the stated time, add a key from another Groq account, or add a Gemini key. Simple commands still work offline (README section 5). |
| "The AI is busy for a moment, so I'm waiting about N seconds" | A per-minute limit; ADVI waits once and retries | Nothing to do |
| The Gemini backup fails too ("Gemini tool-calling request failed") | Gemini's free quota for the brain model is small (about 20 requests a day) and is shared by all keys of one project | Wait, or use Groq keys from different accounts |
| "Vision is unavailable: the vision service is busy or too slow right now" | Gemini vision returned 503/504 or a quota error; ADVI pauses vision for a while | Nothing to do; ADVI continues with its structured tools |
| "I'm understanding your voice offline right now ... I may be a little slower" | Groq's speech service was unreachable; local recognition took over | Nothing to do; ask "why are you slow?" for the reason |
| ADVI mishears a name ("CU" for "Siyu") | Speech recognition | Say "it's S I Y U, not C U", or spell it when ADVI asks. After a successful send it remembers the right name. |
| ADVI answers its own sentences | Echo cancellation is off: `livekit` is missing, the duplex audio stream couldn't open, or `ADVI_ECHO_CANCEL=off` | Rerun setup; `logs/advi.log` says "Voice started: echo cancellation on/off" and why |
| "Wake up ADVI" does nothing | The wake listener isn't running, Windows blocks desktop apps from the microphone, or the phrase wasn't recognised | `.venv\Scripts\python.exe -m advi.wake status`, then `... -m advi.wake install`; Settings > Privacy & security > Microphone > allow desktop apps; say "wake up ADVI" clearly as one short sentence. `logs/wake.log` records each start and each wake phrase heard |
| ADVI starts by itself | The listener heard something like "hey ADVI" | Note what was said and when (`logs/wake.log` records the heard phrase); turn it off with `... -m advi.wake uninstall` |
| ADVI doesn't react while music or a video plays | By design, only "ADVI, ..." and plain media commands count during other audio | Start with "ADVI, ..." |
| "I couldn't find a WhatsApp chat named ..." | The name differs from the chat title | Spell just the name when ADVI asks |
| "WhatsApp is open but I couldn't bring its window to the front" | Windows' foreground lock (another app was in front) | Since 0.4.0 ADVI retries with the standard workarounds; if it persists, click WhatsApp once and say "try again" |
| "Telegram isn't signed in" / Telegram can't find chats | Not signed in, or Tesseract missing | Sign in to Telegram Desktop; `winget install UB-Mannheim.TesseractOCR` |
| Office shows "Product Activation Failed" | Unlicensed Office | Nothing to do: ADVI builds documents through Word automation and closes the pop-up |
| A website asks you to prove you're human | CAPTCHA | ADVI never solves these; complete it in ADVI's Chrome window and ask again |
| Gmail opens but asks you to sign in | ADVI's Chrome profile is separate from yours | Sign in once in the Chrome window ADVI opened |
| Typed `exit` doesn't close ADVI immediately | A task is running | Since 0.4.0, exit asks the task to stop and force-closes after 8 seconds |

## Tests

| Symptom | Cause | Fix |
|---|---|---|
| Many tests fail with import errors | Tests run from the wrong folder or outside `.venv` | Run from the repository root with `.\.venv\Scripts\python.exe -m pytest` (`pyproject.toml` adds `src` to the path) |
| `test_groq_smoke.py` fails | It calls the real Groq API | Skip it with `--deselect tests/current/test_groq_smoke.py`, or set a valid `GROQ_API_KEY` |
