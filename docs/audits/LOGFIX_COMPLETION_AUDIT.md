# ADVI 0.3.4 — 2026-09-30 Log-Fix Audit

This build contains only fixes driven by the 2026-09-30 runtime log.

## Fixed from observed failures

- PDF creation: stopped the terminal-evidence crash when `visual_sanity` arrived serialized as a string.
- PPT creation: stopped model-generated/stale document arguments from overwriting user-owned task parameters such as filename and slide count.
- Folder creation: verified `create_folder` is terminal evidence.
- Folder opening: preserved the verified created folder path and resolved "this/that/the folder" references to that exact path; also normalized an accidental `open_file` call to `open_folder` when task state proves the target is a folder.
- PDF opening/reading: a unique `find_file` result now promotes its absolute path for the dependent open/read action, avoiding basename-wide ambiguity; read content is preserved for a follow-up question about that file.
- Simple PDF listing: optional `null` list arguments are normalized away before validation; successful `list_files` is terminal evidence.
- Memory remember/update: successful memory writes are terminal evidence rather than false `uncertain` results.
- Conversation recency: "what was the last thing I asked you to do?" is answered from session history locally instead of hallucinating a missing record.
- Natural imperative task boundary: "you have to search/find/..." is recognized as a new action request so an old uncertain task cannot absorb the new task.
- Media playback verification: explicit playback state (`playing=true` / playback verification metadata) is accepted by the generic browser click verifier.

## Intentionally not changed

Groq token-per-minute/daily quota errors and the Gemini interactive timeout were treated as provider/environment conditions observed in the log, not as a reason to add more orchestration. The existing bounded provider recovery remains in place.

MCP integration exists in the project, but local Windows filesystem operations continue to use ADVI's native file-management capability; an external MCP server is not required for local folder/file creation and opening.

## Validation

- Full test suite: 500 collected tests, 2 expected skips; final run passed.
- Source fingerprint: 4a28493074a80227
