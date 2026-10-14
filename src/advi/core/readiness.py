from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ReadinessResult(Generic[T]):
    """Result of a local condition-driven wait."""

    ready: bool
    elapsed: float
    attempts: int
    value: T | None = None
    reason: str | None = None


def wait_until(
    predicate: Callable[[], T | bool],
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.10,
    stable_for: float = 0.0,
) -> ReadinessResult[T]:
    """Poll a local readiness condition without involving an LLM.

    If ``stable_for`` is non-zero, the predicate must remain truthy for the
    requested duration. Falsey samples reset the stability timer.
    """
    timeout = max(0.0, float(timeout))
    poll_interval = max(0.01, float(poll_interval))
    stable_for = max(0.0, float(stable_for))
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    stable_started: float | None = None
    last_value: T | None = None

    while True:
        attempts += 1
        try:
            value = predicate()
            last_value = value if value is not True and value is not False else None
            truthy = bool(value)
        except Exception:
            truthy = False
            last_value = None

        now = time.monotonic()
        if truthy:
            if stable_for <= 0:
                return ReadinessResult(True, now - started, attempts, last_value)
            if stable_started is None:
                stable_started = now
            if now - stable_started >= stable_for:
                return ReadinessResult(True, now - started, attempts, last_value)
        else:
            stable_started = None

        if now >= deadline:
            return ReadinessResult(
                False,
                now - started,
                attempts,
                last_value,
                reason="timeout",
            )
        time.sleep(min(poll_interval, max(0.0, deadline - now)))


def wait_for_stable_value(
    getter: Callable[[], T],
    *,
    timeout: float = 2.0,
    poll_interval: float = 0.10,
    stable_for: float = 0.20,
) -> ReadinessResult[T]:
    """Wait until a getter returns the same value for ``stable_for`` seconds."""
    previous: object = object()
    stable_started: float | None = None
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout))
    attempts = 0
    last_value: T | None = None

    while True:
        attempts += 1
        try:
            value = getter()
            last_value = value
        except Exception:
            value = None  # type: ignore[assignment]
            last_value = None

        now = time.monotonic()
        if value == previous:
            if stable_started is None:
                stable_started = now
            if now - stable_started >= max(0.0, float(stable_for)):
                return ReadinessResult(True, now - started, attempts, last_value)
        else:
            previous = value
            stable_started = now

        if now >= deadline:
            return ReadinessResult(False, now - started, attempts, last_value, reason="timeout")
        time.sleep(min(max(0.01, float(poll_interval)), max(0.0, deadline - now)))
