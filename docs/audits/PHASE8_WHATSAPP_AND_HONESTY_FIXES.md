# Phase 8: WhatsApp and honesty fixes (log of 2026-10-21)

| Log symptom | Cause | Fix |
|---|---|---|
| "couldn't find a WhatsApp chat named 'anurodh'" | Chats with unread messages are labelled "2 unread messages Anurodh …", which does not start with "anurodh" | `chat_title_from_label` strips "N unread messages", "pinned", "muted" and a leading "~" (verified live) |
| "I clicked 'David FCRIT College' but couldn't confirm that chat opened" | The contact isn't saved on the phone: the result shows the profile name, but the message box says "Type a message to +91 …" | Accepted when the message box switches to a phone-number chat right after clicking that result |
| Sent to "David Almeida" instead of "David" | The AI replaced the spoken chat name with an email contact | `_restore_spoken_chat` keeps the name the user said, both in the confirmation and at send time; the prompt forbids contact_lookup for chats |
| "I've sent the WhatsApp message" with no tool call | AI hallucination | `_no_unbacked_send_claim`: any "sent" claim without a successful send in that turn is replaced with the truth |
| "send message to David saying Hello from advi , on whatsapp." mis-parsed | A trailing ", on whatsapp" was not handled | Parser handles a trailing app name, "an message" and "whatsapp, saying" |
| Email confirmation "to the saved recipient" | The description ignored the draft state | Confirmation names the recipient, subject and body |
| "play baby shark on youtube" → "No ADVI-owned Chrome tab" | `play_media` required an open tab before launching Chrome | `_owned_or_started_tab` starts ADVI's Chrome (verified cold start: 14 s, playing) |
| "I couldn't produce a usable response." | Empty AI reply | The message now says to repeat the request in one sentence, with an example |
| Fresh-install test run: "Windows fatal exception: access violation" | 64-bit clipboard handles truncated by ctypes defaults; clipboard reads and writes had silently failed, so typing never pasted | Typed signatures on private WinDLL handles; Unicode paste verified live; tests never touch the real clipboard |

Tests: `tests/current/test_phase8_whatsapp_fixes.py`. Full suite: 656 passed (also in a fresh install copy).
