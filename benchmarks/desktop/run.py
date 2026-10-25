"""Run the ADVI Desktop Suite.

    .venv\\Scripts\\python.exe -m benchmarks.desktop.run --split dev
    .venv\\Scripts\\python.exe -m benchmarks.desktop.run --split test --name baseline
    .venv\\Scripts\\python.exe -m benchmarks.desktop.run --only waa-hide-file advi-battery

Results land in benchmarks/results/desktop/<name>/<task>/record.json and a
summary.md. Re-running the same --name skips finished tasks (quota stops are
retried), so a run can be spread over several days.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from benchmarks.common.metrics import markdown_table, model_line, summarize  # noqa: E402
from benchmarks.common.runner import Budget, load_records, quota_kind, quota_wait_seconds, run_task, save_record  # noqa: E402
from benchmarks.common.splits import load_or_create, stratified_split  # noqa: E402
from benchmarks.desktop.suite import bench_root, load_tasks, prepare, run_checks  # noqa: E402

SPLITS = Path(__file__).with_name("splits.json")
RESULTS = ROOT / "benchmarks" / "results" / "desktop"


def split_for(tasks):
    return load_or_create(SPLITS, lambda: stratified_split(
        tasks, group_of=lambda t: t["domain"], id_of=lambda t: t["id"], dev_fraction=0.4, seed=20261023))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--only", nargs="*", help="run just these task ids")
    parser.add_argument("--name", default=None, help="run name (default: <split>-<date>)")
    parser.add_argument("--batch", type=int, default=0, help="stop after this many tasks (0 = all)")
    parser.add_argument("--groq-only", action="store_true", help="no Gemini backup when the Groq keys are out")
    parser.add_argument("--tasks", choices=("suite", "capabilities"), default="suite",
                        help="the scored Desktop Suite, or the capability sweep (feature check, not a score)")
    args = parser.parse_args(argv)

    if args.tasks == "capabilities":
        tasks = load_tasks(Path(__file__).with_name("capabilities.json"))
        wanted = set(args.only) if args.only else {t["id"] for t in tasks}
        default_name = f"capabilities-{datetime.now():%Y%m%d}"
    else:
        tasks = load_tasks()
        split = split_for(tasks)
        wanted = set(args.only) if args.only else (
            {t["id"] for t in tasks} if args.split == "all" else set(split[args.split]))
        default_name = f"{args.split}-{datetime.now():%Y%m%d}"
    selected = [t for t in tasks if t["id"] in wanted]  # file order: a "close" task follows its "open" task
    run_dir = RESULTS / (args.name or default_name)
    done = {tid for tid, rec in load_records(run_dir).items() if rec.get("status") != "quota"}

    from benchmarks.common.agent_factory import build_runtime, new_agent

    advi = build_runtime(include_gemini=not args.groq_only)
    root = bench_root()
    ran = 0
    for task in selected:
        if task["id"] in done:
            continue
        if args.batch and ran >= args.batch:
            break
        for attempt in range(8):
            folder = prepare(task, root)
            record = run_task(new_agent(advi), task["instruction"], budget=Budget(max_llm_calls=15, max_seconds=300))
            if record["status"] != "quota" or attempt == 7:
                break
            # Quota is external and Groq's limits are rolling windows: wait as long as the
            # provider says (at most 15 minutes), then rerun from a clean start.
            # Longer each time when every provider is out: Groq's rolling daily window
            # frees up minutes at a time (capability sweep, 2026-10-24).
            wait = max(quota_wait_seconds(record), (65, 120, 300, 600, 900, 900, 900, 900)[attempt])
            print(f"      {task['id']}: AI limit ({quota_kind(record)}), waiting {wait:.0f} s and retrying")
            sys.stdout.flush()
            time.sleep(wait)
        time.sleep(1.0)  # let a just-saved file or a closing app settle before checking
        checks = run_checks(task, folder, record)
        record.update(task_id=task["id"], domain=task["domain"], waa=task.get("waa"),
                      brain_model=advi.brain_model, checks=checks,
                      passed=record["status"] != "quota" and all(c["passed"] for c in checks))
        save_record(record, run_dir / task["id"])
        ran += 1
        mark = "PASS" if record["passed"] else ("QUOTA" if record["status"] == "quota" else "FAIL")
        print(f"{mark:5} {task['id']:28} {record['seconds']:6.1f}s  AI {record['llm_calls']:2}  tools {record['tool_calls']:2}  {record['answer'][:90]!r}")
        sys.stdout.flush()
        if record["status"] == "quota":
            print(f"AI quota stopped this task ({quota_kind(record)} limit, after retries); stopping this batch. "
                  "Re-run the same --name later to continue.")
            break

    records = load_records(run_dir)
    verdicts = {tid: bool(rec.get("passed")) for tid, rec in records.items()}
    summary = summarize(records, verdicts, group_of=lambda tid, rec: rec.get("domain", "?"))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    table = markdown_table(summary, "Domain") + "\n\n" + model_line(records)
    (run_dir / "summary.md").write_text(table + "\n", encoding="utf-8")
    print("\n" + table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
