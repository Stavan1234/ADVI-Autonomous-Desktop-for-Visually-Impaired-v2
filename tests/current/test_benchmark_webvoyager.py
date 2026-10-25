"""Emergence WebVoyager tooling: safe instantiation, fixed split, judging, report."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from benchmarks.common.runner import save_record
from benchmarks.common.splits import stratified_split
from benchmarks.webvoyager import judge, report
from benchmarks.webvoyager.tasks import (
    UnsupportedExpression,
    evaluate,
    generate_weekend_dates_after_x_days,
    instantiate,
    start_url,
    task_id,
)

NOW = datetime(2026, 10, 23, 12, 0)


def _template(**extra):
    item = {
        "domain": "Booking",
        "intent_template": "Find a hotel in Paris for {{dates}} under {{price}}.",
        "instantiation_dict": {"dates": "@eval:generate_weekend_dates_after_x_days(20)", "price": "$200"},
        "questions_for_annotation": ["Is the stay {{dates}}?"],
    }
    item.update(extra)
    return item


def test_dates_are_filled_for_today():
    tasks, excluded = instantiate([_template()], NOW)
    # 23 Oct + 20 days = 12 Nov 2026 (Thursday); the weekend is Fri 13 - Sun 15.
    assert tasks[0]["task"] == "Find a hotel in Paris for Nov 13-15, 2026 under $200."
    assert tasks[0]["questions_for_annotation"] == ["Is the stay Nov 13-15, 2026?"]
    assert tasks[0]["index"] == 1 and not excluded


def test_relative_strftime_and_valentines():
    assert evaluate("(now() + timedelta(days=20)).strftime('%B %d, %Y')", NOW) == "November 12, 2026"
    assert evaluate("generate_valentines_week()", NOW).startswith("Valentine's Day week, February 07-14, 2027")


def test_anything_else_is_refused_never_evaluated():
    with pytest.raises(UnsupportedExpression):
        evaluate("__import__('os').system('calc')", NOW)
    tasks, excluded = instantiate([_template(instantiation_dict={"dates": "@eval:open('x').read()", "price": "1"})], NOW)
    assert tasks == [] and "unsupported" in excluded[0]["reason"]


def test_removed_tasks_are_dropped_and_golden_answers_kept():
    tasks, _ = instantiate([
        _template(status="remove: outdated"),
        _template(domain="Wolfram Alpha", reference_answer_type="golden", reference_answers="42"),
    ], NOW)
    assert len(tasks) == 1 and tasks[0]["index"] == 1 and tasks[0]["golden_answer"] == "42"
    assert task_id(tasks[0]) == "wolfram-alpha-001"


def test_every_site_has_a_start_page():
    for site in ("Allrecipes", "Amazon", "Apple", "ArXiv", "GitHub", "Booking", "ESPN", "Coursera",
                 "Cambridge Dictionary", "BBC News", "Google Flights", "Google Map", "Google Search",
                 "Huggingface", "Wolfram Alpha"):
        assert start_url(site), site
    assert "flights" in start_url("Google Flights") and "maps" in start_url("Google Map")


def test_split_takes_two_dev_and_three_test_per_site():
    tasks = [{"id": f"{site}-{i}", "domain": site} for site in ("Amazon", "BBC") for i in range(35)]
    split = stratified_split(tasks, group_of=lambda t: t["domain"], id_of=lambda t: t["id"],
                             dev_per_group=2, test_per_group=3, seed=20261023)
    assert len(split["dev"]) == 4 and len(split["test"]) == 6
    assert not set(split["dev"]) & set(split["test"])


def _run(tmp_path):
    for tid, answer, status in (("amazon-001", "The cheapest is $12.", "answered"),
                                ("amazon-002", "I couldn't find it.", "answered"),
                                ("bbc-001", "", "quota")):
        save_record({"instruction": f"task {tid}", "status": status, "answer": answer, "domain": tid.split("-")[0],
                     "seconds": 30, "llm_calls": 4, "tool_calls": 5, "admits_failure": "couldn't" in answer,
                     "models": ["openai/gpt-oss-120b"], "executed_at": "2026-10-24T10:00:00",
                     "screenshots": [], "questions": ["Is the price right?"], "tools": []}, tmp_path / tid)
    return tmp_path


def test_prescreen_reads_the_models_json(tmp_path):
    run = _run(tmp_path)
    rec = json.loads((run / "amazon-001" / "record.json").read_text(encoding="utf-8"))
    seen = {}
    verdict = judge.prescreen(rec, run / "amazon-001", ask=lambda prompt, images: seen.update(p=prompt) or
                              'Sure: {"pass": true, "reason": "price shown"}')
    assert verdict == {"pass": True, "reason": "price shown"} and "The cheapest is $12." in seen["p"]


def test_review_page_and_report(tmp_path):
    run = _run(tmp_path)
    from benchmarks.common.runner import load_records

    page = judge.review_page(run, load_records(run), {"amazon-001": {"pass": True, "reason": "ok"}})
    assert "amazon-002" in page and "annotations.json" in page and "navigation stuck" in page
    (run / "annotations.json").write_text(json.dumps({
        "amazon-001": {"pass": True, "category": "", "note": ""},
        "amazon-002": {"pass": False, "category": "navigation stuck", "note": ""},
    }), encoding="utf-8")
    (run / "prescreen.json").write_text(json.dumps({"amazon-001": {"pass": True}, "amazon-002": {"pass": True}}), encoding="utf-8")
    text = report.build_report(run)
    assert "ADVI completed 1 of 2 tasks (50.0%)" in text and "single human annotator" in text
    assert "openai/gpt-oss-120b" in text and "['bbc-001']" in text
    assert "of 1 failed tasks, ADVI said so in 1" in text and "navigation stuck: 1" in text
    assert "agreed on 1 of 2 tasks" in text


def test_year_and_past_date_shapes_from_the_real_template():
    assert evaluate("(datetime.now().year - 2)", NOW) == "2024"
    assert evaluate("(datetime.now().year)", NOW) == "2026"
    assert evaluate("str(datetime.now().year - 1)+'-'+str(datetime.now().year)[-2:]", NOW) == "2025-26"
    assert evaluate("(now()).strftime('%Y')", NOW) == "2026"
    assert evaluate("(now() + timedelta(days=-3)).strftime('%B %d %Y')", NOW) == "October 20 2026"


def test_the_downloaded_template_instantiates_completely():
    from benchmarks.webvoyager.tasks import TEMPLATE, load_tasks

    if not TEMPLATE.exists():
        pytest.skip("upstream template not downloaded")
    tasks, excluded = load_tasks(NOW)
    assert len(tasks) == 535 and excluded == []
    assert all("{{" not in t["task"] and t["start_url"] for t in tasks)
