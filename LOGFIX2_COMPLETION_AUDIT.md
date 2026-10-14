# ADVI 0.3.4 — Remaining Log-Fix Audit (2026-09-30)

This patch is intentionally limited to defects still visible in the supplied
2026-09-30 runtime log after the earlier log-fix pass.

## Fixed

- `list_files` verification now accepts the structured `{directory, files, folders}` payload actually returned by the native file capability. The previous verifier treated that valid payload as invalid and poisoned an otherwise successful PDF listing.
- Recent artifact aliases are merged instead of choosing either the current task or previous task. This preserves a known `last_folder` when a later task creates/reads a file and then the user says "open this folder".
- Known recent-folder references are executed locally with the exact saved path, avoiding an unnecessary provider/David round and avoiding Explorer Home/default-folder drift.
- A logged "wrong folder / opened Home" correction is now repaired locally by reopening the exact last-known folder path.
- A just-read PDF/file "what is it about?" follow-up no longer exposes an unrelated `list_files` tool surface when the needed read content is already present. The model answers from the bounded recent-read context.
- PDF visual sanity verification now checks every generated page rather than stopping after the first eight pages, and records the checked/document page counts.
- Simple desktop-application launch responses name the application actually requested instead of using the generic "requested application" wording, reducing stale/incorrect narration.

## Not changed

- Provider rate limits and Gemini timeouts remain provider/environment conditions from the log. No extra orchestration was added for them.
- MCP remains optional. Native Windows filesystem operations remain the fast/default path for local file/folder work.
- The earlier log-fix changes already present in the supplied project were retained.

## Validation

- Focused log-fix tests: 6 passed.
- Full regression suite: all tests passed; 2 expected environment-dependent skips.
- Native structured-document smoke check: 10-page PDF generated and verified; 5-slide PPTX generated and verified.
