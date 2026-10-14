# ADVI — Windows Validation

Run this only from the canonical ADVI repository after installing its `.venv`.

## Automated gate

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\smoke_windows.ps1
```

The smoke test validates Windows desktop access, a temporary filesystem round-trip, a dedicated Chrome/CDP profile, watcher events, Gmail credential readiness, and capability readiness. It performs no LLM calls and sends no email.

## Real execution checklist

After the smoke gate passes, manually validate the canonical source with a disposable test file and a harmless desktop workflow:

1. Launch Notepad.
2. Type a short unique string once.
3. Verify the text is present exactly once.
4. Open Chrome through ADVI's dedicated browser path.
5. Navigate to a harmless public URL.
6. Verify the exact owned tab.
7. Ask ADVI to read a page.
8. Test one filesystem create/read/delete round-trip.
9. Test Gmail read/draft/send only after OAuth is configured and the destination is controlled.
10. Force or simulate primary-provider failure and verify David fallback takes over without repeating an already-completed side effect.

Do not use real destructive filesystem targets, production email recipients, or the user's normal Chrome profile for initial validation.
