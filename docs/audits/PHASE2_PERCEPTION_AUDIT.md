# ADVI 0.3.4: Phase 2 Audit (2026-10-14)

Covers the first live trial of the WhatsApp skill, turns 17–22 of the runtime log, and the first part of Phase 2
(structured perception) from `docs/ADVI_Agent_Architecture_Overhaul.md`.

## Log turns 17–22

| Turn | Request | Result |
|---:|---|---|
| 17 | Miruro: search and play episode | Failed: `groq-1` key rejected, then the other keys hit their per-minute limits and Gemini timed out (6 failovers) |
| 18 | 1000-word speech → `wake_up.pdf` | **Worked**: one `create_file` call, verified, full text |
| 19 | open that pdf | **Worked** |
| 20 | read this pdf | **Worked**: real speech text read back |
| 21–22 | WhatsApp "New Chapter" + "yes" | Routed to the skill and confirmation was asked, as designed; failed at `logged_in` (see below) |

## WhatsApp skill: trial failures and fixes

A read-only probe after the trial showed what the real app does:

| Observation | Failure it caused | Fix |
|---|---|---|
| The search box is named "Search or start a new chat" only while empty; its name is `''` once it holds text | Runs 3 and 5 and ADVI turn 22: "logged out" | Search box = the name match, else the Edit that is not the composer |
| Cells are named "New Chapter 21/07/2025"; no node holds the bare title | Run 2: title read as "New Chapter 21/07/2025" | `chat_title_from_label` strips the date/time stamp and preview |
| The composer is named "Type a message to group New Chapter" | (my header-geometry check could not match the header "New Chapter You") | The open chat is verified from the composer's name |
| Keystrokes were lost after restoring a minimized window, although UIA reported focus | Run 1: "search box didn't accept the chat name" | Click the field for real input focus, retry once, then `ValuePattern.SetValue`, allowed for the search box only, never the rich-text composer |

Read-only validation against the live window: search box found (holding "New Chapter"), title parsed as
"New Chapter", composer identified, chat-open check true.

## Added: structured perception for any app

- **`observe_window(application?)`** lists the window's on-screen interactive elements with IDs:
  `{id: "e7", role, name, value, focused, disabled}`. One native UIA query (interactive control types AND
  on-screen) takes 0.01–0.17 s per window on this machine. Full observations, including per-element property
  reads, took 0.7–1.1 s and about 2,800 characters. Web apps are read from their `RootWebArea` document to avoid
  duplicated trees. Above 45 elements it keeps a mix: the focused element, input fields, content items (files,
  rows, links), then buttons. Plain top-to-bottom order had filled File Explorer's budget with 28 ribbon buttons
  and one file.
- **`interact(element_id, action, text?)`** supports click, type, set_value, select, toggle, expand and focus.
  It uses UIA patterns first (Invoke/Toggle/…), then a real mouse click. A click counts as verified only if the
  window observably changed; typed text is read back. Every result includes a fresh observation. IDs from an
  older observation are refused.
- **Routing:** any surface that offers click/type_text/read_screen also offers the pair, as does any compound
  app request ("open settings and turn on dark mode"). The system prompt says to observe → interact, with OCR
  for reading text only.
- **Recovery:** after an unverified desktop action, ADVI's automatic look at the screen now uses
  `observe_window` instead of OCR. These internal observations also feed the no-progress detector, which
  previously never saw them.

## Not done / next

- Not yet exercised by the LLM end to end; the WhatsApp skill still needs its live retry.
- No Set-of-Mark vision fallback for apps whose UIA tree is empty (games, some canvas apps).
- Provider capacity: `groq-1` is still rejected on every run. Each generic browser step costs a full LLM round
  trip, so long site workflows (Miruro) keep hitting per-minute limits.
- Remaining roadmap: asynchronous spoken progress, a file index (Everything / Windows Search), reversible-action
  journal.

## Validation

- `test_phase1_whatsapp.py` now has 16 tests, including the 3 trial failures. `test_phase2_perception.py` has 9.
- Full suite: 576 passed (live Groq smoke test deselected).
