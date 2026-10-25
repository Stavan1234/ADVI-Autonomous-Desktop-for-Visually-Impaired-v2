"""Run ADVI on the Emergence WebVoyager sample, in resumable daily batches.

    .venv\\Scripts\\python.exe -m benchmarks.webvoyager.run --split dev --batch 8
    .venv\\Scripts\\python.exe -m benchmarks.webvoyager.run --split test --name baseline --batch 8

Per task: a fresh ADVI session, ADVI's Chrome opened on the task's website, the
task text sent exactly as written, confirmations answered "no", a screenshot
after every browser action and at the end. Results go to
benchmarks/results/webvoyager/<name>/<task>/. Judge them with
``-m benchmarks.webvoyager.judge``.
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

from benchmarks.common.runner import Budget, load_records, quota_kind, run_task, save_record  # noqa: E402
from benchmarks.webvoyager.tasks import load_tasks, splits  # noqa: E402

RESULTS = ROOT / "benchmarks" / "results" / "webvoyager"
_BROWSER_TOOLS = {"open_browser", "navigate", "search", "click_web_element", "type_web_element",
                  "read_web_page", "inspect_web_page", "go_back", "go_forward", "reload_page", "play_media"}


def _screenshot(path: Path) -> bool:
    try:
        from advi.capabilities.browser import pw_session

        browser = pw_session._shared
        if browser is None or browser.page is None or browser.page.is_closed():
            return False
        browser.page.screenshot(path=str(path), type="jpeg", quality=60)
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--name", default=None)
    parser.add_argument("--batch", type=int, default=8, help="tasks per run (default 8, about a day of free quota)")
    parser.add_argument("--groq-only", action="store_true", help="no Gemini backup when the Groq keys are out")
    args = parser.parse_args(argv)

    tasks, excluded = load_tasks()
    split = splits(tasks)
    wanted = set(args.only) if args.only else set(split[args.split])
    selected = [t for t in tasks if t["id"] in wanted]
    run_dir = RESULTS / (args.name or f"{args.split}-{datetime.now():%Y%m%d}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "excluded.json").write_text(json.dumps(excluded, indent=2), encoding="utf-8")
    done = {tid for tid, rec in load_records(run_dir).items() if rec.get("status") != "quota"}

    from benchmarks.common.agent_factory import build_runtime, new_agent
    from advi.tools.contracts import ToolCall

    advi = build_runtime(include_gemini=not args.groq_only)
    ran = 0
    for task in selected:
        if task["id"] in done:
            continue
        if ran >= args.batch:
            break
        folder = run_dir / task["id"]
        folder.mkdir(parents=True, exist_ok=True)
        shots: list[str] = []

        def on_tool(index, call, result, folder=folder, shots=shots):
            if getattr(call, "name", None) not in _BROWSER_TOOLS:
                return None
            name = f"step-{index:02d}.jpg"
            if _screenshot(folder / name):
                shots.append(name)
                return {"screenshot": name}
            return None

        for attempt in range(3):
            shots.clear()
            agent = new_agent(advi)
            # Start on the task's website, as WebVoyager does; this step is not part of the task's record.
            if task.get("start_url"):
                agent.tool_runner.run(ToolCall(call_id=f"start-{task['id']}", name="open_browser",
                                               arguments={"url": task["start_url"]}), approved=False)
            record = run_task(agent, task["task"], budget=Budget(max_llm_calls=15, max_seconds=360), on_tool_result=on_tool)
            if quota_kind(record) != "minute" or attempt == 2:
                break
            print(f"      {task['id']}: per-minute AI limit, waiting 65 s and retrying")
            time.sleep(65)
        if _screenshot(folder / "final.jpg"):
            shots.append("final.jpg")
        record.update(task_id=task["id"], domain=task["domain"], start_url=task.get("start_url"),
                      questions=task["questions_for_annotation"], golden_answer=task.get("golden_answer"),
                      brain_model=advi.brain_model, screenshots=shots,
                      executed_at=datetime.now().isoformat(timespec="seconds"))
        save_record(record, folder)
        ran += 1
        print(f"{record['status']:10} {task['id']:24} {record['seconds']:6.1f}s  AI {record['llm_calls']:2}  "
              f"tools {record['tool_calls']:2}  {record['answer'][:100]!r}")
        sys.stdout.flush()
        if record["status"] == "quota":
            print(f"AI quota stopped this task ({quota_kind(record)} limit, after retries); stopping this batch. Re-run the same --name later to continue.")
            break
    print(f"\nRecords: {run_dir}. Next: python -m benchmarks.webvoyager.judge {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
