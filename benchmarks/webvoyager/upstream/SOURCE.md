# Upstream task source

- File: `webvoyager_template.json`, 535 tasks (35 per site; 45 for "Search Engine"), all `status: active`.
- From: https://github.com/EmergenceAI/EmergenceWebVoyager/blob/main/tasks/webvoyager_template.json (MIT licence)
- Commit: `e988bb135551a10bab2d33d969f2fb0f5c569fe7` (2025-07-21). Downloaded 2026-10-23.
- Instantiated by `benchmarks/webvoyager/tasks.py`, never by the upstream `instantiate_tasks.py`, which `eval()`s strings from this file.
- The safe instantiator covers all nine `@eval:` shapes in this version. On 2026-10-23 all 535 instantiated with 0 exclusions.
