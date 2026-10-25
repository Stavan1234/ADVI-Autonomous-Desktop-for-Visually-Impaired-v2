# Phase 15: ADVI stops hearing itself, sleep closes ADVI, the wake word

**Date:** 2026-10-25
**Trigger:** the fourth live voice session (logs/advi.log, 16:52–16:58) and three requests from the user:
1. ADVI listens to its own voice and responds to it.
2. "Sleep" must close ADVI completely, not just stop listening.
3. "Wake up ADVI" must start ADVI even when it is closed, and after a restart, with no human action.

## 1. ADVI answered its own voice

### What the log showed
ADVI's own speech came back through the microphone and was submitted as requests:

| Heard as | What ADVI had said |
|---|---|
| "Everything is ready, sir. I'm listening, just told." | Its own startup greeting |
| "Backslash, backslash, backslash, backslash, backslash, points. Let me know if you want to." | A file path, read aloud character by character |
| "The protein works solid." | Its own sentence, misheard |
| "Just let me know that you've managed." | Its own sentence, misheard |

### Why the existing guard failed
The only guard compared each transcript with ADVI's recent words. That fails whenever its own voice is misheard. Some echoes also started in the gap between two queued sentences, when ADVI counted as "quiet".

### How voice assistants solve it
Acoustic echo cancellation (AEC). The app knows exactly what it plays, and an adaptive filter subtracts that signal from the microphone:
- WebRTC AEC3 in Chrome and Meet, which ChatGPT and Gemini in the browser get through `getUserMedia({echoCancellation: true})`;
- iOS voice-processing I/O and Android's AcousticEchoCanceler;
- hardware AEC with microphone arrays in smart speakers.

The echo-cancelled signal then goes to speech detection. Barge-in works because the user's voice is what remains.

Phase 12 tried `echoff`, which uses Windows' loopback as the reference. The loopback delivers no frames while the PC is silent, so it was rejected.

### What was built (`src/advi/voice/audio_io.py`)
- **One duplex stream:** a single sounddevice stream (16 kHz, 30 ms blocks) carries all of ADVI's speech and earcons. Its callback receives the microphone block recorded while each output block played, so the reference and the microphone stay aligned.
- **The canceller:** WebRTC's audio processing module from LiveKit (`livekit`, Apache-2.0). It runs AEC3, noise suppression and a high-pass filter on 10 ms frames, in a worker thread. Its delay setting is the stream's reported input plus output latency (30 + 180 ms on this laptop).
- **Speech output:**
  - SAPI (or Piper) now synthesises into memory, sentence by sentence (`PiperTTS._speak_through_player`).
  - Speech plays through this stream, which also makes stopping instant: the PowerShell voice process is no longer killed and restarted.
- **Barge-in:**
  - With the canceller on, speech that *starts* while ADVI talks is the user, and ADVI stops talking at once (`VoiceConversation._on_speech_start`).
  - The onset needs 0.09 s of continuous speech, so one stray frame can't stop ADVI.
- **Kept as a second guard:** the transcript echo check.
- **Fallback:** without `livekit`, or if the duplex stream can't open, the raw microphone is used and the old rules apply. `ADVI_ECHO_CANCEL=off` forces this for troubleshooting.
- **Spoken paths:** paths are now said as "poem.txt in Downloads" (`speakable_paths`), not "C colon backslash...".

### Measurements (dev laptop speakers and microphone, 2026-10-25)
29 s of SAPI speech was played through the speakers while nobody talked.

| Signal | Silero speech frames | Utterances the recorder would make | Whisper on the whole recording |
|---|---|---|---|
| Raw microphone | 53 | 1 | ADVI's sentences, including the greeting |
| After AEC (delay 210 ms) | 0 | 0 | 'Yeah, you know what? Yeah.' (a hallucination on the whole 30 s; the VAD gate means it is never transcribed live) |

**Double talk.** A synthetic user command was added at −30 to −18 dBFS on top of the recording:
- After AEC it kept 37–59 speech frames, so the user can still interrupt.
- Recognition while both voices overlap was poorer early in the stream ("Open this pad right hello") and correct later ("Open Notepad and write a book" / "Hollow").
- This is why ADVI stops talking at the speech *onset*: the rest of the sentence is then clean.

**Live startup, 2026-10-25 20:34 and 20:37.** ADVI spoke its long ready message with the microphone open, and no "Voice heard" entry followed. Before the fix, the same greeting was submitted as a request (16:53:58).

