"""Run one benchmark task through a fresh ADVI session and record everything.

The recorder wraps the session's provider manager and tool runner, so every
AI call (provider, model, tokens, latency, error) and every tool call (name,
arguments, success, verified, summary) is captured directly. Nothing is
parsed from log text.

Rules (Emergence WebVoyager's, which suit ADVI too):
- confirmations are always answered "no": a benchmark never buys, submits or
  sends; such a task ends as ``needs_user``;
- a fixed budget of AI calls and seconds; running out is a failure, not an
  external problem;
- running out of AI quota is external: the task is marked ``quota`` and rerun.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class BudgetExceeded(Exception):
    """Raised inside the session when the task's budget is spent."""


@dataclass(frozen=True)
class Budget:
    # WebVoyager allowed at most 15 steps; one ADVI AI round is the closest unit.
    max_llm_calls: int = 15
    max_seconds: float = 360.0


# External AI problems: quota, rate limits, and outages (timeouts, unavailable).
# waa-notepad-draft "failed" with 0 successful AI calls because the provider
# timed out; Emergence WebVoyager's rules treat outages as external and rerun.
_QUOTA_ERRORS = ("LLMRateLimitError", "LLMQuotaExhaustedError", "LLMTimeoutError", "LLMUnavailableError")
_ADMITS_FAILURE = re.compile(
    r"\b(?:i couldn't|i could not|i can't|i cannot|i wasn't able|i was unable|unable to|i stopped|"
    r"i didn't find|i did not find|no results|couldn't find|failed)\b",
    re.I,
)


def admits_failure(answer: str) -> bool:
    """Whether a final answer says the task did not work (Agent-E's error-awareness)."""
    return bool(_ADMITS_FAILURE.search(str(answer or "").replace("’", "'")))


