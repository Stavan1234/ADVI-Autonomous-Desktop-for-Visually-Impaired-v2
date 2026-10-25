"""Emergence WebVoyager tasks: instantiate the templates for today, and fix the sample.

The upstream ``instantiate_tasks.py`` fills ``{{placeholders}}`` and runs
``eval()`` on "@eval:" strings from the template file. Evaluating code from a
downloaded file is not something ADVI's tooling does, so this module accepts
only the expression shapes the upstream script supports (its two date helpers
and ``now() + timedelta(days=N)`` with strftime) and reproduces them exactly.
A task with any other expression is not instantiated and is listed as excluded.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
UPSTREAM = HERE / "upstream"
TEMPLATE = UPSTREAM / "webvoyager_template.json"
SPLITS = HERE / "splits.json"

# WebVoyager's 15 sites; the agent starts on the site's page, as in the paper.
START_URLS = {
    "allrecipes": "https://www.allrecipes.com/",
    "amazon": "https://www.amazon.com/",
    "apple": "https://www.apple.com/",
    "arxiv": "https://arxiv.org/",
    "github": "https://github.com/",
    "booking": "https://www.booking.com/",
    "espn": "https://www.espn.com/",
    "coursera": "https://www.coursera.org/",
    "cambridge": "https://dictionary.cambridge.org/",
    "bbc": "https://www.bbc.com/news",
    "google flights": "https://www.google.com/travel/flights",
    "google map": "https://www.google.com/maps",
    "google search": "https://www.google.com/",
    "search engine": "https://www.google.com/",
    "huggingface": "https://huggingface.co/",
    "wolfram": "https://www.wolframalpha.com/",
}


def start_url(domain: str) -> str | None:
    key = re.sub(r"[^a-z ]+", " ", str(domain).casefold()).strip()
    for name, url in START_URLS.items():
        if name in key or key.replace(" ", "") in name.replace(" ", ""):
            return url
    return None


# --- the upstream helpers, reproduced ---------------------------------------


def generate_valentines_week(now: datetime) -> str:
    valentines = datetime(now.year, 2, 14)
    start = valentines - timedelta(days=valentines.weekday() + 1)
    end = start + timedelta(days=7)
    if now > end:
        valentines = datetime(now.year + 1, 2, 14)
        start = valentines - timedelta(days=valentines.weekday() + 1)
        end = start + timedelta(days=7)
    return f"Valentine's Day week, {start.strftime('%B %d')}-{end.strftime('%d')}, {start.year}"


def generate_weekend_dates_after_x_days(now: datetime, x_days: int) -> str:
    target = now + timedelta(days=x_days)
    friday = target + timedelta(days=(4 - target.weekday() + 7) % 7)
    sunday = friday + timedelta(days=2)
    return f"{friday.strftime('%b %d')}-{sunday.strftime('%d')}, {friday.year}"


_SAFE_EXPRESSIONS: list[tuple[re.Pattern[str], Callable[[datetime, re.Match[str]], str]]] = [
    (re.compile(r"^generate_valentines_week\(\)$"), lambda now, m: generate_valentines_week(now)),
    (re.compile(r"^generate_weekend_dates_after_x_days\((\d+)\)$"),
     lambda now, m: generate_weekend_dates_after_x_days(now, int(m.group(1)))),
    (re.compile(r"""^\(now\(\)\s*\+\s*timedelta\(days\s*=\s*(-?\d+)\)\)\.strftime\((['"])([%A-Za-z ,.\-/]+)\2\)$"""),
     lambda now, m: (now + timedelta(days=int(m.group(1)))).strftime(m.group(3))),
    # The shapes below appear in the template downloaded 2026-10-23 (commit e988bb1).
    (re.compile(r"""^\(now\(\)\)\.strftime\((['"])([%A-Za-z ,.\-/]+)\1\)$"""),
     lambda now, m: now.strftime(m.group(2))),
    (re.compile(r"^\(datetime\.now\(\)\.year(?:\s*-\s*(\d+))?\)$"),
     lambda now, m: str(now.year - int(m.group(1) or 0))),
    (re.compile(r"""^str\(datetime\.now\(\)\.year\s*-\s*(\d+)\)\s*\+\s*'-'\s*\+\s*str\(datetime\.now\(\)\.year\)\[-(\d+):\]$"""),
     lambda now, m: f"{now.year - int(m.group(1))}-{str(now.year)[-int(m.group(2)):]}"),
]


class UnsupportedExpression(ValueError):
    pass


def evaluate(expression: str, now: datetime) -> str:
    text = expression.strip()
    for pattern, render in _SAFE_EXPRESSIONS:
        match = pattern.match(text)
        if match:
            return render(now, match)
    raise UnsupportedExpression(text)


def instantiate(templates: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (tasks, excluded). Mirrors upstream: drops status 'remove', 1-based index over kept tasks."""
    tasks, excluded = [], []
    for item in templates:
        if "remove" in str(item.get("status") or ""):
            continue
        task = str(item["intent_template"])
        questions = list(item.get("questions_for_annotation") or [])
        try:
            for key, value in dict(item.get("instantiation_dict") or {}).items():
                value = str(value)
                if value.startswith("@eval:"):
                    value = evaluate(value[len("@eval:"):], now)
                token = "{{" + key + "}}"
                task = task.replace(token, value)
                questions = [q.replace(token, value) for q in questions]
        except UnsupportedExpression as exc:
            excluded.append({"domain": item.get("domain"), "template": item.get("intent_template"),
                             "reason": f"unsupported @eval expression: {exc}"})
            continue
        answer_type = item.get("reference_answer_type", "possible")
        entry = {"index": len(tasks) + 1, "domain": item["domain"], "task": task, "questions_for_annotation": questions,
                 "upstream_id": item.get("id"),
                 "website": str((item.get("instantiation_dict") or {}).get("website") or "") or None}
        if answer_type == "golden":
            entry["golden_answer"] = item.get("reference_answers", "")
        tasks.append(entry)
    return tasks, excluded


def task_id(task: dict[str, Any]) -> str:
    """Domain plus the upstream task id, so a task keeps its name across days and template edits."""
    domain = re.sub(r"[^a-z0-9]+", "-", str(task["domain"]).casefold()).strip("-")
    number = task.get("upstream_id") or task["index"]
    return f"{domain}-{int(number):03d}"


def load_tasks(now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"{TEMPLATE} is missing; see benchmarks/webvoyager/README.md to fetch it.")
    templates = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    tasks, excluded = instantiate(templates, now or datetime.now())
    for task in tasks:
        task["id"] = task_id(task)
        # Each upstream task names its website; the domain map is only a fallback.
        task["start_url"] = task.get("website") or start_url(task["domain"])
    return tasks, excluded


def splits(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    from benchmarks.common.splits import load_or_create, stratified_split

    # Indices are stable across days (only dates change), so ids are too.
    return load_or_create(SPLITS, lambda: stratified_split(
        tasks, group_of=lambda t: str(t["domain"]), id_of=lambda t: t["id"],
        dev_per_group=2, test_per_group=3, seed=20261023))
