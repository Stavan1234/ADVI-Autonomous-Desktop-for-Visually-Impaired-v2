"""The shared benchmark harness: recording, budgets, confirmations, quota, summaries."""
from __future__ import annotations

from types import SimpleNamespace

from benchmarks.common.metrics import markdown_table, summarize
from benchmarks.common.runner import Budget, admits_failure, run_task
from advi.providers.errors import LLMError, LLMRateLimitError


class _Manager:
    def __init__(self, responses):
        self.responses = list(responses)
        self.last_attempts = ()

    def send_user(self, *args, **kwargs):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            self.last_attempts = (SimpleNamespace(provider="groq-1", error_type=type(item).__name__),)
            raise item
        return item

    send_tool_results = send_user


class _Runner:
    def run(self, call, approved=False):
        return SimpleNamespace(success=True, verified=True, human_readable=f"ran {call.name}", error=None)


class _Agent:
    """Calls the AI once per turn, runs one tool on the first turn, and can ask to confirm."""

    def __init__(self, responses, *, confirm=False, answers=("Done.",)):
        self.provider_manager = _Manager(responses)
        self.tool_runner = _Runner()
        self.pending_confirmation = None
        self.confirm = confirm
        self.answers = list(answers)
        self.turns = []

    def respond(self, text):
        self.turns.append(text)
        try:
            self.provider_manager.send_user(text)
        except LLMError:
            return SimpleNamespace(text="I couldn't respond just now: the AI service's daily quota is used up.")
        if len(self.turns) == 1:
            self.tool_runner.run(SimpleNamespace(name="search", arguments={"query": "speaker"}))
            if self.confirm:
                self.pending_confirmation = object()
                return SimpleNamespace(text="Before I do that, I need your confirmation: buy it?")
        self.pending_confirmation = None
        return SimpleNamespace(text=self.answers.pop(0) if self.answers else "Okay.")


def _llm(provider="groq-1"):
    return SimpleNamespace(provider=provider, model="openai/gpt-oss-120b", input_tokens=5000, output_tokens=200, tool_calls=())


def test_a_task_records_ai_calls_tools_and_answer():
    shots = []
    record = run_task(_Agent([_llm()], answers=["Top speaker: Sony HT-S20R, ₹17,990."]), "find a speaker",
                      on_tool_result=lambda i, call, result: shots.append(i) or {"screenshot": f"{i}.png"})
    assert record["status"] == "answered" and "Sony" in record["answer"]
    assert record["llm_calls"] == 1 and record["input_tokens"] == 5000 and record["models"] == ["openai/gpt-oss-120b"]
    assert record["tool_calls"] == 1 and record["tools"][0]["screenshot"] == "0.png" and shots == [0]
    assert not record["admits_failure"]


def test_confirmations_are_always_answered_no():
    agent = _Agent([_llm(), _llm()], confirm=True, answers=["Okay, I won't."])
    record = run_task(agent, "buy the speaker")
    assert agent.turns == ["buy the speaker", "no"]
    assert record["status"] == "needs_user" and "confirmation" in record["answer"]


def test_budget_stops_the_task():
    agent = _Agent([_llm(), _llm()])
    record = run_task(agent, "x", budget=Budget(max_llm_calls=0))
    assert record["status"] == "budget" and "0 AI calls" in record["error"]


def test_time_budget_counts_too():
    ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0, 999.0, 999.0])
    record = run_task(_Agent([_llm()]), "x", budget=Budget(max_seconds=10), clock=lambda: next(ticks, 999.0))
    assert record["status"] == "budget"


def test_quota_is_external():
    record = run_task(_Agent([LLMRateLimitError("daily quota")]), "x")
    assert record["status"] == "quota" and record["llm_errors"] == 1


def test_error_awareness_phrases():
    assert admits_failure("I couldn't find that product on Amazon.")
    assert admits_failure("I stopped because the page needed a sign-in.")
    assert not admits_failure("The cheapest one is ₹1,299.")


def test_summary_scores_excludes_quota_and_measures_honesty():
    records = {
        "a": {"status": "answered", "seconds": 30, "llm_calls": 3, "tool_calls": 4, "admits_failure": False, "site": "Amazon"},
        "b": {"status": "answered", "seconds": 50, "llm_calls": 5, "tool_calls": 6, "admits_failure": True, "site": "Amazon"},
        "c": {"status": "answered", "seconds": 40, "llm_calls": 4, "tool_calls": 5, "admits_failure": False, "site": "BBC"},
        "d": {"status": "quota"},
    }
    summary = summarize(records, {"a": True, "b": False, "c": False}, group_of=lambda t, r: r["site"])
    assert summary["overall"]["n"] == 3 and summary["overall"]["passed"] == 1
    assert summary["groups"]["Amazon"]["success_rate"] == 0.5
    assert summary["error_awareness"] == {"failed": 2, "admitted": 1, "rate": 0.5}
    assert summary["excluded_quota"] == ["d"]
    assert "| **Overall** | 3 | 1 | 33% |" in markdown_table(summary)


def test_a_per_minute_limit_is_retried_not_a_daily_stop():
    from benchmarks.common.runner import quota_kind

    minute = {"status": "quota", "answer": "the AI service is rate-limited; try again in about 20 seconds.", "llm": []}
    daily = {"status": "quota", "answer": "the AI service's daily quota is used up on every configured key", "llm": []}
    assert quota_kind(minute) == "minute" and quota_kind(daily) == "daily"
    assert quota_kind({"status": "answered"}) is None


def test_a_provider_outage_is_external_not_a_failure():
    from advi.providers.errors import LLMTimeoutError

    record = run_task(_Agent([LLMTimeoutError("timed out")]), "x")
    assert record["status"] == "quota"


def test_quota_wait_follows_the_providers_try_again_time():
    from benchmarks.common.runner import quota_wait_seconds

    record = {"llm": [{"error": "LLMRateLimitError",
                       "message": "Groq daily token quota is exhausted. Try again in about 7 minutes and about 30 seconds."}]}
    assert quota_wait_seconds(record) == 460
    assert quota_wait_seconds({"llm": [{"error": "x", "message": "try again in about 20 seconds"}]}) == 30
    assert quota_wait_seconds({"llm": []}) == 65
    assert quota_wait_seconds({"llm": [{"error": "x", "message": "Try again in about 90 minutes"}]}) == 900  # capped
