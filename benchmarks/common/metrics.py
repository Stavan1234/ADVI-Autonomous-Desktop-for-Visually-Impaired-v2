"""Summarize a benchmark run: success, efficiency, honesty, and why tasks failed.

``verdicts`` maps task id to True/False (human annotation for WebVoyager, the
execution check for the Desktop Suite). Tasks stopped by quota are external
and excluded from scores, but always counted and disclosed.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Callable


def summarize(
    records: dict[str, dict[str, Any]],
    verdicts: dict[str, bool],
    *,
    group_of: Callable[[str, dict[str, Any]], str] = lambda task_id, record: "all",
) -> dict[str, Any]:
    scored = {tid: rec for tid, rec in records.items() if rec.get("status") != "quota" and tid in verdicts}
    excluded = sorted(tid for tid, rec in records.items() if rec.get("status") == "quota")
    unjudged = sorted(tid for tid, rec in records.items() if rec.get("status") != "quota" and tid not in verdicts)

    groups: dict[str, list[str]] = defaultdict(list)
    for tid, rec in scored.items():
        groups[group_of(tid, rec)].append(tid)

    def stats(ids: list[str]) -> dict[str, Any]:
        recs = [scored[t] for t in ids]
        passed = sum(1 for t in ids if verdicts[t])
        return {
            "n": len(ids),
            "passed": passed,
            "success_rate": round(passed / len(ids), 3) if ids else None,
            "median_seconds": _median(r.get("seconds") for r in recs),
            "median_llm_calls": _median(r.get("llm_calls") for r in recs),
            "median_tool_calls": _median(r.get("tool_calls") for r in recs),
            "median_tokens": _median((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in recs),
        }

    failed = [t for t in scored if not verdicts[t]]
    # Error-awareness (Agent-E): of the failed tasks, how many answers said so.
    aware = sum(1 for t in failed if scored[t].get("admits_failure"))
    return {
        "overall": stats(list(scored)),
        "groups": {name: stats(ids) for name, ids in sorted(groups.items())},
        "error_awareness": {"failed": len(failed), "admitted": aware,
                            "rate": round(aware / len(failed), 3) if failed else None},
        "status_counts": _counts(rec.get("status") for rec in records.values()),
        "excluded_quota": excluded,
        "unjudged": unjudged,
    }


def markdown_table(summary: dict[str, Any], group_label: str = "Group") -> str:
    rows = [f"| {group_label} | n | Passed | Success | Median s | Median AI calls | Median tool calls |",
            "|---|---|---|---|---|---|---|"]
    for name, s in [*summary["groups"].items(), ("**Overall**", summary["overall"])]:
        rate = f"{s['success_rate']:.0%}" if s["success_rate"] is not None else "—"
        rows.append(f"| {name} | {s['n']} | {s['passed']} | {rate} | {s['median_seconds']} | "
                    f"{s['median_llm_calls']} | {s['median_tool_calls']} |")
    return "\n".join(rows)


def model_line(records: dict[str, dict[str, Any]]) -> str:
    """Which brain answered: tasks served only by Groq, only by Gemini, or by both."""
    counts: dict[str, int] = defaultdict(int)
    for rec in records.values():
        models = rec.get("models") or []
        if not models:
            continue
        counts[" + ".join(sorted(models))] += 1
    parts = [f"{name}: {n} task(s)" for name, n in sorted(counts.items())]
    return "**Brain per task:** " + ("; ".join(parts) if parts else "none recorded")


def _median(values: Any) -> float | None:
    numbers = [float(v) for v in values if isinstance(v, (int, float))]
    return round(median(numbers), 1) if numbers else None


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[str(value)] += 1
    return dict(sorted(counts.items()))