## 2. Sleep closes ADVI
- `VoiceConversation.sleep()` now says "Going to sleep. Say wake up ADVI when you need me." and requests exit, the same path as "exit".
- Typed "sleep" already exited.
- The hotkey no longer toggles a sleep state; it stops ADVI's speech, or plays "your turn".
- Closing ADVI mid-answer used to wait for the whole answer (38 s measured). `OutputManager.close()` now cuts remaining speech; the goodbye is still waited for in `app.py`.

**Live check:** "sleep" typed into ADVI's window closed the process within 5–6 s, twice.

## 3. Wake word (`src/advi/wake/`)

### Design
The design is the two-stage cascade used by on-device assistants: a cheap, always-on first stage and an accurate second stage that runs only on candidates.
- **Stage 1:** Silero VAD (0.15 ms per 32 ms frame) finds utterances with 0.3–4 s of speech. Longer talk is cut at 6 s and never transcribed.
- **Stage 2:** local Whisper `tiny.en`, biased with `hotwords="ADVI"`, transcribes on this PC (about 0.4 s), then `phrase.is_wake_phrase` decides.
  - The name counts in "wake up ADVI", "hey ADVI", "ADVI" and similar.
  - Whisper's spellings are matched: ADVI, Advi, Adhvi, Atwi, adv, ad-v, Edv, "Add V", "at V".
  - "Advice" is excluded.
  - Only short utterances count: at most 3 words, or 7 with "wake". A name split in two ("at V") also needs "wake" or a greeting.
- **Optional engine:** Picovoice Porcupine, a dedicated keyword spotter. It is used when `PICOVOICE_ACCESS_KEY` and an "ADVI" `.ppn` file from the Picovoice Console are configured, and falls back to the default engine if it can't start.
  - **Not verified live:** no AccessKey was available. Covered by a unit test with a fake engine only.

### Process model
- `python -m advi.wake` is a supervisor that holds a named mutex (`Local\ADVI_Wake_Listener`; one copy only).
- It runs a worker process and restarts it after a crash, with a pause of 5 s that doubles up to 5 minutes.
- The worker ends when its supervisor is gone.
- **Start-up:**
  - At every Windows sign-in, through `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` ("ADVI Wake", `pythonw.exe -m advi.wake`; no administrator rights).
  - Also by ADVI at its own start, so it is listening as soon as "sleep" closes ADVI.
  - Programs in the Run key start after sign-in. A PC with a password must be signed in first; nothing can listen at the lock screen.
- **While ADVI runs** (it holds `Local\ADVI_App_Running`, which Windows releases even after a crash):
  - The listener lets go of the microphone and the hotkey, and checks once a second.
  - After it starts ADVI, it waits up to 120 s for ADVI to come up instead of starting a second copy.
- **Recovery:**
  - With no microphone, the listener retries every 30 s.
  - A microphone that stops delivering audio for three windows in a row is reopened.
- **Hotkey:** while ADVI is closed, the listener also holds the ADVI hotkey (Control Alt Z on this PC), which starts ADVI.
- **Privacy:** heard text is never logged unless it was the wake phrase. faster-whisper's per-utterance log line is silenced.
- **"Wake up ADVI" while ADVI runs** is answered locally ("I'm here. What can I do?"); it is not sent to the AI.
- **Commands:** `python -m advi.wake install | uninstall | status`. Setup runs `install` unless `ADVI_WAKE=off`.

### Measurements (2026-10-25)
Synthetic clips: Windows' two voices (Hazel, Zira), 6 wake phrases and 17 other phrases each.

**Stage-2 model choice** (transcription plus the first matcher):

| Model | Bias | Wake phrases found | Other phrases accepted | Median time |
|---|---|---|---|---|
| tiny.en | none | 8/12 | 1/34 | 0.34 s |
| tiny.en | "ADVI" | **12/12** | 1/34 | 0.40 s |
| base.en | none | 10/12 | 0/34 | 0.91 s |
| base.en | "ADVI" | 10/12 | 1/34 | 0.91 s |
| distil-small.en | either | 3/12 (first matcher) | 2/20 | 2.6 s |

**Full chain** (real Recorder + Silero + tiny.en + matcher, the clips streamed as 32 ms frames with noise): **12/12** wake phrases, **0/34** other phrases. This came after the split-name rule removed the one false start ("Look at V").
- **This is a development set.** The rule was tuned on these same clips. It is not a measure of accuracy on the user's voice or on new speech.

