# Phase 6: quota diagnosis, quota-free commands, David Gmail, Telegram

Source: `logs/advi.log`, 2026-10-14 night run (turns 31–45).

## Was it the limit or ADVI?

Mostly the limit, but ADVI made it look worse.

| Observation in log | Cause | Fix |
|---|---|---|
| Turns 35–44: Groq `rate_limit_exceeded`, `project_daily`, retry 15–19 min | Real daily token quota (TPD) on the Groq project. groq-2/groq-3 appear to share one project quota. | Nothing in code can lift it. Use keys from **separate Groq accounts/projects**. |
| groq-1 `401 invalid_api_key` | Key revoked/typo | Replace key groq-1 in config. |
| Gemini shown as "timed out" | Key was also out of quota (429), but the native Interactions API hung 100 s+ instead of failing | Native Interactions disabled unless `ADVI_GEMINI_INTERACTIONS=1`; the plain path reports the quota in ~0.5 s. |
| "Please wait a moment" on a daily quota | Message ignored `quota_scope` | Now says the daily quota is used up on every key and gives the real wait. |
| David Gmail opened a Google search | "search for email x@y.com" matched the web-search intent before the email intent | Email intent checked first; `(none)` subject → empty; quoted body parsing. |
| "open my videos folder" → `Desktop\Videos` | Aliases were hard-coded paths | Videos/Music/Pictures resolve through Windows Known Folder GUIDs. |

## Quota-free command router (`agent/local_intents.py`)

Clear, common commands run without any AI request, so they keep working when
every key is exhausted:

- Email: `send an email to <name|address> [subject ...] saying <body>`, with
  `using david (fallback)` going through David's Gmail-in-browser path. The recipient comes from
  `contact_lookup` and ADVI asks when there are none or several. **Nothing is sent without "yes".**
- WhatsApp / Telegram: `send <text> to <chat> on whatsapp|telegram`, `send a telegram message to X saying Y`. These always
  stop at confirmation.
- Sites: `open gmail/youtube/reddit/amazon/...`, `search <q> on amazon|reddit|youtube|flipkart|wikipedia|github|maps|google`.
- Media: `play X on youtube (music)`, `play X from my videos folder`, plus pause, resume and skip.
- Files: `organize my downloads`, `move all pdf files from downloads to a folder called PDF`, `undo`, `open my videos folder`.
- Apps: `open notepad / word / excel / powerpoint / whatsapp / telegram / ...`.

Anything multi-step or ambiguous ("send a message to Joel saying hi": which app?) still goes to the model.

## Telegram (`capabilities/apps/telegram.py`)

Telegram Desktop (Qt) exposes no text fields to UI Automation, so the flow is
keys plus Tesseract checks of each screen region:

1. Signed-in check.
2. The search list shows the chat.
3. The header shows the chat.
4. The composer shows the exact draft.
5. Enter, then the draft is gone and the message is visible.

Any failed check stops the flow, and an unconfirmed draft is cleared.

Live state on this PC: **Telegram is not signed in** (welcome screen). ADVI now
says so. The send path is covered by tests against a simulated Telegram. It has
not been run live yet, because that needs a signed-in Telegram.

## Live checks run (nothing sent)

- David Gmail draft to the user's address, stopped at "Should I send it, sir?". 7.3 s, no AI used.
- `search wireless earbuds on amazon` → Amazon.in results (verified).
- `open gmail` → Inbox (signed in as stavancoder@gmail.com).
- `search python tips on reddit` → once served Reddit's "Prove your humanity" check, which was then
  reported as verified. Bot-check pages are now reported as needing the user, never solved by ADVI.

## Tests

`tests/current/test_phase6_local_commands.py` (15) + bot-check title test. Full suite: 629 passed.
