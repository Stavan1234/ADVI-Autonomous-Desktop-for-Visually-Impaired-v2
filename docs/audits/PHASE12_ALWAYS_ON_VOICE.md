# Phase 12: voice that is always on, plus the fixes from the first live voice session

Date: 2026-10-24. Source: the user's first session using voice, with the log, and 7 reported problems.

| # | Reported | Cause | Fix |
|---|---|---|---|
| 1 | Ctrl+Alt+Z had to be pressed for every sentence | Push-to-talk design (Phase 11) | `voice/conversation.py`: ADVI listens continuously while awake, starting at launch |
| 2 | Must ignore its own voice and its music | No echo handling | Half duplex: no listening while ADVI speaks, plus a 0.3 s tail. While other audio plays, only sentences containing "ADVI" count (`voice/system_audio.py` reads Windows' output peak meter and skips ADVI's own sound) |
| 3 | "Siyu" heard as "CU" | Whisper wrote letters; nothing knew the user's chats | `agent/names.py` (sound key, spelled letters) and the `chat_names` table. Known chats and learned aliases are matched before the confirmation; "chat not found" asks to spell just the name |
| 4 | "It is S I Y U, not C U" got "I need an explicit yes or no" | The confirmation accepted only yes, no or a new task | Corrections change the pending message and ask again. Any other reply goes to the AI with the pending action spelled out (all confirmed tools). Offline, ADVI says how to answer |
| 5 | Wants a gentle "listening" sound | Harsh `winsound.Beep` | Synthesised soft chimes (`voice/earcons.py`): wake, heard (a soft pop at speech start), got it, thinking pulse, not heard, cancelled, sleep. Played through sounddevice so they never cut off ADVI's speech |
| 6 | "Sleep" went to the AI | Not handled | Spoken or typed sleep words stop listening locally; the hotkey wakes ADVI (typing "wake up" also works). Typed exit and quit still close ADVI |
| 7 | "Simple commands still work" was untrue | No rule for an app-less message, stutters, or time and date; the fallback was blocked by an older task's progress | Openers, the name and stutters are stripped. Offline, an app-less message becomes a WhatsApp send that still needs a yes. Time and date are answered locally. The fallback looks only at the current turn. The failure message says the command needs the AI and gives phrasings that work. "Why couldn't you …" is answered locally |

Also from the log:
- **The MP4 that opened was reported as failed.** Media Player's window doesn't carry the file name, so a media player in front now counts as proof.
- **Console lines were interleaved.** A shared print lock fixes this.
- **The recorder could wait forever** if the mic stopped delivering frames after speech. A missing frame now counts as silence.
- **A voice "stop" while ADVI works** stops the task at its next step (`AgentSession.request_stop`).

## Echo cancellation test (echoff), no-go
- echoff 0.3.0 was installed with the user's OK and tested by playing speech through the speakers with nobody talking.
- **No frames at all while the PC was silent** (0 in 8 s), because Windows loopback is silent then. Always-on listening would go deaf whenever nothing plays.
- **Measured suppression was 0.6 dB** (its quality check never became ready).
- **The laptop mic already suppresses speaker audio:** 45 s of loud speech from the speakers gave only 1 false utterance from the raw mic.
- So echoff and its packages (livekit, PyAudioWPatch) were uninstalled, and the fallback design is used.

## Rules
- **States:** awake (start) and asleep. The hotkey interrupts speech when ADVI is talking; otherwise it toggles awake and asleep.
- **While busy,** only "stop" or "cancel" acts; anything else gets "I'm still on the last one. Say stop to cancel it."
- **Offline only:** an app-less "send message to X saying Y" is treated as WhatsApp. With the AI up, it decides the app (Phase 6 rule).
- **Name matching:** a name the user corrected, or that ADVI matched to a known chat, is not "restored" to the spoken form by the Phase 10 guard.

## Verified
- Full suite: 833 passed.
- New tests:
  - `test_phase12_names_and_corrections.py`: the CU → Siyu correction, then a new yes, then the learned alias; spelling after "chat not found"; Telegram text change; AI routing; offline replies and commands;
  - `test_phase12_voice_always_on.py`: name stripping, sleep words, hotkey toggle and barge-in, the music gate, busy and stop, the listen loop, half duplex, stalled mic, earcon shape, the audio monitor.
- **Real start-up:** about 40 s of always-on listening in a quiet room, with no false command. ADVI's own long announcement wasn't heard. Fallback to Ctrl+Alt+Z.
- **Not yet verified:** a live session with the user (talking without a key, music and lyrics, CU → Siyu correction, sleep and wake, Wi-Fi off).
