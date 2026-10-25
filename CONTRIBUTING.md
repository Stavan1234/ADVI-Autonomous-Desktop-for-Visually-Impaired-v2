# Contributing to ADVI

Thanks for helping. ADVI is used by people who can't see the screen, so changes are judged on one
question: does the user end up with a correct, honestly reported result?

## Getting started

Set up and run the tests with [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). Read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing anything in `agent/`, `tools/` or `core/`.

## Workflow

1. Create a branch from `main` (`fix/whatsapp-focus`, `feature/excel-charts`, ...).
2. Make the change with a test in `tests/current/`.
3. Run the full suite:
   `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --deselect tests/current/test_groq_smoke.py`
4. If the change affects live behaviour, try it in ADVI itself. For larger changes, also run the relevant
   benchmark tasks (`benchmarks/README.md`) and say in the pull request what you ran.
5. Open a pull request describing the problem, the change, and the evidence (tests, logs, runs).

## Rules that keep ADVI trustworthy

- **The model proposes; code decides.** Side effects live in a capability under `src/advi/capabilities/`
  and return a `ToolResult` whose `verified` flag reflects a real check (file read back, window found,
  message gone from the composer, ...). Never mark something verified because the model said so.
- **Ask before anything that sends, or that deletes mail or memories.** Set `requires_confirmation` for the
  tool in `capabilities/registry.py`; `ToolGuard` then refuses to run it without the user's "yes". File
  deletions go to the Recycle Bin.
- **Say what really happened.** If a step failed, the answer must say so. If ADVI isn't sure, it must say
  that too.
- **Tests stay offline.** No network, microphone, real screenshots or user files: use the fakes in the
  existing tests and the fixtures in `tests/current/conftest.py`.
- **Explain rules that come from real failures.** Add a short comment with the date and what went wrong in
  the log, as the existing code does.
- **Privacy.** Never commit `.env`, `credentials/`, `logs/`, `runtime/`, `assets/whisper/` or
  `benchmarks/results/` (all git-ignored). Use `example.com` addresses and made-up names in tests and
  benchmark tasks.
- **Keep the AI and the DAVID fallback separate.** DAVID (`src/advi/fallback/`) is reached only through
  the `david_fallback` tool; don't call into it from the primary path.

## Style

- Python 3.13, `from __future__ import annotations`.
- Type hints on public functions.
- A module docstring saying what the module is for.
- Follow the naming and comment density of the surrounding code; keep functions focused.
- There is no formatter or linter configured yet; match the existing style.