**Resources:**
- **Idle cost:** the worker uses 2.6% of one core over 30 s, with 202 MB resident (Whisper tiny.en plus ONNX Runtime).
- **Per check:** about 0.4 s of CPU for each short utterance.

**Live checks on this PC:**

| Check | Result |
|---|---|
| Start through the listener's hotkey | ADVI's process held its mutex 2 s after the key. First launch took 54 s to "listening" (cold first load); the second took 12.2 s |
| Supervisor | Worker killed at 20:41:31; listening again at 20:41:37 |
| After "sleep" | The listener reopened the microphone and the hotkey at once |
| Install | The Run value was written and the listener started |

**Not verified live:** the spoken wake phrase itself. Played through the laptop's own speakers, "Wake up, Advee" never reached the listener, because the laptop microphone's built-in echo suppression removes its own speaker output (the same effect as in Section 1). It must be tried with the user's voice.

## 4. Startup time
The semantic-memory model (PyTorch, about 14.5 s) now loads in the background after ADVI is ready; memory search is lexical until then. Warm start to "listening" went from 23.7 s to 11.8 s (profiled with `profile_startup.py`).

## Tests
- `tests/current/test_phase15_echo_and_wake.py`: 50 tests. Wake and non-wake phrases; listener states (start once, startup grace, letting go of the microphone, stalled microphone, keyword engine, failed launch); Porcupine framing; supervisor restarts and single instance; autostart; duplex mixing, stopping, latency tail, cancelled capture, fallback without a canceller; sentence-by-sentence speech and stopping; spoken paths; onset threshold; barge-in; "wake up ADVI" while running.
- Phase 12 tests updated: sleep closes ADVI; the hotkey never sleeps.
- `conftest.py` sets `ADVI_WAKE=off`, so no test starts a listener.
- Full suite: 942 passed.

## Remaining
- Try the spoken wake phrase, interrupting by voice, and "sleep" with the user's own voice.
- Porcupine needs the user's AccessKey to be verified.
- Music played by other apps is not in the echo reference: the "ADVI, ..." gate during music remains.

## 5. Fixes from the first live session with all of the above (2026-10-25, 22:04–22:13)
The user reported that "it is working just fine" with two problems, both confirmed in the log.

**ADVI stopped mid-sentence without being interrupted.**
- **What happened:**
  - At 22:12 the user had just had ADVI turn the volume up.
  - Some of ADVI's own voice leaked through the canceller, and the speech onset stopped ADVI's answer.
  - The transcript ("page so did page opened") was then correctly recognised as ADVI's own words, but the rest of the answer was already gone.
- **The fix:**
  - The onset now only **pauses** ADVI's speech (`DuplexAudio.pause`, which holds the playback position).
  - After the transcript, ADVI decides. It resumes from the same place for echo, noise, nothing understood, or lyrics during music, and logs "Resuming ADVI's speech". It stops only for real speech.
  - `Sound.wait` now allows a long pause.

**"I completed the attempted action, but I couldn't verify the final state" for read-only questions.**
- **"Tell me the status of laptop":**
  - The first `get_system_info` call was rejected before it ran (unknown parameter `detail`); the second succeeded.
  - `_only_verified_observations()` required *every* step to succeed, so the answer was replaced.
- **"Is internet connected?":**
  - A `research_web` with no results was followed by `open_browser example.com`.
- **The fix:** for a task made only of read-only steps (observations, browsing, read-only tools, now including `research_web`), only the last step must have succeeded, because failed looks changed nothing. Both cases are regression tests that fail under the old rule.

**A real internet check.**
- There was no tool, so the AI opened example.com.
- New read-only `get_network_status` tool. It connects to 1.1.1.1:443 or 8.8.8.8:53 with a 1.5 s timeout, checks DNS, and reads the Wi-Fi name with `netsh`.
- **Answers:**
  - "Yes, the internet is connected through Wi-Fi "…"";
  - "The network reaches the internet, but web addresses aren't resolving…";
  - "No internet right now…".
- Routed locally for "is internet connected / am I online / is the wifi working", and offline as a built-in answer.
- "Status of (my) laptop" now matches the system-status fast path.
- **Live run on the dev laptop:** connected, with the Wi-Fi name, in 0.29 s.

**Tests:** `tests/current/test_phase15_live_fixes.py` (21). Full suite: 963 passed.
