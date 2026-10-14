# ADVI 0.3.4 — Phase 5 Completion Audit

## Scope

Phase 5 adds a conservative experience-memory layer on top of the existing SQLite long-term memory. It records only explainable, reusable recovery patterns and exposes a tiny relevant lesson set to the primary ADVI brain on each model turn.

No second database was introduced. No raw conversation transcript is stored as an experience lesson. Experience cannot execute actions directly; it only informs later model decisions.

## Implemented

- Added `advi.memory.experience.ExperienceMemory` and `ExperienceLesson`.
- Experience lessons use the existing `memories` table with `kind='lesson'` and rich fields (`scope`, `subject`, `importance`, `status`, `source_session_id`).
- Added high-confidence startup warming (`MemoryService.warm_experience`).
- Added domain/query-aware relevant lesson retrieval (`MemoryService.experience_lessons`).
- Added automatic lesson recording after a previously failed/uncertain tool is subsequently recovered with verified execution.
- Added conservative reusable rules for:
  - desktop text-entry focus verification
  - browser target/tab loss and reacquisition
  - desktop application launch/window verification
  - browser uncertain-action re-observation
- Repeated evidence strengthens the existing lesson through higher confidence and an evidence count instead of creating duplicate lesson records.
- Kept ordinary user/project memory separate from lessons in `MemoryService.core()` and `MemoryService.recall()`.
- Fixed `LongTermMemory.recall()` and lexical `search()` so rich memory fields are returned correctly; this was required for lessons to remain typed as lessons after persistence/reload.
- Wired experience warming into startup preparation without adding another heavyweight model-load step.
- Kept the existing short-term `SessionBuffer` intact. Phase 5 does not remove conversational continuity.

## Behavior

The intended information flow is:

`execution evidence -> verified recovery -> experience lesson -> startup cache / relevant retrieval -> primary LLM context`

Experience memory is deliberately non-authoritative. Live task/domain/artifact state remains authoritative.

## Verification

- Phase 5 focused tests: **6 passed, 0 failed**.
- Full `tests/current` suite: **452 collected; 450 passed; 2 expected skips; 0 failures**.
- `python -m compileall -q src`: **PASS**.
- Restart persistence smoke test for experience lessons: **PASS**.
- Phase 4 -> Phase 5 source delta contains only the intended AgentSession, app, memory service, long-term memory, and new experience-memory/test files.
- Generated `__pycache__` and `.pytest_cache` directories removed before packaging.

## Expected skips

1. `tests/current/test_groq_smoke.py` — `GROQ_API_KEY` not configured in this environment.
2. `tests/current/test_provider_session_native.py` — `google.genai` unavailable in this environment.

## Build identity

Release: `0.3.4-final-reliability`

Source fingerprint: `ad98b2f7eb0d7ee3`

## Acceptance limitation

This validation environment is Linux. ADVI's production target is Windows, so Windows-native GUI behavior (PyAutoGUI/Win32/application windows) still requires the real Windows acceptance run on the user's machine.

## Phase verdict

**PHASE 5 COMPLETE — READY FOR PHASE 6**

The experience system is intentionally conservative. It currently evolves known high-value lessons through repeated verified evidence rather than allowing an LLM to invent arbitrary executable rules. Novel lesson synthesis can be considered later only after the execution/observation foundation is stable.
