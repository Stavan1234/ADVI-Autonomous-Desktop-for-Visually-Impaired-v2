# Phase 7: live capability sweep (Word, David Gmail send, Telegram, sites, apps, files)

Everything below was run live on this PC on 2026-10-14 unless marked "unit-tested only".

## Microsoft Word, including the "Product Activation Failed" state

- **New tool `word_create_document`** (`capabilities/office/`) builds the whole document through Word's
  automation interface in a hidden private Word instance. That interface keeps working while activation has failed.
  - Supports title, headings, bullets, numbered lists, quotes, styled tables, bold/italic and page numbers.
  - Saves as .docx or exports .pdf, then reads the saved file back to verify it.
  - Never overwrites (`name (2).docx`), and opens the result in Word.
  - Live: "Benefits of Morning Walks.pdf", "Courage Speech.docx" (152 words) and "Time Management Tips.docx"
    are in Documents\ADVI Test.
- **Activation wizard** (`office/nags.py`): "Microsoft Office Activation Wizard" opens over Word, Excel and
  PowerPoint seconds after launch. It blocked closing them ("You cannot close Microsoft Word because a dialog
  box is open"). ADVI now closes it (its own Close button, via WM_CLOSE) on open, on close and after creating a
  document. Live: the wizard appeared and was dismissed for Word, Excel and PowerPoint, and all three
  opened and closed cleanly.
- Routing: "write/create … in Word / MS Word / Word document" offers only `word_create_document`, never
  type_text key by key.
- Saving where the user says:
  - A folder path ("documents/ADVI Test") saves *inside* the folder, named after the document title.
  - The model's one-letter misspellings of names the user said ("ADMI Test") are restored to the user's spelling.

## David Gmail: why sends "didn't happen"

1. **"yes send it", "send it", "sure", "yes please", "yes sir" were not accepted as a yes.** "send it"-style
   replies were treated as a *new task*, so the prepared draft was silently abandoned. The confirmation pattern
   now accepts any reply built only from affirmative words. "don't send it", "send it to Rahul instead" and
   "yes but change the subject" still never match.
2. On failure, David said "David completed the fallback task." if any earlier step had passed. It now names
   the failed step.
3. Send click: the Send button was clicked twice (DOM click + mouse), and "sent to" anywhere on the page counted
   as proof. Now there is one click, a trusted mouse click only if the first did nothing, Gmail's "Message sent"
   toast as proof, and "Send feedback" / "Schedule send" excluded.

Live: two test emails (subjects "ADVI send test A/B …") were sent from stavancoder@gmail.com to
stavankalkumbe@gmail.com and **confirmed in Gmail's Sent folder**.

## Telegram

- Signed-in detection was wrong: the empty-chat text "Select a chat to start messaging" matched "start
  messaging".
- The search echo ("There were no results for X") counted as a found chat.
- Live: opens "Saved Messages", confirms the typed draft in the composer, clears it, and refuses an unknown
  chat. No Telegram message was sent; sending needs your yes.

## Apps

- `open telegram` failed after 12–25 s while Telegram was running. There were two causes:
  - Store apps resolve to explorer.exe.
  - Window titles carry counts ("Telegram (16372)").
- `open notepad` waited 12 s and said "still loading", because the title "Untitled - Notepad" failed the
  title rule.
- Fixes: a known process identity for Telegram, WhatsApp, Office, Notepad, Calculator, Settings and similar
  apps; the topmost window when an app has several. Live: Notepad 1.0 s, Calculator 0.5 s, Paint 1.2 s,
  Word / Excel / PowerPoint 1–4 s, Telegram / Settings / WhatsApp 0.2 s (already open).
- "open notepad and type X" now runs without AI. It opens a new tab first, because Windows 11 Notepad restores
  unsaved tabs and the text was appended to old content.

## Websites (live, all verified)

YouTube (open, play, pause, resume), YouTube Music, Flipkart, Wikipedia, GitHub, Google Maps, Google search,
Instagram, LinkedIn, ChatGPT, Reddit search, Gmail inbox and Gmail search, bbc.com, and Amazon search.
- Reddit's intermittent "Prove your humanity" page is now reported as needing you, never "verified".
- Spoken titles no longer include "3 minutes, 37 seconds".

## Files (live, in a scratch folder)

Every operation passed:
- create folder; create txt / pdf / pptx / xlsx / docx
- read / append / replace
- list, copy, rename, move
- bulk move, organize + undo
- content search
- Recycle-Bin delete of a file and a folder

Fixes:
- "move all images" selected the extension `image` and matched nothing. Kind words (images, photos, videos,
  songs, documents, archives, installers) now map to their extensions.
- `list_files` with `path` silently listed the Desktop.

## Quota-free commands added

These now run without AI:
- "message X on whatsapp saying Y"
- "tell X on telegram that Y"
- email "subject X saying Y"
- "open documents / desktop"
- "close notepad / chrome / word"
- "open youtube and play X"
- "play X": a local file in Videos / Music / Downloads first, otherwise YouTube
- "search for X" / "google X" / "look up X"
- volume up / down, mute
- "create a folder called X on desktop"
- "open notepad and type X"

"What can you do" now answers in spoken sentences instead of listing tool identifiers.

## Still open

- Groq's daily quota ran out again after about 6 model calls in this sweep, so model-driven tasks (writing
  documents, multi-step browser tasks) stop when it is used up.
- The Telegram send step has not been live-tested; it stops before Enter without a yes.

Tests: 645 passed (`tests/current/test_phase6_local_commands.py`, `test_phase7_word.py` plus updates).
