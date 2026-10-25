# Phase 10: fixes from the 2026-10-23 evening session

**Source:** the user's pasted session, traced in `logs/advi.log` (lines 32963–52274).

**Worked well that session:**
- WhatsApp send.
- Gmail send, directly and through David.
- A Word document with tables.
- YouTube play.
- Listing contacts.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | Amazon speaker search: searched twice, hit the tool limit, no products listed | `read_web_page` wasn't offered for browsing; `search` returned only a URL; the brain tried the desktop `scroll` | `read_web_page` is part of every browsing tool set and reads the page's **main** content (not the header menus), 3,000 characters at a time with `start`. `search` returns the results text in the same step. A prompt rule says to answer from the results and never repeat a search. (This is Agent-E's text-only DOM distillation idea.) |
| 2 | "I'll remember that, Stavan", but nothing was saved | No tool call; fact capture covered emails only | "my name is / call me" and "I am a … student / I built …" are saved as `user_profile` facts. A `[USER]` line goes in the turn preamble. `_no_unbacked_memory_claim` blocks "I'll remember" when nothing was written. |
| 3 | Telegram "couldn't confirm that chat opened" | Live probe: at an 800-px window the header box starts right of the title, so OCR read "n Jio" for "Stavan Jio". The first guess, Saved Messages, was wrong but is handled too. | The header is also accepted when its first line is the end of the top search result's title ("Stavan Jio"); a wider box would catch the search field, which echoes the query. A changed header showing "Saved Messages" or a message box counts as open, and ADVI says what opened. A **send** still requires the header to name the chat. Live: "Opened the Telegram chat with Stavan Jio." |
| 4 | Resume played the song, ADVI said "not playing", then pressed space and paused it | One playback check after 0.6 s; `play()` is asynchronous | Poll for up to 3 s. Media keys report what was sent ("Turned the volume up.") instead of "couldn't verify the final state". |
| 5 | "the language provider rejected my request, which is a bug" | Groq 400: the model wrote `it\'s` and a stray `"}` in its tool call | `_repair_generated_call` recovers the name and the arguments object, and the existing rejected-call adoption runs it. If it can't be repaired, the message says "the AI's answer came back garbled; please say that again". |
| 6 | "I couldn't verify the final state: Found 20 files…" instead of the video names | Read-only results had to be *verified* before the model's answer could stand | A turn made only of successful read-only observations keeps the model's answer. Side effects are still checked as before. |
| 7 | Vision warnings on screen; then "AI rate-limited" | Vision used `gemini-3.8-flash`, whose free tier allows 20 requests/day **per model**, shared with the brain's Gemini backup. It also looked after volume keys. | Vision uses `gemini-3.1-flash-lite`, which has its own quota and took 3 s against 11 s for 3.5-flash-lite. A circuit breaker turns a key off until its `retryDelay` after a 429 and pauses all vision for 2 minutes after a 503/504. Failures log at INFO. There are no automatic looks after media keys, `press_key` or `hotkey`, and a final-state look happens only after **critical** actions. The window title from Windows is passed to vision, because the lite model called the "Claude" window "Cursor" until told. |
| 8 | "Batches: 100%" bars on every turn | sentence-transformers `encode` progress bars | `show_progress_bar=False`. |

**Tests:** `tests/current/test_phase10_session_fixes.py` (24 tests).

**Live (2026-10-23):**
- Amazon "home theatre speaker" search returned about 4 products with prices in one step, and `read_web_page start=` continued to more.
- Telegram opened "Stavan Jio".
- Vision with the window title answered in 3–5 s.
- The full agent replay stopped at the first prompt because the Groq daily quota was used up.
