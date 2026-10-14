# ADVI 0.3.4 — Phase 6 Completion Audit

## Scope

Phase 6 hardens the LLM/provider tool-contract boundary. The primary LLM brain and dual-provider architecture remain unchanged. The work makes model-visible tool surfaces coherent with the execution vocabulary and prevents deterministic tool/request schema defects from triggering provider failover storms.

## Implemented

- Added small, canonical one-step tool dependency clusters to `ToolRegistry`.
- Browser model surfaces now keep the basic interaction cluster coherent, including `type_web_element` and `wait`, instead of advertising an interaction pattern while omitting a required tool declaration.
- Browser continuation surfaces after `navigate`, `search`, and `click_web_element` now retain the dependent typing/readiness tools.
- Added a final tool-surface expansion pass before provider requests so selected tools acquire only safe, immediately relevant dependencies.
- Provider request/schema errors (`LLMRequestError`) are treated as deterministic contract failures, not provider-health failures.
- Provider session startup no longer cools down a healthy provider for a deterministic request error.
- Missing-tool recovery is limited to one repair attempt per interactive user turn.
- Missing-tool recovery stays on the current provider instead of rotating through other providers.
- Added Phase 6 regression coverage for tool-surface coherence, bounded dependency expansion, no-failover request errors, same-provider missing-tool repair, and alias normalization.
- Added tool-count/name telemetry to provider-session traces for easier diagnosis of future surface mismatches.

## Why this fixes the logged failure class

The prior runtime could send a browser request with `inspect_web_page`/`click_web_element` but omit `type_web_element`, even though the system instructions allowed the model to use exact-selector typing after inspection. A provider could then reject the generated call as "not in request.tools". Phase 6 removes that mismatch for the common browser continuation path.

The provider manager also no longer treats deterministic request/schema errors as a reason to fail over. A single local contract repair is attempted instead.

## Verification

- Focused Phase 6 tests: **5 passed, 0 failed**.
- Full `tests/current` suite: **446 collected; 444 passed; 2 expected skips; 0 failures**.
- `python -m compileall -q src`: **PASS**.
- Phase 5 -> Phase 6 source delta: only intended provider/session, agent/session, tool-registry, and Phase 6 test files changed.
- Persistent runtime memory database restored to the Phase 5 baseline after testing.
- Generated Python/test caches removed before packaging.

## Expected skips

1. Provider smoke requiring `GROQ_API_KEY`.
2. Gemini-native provider test requiring the optional `google.genai` dependency.

## Acceptance limitation

This validation environment is Linux. ADVI's production GUI target is Windows, so the final Windows acceptance run still needs to exercise real browser/application behavior.

## Phase verdict

**PHASE 6 COMPLETE — READY FOR PHASE 7**
