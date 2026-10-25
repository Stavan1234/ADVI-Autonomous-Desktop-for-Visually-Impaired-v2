# Phase 11: voice input (push-to-talk, local Whisper)

Date: 2026-10-24. Source: the user's `voice_input.zip` prototype (`VoiceInputListener`), adapted to ADVI. The wake word is phase 2.

## Flow
Hotkey → rising tone → record (VAD endpointing) → falling tone → local Whisper → the same queue as typed input → `agent.respond`.

Code is in `src/advi/voice/`:
- `recorder.py`: microphone, pre-roll, Silero VAD, endpointing;
- `transcriber.py`: faster-whisper, hallucination guard;
- `hotkey.py`: Win32 `RegisterHotKey`;
- `earcons.py`: tones;
- `listener.py`: one voice turn, follow-up mode, start-up.

Supporting changes:
- `io/console.py`: `InputHub`;
- `io/output.py`: `silence`, `wait_quiet`, `say`;
- `io/tts.py`: `stop`, `is_speaking`;
- `app.py`: wiring.

## Taken from the zip, and what changed
| Prototype | ADVI | Reason |
|---|---|---|
| sounddevice stream, queue, RMS gate | Kept; RMS is now the fallback | Simple and correct |
| Fixed 6 s recording | Silero VAD endpointing: 0.8 s of silence, or 1.5 s after 4 s of speech; 6 s with no speech; 30 s maximum; 0.3 s pre-roll | A short command doesn't wait 6 s, and a long one isn't cut off |
| Spoken "I'm listening" | Tones | Faster, and not recorded as input |
| Read-back plus yes/no on every command | Removed; follow-up mode instead | ADVI's confirmation gate already covers consequential actions, and its question reopens the microphone |
| `pyttsx3` | ADVI's Piper/SAPI voice | One voice; no talking over itself |
| Whisper `tiny` | `distil-small.en` (measured below) | |
| `hey_jarvis` wake word | Phase 2 | It must be measured for false wake-ups before it is turned on |

## Model measurements
Dev laptop: i5-8265U, 4 cores, no GPU; faster-whisper int8; one command each.

| Model | Time | "Stavan" heard as |
|---|---|---|
| large-v3-turbo | 12–15 s | Stavon |
| small.en | ~3 s | Stav and |
| distil-small.en | **~2 s** in the full pipeline | Stavan ✓ |
| base.en | ~1 s | Stavans |

- Whisper always encodes a 30 s window, so short commands cost as much as long ones, and `chunk_length` didn't change that. Turbo would need a GPU.
- `ADVI_STT_MODEL` switches the model.
- A comma-separated vocabulary prompt made distil-small.en loop ("Open, Note, Open, …"), so there is no prompt.

## Dual recognition: online Turbo, local fallback (added the same day)
`DualTranscriber` in `transcriber.py` decides which recogniser handles each command.

**Online:** Groq `whisper-large-v3-turbo`.
- Free, with an audio quota separate from the brain's; it worked while the gpt-oss quota was used up.
- It tries each key in turn, and a key that hit a 429 rests.
- There is no point trying a second key when the network is down.

**Local:** distil-small.en is **always preloaded**, so falling back never waits for a model to load.
- After a cloud failure, ADVI stays local for a cooldown: 20 s for no network, 30 s for a timeout, 5 min for quota, 1 h for a bad key. This way a dead network costs a delay only once.

**Telling the user:**
- On switching to local, ADVI says once: "I'm understanding your voice offline right now, because <reason>, so I may be a little slower. Thanks for your patience." It doesn't repeat this until it has been back online.
- "Why are you slow?" is answered by `_handle_slowness_question` in session.py with the voice reason, how long recognition takes now, and any recent AI-service failure. It needs no AI call, so it works offline. Questions about the user's PC ("why is my laptop slow") are not caught.
- While voice is local, the AI also gets a `[VOICE]` line in its context.

**Settings:** `ADVI_STT_CLOUD=off` keeps audio on the PC; `ADVI_STT_CLOUD_MODEL` changes the online model.

**Measured end to end (2026-10-24):**
- Online: 0.45–0.76 s per command.
- Local: 2.5–2.8 s.
- The first failed connection on a closed port added about 2.5 s once.

Gemini is not used for speech: it isn't Whisper, and its free flash quota (20 requests a day) is the brain's backup.

## Turn rules
- **Barge-in:** the hotkey silences ADVI and drops queued speech; SAPI is stopped by restarting its worker.
- Pressing again ends the recording; holding records until release; Escape cancels.
- **Nothing heard:** "Sorry, I didn't catch that", one more try, then stop.
- A command spoken while ADVI is busy gets "Still working on the last one."
- **Follow-up:** after a spoken request whose reply asks something (a pending confirmation, or text ending with "?"), the microphone reopens once ADVI has finished speaking.
- Only **typed** exit, quit or sleep closes ADVI, so a misheard word can't shut it down.

## Hotkey
- `RegisterHotKey` fails when another program owns the key.
- On the dev laptop Ctrl+Alt+Space was taken (Flow Launcher / PowerToys), so ADVI tries Ctrl+Alt+Space, then Ctrl+Alt+Z, then Ctrl+Alt+Enter, and says which one it got.
- Ctrl+Alt+V was skipped because it is Paste Special in Office.

## Verified
- 24 unit tests with fakes; full suite 775 passed.
- **Without a mic:**
  - SAPI-spoken commands with room hiss went through the real Silero VAD, endpointing and distil-small.en, and all three were transcribed exactly in about 2 s;
  - silence closed after 6 s with no text.
- **Real start-up:** the model loaded, the mic opened, it fell back to Ctrl+Alt+Z, and "Press Control Alt Z to speak" was announced.
- **Not yet verified:** a live test with a person speaking: accent, real room noise, barge-in, follow-up "no" on a WhatsApp confirmation, and Wi-Fi off.
