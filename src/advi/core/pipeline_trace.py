from __future__ import annotations

import contextvars
import json
import logging
import re
import traceback
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("advi.trace")

# ---------------------------------------------------------------------------
# Correlation context (propagates through async/sync call stacks)
# ---------------------------------------------------------------------------

_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_turn_id",
    default=None,
)
_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_task_id",
    default=None,
)
_llm_call_seq: contextvars.ContextVar[int] = contextvars.ContextVar(
    "trace_llm_call_seq",
    default=0,
)
_llm_call_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_llm_call_id",
    default=None,
)

_MAX_FULL_LEN = 20_000
_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|credential|authorization|"
    r"oauth|refresh|access_token|client_secret|client_id|private_key|auth_code|code)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"ya29\.[A-Za-z0-9_\-]+"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gsk_[A-Za-z0-9]+"),
    re.compile(r"1//[0-9A-Za-z_\-]+"),
)
_KEY_VALUE_SECRET_PATTERN = re.compile(
    r'(?i)(["\']?(?:code|access_token|refresh_token|client_secret)["\']?\s*[:=]\s*["\']?)([^"\'&\s,]+)'
)


_turn_summary: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "trace_turn_summary",
    default=None,
)


def begin_turn(user_input: str | None = None) -> str:
    """Start a new correlated trace for one user turn."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:8]
    turn_id = f"{stamp}-{suffix}"
    _turn_id.set(turn_id)
    _task_id.set(None)
    _llm_call_seq.set(0)
    _llm_call_id.set(None)
    _turn_summary.set(
        {
            "turn_id": turn_id,
            "task_id": None,
            "intent": None,
            "plan": None,
            "executed_action": None,
            "execution_success": None,
            "task_status_before": None,
            "task_status_after": None,
            "final_response_generated": False,
            "exception": None,
            "metrics": {
                "llm_calls": 0,
                "tool_calls": 0,
                "observation_calls": 0,
                "local_continuations": 0,
                "provider_switches": 0,
                "provider_failures": 0,
            },
        }
    )
    return turn_id


def set_task_id(task_id: str | None) -> None:
    _task_id.set(task_id)
    summary = _turn_summary.get()
    if summary is not None and task_id is not None:
        summary["task_id"] = task_id


def record_summary(**fields: Any) -> None:
    """Accumulate turn-level summary fields for end-of-turn output."""
    summary = _turn_summary.get()
    if summary is None:
        return
    for key, value in fields.items():
        if value is not None:
            summary[key] = value


def record_metric(name: str, amount: int = 1) -> None:
    """Increment a bounded turn metric used by runtime acceptance diagnostics."""
    summary = _turn_summary.get()
    if summary is None:
        return
    metrics = summary.setdefault("metrics", {})
    key = str(name).strip()
    if not key:
        return
    try:
        delta = int(amount)
    except (TypeError, ValueError):
        return
    metrics[key] = max(0, int(metrics.get(key, 0)) + delta)


def current_turn_metrics() -> dict[str, int]:
    """Return a copy of current turn metrics for diagnostics/tests."""
    summary = _turn_summary.get()
    if summary is None:
        return {}
    return {str(k): int(v) for k, v in dict(summary.get("metrics") or {}).items()}


def boundary(component: str, phase: str, **fields: Any) -> None:
    """Log an ENTER/EXIT or intermediate pipeline boundary."""
    trace(f"{component}.{phase}", source_component=component, **fields)


def emit_turn_summary() -> None:
    """Print a compact end-of-turn diagnostic summary."""
    summary = _turn_summary.get()
    if summary is None:
        return

    turn = summary.get("turn_id") or _turn_id.get() or "unknown"
    lines = [
        "TURN SUMMARY",
        f"turn_id={turn}",
        f"task_id={summary.get('task_id')}",
        f"intent={summary.get('intent')}",
        f"plan={summary.get('plan')}",
        f"executed_action={summary.get('executed_action')}",
        f"execution_success={summary.get('execution_success')}",
        f"task_status_before={summary.get('task_status_before')}",
        f"task_status_after={summary.get('task_status_after')}",
        f"final_response_generated={summary.get('final_response_generated')}",
        f"exception={summary.get('exception')}",
    ]
    message = "\n".join(lines)
    logger.info(message)


def next_llm_call_id() -> str:
    seq = _llm_call_seq.get() + 1
    _llm_call_seq.set(seq)
    turn = _turn_id.get() or "no-turn"
    llm_id = f"{turn}-llm{seq}"
    _llm_call_id.set(llm_id)
    return llm_id


def current_turn_id() -> str | None:
    return _turn_id.get()


def current_task_id() -> str | None:
    return _task_id.get()


def current_llm_call_id() -> str | None:
    return _llm_call_id.get()


# ---------------------------------------------------------------------------
# Safe serialization
# ---------------------------------------------------------------------------


def _sanitize_string(value: str) -> str:
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("<REDACTED>", redacted)
    redacted = _KEY_VALUE_SECRET_PATTERN.sub(r"\1<REDACTED>", redacted)
    return redacted


sanitize_string = _sanitize_string



def _maybe_truncate(value: str) -> str:
    if len(value) <= _MAX_FULL_LEN:
        return value
    return (
        value[:_MAX_FULL_LEN]
        + f" <TRUNCATED_FOR_LOGGING original_length={len(value)}>"
    )


def safe_repr(value: Any) -> str:
    """JSON-safe representation; redacts secrets; marks huge values."""
    try:
        serialized = _serialize_value(value)
        text = json.dumps(
            serialized,
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    except Exception:
        text = repr(value)

    text = _sanitize_string(text)
    return _maybe_truncate(text)


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return _sanitize_string(value)

    if isinstance(value, Enum):
        return value.value

    if is_dataclass(value):
        return {
            key: _serialize_value(val)
            for key, val in asdict(value).items()
        }

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, val in value.items():
            key_str = str(key)
            if _SECRET_KEY_PATTERN.search(key_str):
                result[key_str] = "<REDACTED>"
            else:
                result[key_str] = _serialize_value(val)
        return result

    if isinstance(value, (list, tuple, set)):
        return [_serialize_value(item) for item in value]

    if hasattr(value, "__dict__"):
        return {
            key: _serialize_value(val)
            for key, val in vars(value).items()
            if not key.startswith("_")
        }

    return str(value)


def llm_response_to_dict(response: Any) -> dict[str, Any]:
    return {
        "text": getattr(response, "text", None),
        "provider": getattr(response, "provider", None),
        "model": getattr(response, "model", None),
        "input_tokens": getattr(response, "input_tokens", None),
        "output_tokens": getattr(response, "output_tokens", None),
        "latency_ms": getattr(response, "latency_ms", None),
        "finish_reason": getattr(response, "finish_reason", None),
        "request_id": getattr(response, "request_id", None),
    }


# ---------------------------------------------------------------------------
# Trace logging
# ---------------------------------------------------------------------------


def _format_prefix(stage: str) -> str:
    parts = ["[TRACE]"]
    turn = _turn_id.get()
    if turn:
        parts.append(f"[turn={turn}]")
    task = _task_id.get()
    if task:
        parts.append(f"[task={task}]")
    llm = _llm_call_id.get()
    if llm:
        parts.append(f"[llm={llm}]")
    parts.append(f"[stage={stage}]")
    return "".join(parts)


def trace(stage: str, **fields: Any) -> None:
    """Emit one structured trace line for a pipeline boundary."""
    prefix = _format_prefix(stage)
    pieces: list[str] = []

    for key, value in fields.items():
        if value is None:
            continue

        if isinstance(value, str) and not value:
            pieces.append(f"{key}=\"\"")
            continue

        if isinstance(value, (dict, list)):
            pieces.append(f"{key}={safe_repr(value)}")
        elif isinstance(value, Enum):
            pieces.append(f"{key}={value.value}")
        else:
            text = str(value)
            text = _sanitize_string(text)
            if "\n" in text or len(text) > 120:
                pieces.append(f"{key}={safe_repr(text)}")
            else:
                pieces.append(f"{key}={text!r}")

    message = f"{prefix} " + " ".join(pieces) if pieces else prefix
    logger.info(message)


def trace_exception(
    stage: str,
    exc: BaseException,
    **fields: Any,
) -> None:
    """Log a pipeline exception with full traceback."""
    record_summary(
        exception=f"{type(exc).__name__}: {exc}",
    )
    trace(
        stage,
        exception_class=type(exc).__name__,
        exception_message=str(exc),
        traceback=traceback.format_exc(),
        **fields,
    )
    logger.exception(
        "[TRACE][stage=%s] %s: %s",
        stage,
        type(exc).__name__,
        exc,
    )