def _short(value: Any, limit: int = 400) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {str(k): _short(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_short(v, limit) for v in list(value)[:20]]
    return value


class TaskRecorder:
    def __init__(
        self,
        agent: Any,
        budget: Budget,
        *,
        on_tool_result: Callable[[int, Any, Any], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agent = agent
        self.budget = budget
        self.on_tool_result = on_tool_result
        self.clock = clock
        self.started = clock()
        self.llm: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.over_budget: str | None = None

    def attach(self) -> "TaskRecorder":
        manager = self.agent.provider_manager
        for name in ("send_user", "send_tool_results"):
            original = getattr(manager, name)
            setattr(manager, name, self._wrap_llm(original))
        runner = self.agent.tool_runner
        runner.run = self._wrap_tool(runner.run)
        return self

    def _check_budget(self) -> None:
        if self.over_budget is None:
            if len(self.llm) >= self.budget.max_llm_calls:
                self.over_budget = f"{self.budget.max_llm_calls} AI calls"
            elif self.clock() - self.started > self.budget.max_seconds:
                self.over_budget = f"{int(self.budget.max_seconds)} seconds"
        if self.over_budget is not None:
            raise BudgetExceeded(self.over_budget)

    def _wrap_llm(self, send: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            self._check_budget()
            began = self.clock()
            entry: dict[str, Any] = {"t": round(began - self.started, 2)}
            try:
                response = send(*args, **kwargs)
            except Exception as exc:
                entry.update(error=type(exc).__name__, message=str(exc)[:300])
                attempts = getattr(self.agent.provider_manager, "last_attempts", ()) or ()
                entry["attempts"] = [f"{a.provider}:{a.error_type}" for a in attempts if getattr(a, "error_type", None)]
                self.llm.append(entry)
                raise
            entry.update(
                provider=getattr(response, "provider", None),
                model=getattr(response, "model", None),
                input_tokens=getattr(response, "input_tokens", None),
                output_tokens=getattr(response, "output_tokens", None),
                seconds=round(self.clock() - began, 2),
                tool_calls=[c.name for c in (getattr(response, "tool_calls", None) or ())],
            )
            self.llm.append(entry)
            return response

        return wrapped

    def _wrap_tool(self, run: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(call: Any, *args: Any, **kwargs: Any) -> Any:
            if self.over_budget is None and self.clock() - self.started > self.budget.max_seconds:
                self.over_budget = f"{int(self.budget.max_seconds)} seconds"
            if self.over_budget is not None:
                raise BudgetExceeded(self.over_budget)
            began = self.clock()
            result = run(call, *args, **kwargs)
            entry = {
                "t": round(began - self.started, 2),
                "name": getattr(call, "name", None),
                "arguments": _short(dict(getattr(call, "arguments", {}) or {})),
                "success": bool(getattr(result, "success", False)),
                "verified": bool(getattr(result, "verified", False)),
                "summary": _short(getattr(result, "human_readable", None) or getattr(result, "error", None) or ""),
                "seconds": round(self.clock() - began, 2),
            }
            if self.on_tool_result is not None:
                try:
                    extra = self.on_tool_result(len(self.tools), call, result)
                    if extra:
                        entry.update(extra)
                except Exception as exc:  # recording must never break the task
                    entry["record_error"] = type(exc).__name__
            self.tools.append(entry)
            return result

        return wrapped


def run_task(
    agent: Any,
    instruction: str,
    *,
    budget: Budget = Budget(),
    on_tool_result: Callable[[int, Any, Any], dict[str, Any] | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run one instruction to its end; return the task record."""
    recorder = TaskRecorder(agent, budget, on_tool_result=on_tool_result, clock=clock).attach()
    replies: list[str] = []
    confirmations: list[str] = []
    status = "answered"
    error = None
    try:
        replies.append(agent.respond(instruction).text)
        # Answer every confirmation with "no", a bounded number of times.
        for _ in range(3):
            if getattr(agent, "pending_confirmation", None) is None:
                break
            confirmations.append(replies[-1])
            replies.append(agent.respond("no").text)
    except BudgetExceeded as exc:
        error = str(exc)
    except Exception as exc:
        status, error = "crashed", f"{type(exc).__name__}: {exc}"[:500]
    if recorder.over_budget is not None:
        status, error = "budget", f"stopped after {recorder.over_budget}"
    elif status != "crashed" and confirmations:
        status = "needs_user"
    elif status != "crashed" and _quota_stopped(recorder.llm):
        status = "quota"

    answer = replies[0] if confirmations else (replies[-1] if replies else "")
    return {
        "instruction": instruction,
        "status": status,
        "error": error,
        "answer": answer,
        "replies": replies,
        "confirmations": confirmations,
        "admits_failure": admits_failure(answer),
        "seconds": round(clock() - recorder.started, 1),
        "llm_calls": sum(1 for e in recorder.llm if "error" not in e),
        "llm_errors": sum(1 for e in recorder.llm if "error" in e),
        "input_tokens": sum(int(e.get("input_tokens") or 0) for e in recorder.llm),
        "output_tokens": sum(int(e.get("output_tokens") or 0) for e in recorder.llm),
        "providers": sorted({str(e["provider"]) for e in recorder.llm if e.get("provider")}),
        "models": sorted({str(e["model"]) for e in recorder.llm if e.get("model")}),
        "tool_calls": len(recorder.tools),
        "llm": recorder.llm,
        "tools": recorder.tools,
    }


_DAILY = re.compile(r"per day|daily|tokens per day|\bTPD\b|\bRPD\b|quota", re.I)


def quota_kind(record: dict[str, Any]) -> str | None:
    """'daily' (stop the batch) or 'minute' (wait and retry) for a quota-stopped task.

    The first dev batch stopped for the day on "rate-limited; try again in
    about 20 seconds" (2026-10-23), which a one-minute pause would have cleared.
    """
    if record.get("status") != "quota":
        return None
    text = " ".join(str(e.get("message") or "") for e in record.get("llm", []) if "error" in e)
    text += " " + str(record.get("answer") or "")
    if re.search(r"rate-limited|per minute|\bTPM\b|\bRPM\b|try again in about \d+ seconds", text, re.I):
        return "minute"
    return "daily" if _DAILY.search(text) else "minute"


def quota_wait_seconds(record: dict[str, Any], default: float = 65.0, cap: float = 900.0) -> float:
    """How long the provider said to wait ("try again in about 7 minutes and about 30 seconds").

    Groq's "daily" token limit is a rolling window that frees up within minutes
    (capability sweep, 2026-10-24), so a batch can wait instead of stopping.
    """
    text = " ".join(str(e.get("message") or "") for e in record.get("llm", []) if "error" in e)
    text += " " + str(record.get("answer") or "")
    waits = []
    for minutes, seconds in re.findall(r"try again in about (\d+) minutes?(?: and about (\d+) seconds?)?", text, re.I):
        waits.append(int(minutes) * 60 + int(seconds or 0))
    for seconds in re.findall(r"try again in about (\d+) seconds", text, re.I):
        waits.append(int(seconds))
    return min(cap, max(waits) + 10) if waits else default


def _quota_stopped(llm: list[dict[str, Any]]) -> bool:
    """The last AI call failed because every key was out of quota."""
    return bool(llm) and llm[-1].get("error") in _QUOTA_ERRORS


def save_record(record: dict[str, Any], folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "record.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def load_records(run_dir: Path) -> dict[str, dict[str, Any]]:
    records = {}
    for path in sorted(Path(run_dir).glob("*/record.json")):
        records[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    return records
