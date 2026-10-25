# Phase 9: vision as a last-resort sense (`look_at_screen`)

Source: `final_output.zip`, a module that screenshotted the screen after web tasks and had Gemini describe it. We kept its prompt idea and formatter and rebuilt the rest to fit ADVI.

## How other agents use vision
| System | Primary sense | When it uses pixels |
|---|---|---|
| Microsoft UFO2 (Windows) | UI Automation | OmniParser vision only for controls UIA can't see (custom-drawn, canvas, games) |
| Browser agents (browser-use and others) | DOM / accessibility tree | Screenshots when the task needs visual information; the hybrid is the common production pattern |
| ADVI | UIA `observe_window`, Playwright refs, OCR | `look_at_screen`, on the triggers below |

ADVI's brain (Groq gpt-oss-120b) is text-only, so vision is a **tool that turns pixels into words**. The brain asks a question, Gemini answers from the screenshot, and the brain decides. General vision models are good at "what state is this screen in" and unreliable at pixel coordinates. So the vision answer **never carries a place to click**; actions still go through element IDs, refs or OCR targets.

## When vision runs
| Trigger | Signal | Example |
|---|---|---|
| The user asks | Words like "screen", "what do you see", "describe", "is there an error" offer the tool | "what's on my screen?" |
| Structure is blind | The structured observer failed, returned nothing, or found ≤ 4 elements (window chrome only) | Telegram (Qt), video, canvas |
| Stalled | `progress.record_observation` reports the screen didn't change after an action | "Proceed to checkout" does nothing |
| Uncertain final state | A high or critical side-effect action ended unverified | The last page of a web form or order |
| Timing | The look waits for two matching frames (up to 1.5 s) and reports "still changing" when the screen never settles | A slow page still loading |

**Limits**
- At most **2 automatic looks per task**.
- No looks in simple mode, and none after a verified result.
- An unchanged screen asked the same question is answered from cache, with no model call.
- Automatic looks crop to the foreground window (the one ADVI is working in). A full-screen capture happens only when the user asks.
- Automatic questions name the step ("click 'Proceed to checkout'") but never include typed text.

## Evidence rule
A vision answer is visual evidence. It is labelled "not proof the task succeeded".
- It is not in `_SEND_TOOLS`, so it can never back a "sent" claim.
- It cannot verify the action it observed.
- ADVI may say "the screen shows 'Order placed'". It may not say "I placed the order".

## What changed from the zip
| Zip | Now |
|---|---|
| Describes the screen after most web tasks | On demand, plus the bounded triggers above |
| Fixed 1.5 s sleep, plus a window-station hack | Stable-frame wait; no hack |
| Fallback models that don't exist, 3 retries with backoff | `ADVI_GEMINI_MODEL` (or `ADVI_VISION_MODEL`), one attempt per Gemini key, 20 s timeout, low thinking |
| A new timestamped PNG each call | In memory; nothing written |
| Full PNG upload | JPEG, at most 1600 px |
| pyautogui, PIL and mss | pyautogui (already used) |

When vision is unavailable (no key, all keys failed, or `ADVI_VISION=off`):
- an explicit request returns OCR text and says so;
- an automatic look is skipped.

## Files
- `src/advi/capabilities/desktop/screen_describer.py` (new): the describer, the stable-frame wait, the formatter.
- `src/advi/capabilities/desktop/vision.py`, `perception.py`: `grab`, `crop_to_window`, `window_for`, `window_image`.
- `src/advi/capabilities/desktop/executor.py`: `_execute_look_at_screen`.
- `src/advi/agent/session.py`:
  - `_vision_trigger`, `_observation_is_blind`, `_look_at_screen_for`;
  - hooks in `_capture_native_recovery_observation` and `_adaptive_checkpoint_after_tool`;
  - routing and a prompt line.
- Registry, action contract, `is_observation_tool`, `progress.SCREEN_OBSERVATION_TOOLS`.
- Tests: `tests/current/test_phase9_vision.py`. `conftest.py` sets `ADVI_VISION=off`, so tests never screenshot or call Gemini.

## Live check (2026-10-23)
- Full screen: 11.4 s. It named the app, the chat title, the side panel and the input box correctly.
- Foreground window with a question: 8.2 s. It answered "Claude … no dialogs, pop-ups, or error messages are visible."
- Measured with the default thinking level. In a direct comparison, low thinking was 6.0 s against 9.1 s for the default, so the tool uses low.
- Later the same day Gemini returned 503/504 for every request.
  - With three keys, one look took 60 s before giving up.
  - Fix: rotate keys only on quota or key errors, and stop at the first busy or timeout error. A failed look now takes about 20 s.
  - The spoken reason is plain ("the vision service is busy or too slow right now"), not a JSON error.
- Latency is the cost of vision, which is one more reason it is a last resort.
