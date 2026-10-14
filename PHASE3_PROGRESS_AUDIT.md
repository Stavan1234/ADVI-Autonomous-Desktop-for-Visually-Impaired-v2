# ADVI 0.3.4: Phase 3 Audit (2026-10-14)

Covers the second live WhatsApp trial, log turns 23–26 (Notepad), and spoken progress (roadmap §2.5).

## WhatsApp: why typing never arrived, and the fix

The trial opened the chat correctly but stopped at "couldn't confirm it in the message box". Live experiments
(typing only, then clearing; nothing was sent) found:

| Finding | Evidence |
|---|---|
| Keyboard injection works in general | Typed into a private Tk window |
| WhatsApp's keyboard focus sits on its WebView2 host `Chrome_WidgetWin_0`; UIA focus was a generic Pane | `GetGUIThreadInfo`, `GetFocusedElement` |
| `SetValue` works on the search box (results update) but the rich-text composer ignores it | Value stayed `"\n"` |
| `WM_CHAR` posted to the host after UIA `SetFocus` reaches the focused field | Search box and composer read back the text |
| …except for ~2 s after a real mouse click into the window | Same call: `'\n'` right after a click, `'ab'` two seconds later |
| Right after a click, real keystrokes work, unless UIA `SetFocus` is called first | `cd` arrived with click+keys and no SetFocus |

The old keyboard route did click → **UIA SetFocus** → type, the one combination that always fails. Text entry now
tries, with read-back after each attempt:
1. `SetValue` (search box only).
2. UIA focus + posted characters.
3. Click + keystrokes without SetFocus.
4. Route 2 again after a 2 s settle.

Each failed attempt removes only the characters it added. Sending invokes WhatsApp's own **Send** button through
UIA, which appears once the composer has text. Enter is only a fallback. Newlines are flattened so text can't
be sent early.

Live dry run to the point of sending: chat opened and verified, "test" typed and read back in 2.6 s, Send
button present. The composer was then cleared; **nothing was sent**.

## Notepad turns 23–26

- The model observed with `observe_window` (Phase 2 working) but typed with `type_text`. The text landed: the
  window title became "*Whispers of dawn…".
- **Notepad's editor was missing from observations.** It is a UIA `Document` named "Text editor", not an
  `Edit`. `Document` is now an interactive type and its text is read as the value.
- **Save As saved under the wrong name.** `set_edit_text` raised no error, nothing read the filename field back,
  and Notepad saved as "Whispers of dawn in quiet light,.txt" in `Documents\@AVNI Project\chatGPT\`. Now the
  field is read back, typing is the fallback, and **if the filename still isn't confirmed the dialog is
  cancelled rather than saved**. If the app saves elsewhere anyway, the actual name from the window title is
  reported.
- `interact(type)` gained the same posted-characters route for WebView2/Chromium apps.
- Every turn still burned provider failovers (`groq-1` rejected; per-minute limits on the others).

## Added: spoken progress

- `io/progress.py`: one short phrase per meaningful tool start ("Opening Notepad.", "Sending your WhatsApp
  message to New Chapter.", "Creating wake_up.pdf."). Observations, clicks and keystrokes stay silent.
- `OutputManager.narrate()`: shows the phrase, and speaks it on the existing single speech thread with
  "latest only" semantics. A phrase still waiting when the next phrase or the final answer arrives is dropped,
  repeats are skipped, and it replaces the generic "Working on that." The agent's work never waits for speech.
- Hooked once in `AgentSession._run_tool`, so confirmed actions and local continuations are announced too.

## Validation

- New tests: `test_phase2_save_as.py` (3), `test_phase3_progress.py` (4), 4 new WhatsApp route tests,
  1 observer test. Full suite: 587 passed (live Groq smoke test deselected).

## Next

- File index (Everything / Windows Search) and a reversible-action journal.
- Interrupting speech by voice (barge-in) needs the voice input path; the current loop reads typed lines.
