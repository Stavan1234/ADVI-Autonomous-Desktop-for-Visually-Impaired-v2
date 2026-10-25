"""Score a judged WebVoyager run from its annotations.json.

    .venv\\Scripts\\python.exe -m benchmarks.webvoyager.report benchmarks\\results\\webvoyager\\test-baseline

Writes report.md into the run folder: the claim sentence with every disclosure,
per-site results, efficiency, error-awareness, the failure taxonomy, and the
AI pre-screen's agreement with the human judge.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from benchmarks.common.metrics import markdown_table, model_line, summarize  # noqa: E402
from benchmarks.common.runner import load_records  # noqa: E402


def build_report(run_dir: Path) -> str:
    records = load_records(run_dir)
    annotations = json.loads((run_dir / "annotations.json").read_text(encoding="utf-8"))
    prescreen_path = run_dir / "prescreen.json"
    prescreens = json.loads(prescreen_path.read_text(encoding="utf-8")) if prescreen_path.exists() else {}
    excluded_path = run_dir / "excluded.json"
    excluded = json.loads(excluded_path.read_text(encoding="utf-8")) if excluded_path.exists() else []

    verdicts = {tid: bool(a["pass"]) for tid, a in annotations.items() if tid in records}
    summary = summarize(records, verdicts, group_of=lambda tid, rec: str(rec.get("domain")))
    overall = summary["overall"]
    models = sorted({m for rec in records.values() for m in rec.get("models", [])})
    dates = sorted(str(rec.get("executed_at", ""))[:10] for rec in records.values() if rec.get("executed_at"))
    categories = Counter(a.get("category") or "uncategorized" for tid, a in annotations.items() if not a["pass"])
    agree = [prescreens[t]["pass"] == verdicts[t] for t in verdicts if t in prescreens]

    rate = f"{overall['success_rate']:.1%}" if overall["success_rate"] is not None else "n/a"
    lines = [
        f"# WebVoyager results: {run_dir.name}",
        "",
        f"**Claim:** ADVI completed {overall['passed']} of {overall['n']} tasks ({rate}) on a fixed, stratified "
        f"sample of Emergence WebVoyager (github.com/EmergenceAI/EmergenceWebVoyager), run {dates[0] if dates else '?'}"
        f" to {dates[-1] if dates else '?'} from India, judged by a single human annotator. Brain: {', '.join(models) or '?'} (Gemini only when all Groq keys were out; see below).",
        "",
        "**Disclosures:**",
        "- This is a sample, not the full 535-task benchmark. The sample was fixed by a seeded split (`splits.json`) before the first run.",
        "- Each task started on its site's page in ADVI's own Chrome profile. The task text was sent unchanged.",
        "- Confirmations were always answered \"no\", so ADVI never purchased, submitted or sent anything.",
        "- Each task had a budget of 15 AI calls or 6 minutes.",
        f"- {len(summary['excluded_quota'])} task(s) were stopped by the AI quota (external) and not scored: {summary['excluded_quota'] or 'none'}.",
        f"- {len(excluded)} upstream task(s) could not be instantiated safely (listed in `excluded.json`).",
        f"- {len(summary['unjudged'])} task(s) ran but were not judged: {summary['unjudged'] or 'none'}.",
        "",
        markdown_table(summary, "Site"),
        "",
        f"**Error-awareness** (Agent-E): of {summary['error_awareness']['failed']} failed tasks, ADVI said so in "
        f"{summary['error_awareness']['admitted']}.",
        "",
        "**Why tasks failed** (human-labelled):",
        *[f"- {name}: {count}" for name, count in categories.most_common()],
        "",
        f"**AI pre-screen vs the human:** agreed on {sum(agree)} of {len(agree)} tasks." if agree else "**AI pre-screen:** not run.",
        "",
        model_line(records),
        "",
        f"End states: {summary['status_counts']}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    run_dir = Path((sys.argv[1:] if argv is None else argv)[0])
    report = build_report(run_dir)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
