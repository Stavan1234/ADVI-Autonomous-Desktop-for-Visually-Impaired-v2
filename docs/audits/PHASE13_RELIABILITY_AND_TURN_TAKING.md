# Phase 13: reliability and voice turn-taking

Date: 2026-10-24. Sources:
- the user's second live voice session and its log;
- a full Desktop Suite regression run (all 23 tasks, live AI).

## Voice (`voice/conversation.py`, `voice/recorder.py`, `voice/earcons.py`)
| Problem | Cause | Fix |
|---|---|---|
| Cut off before finishing | End-of-speech was 0.8 s of silence (1.5 s after 4 s) | Now 1.2 s (2 s after 3 s). Sentences that end mid-thought ("… saying", "the…", a comma) get a 2.5 s continuation window, and the two parts are joined |
| "I don't know when to speak" | No cue after ADVI finished | A soft "your turn" chime once ADVI is quiet and idle; a louder working pulse while busy; the startup message explains the chime |
| Can't interrupt ADVI by voice | Half duplex, so nothing was heard while ADVI spoke | The mic stays open. While ADVI speaks, only stop words or "ADVI, …" act. Text matching what ADVI said in the last 20 s is dropped as echo (`OutputManager.recent_speech`). The laptop mic's own echo suppression does the rest (Phase 12 test). This is how smart speakers handle interruption: echo cancellation plus a small command set while talking |
| "I'm still on the last one" said 5 times | Said for every sentence while busy | Said once per task, then only a soft cue |
| "Pause the song" ignored during music | The name gate blocked everything | Plain media commands (pause, next, volume…) pass without the name; lyrics still don't |
| Sleep and exit didn't close ADVI | Typed input waited behind a 4-minute task; spoken "sleep" only stops listening | Typed `exit`, `quit` and `sleep` are handled on the input thread, ask the running task to stop, and force-close after 8 s if a tool is stuck. Spoken "exit", "quit" or "close ADVI" also close ADVI |
| "Stop" didn't stop a long search | A stop was checked only between tool calls | `core/cancel.py` holds a process-wide stop flag checked inside `find_file` and the computer-wide `list_files` walk. The walk now starts in your own folders, skips Windows, program and AppData folders, stops after 20 s, and says so |

## Tasks
| Problem (log or benchmark) | Fix |
|---|---|
| WhatsApp "couldn't confirm that chat opened" for "Siyu ^_^" | The chat check compares letters and digits only, so emoticons and emoji in titles don't matter |
| "Couldn't bring WhatsApp to the front" | `uia.force_foreground`: a plain call, then an Alt-key unlock, then attaching to the foreground thread's input. Shared by WhatsApp and Telegram |
| "Try again" re-sent to the first-heard name "Siu" | A failed confirmed send is kept; "try again" re-confirms exactly that call, corrections included |
| "Change the body of the email to X" gave the body "of the email to X" | Shared `_email_edit_value` parser |
| "It's ADVI, not Adri" led to sending the uncorrected email | Local word swap (`names.word_correction` and `replace_word`) for email drafts and messages, followed by a new confirmation. After the AI answers a reply to a pending action, ADVI adds "Right now, yes will … unchanged" |
| "Read out that email" read the email ADVI had just sent | After a send, only "the email you sent" or "the draft" is answered from the sent draft; anything else goes to the inbox |
| "Couldn't verify the final state: Read 1 email" | Email, draft, contact and system reads count as read-only, so the AI's answer stays |
| "Couldn't verify the final state: Paused." | When only the AI's closing sentence was lost (a per-minute limit) after a verified step, ADVI reports the step |
| Email couldn't forward, delete, archive or mark | New verified tools: `email_forward` and `email_trash` (both confirmed; Trash keeps mail for 30 days), `email_archive`, `email_mark` (read, unread, starred, unstarred). They use the existing `gmail.modify` scope, and confirmations name the email ("'Statement' from bank@…") |
| Suite: a verified folder move reported as unverified | `_MORE_STEPS_RE` counted the request's own "Move the …" as a second step; now only "and/then move …" counts |
| Suite: a re-check after the work left the task "unverified" | A verified action followed by a look still ends the task |
| Suite: "remove all empty folders" left to the AI | New `remove_empty_folders` tool: one verified step, to the Recycle Bin |
| Suite: Notepad typed into an old reopened tab | After the AI opens Notepad for writing, ADVI opens a fresh tab (as the offline path already did) |
| Suite: saved to `C:\Users\Public\Documents\…` | Paths under another Windows profile are remapped to this user when the folder exists here |
| Suite: "open the file … count … save" opened the file as a folder | The controller only auto-opens for a plain open or read request, and only uses open_folder for a real folder |

## Results
- **Desktop Suite regression (before these fixes):** 20/23. The failures were remove-empty, notepad-draft and notepad-count.
- **After the fixes, those 3 plus the 2 tasks with the wrong wording:** 5/5. The brain was gpt-oss-120b on every task.
- This is a regression measure, not a held-out score: the Suite's test split was already used for fixes (see DESKTOP_RESULTS.md).
- **Unit tests:** 860 passed, including `test_phase13_reliability.py` and the extended Phase 12 voice tests.
- **Not yet verified live:** voice interruption with real speakers, the "your turn" chime with a person, and WhatsApp focus from a background app.
