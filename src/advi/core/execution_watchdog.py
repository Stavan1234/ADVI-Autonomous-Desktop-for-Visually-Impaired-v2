from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any


@dataclass(frozen=True)
class WatchdogDecision:
    allowed: bool
    reason: str = "allowed"


class ExecutionWatchdog:
    """Small deterministic guard against runaway/repeated tool execution.

    Observation/settling operations are intentionally exempt from the identical
    action counter: legitimate recovery may inspect or scroll the same surface
    more than twice. They remain bounded by the total action, failure, and timeout
    budgets.
    """

    _REPEATABLE_OBSERVATION_ACTIONS = frozenset({
        "wait", "list_windows", "inspect_window", "list_applications",
        "inspect_web_page", "read_web_page", "scroll", "read_screen",
    })

    def __init__(
        self,
        *,
        max_actions: int = 40,
        max_failures: int = 6,
        max_same_action: int = 2,
        task_timeout_seconds: float = 300.0,
    ) -> None:
        self.max_actions = max(1, int(max_actions))
        self.max_failures = max(1, int(max_failures))
        self.max_same_action = max(1, int(max_same_action))
        self.task_timeout_seconds = max(5.0, float(task_timeout_seconds))
        self._started_at: float | None = None
        self._actions = 0
        self._failures = 0
        self._fingerprints: deque[str] = deque(maxlen=32)
        self._counts: dict[str, int] = {}

    def begin_task(self) -> None:
        self._started_at = time.monotonic()
        self._actions = 0
        self._failures = 0
        self._fingerprints.clear()
        self._counts.clear()

    def before_action(self, name: str, arguments: dict[str, Any]) -> WatchdogDecision:
        if self._started_at is None:
            self.begin_task()
        if time.monotonic() - (self._started_at or time.monotonic()) > self.task_timeout_seconds:
            return WatchdogDecision(False, "task_timeout")
        if self._actions >= self.max_actions:
            return WatchdogDecision(False, "maximum_action_count_reached")
        if self._failures >= self.max_failures:
            return WatchdogDecision(False, "maximum_failure_count_reached")
        fingerprint = self._fingerprint(name, arguments)
        count = self._counts.get(fingerprint, 0)
        if name not in self._REPEATABLE_OBSERVATION_ACTIONS:
            if count >= self.max_same_action:
                return WatchdogDecision(False, "identical_action_repeated_too_many_times")
            self._counts[fingerprint] = count + 1
        self._fingerprints.append(fingerprint)
        return WatchdogDecision(True)

    def after_action(self, *, success: bool) -> None:
        self._actions += 1
        if not success:
            self._failures += 1

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    @property
    def expired(self) -> bool:
        return self._started_at is not None and self.elapsed_seconds > self.task_timeout_seconds

    @property
    def actions(self) -> int:
        return self._actions

    @property
    def failures(self) -> int:
        return self._failures

    def budget(self) -> dict[str, float | int | bool]:
        """Return bounded task-budget telemetry; this does not interrupt running work."""
        return {
            "actions": self._actions,
            "max_actions": self.max_actions,
            "failures": self._failures,
            "max_failures": self.max_failures,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "timeout_seconds": self.task_timeout_seconds,
            "expired": self.expired,
        }

    @staticmethod
    def _fingerprint(name: str, arguments: dict[str, Any]) -> str:
        payload = json.dumps({"name": name, "arguments": arguments}, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
