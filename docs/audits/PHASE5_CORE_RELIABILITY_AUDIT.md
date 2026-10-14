# ADVI 0.3.4: Phase 5 Audit, Local Apps, Files and Browser (2026-10-14 evening)

Scope requested: make **local application control, file/desktop operations and browser operations** secure and
smooth, and fix everything else the 30-turn evening log showed.

## Fixes by area (log turn in brackets)

### Status honesty
- Verified skills (WhatsApp send, play_media) now complete their task. They had reported "couldn't verify" [2, 11, 19].
- Replies are no longer overridden when nothing was executed. A model question had become "the final state could
  not be verified" [22, 29].
- "First step done" is no longer "task done": "create a folder **and move all** the PDFs" or "organize" keeps going
  after the folder exists [9].
- Each task family's completion rule judges only that family's tools. "Create a folder called **PDF**" was
  classed as a document task and could never complete on folder evidence.
- A verified bulk move or organize completes its request.

### Safety
- "cancel all active task" with nothing running is answered locally. It had reached the model, which tried to
  **close Word** [25].
- **Deletes go to the Recycle Bin** (`SHFileOperationW` with `FOF_ALLOWUNDO`). They used to be `unlink()` and
  `rmtree()`, permanent and unconfirmed. On drives without a Recycle Bin (USB, network) ADVI refuses rather than
  deleting permanently. A non-empty folder needs `recursive=true`.
- **Consequential web clicks need a spoken yes:** labels like buy, pay, place order, checkout, transfer, send,
  submit, delete, subscribe, publish. Ordinary clicks (play, search, add to cart, sign in, filters) are not gated.
- `create_file` refuses a stub when a speech, sermon, essay, poem, letter or story was asked for: the PPT
  "sermon" contained only "Beautiful sermon." [13]. The prompt also says to compose such text, not to ask for it.

### Files and desktop
- **`move_files` / `organize_folder` / `undo_file_operation`** (`capabilities/files/bulk.py`): one call instead of
  one model round per file [16–18]. Never overwrites (clashes get "(2)"), skips in-progress downloads, verifies on
  disk, and journals every batch to `%LOCALAPPDATA%\ADVI\file_journal.json` for undo.
- `create_folder` on an existing folder succeeds (the state already holds); "try again" had failed [17].
- `find_file` gains videos, music and pictures scopes, and **partial matches** (all query words), labelled as
  partial. "here I am to worship" took 20 s and found nothing; now it takes 0.01 s [12].
- **`play_local_media`** plays a file from Videos, Music, Downloads or Desktop in the default player, verified by
  its window [11–12]. Play requests offer both players; explicit local cues ("in my videos folder") offer only
  the local one.

### Local applications
- **Save As saved under the wrong name** (reproduced live twice). `set_edit_text` changes the box's text but not
  the dialog's filename. The dialog is now found by control ID `1001` (the file list exposes dozens of Name/Type
  Edit cells), filled by real typing, and read back; it is cancelled if unconfirmed.
- **`close_application`** finds packaged apps (Windows 11 Notepad, Word) by window title "<doc> - <App>" or the
  launch matcher. It had failed with "no closable visible window" [25].
- Readiness: a window visible for 2.5 s with no blocking dialog is usable. Notepad was reported "still loading"
  because its UI never holds two identical samples.

### Browser and media
- `play_media` navigates with Playwright. The legacy websocket navigate timed out on YouTube Music [20]. YouTube
  also tries the word-overlap picker first.
- **`media_control`** (pause, resume, next, previous): verified against the page's media elements in ADVI's
  browser, or the Windows media key for other players. A bare "stop" right after ADVI started playback pauses it
  locally, with no AI round.

### Provider load (the main cause of failed turns)
- **Rejected undeclared tool calls run without another round.** Groq's `failed_generation` is attached to the
  error; the session seeds the call into a fresh provider session and executes it through the normal guard.
  There were 9 such rejections in this log, each costing a round.
- **System prompt cut from ~2,200 to ~980 tokens** with every rule kept: about 25% less per request. At ~8k tokens
  per minute per key, that is the difference between ~1 and ~1.4 requests per minute.

## Live checks on this machine

| Check | Result |
|---|---|
| Notepad: open_application | verified, 1.3 s (was "still loading") |
| observe_window, then interact type into `e23 Document "Text editor"` | verified |
| save_document_as to a new path | verified, correct name and content on disk |
| close_application Notepad | verified, 0.4 s (was failing) |
| find_file / play_local_media "here i am to worship" | found in 0.01–0.02 s |
| play_media YouTube, then media_control pause/resume/pause | all verified |
| Recycle Bin helper on a file and a folder | moved to Recycle Bin |

Test artifacts were removed (two stray Notepad saves in `Documents\@AVNI Project\chatGPT`). One small test file
and folder, `advi_recycle_test.txt` and `advi_recycle_folder`, are in the Recycle Bin.

## Still open
- Provider capacity: `groq-1` is still rejected; per-minute limits on the others remain the binding constraint.
- Word showed "Product Activation Failed". That is a licensing state ADVI can report but not fix.
- Ambiguous media requests depend on the model choosing local vs YouTube.

## Validation
- New/updated: `test_phase5_files_and_status.py` (17), `test_phase4_browser.py` (+2), `test_phase1_media.py`,
  `test_phase2_save_as.py`, and `tests/current/conftest.py` (tests never use the real Recycle Bin).
  Full suite: **613 passed** (live Groq smoke test deselected).
