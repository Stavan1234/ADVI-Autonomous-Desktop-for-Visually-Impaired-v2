# ADVI Desktop Suite v1: results

**Suite:**
- 23 Windows tasks: 9 adapted from Windows Agent Arena (MIT), 14 ADVI's own.
- Each runs in its own sandbox folder (`Documents\ADVI Bench\<task>`).
- Each is scored by checking the real end state: files on disk, their contents, the browser's URL, and ADVI's answer or confirmation prompt. There is no AI judge.

**Split:** fixed by seed before any run (`benchmarks/desktop/splits.json`): 9 dev tasks and 14 test tasks.

**Run conditions:**
- Brain: `openai/gpt-oss-120b` on Groq (pinned; no Gemini fallback).
- Confirmations are always answered "no".
- Budget: 15 AI calls or 5 minutes per task.
- Machine: the developer's Windows 11 laptop (i5-8265U, 16 GB).

> Not a Windows Agent Arena score. WAA's tasks run in a 30 GB Windows VM through a screenshot-based agent interface. Here 9 of its tasks keep their goal and the same kind of end-state check, re-rooted into a sandbox folder. Two WAA originals had broken checks (`5316686e` sets the opposite registry value; `5548314e` checks a misspelled path), so all checks were re-implemented.

## Test split (`test-v1`, partial, no longer clean; see the disclosure)
**In progress (2026-10-23):** 5 of 14 test tasks have run; the batch stopped on the AI quota and resumes with the same `--name test-v1`. These are partial numbers, not the score:

| Task | Result | What happened |
|---|---|---|
| waa-archive-docx | pass | |
| waa-move-folder | pass | 9 AI calls and 28.6 s for one move; the answer still said "couldn't verify the final state" |
| waa-large-files-report | pass | |
| waa-remove-empty | **fail** | ADVI asked the user instead of listing the folders itself (prompt misalignment) |
| waa-hide-file | **fail** | No tool can hide a file (missing tool) |
| waa-notepad-draft | external | All 3 Groq keys were rate-limited, then the Gemini backup timed out (0 successful AI calls); it will be rerun |

**Disclosure: test-v1 is no longer a clean held-out score.** At the user's request (2026-10-23), every weakness found so far was fixed, including three that only scoring tasks had shown:
- `set_file_hidden` (from waa-hide-file);
- listings now say which folders are empty, plus a "look it up yourself, don't ask" rule (from waa-remove-empty);
- folder names containing "folder" no longer break paths (from waa-move-folder: `waa-move-folder` became `waa-move- `).

The 5 tasks above keep their original results. The remaining 9 run with the fixes. A clean score needs new test tasks in Suite v2, written and split before ADVI sees them.

## Dev rounds: what the benchmark found and what changed in ADVI

| Round | Dev score | Failures found | General fix |
|---|---|---|---|
| r1 | 6/9 | **zip archive:** no tool existed | `zip_files`: zips a folder or listed files, never overwrites, and verifies by reading the archive back |
| r1 | | **word count:** wrote 20, the truth is 37 | `search_file_content` stopped listing at 20 lines and said "Found 20". It now counts every occurrence in a file ("appears 37 times on 37 lines") and says when its listing was cut off |
| r1 | | **"a PDF named Water"** was saved as "Drinking Water.pdf" | The file name the user says ("named/called X") is kept for Word documents and text files |
| r1 | | "I couldn't complete that: Saved file…" after a correct save | A harmless wrong step (`open_folder` on a file, which changed nothing) no longer fails a task that a later verified step finished. Failed sends, deletes or typing still do. |
| r1 | | "Open wikipedia.org" answered "couldn't verify the final state" | A verified open is the end of a plain "open X" request, even if the AI looked at the page afterwards |
| r1 | | All 3 Groq keys hit the per-minute limit; ADVI told the user "try again in 20 s" | ADVI now waits out a short limit (25 s or less, until the first key is free), says "The AI is busy for a moment, so I'm waiting about N seconds", and retries once. A daily quota or a longer limit is still reported straight away |
| r2 | 8/9 | **word count:** the AI counted by reading 12K characters itself and wrote 8 | Counting requests are routed to the exact counting tool, plus a prompt rule: "never count by reading". The AI decides; local software owns the facts |
| r3 | 9/9 | "I've saved it to example_count.txt" was replaced by "I haven't saved that to memory" | The Phase 10 memory-honesty guard was too broad; it now reacts only to claims about memory |

Each fix has unit tests in `tests/current/test_benchmark_dev_fixes.py`.

## Regression run, 2026-10-24 (all 23 tasks; a regression check, not a score)
Run name `regression-20261024`. Same settings as above.

| Domain | n | Passed |
|---|---|---|
| browser | 2 | 2 |
| files | 15 | 14 |
| messaging | 1 | 1 |
| notepad | 2 | 0 |
| system | 1 | 1 |
| word | 2 | 2 |
| **Overall** | **23** | **20** |

The three failures each had a defect behind them:
- **remove-empty:** no tool could remove empty folders.
- **notepad-draft:** a restored Notepad tab, plus a path under the wrong user profile.
- **notepad-count:** the controller opened a file as if it were a folder.

After the fixes, those three were rerun as `regression-20261024-fixes`, together with two passing tasks whose answers were badly worded: **5/5**. These are dev and test tasks that had already been seen, so the numbers show that the fixes work. They don't measure how well ADVI generalises.

## Capability sweep, 2026-10-24/25 (38 live feature checks; not a score)
`benchmarks/desktop/capabilities.json` was written in the same session as the fixes it led to. It shows which features work on the development PC. It doesn't measure generalisation.

| Run | Tasks | Passed |
|---|---|---|
| `capabilities-r1` (first full pass) | 38 | 31 |
| `capabilities-r1-fixes` (failed or badly worded tasks, after fixes) | 15 | 15 |
| `capabilities-r1-fixes2` | 3 | 2 (a video task failed on the new wording "video clip") |
| `capabilities-r1-fixes3` (after that fix) | 2 | 2 |

What the first pass found:
- an attempt to close an unrelated browser window (a close-target guard was added);
- a failover handoff that hid file details;
- "search Amazon" run on Google;
- inbox answers overridden by an over-broad "unbacked send" guard;
- the model refusing to tell the time;
- media files not found by their spoken description.

Every one of the 38 tasks has passed at least once on the final code. The full sweep hasn't been rerun end to end since.
