# Phase 14: voice experience and a full capability sweep

Date: 2026-10-24. Source: the user's third live voice session, and a live sweep of every feature ADVI claims.

## Why ADVI "didn't hear" and "interrupt sometimes worked"
The log showed it directly:
- "Yes.", "S-I-Y-U", "The table is not proper, fix it" and "Open YouTube and play praise song" were logged "while ADVI spoke" and dropped, so the user had to repeat them.
- Phase 13 set that flag if ADVI talked at **any point during the recording**. A recording often starts while ADVI finishes its sentence, so a user who waited politely and then answered was still ignored.
- Interrupting a long speech worked because there the user really did talk over ADVI. After short messages it "didn't work" because those sentences were the ones being dropped.

## Turn-taking now (`voice/conversation.py`)
- **Who had the floor is decided when the user starts talking** (`Recorder.on_speech_start`). Only speech that *begins* while ADVI is talking counts as talking over it.
- **Talking over ADVI interrupts it, then the sentence is handled** (Phase 13 dropped anything except "stop" or "ADVI, …"):
  - ADVI's own voice leaking into the mic (three or more words matching what it said in the last 20 s) is ignored;
  - a one- or two-word answer that ADVI also just said ("yes" during "say yes to continue") stops ADVI and plays "your turn", so a leaked "yes" can never confirm a send.
- **Noise filter:** speech under 0.3 s, or a single filler word ("He", "uh", "and"), is not a request. "He" got "I'm not sure what you'd like" in the log.
- **"I'm still on the last one"** is said once per task, with no repeated tones.

## Sounds (`voice/earcons.py`)
- **Design:**
  - one soft bell or marimba timbre: a sine with fast-fading overtones and a short room tail, never a beep;
  - one pentatonic scale, so no two sounds clash;
  - rising = over to you, falling = got it / stopped, low and slow = working;
  - quiet, sitting under speech.
- **The set:**
  - your_turn (C6→G6);
  - heard (a soft tick);
  - got_it (G5→E5);
  - working (a slow C4+G4 "breath" every 2.6 s, only while ADVI is silent and busy);
  - stopped (E5→C5);
  - not_heard (D5, D5);
  - wake and sleep (rising and falling arpeggios).
- **Teaching:**
  - "what do the sounds mean?" plays each sound after saying its meaning;
  - the guide also plays once, the first time voice is used (marker file `runtime/voice_sound_guide_played`). Google's earcon guidance is that earcons only help once they are taught.

## Word, Excel and conversation fixes
- **Word table came out as Markdown:**
  - a row needed three pipes, so a one-column table ("| Month |") was written as text;
  - rows with two pipes, and borderless rows next to a `|---|` separator, are now tables;
  - the save is rejected unless every table is a real Word table with no literal `|` lines.
- **Excel:**
  - a Markdown table written as CSV put "| Item | Cost |" into cells; Markdown, tab-separated and CSV tables are all parsed now;
  - the header is bold, filled and frozen, and columns are sized to their content.
- **"Put this speech into a PDF" got "I don't have the speech text":**
  - rebuilt AI sessions keep only the first 500–1,200 characters of earlier answers;
  - the last long answer (within 15 minutes) is passed in full, up to 6,000 characters, when the user points back to it.

## Capability sweep
`benchmarks/desktop/capabilities.json` holds 38 live tasks. This is a feature check written while fixing, not a score. The areas:
- **Word:** table, structured report, two-column table.
- **Excel:** create, and read and total.
- **PowerPoint:** a three-slide deck.
- **PDF:** create.
- **Files:** read a .docx; open and close Notepad; create, rename, copy and delete; search by content; count; find the largest; recommend; organize.
- **Media:** play and close a video, play and close audio, YouTube play and pause.
- **Websites:** Google, Amazon, Flipkart, GitHub, and a Wikipedia answer.
- **Email:** read the inbox, count unread, and send and trash up to the confirmation (answered "no").
- **System:** time, disk space, open and close Calculator.

Run: `.venv\Scripts\python.exe -m benchmarks.desktop.run --tasks capabilities --name <run>`.

## Final results (2026-10-25)
- **First full pass, `capabilities-r1`:** 31/38 (82%).
- **After fixes:**
  - **every failure re-run:** `capabilities-r1-fixes`, 15/15, plus `-fixes2` and `-fixes3` for the last three fixes;
  - **result:** **all 38 capabilities verified working at least once**, with plain answers. The brain was gpt-oss-120b on every task.
- **Not a benchmark score:** the tasks were written and fixed in the same session.

| Found by the sweep | Fix |
|---|---|
| "Close the video player" (the clip had already ended) tried 4 times to close the user's **Brave** window | **Safety:** `_close_target_refusal` closes only the app named, or the kind asked for (video player → media players; browser → browsers). Otherwise it refuses and the AI says the app isn't open |
| "Which is the largest file" / "remove empty folders": the AI "couldn't see" the files | After a rate-limit failover, the rebuilt conversation cut nesting at depth 5, so every file name and size became "[depth limit]". The depth is now 9; size stays bounded by the 7,000-character cap |
| "Search Amazon for a wireless mouse" searched Google (`site:amazon.com`); "Search Google for…" first searched the open YouTube page | A search that names a known site goes to that site's search page, unless the browser is already on it; the "Search X for Y" form is now parsed |
| Who sent the latest email: the answer was replaced by "I haven't sent anything yet" | The false-send guard ignores descriptions of received mail |
| "What time is it": the AI said it couldn't tell | Time and date are answered from the PC clock, without the AI |
| Excel: "I can't read .xlsx files" | `read_file` lists the formats it reads |
| Play `tone.wav` / "video clip": not found | File extensions and generic words ("video", "song") are not title words; an exact path plays directly |
| A video playing behind other windows counted as a failure, and the answer quoted the screen description | A media player that just started counts as proof; failure answers name the failed step |
| Pause, web search and folder creation said "couldn't verify the final state" or "hit the safety limit" | `media_control` and `remove_empty_folders` end a task; search-box typing that lands on results counts as browsing; independent creations run in one round; a call rejected before it ran isn't a task failure |
| `wait 1000` reported "Waited for 1000.0s" | Milliseconds are understood, and the real wait is reported |
| Desktop scroll during browsing scrolled the Claude window | During browsing, scroll and observe go to the page; empty pages get a second, longer wait |

## Results of the first day (run `capabilities-r1`, 2026-10-24)
| Task | Result |
|---|---|
| cap-word-table (one-column months table) | PASS: a real 13-row Word table |
| cap-word-report (title, headings, bullets) | PASS: real Word styles, no Markdown left |
| cap-word-two-col-table (planets) | PASS |
| cap-excel-create | PASS: values in real cells |
| cap-excel-read | FAIL → fixed. The AI said it "can't read .xlsx"; `read_file` now says it reads xlsx, docx, pptx and pdf |
| cap-ppt-create and the 32 remaining tasks | not run: Groq's daily token quota ran out on all 3 keys and the Gemini backup failed |

- **Also fixed before they ran:** "which is the largest file" was offered no file tool (largest/newest/oldest and "which file should I open" now route to the file tools).
- **The runner now waits as long as Groq asks,** with growing waits (1 → 15 minutes), instead of stopping after three 65-second retries.
- **To resume:** `.venv\Scripts\python.exe -m benchmarks.desktop.run --tasks capabilities --name capabilities-r1`. It skips finished tasks.
- **To re-check Excel reading after its fix:** add `--only cap-excel-read --name capabilities-r1-fixes`.
