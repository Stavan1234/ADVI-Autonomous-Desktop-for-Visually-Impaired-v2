# ADVI benchmarks

There are two benchmarks, with two purposes:
- measure ADVI honestly;
- find where it falls short and fix that with simple, general changes.

Every run records each AI call (provider, model, tokens) and each tool call. A run never sends, buys or submits anything: any confirmation ADVI asks for is answered "no".

## ADVI Desktop Suite (Windows)
`benchmarks/desktop/tasks.json` has 23 tasks.
- **9 are adapted from Windows Agent Arena.** They keep WAA's goal and the same kind of end-state check, but work in a sandbox folder instead of a VM's user folders. The WAA task id is kept in `waa`.
- **14 are ADVI's own:** files, Notepad, Word, the browser, a system question, and a WhatsApp send that must stop at the confirmation prompt.

Each task gets a fresh folder, `Documents\ADVI Bench\<task id>`. It is scored by checking the real end state (files, contents, the browser's URL, ADVI's answer). There is no AI judge.

```bash
.venv\Scripts\python.exe -m benchmarks.desktop.run --split dev --name dev-r1
```

Report it as "ADVI Desktop Suite v1 (n tasks, 9 adapted from WAA)", never as a Windows Agent Arena score. WAA itself needs a 30 GB Windows VM image (this laptop has 17 GB free), and its agent interface (`predict()` from screenshots) doesn't fit how ADVI works.

## Emergence WebVoyager sample (web)
This uses the maintained WebVoyager: 535 tasks, dates relative to today, and human judging (arXiv 2603.29020, github.com/EmergenceAI/EmergenceWebVoyager, MIT).
- The fixed sample is 2 dev + 3 test tasks per site (30 + 45), chosen by seed in `webvoyager/splits.json` before any run.
- Tasks are independent, so they run in daily batches. About 8 a day fit the free Groq quota.

```bash
# once: put upstream tasks/webvoyager_template.json in benchmarks/webvoyager/upstream/
.venv\Scripts\python.exe -m benchmarks.webvoyager.run --split dev --batch 8
.venv\Scripts\python.exe -m benchmarks.webvoyager.judge benchmarks\results\webvoyager\dev-YYYYMMDD
.venv\Scripts\python.exe -m benchmarks.webvoyager.report benchmarks\results\webvoyager\dev-YYYYMMDD
```

**Judging:**
- `judge` writes an AI pre-screen and `review.html`.
- Open the page, mark each task pass or fail with a failure category, and save `annotations.json` into the run folder.
- The human verdict is the score.

**Authentic claim wording** (what `report` prints): "ADVI completed k of n tasks (x%) on a fixed, stratified sample of Emergence WebVoyager … judged by a single human annotator. Brain: openai/gpt-oss-120b", plus the disclosures. Never "ADVI passes WebVoyager".

## Rules that keep the numbers honest
- **The brain is Groq `gpt-oss-120b`, with Gemini as backup** only when all 3 Groq keys are out, as in everyday ADVI. Every report says how many tasks each model answered. `--groq-only` gives a single-model run.
- **Quota stops are external:**
  - A per-minute limit is waited out (65 s) and retried.
  - A daily limit stops the batch. Re-run with the same `--name` to continue.
- **Dev tasks are for fixing; test tasks are only scored.** Each fix must be general, cite the dev tasks that showed the problem, and come with a unit test.
- **Budget:** 15 AI calls or 5–6 minutes per task (WebVoyager allowed 15 steps).
