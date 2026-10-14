from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .contracts import TaskStatus


class TurnKind(str, Enum):
    NEW_TASK = "new_task"
    CONTINUE_TASK = "continue_task"
    CONFIRM_PENDING_ACTION = "confirm_pending_action"
    CANCEL_TASK = "cancel_task"
    QUERY_STATE = "query_state"
    CONVERSATION = "conversation"


@dataclass(frozen=True)
class TurnClassification:
    """Deterministic classification of one user turn at the interaction boundary."""

    kind: TurnKind
    normalized_text: str
    evidence: str

    @property
    def is_task_boundary(self) -> bool:
        return self.kind in {TurnKind.NEW_TASK, TurnKind.CONTINUE_TASK}


def classify_turn(
    text: str,
    *,
    task_status: TaskStatus | str | None = None,
    has_pending_confirmation: bool = False,
    has_pending_david_send: bool = False,
) -> TurnClassification:
    normalized = _normalize(text)
    if not normalized:
        return TurnClassification(TurnKind.CONVERSATION, normalized, "empty")

    status = _coerce_status(task_status)
    has_pending_action = bool(has_pending_confirmation or has_pending_david_send)
    has_active_task = status in {
        TaskStatus.IN_PROGRESS,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.AWAITING_CONFIRMATION,
        TaskStatus.FAILED,
        TaskStatus.UNCERTAIN,
    }

    # Pending confirmations own affirmative/negative short replies. A bare
    # "yes" without a pending action must not accidentally execute anything.
    if has_pending_action and _is_confirmation(normalized):
        return TurnClassification(TurnKind.CONFIRM_PENDING_ACTION, normalized, "pending-action confirmation")
    if has_pending_action and _is_cancel(normalized):
        return TurnClassification(TurnKind.CANCEL_TASK, normalized, "pending-action cancellation")

    # A cancellation is meaningful whenever there is an active/pending task.
    # Keep this before QUERY_STATE/NEW_TASK so verbose phrases such as
    # "cancel this task" are handled locally and immediately.
    if has_active_task and _is_cancel(normalized):
        return TurnClassification(TurnKind.CANCEL_TASK, normalized, "active-task cancellation")

    if has_active_task and _is_continuation(normalized):
        return TurnClassification(TurnKind.CONTINUE_TASK, normalized, "explicit task continuation")

    # An imperative command owns the turn even if its payload contains words
    # such as "capabilities", "email", "system", or "files" that also occur
    # in deterministic query patterns.
    if _is_new_task(normalized):
        return TurnClassification(TurnKind.NEW_TASK, normalized, "explicit action request")

    if _is_state_query(normalized):
        return TurnClassification(TurnKind.QUERY_STATE, normalized, "deterministic/state query")

    # A correction such as "I meant save those details" is still actionable.
    # Do not let it fall into generic conversation merely because it is not a
    # canonical imperative sentence.
    if _is_correction_with_action(normalized):
        return TurnClassification(TurnKind.NEW_TASK, normalized, "corrective action request")

    return TurnClassification(TurnKind.CONVERSATION, normalized, "no deterministic task/query signal")


def _normalize(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "").casefold().strip())
    return value


def _coerce_status(value: TaskStatus | str | None) -> TaskStatus | None:
    if value is None:
        return None
    if isinstance(value, TaskStatus):
        return value
    try:
        return TaskStatus(str(value))
    except ValueError:
        return None


def _is_confirmation(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:(?:yes|yeah|yep)(?:\s*,?\s*(?:do it|go ahead))?|"
            r"confirm(?:ed)?|do it|go ahead|okay|ok)",
            text,
            re.I,
        )
    )


def _is_cancel(text: str) -> bool:
    if re.fullmatch(r"(?:no|nope|cancel|don't|do not|stop|abort|never mind)", text, re.I):
        return True
    if re.search(r"\b(?:cancel|abort|stop)\b", text):
        if re.search(r"\b(?:do not|don't)\s+(?:cancel|stop|abort)\b", text):
            return False
        return True
    return bool(
        re.search(
            r"\b(?:i|i'm|i am)\s+(?:don't|do not|dont)\s+(?:want|wish)\s+to\s+(?:send|continue|do|proceed)\b",
            text,
        )
    )


def _is_continuation(text: str) -> bool:
    if text in {
        "continue",
        "resume",
        "go on",
        "carry on",
        "finish it",
        "finish",
        "continue the task",
        "try again",
        "retry",
    }:
        return True
    return bool(re.match(r"^(?:try again|retry|continue|resume|go on|carry on)\b", text, re.I))


def _is_state_query(text: str) -> bool:
    # Keep this intentionally narrow: the goal is to capture deterministic
    # local facts/state, not to turn every question into a special route.
    patterns = (
        r"\b(?:what|which|show|list)\b.*\b(?:task|tasks)\b.*\b(?:active|running|pending|current|status)\b",
        r"\bwhat\s+task(?:s)?\s+(?:are|is)\s+(?:active|running|pending|current)\b",
        r"\b(?:what|what's|tell me)\b.*\b(?:my|david|anurodh|joel)\b.*\bemail\b",
        r"\b(?:what|which|tell me|show me)\b.*\b(?:last|previous)\b.*\b(?:ask|asked|request|requested)\b",
        r"\bwhat\s+(?:was|is)\s+(?:the\s+)?last\s+(?:thing|request)\s+i\s+asked\b",
        r"\b(?:system information|system info|system status|battery status|battery)\b",
        r"\b(?:what can you do|what are your capabilities|list your capabilities|your capabilities)\b",
        r"\b(?:what|which|show|list)\b.*\b(?:file|files|folder|folders)\b.*\b(?:exist|there|available|on (?:my )?(?:desktop|computer))\b",
        r"\b(?:read|show|tell me)\b.*\b(?:email|draft|subject|body|message)\b",
    )
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _is_new_task(text: str) -> bool:
    # Strong imperative/action verbs. This deliberately does not inspect nouns
    # inside the payload, so "include your capabilities list in the email" does
    # not become a capability-query turn.
    return bool(
        re.search(
            r"^(?:please\s+)?(?:open|launch|start|run|send|draft|compose|create|make|delete|remove|search|find|read|write|edit|rename|move|copy|play|watch|navigate|close|quit|shutdown|restart|sleep|download|upload|set|change|update|remember|forget|save|add|store)\b",
            text,
            re.I,
        )
        or re.search(
            r"\b(?:inside|within|under)\b.*\b(?:create|make|write|save|add)\b",
            text,
            re.I,
        )
        or re.search(
            r"\b(?:in|inside|within|under)\b.*\b(?:folder|directory)\b.*\b(?:create|make|write|save|add)\b",
            text,
            re.I,
        )
        # Natural imperative framing used in the runtime log.  These are task
        # requests, not conversational commentary, so an old uncertain task must
        # not absorb them.
        or re.search(
            r"^(?:you have to|you need to)\b.*\b(?:open|launch|start|search|find|create|make|read|write|play|send|download|upload|close)\b",
            text,
            re.I,
        )
    )


def _is_correction_with_action(text: str) -> bool:
    return bool(
        re.match(r"^(?:i meant|i mean|actually|no,|no\s+actually)\b", text, re.I)
        and re.search(
            r"\b(?:open|launch|start|run|send|draft|compose|create|make|delete|remove|search|find|read|write|edit|rename|move|copy|play|watch|navigate|remember|forget|save|add|store)\b",
            text,
            re.I,
        )
    )
