# ADVI 0.3.4 — Phase 8 Windows Acceptance

Run these checks on the actual Windows machine after installing the Phase 8 build. The purpose is to validate the runtime behavior that Linux tests cannot prove: real window identity, PyAutoGUI input, Chrome/CDP ownership, WhatsApp UI changes, Gmail, Office, and live provider behavior.

## Before testing

1. Start ADVI normally. Record the startup `release=` and `source_fingerprint=` lines.
2. Keep the generated runtime log. After the session, run:

```powershell
python .\scripts\analyze_runtime_log.py .\logs\advi.log --json
```

3. Do not use the historical log as a pass/fail baseline. It is a regression corpus for the old failures; the Phase 8 source fingerprint is different.

## Acceptance journeys

### A. YouTube Music

Prompt:

> Open YouTube Music, search for "Never Say Never" and play that song.

Pass criteria:
- ADVI opens or reuses its owned browser target.
- Search happens once unless the live page genuinely requires recovery.
- The correct song is selected.
- Playback is verified before ADVI says it is playing.
- No safe-tool-limit failure.
- No repeated provider rotation caused by a missing tool/schema error.

Repeat with:

> Play a praise song on YouTube.

### B. WhatsApp

Prompt:

> Open WhatsApp application.

Then:

> On WhatsApp, search manually for chat named "Anurodh", select the first matching chat, type "Hello from advi" in the message box, and send it.

Pass criteria:
- Desktop observation identifies WhatsApp, not stale Chrome/YouTube state.
- Search field is targeted before typing.
- Chat transition is verified.
- Message composer is verified editable/focused before typing.
- The exact message is observed before sending.
- The outgoing message is verified after sending.

Important failure to watch for: `AnurodhHello from advi` appearing in the search box. That means focus/target verification failed.

### C. Gmail

Prompt:

> Send an email to stavan saying this is a testing email.

When ADVI asks for the address, provide the address. Confirm once.

Pass criteria:
- Correct contact/address is preserved.
- One confirmation occurs for the irreversible send.
- A verified sent-message result is returned.

Then test explicit David routing:

> Send an email to stavan saying this is testing email but use David's fallback method to send this email.

Pass criteria:
- David remains the selected execution route.
- A David failure does not silently switch to native Gmail.

### D. File and artifact continuity

Prompt:

> Create a folder called stavan_test, and inside it create a PDF called "advi" including your information and capabilities.

Then:

> Open that folder.

Then:

> Open the PDF inside that folder.

Then list PDFs from Downloads and explicitly open a named PDF such as `Music Day Speech.pdf`.

Pass criteria:
- "that folder" resolves to the most recent authoritative folder artifact.
- "the PDF inside that folder" resolves to the artifact just created.
- An explicitly named PDF always overrides recent-artifact aliases.
- A verified `open_file` is not downgraded to `uncertain` by unrelated browser perception.

### E. Unexpected UI / recovery

While a GUI task is active, introduce a benign UI change such as a dialog or focus change. ADVI should re-observe the affected domain, retarget, and continue rather than blindly replaying the previous action.

### F. Provider resilience

Run a multi-step task while observing the log. A provider rate-limit/timeout should preserve task state and allow handoff. A tool-schema mismatch should trigger at most the bounded local repair and must not cause a provider-rotation storm.

### G. Experience memory

After a verified recovery from a known failure class, repeat a similar task. Confirm from the log that the relevant experience lesson is retrieved and the runtime does not blindly repeat the same failure path.

## Final release gate

The Phase 8 build is accepted only when:

```text
all current tests pass
no new Phase 8 regression failures
Windows journeys pass
YouTube playback is verified
WhatsApp message delivery is verified
Gmail send/confirmation is correct
David route integrity is correct
explicit file targets remain authoritative
log analyzer shows no recurrence of the critical failure classes
```
