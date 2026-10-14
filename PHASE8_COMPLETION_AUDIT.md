# ADVI 0.3.4 — Phase 8 Completion Audit

## Scope
Final end-to-end orchestration validation, runtime diagnostics, and release gate coverage.

## Implemented
- Added bounded per-turn runtime metrics for LLM/provider attempts, tool executions, model-visible observations, local continuations, provider switches, and provider failures.
- Added browser acceptance coverage proving the primary LLM remains responsible for semantic decisions while verified local continuation handles explicit mechanical steps.
- Added browser terminal-evidence handling so a verified pure search request can finish without inventing an extra action, while compound search requests remain non-terminal.
- Added desktop acceptance coverage proving an unverified typing action stops stale dependent side effects and escalates to targeted perception.
- Added acceptance coverage for explicit filename authority and relevant experience-memory retrieval.
- Added acceptance coverage proving provider schema repair stays on the same provider instead of causing unnecessary rotation.
- Added `scripts/analyze_runtime_log.py` to quantify critical runtime failure classes from Windows logs.
- Added `docs/PHASE8_WINDOWS_ACCEPTANCE.md` with the real on-device acceptance journeys for YouTube Music, WhatsApp, Gmail, files, unexpected UI changes, provider resilience, and experience memory.

## Critical regression classes now covered
- Model calls a tool that was not exposed in `request.tools`.
- Browser search/navigation must not make a pure search task non-terminal forever.
- Unverified typing must prevent dependent side effects from continuing.
- Desktop and browser state must remain domain-isolated.
- Explicit file names must beat recent-artifact references.
- Provider schema errors must not trigger provider failover storms.
- David remains a separate route and is covered by the prior Phase 7 suite.
- Experience lessons remain relevant by domain/task rather than becoming general transcript memory.

## Verification
- `python -m compileall -q src scripts tests/current/test_phase8_end_to_end.py` — PASS
- Phase 8 focused tests — 9 passed
- Full `tests/current` — 487 collected, 485 passed, 2 expected environment-dependent skips, 0 failures
- Release source fingerprint: `4749f009b7200b09`
- Phase 7 → Phase 8 production-source delta limited to:
  - `src/advi/agent/provider_session.py`
  - `src/advi/agent/session.py`
  - `src/advi/core/execution_strategy.py`
  - `src/advi/core/pipeline_trace.py`
- Additional release-validation files:
  - `tests/current/test_phase8_end_to_end.py`
  - `scripts/analyze_runtime_log.py`
  - `docs/PHASE8_WINDOWS_ACCEPTANCE.md`
  - `PHASE8_COMPLETION_AUDIT.md`
  - `RELEASE_NOTES.md`
- Baseline runtime SQLite memory database restored from the Phase 7 release before packaging.
- Generated `__pycache__`, `.pyc`, `.pytest_cache`, logs, and runtime artifacts are excluded from the release package.

## Historical Windows-log diagnostic baseline
The supplied 2026-09-30 log contains 35 user turns and 90 provider requests. The diagnostic analyzer flags 86 events across the tracked failure classes:

- missing tool contract: 6
- provider request errors: 7
- provider failovers: 26
- unsafe tool-call limit: 1
- unverified typing: 19
- unknown parameter: 3
- cross-domain observation: 5
- David trace error: 7
- wrong artifact reuse: 6
- uncertain file opens: 6

These are diagnostic pattern counts, not a count of 86 independent bugs. They describe the historical failure surface used to design the Phase 8 acceptance coverage. The historical log was produced from a different source fingerprint and must not be treated as evidence of the new build's runtime behavior.

## Known limitation
The automated environment is Linux. It cannot prove live Windows GUI behavior, Chrome/CDP ownership, WhatsApp interaction, Office UI, or real provider credentials. The supplied Windows acceptance checklist is therefore still required before calling this build a fully field-validated release.

## Final status
- Planned implementation phases remaining: **0**.
- Code/test architecture gate: **PASS**.
- Live Windows field-acceptance gate: **PENDING on the target Windows machine**.
