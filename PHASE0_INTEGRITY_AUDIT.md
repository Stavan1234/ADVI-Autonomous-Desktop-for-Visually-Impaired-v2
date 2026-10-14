# ADVI 0.3.4: Phase 0 Integrity Audit (2026-10-14)

First implementation pass of `docs/ADVI_Agent_Architecture_Overhaul.md` (Phase 0) and
`docs/ADVI_Architecture_Review_and_Roadmap.md` (Phase 1, task/tool/path integrity).
Each fix maps to a failure in `logs/ADVI_Runtime_Log_Forensic_Analysis.md`.

## Fixed

- **Routing can no longer deny a capability (log prompts 11, 41–47, 66, 69, 76; 13 `not in request.tools` rejections).**
  New `request_tools` meta-tool (`src/advi/tools/catalog.py`). It is declared on every actionable turn, and its
  description lists every executable tool that the intent router did not include. When the model calls it, those
  schemas stay declared for the rest of the task (`task.context["loaded_tools"]`). It never reaches the
  ToolRunner. Provider "missing tool" recoveries are also kept for the rest of the task. The router itself is
  unchanged, so provider requests stay small: the meta-tool costs about 250–330 tokens, while the full
  65-tool surface costs about 5,800.
- **Path drift (prompt 15, `"a pdf called speech.pdf"` written to the Desktop).** The file-target extractor now keeps
  only the name after "called/named/titled". A protected bare filename no longer overwrites a model path that
  adds a folder to that same filename. A different filename, or a protected full path, still wins.
- **Every verified PDF/PPTX/DOCX ended "couldn't verify" (prompts 24, 37, 63).** `ToolVerifier` emits
  `verification_mode="native_structure_and_content"`, but `terminal_evidence_matches` did not accept that
  value. Each side had its own test using a different string, so the mismatch was never tested together.
- **Verified folder creation and memory writes ended "uncertain" (prompts 2, 13, 26).** `create_folder`,
  `delete_folder`, `memory_remember` and `memory_forget` are now terminal tools.
- **WhatsApp tasks received the Gmail tool cluster (prompt 62).** "send a message" no longer counts as email when a
  chat app is named.
- **GUI loops ran to the hard call limit (prompts 28–29, 62, 80).** New `src/advi/core/progress.py` compares
  each screen observation (`read_screen`, `inspect_window`, `inspect_web_page`, `read_web_page`) with the previous
  one of the same kind. If actions happened in between and the screen did not change, the model receives a
  `progress_check` telling it to change approach. After 3 stalls the task stops and says why. A replay of the
  prompt-62 pattern now stops after 7 calls instead of 25.
- **"laptop status" / "storage" refused (prompts 41–47).** The local system fast path now accepts short noun
  phrases and storage/disk questions, with no provider call.
- **Stop and frustration turns depended on the LLM (prompts 17–22, 45, 77).** A bare "stop" / "shut up" / "be quiet"
  is answered locally. "Cancel" now also ends FAILED tasks. Provider failures now name their cause (key rejected,
  quota used up, rate-limited, timed out), and "what's the problem?" answers locally from the recorded failure.
- **Pre-existing crash.** A fully local app launch (`"open the notepad application"`) raised `NameError` for an
  undefined `app` in `_respond_turn`.

## Not changed

- The 900-line keyword router in `AgentSession._select_intent_tools` is still the first-pass selector. With
  `request_tools` in place it can be simplified later without risking capability denials.
- No perception changes (UIA observation, Set-of-Mark), no app skills (WhatsApp, media), no indexing. Those
  belong to the later phases.
- The live `test_groq_smoke.py` fails with `LLMAuthenticationError`: one configured Groq key is rejected. The
  log shows 23 such auth failures. Replace the key in `.env`.

## Validation

- New tests: `tests/current/test_phase0_integrity.py` (19 tests, each tied to a log prompt).
- Full suite: 524 passed (live Groq smoke test deselected).
- Replayed the log's failing prompts against the real capability registry to confirm the surfaces above.
