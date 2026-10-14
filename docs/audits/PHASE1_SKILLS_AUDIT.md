# ADVI 0.3.4: Phase 1 Audit (2026-10-14)

Second implementation pass. It starts from the 2026-10-14 runtime log (16 prompts, the first run after Phase 0),
then adds the first app skills from `docs/ADVI_Agent_Architecture_Overhaul.md` §4.2 Phase 1.

## What the 2026-10-14 log showed

Phase 0 fixes visible in the run: "laptop status" was answered locally, PDF creation reported `completed`, rate
limits named their cause, and "cancel all active tasks" was handled locally.

New failures, all fixed below:

| Turn | Prompt | What happened | Root cause |
|---:|---|---|---|
| 2 | laptop status + today's date | Only the status was answered | My Phase 0 fast path matched compound questions; the model was also never given the date |
| 3 | 1000-word speech PDF | Two real speeches rejected, then `"[Insert a 1000-word speech here]"` "created" | PDF renderer dropped every paragraph longer than ~4 lines; the verifier then accepted a placeholder |
| 3 | (same) | "File already exists" on retry; 3 junk PDFs left behind | Failed creations were never cleaned up |
| 4–5 | read the PDF | Model said "placeholder text"; ADVI replaced that with "couldn't verify" | The uncertainty override also fired for read-only tasks |
| 7, 10 | "yes, and choose topic …" | Provider 400, then an empty response | Reply turns were sent with zero tools; Phase 0's `request_tools` was gated on task classification |
| 9 | "open it" | "already open; reused" but Edge was focused | Reuse checked only that the window existed, not that it still showed the file |
| 13 | WhatsApp group send | Typed into nothing, never opened the chat, "Pressed key 'enter'" | Generic GUI loop; OCR cannot tell where focus is |

## Fixed

- **PDF rendering (`capabilities/files/pdf_layout.py`).** The renderer measures and wraps text and splits paragraphs
  across pages. It picks the largest font size (10.8 down to 7.6 pt) at which the content fits the requested page
  count, and flows onto extra pages only when it cannot fit. Fonts are chosen by a round-trip check: Calibri Bold
  extracts spaces as U+00A0, which broke verification and screen-reader output. The old renderer had passed
  `fontname="helv"`, which made PyMuPDF silently ignore the font file.
- **Placeholder content is refused** by `create_file`/`save_file` before anything is written.
- **Failed creations are cleaned up.** A file that `create_file` just wrote and that then fails verification is
  removed (`ExecutionEngine._discard_unverified_creation`). Pre-existing files are never touched.
- **Page count.** The verifier accepts at least the requested pages (it no longer requires an exact match),
  because extra pages only exist to avoid dropping text.
- **Read-only answers are no longer overridden.** If a task ran only successful read-only tools, the model's answer stands.
- **`request_tools` on every non-greeting turn.** Missing-tool recovery now chains up to 2 times, can declare
  `request_tools` itself, and accepts `{"name": ...}`.
- **Document reuse** requires the tracked window's title to still name the file.
- **Compound questions** skip the local system fast path. The model now receives a `[LOCAL TIME]` line.
- **Latent crash:** `capabilities/registry.py` used `logger` without defining it.
- **Verifier evidence:** handler evidence is no longer replaced with `{"method": "no_verifier"}`.

## Added: app skills

- **`whatsapp_send_message(chat, text)` / `whatsapp_open_chat(chat)`** (`capabilities/apps/`). WhatsApp Desktop is
  WebView2. A read-only probe on this machine showed UIA exposes its web content. A full tree walk takes ~15 s
  (~18,800 nodes); scoped `FindFirst` takes ~0.02 s. The skill checks each step:
  window ready → search box visible (logged in) → search box focused and holding the query → exactly one matching
  chat (otherwise it asks, listing candidates) → title shown right of the chat list → composer empty (an existing
  draft is never touched) → composer holds the exact text → Enter → composer cleared.
  Sending requires the user's spoken confirmation, the same path as `email_send`. WhatsApp requests now get only
  these two tools, so the model cannot fall back to click/type.
- **`play_media(query, service)`.** It builds the YouTube or YouTube Music search URL and reuses the existing
  result pickers. It succeeds only when the page's media element is playing. Play requests route to it;
  local files, other sites (Miruro, Spotify) and WhatsApp messages do not.

## Not verified live

- The WhatsApp skill's open-chat half (header and composer lookup) could not be probed without opening a chat,
  which marks messages as read. It fails safe: if it cannot positively read the composer back, nothing is sent.
  To try it: `.venv\Scripts\python.exe scripts\try_whatsapp_skill.py --chat "New Chapter"` (open only).
- `play_media` reuses code that worked in earlier logs but has not been run end to end through the new tool.
- The no-progress detector does not see observations made by ADVI's internal recovery/checkpoint code
  (`recovery-*` / `adaptive-*` calls). With the WhatsApp skill this matters less; it remains for the generic loop.

## Validation

- Tests: `test_phase1_documents.py` (16), `test_phase1_whatsapp.py` (11), `test_phase1_media.py` (8), and
  3 updated in `test_phase0_integrity.py`. Full suite: 562 passed (live Groq smoke test deselected).
