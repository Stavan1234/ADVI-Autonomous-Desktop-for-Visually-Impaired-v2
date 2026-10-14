"""No-progress detection for GUI action loops.

A dispatched click or keystroke only proves the OS accepted the input. The
runtime log shows a WhatsApp task spending 25 tool calls (8 of them screen
reads returning the same chat list) before hitting the hard call limit. This
module compares each screen observation with the previous observation of the
same kind; if state-changing actions happened in between and the screen did not
change, that is a stall. Stalls are reported to the model, and repeated stalls
end the task with an honest account instead of a tool-limit error.

State is a plain dict so it can live in ``TaskState.context``.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any

# Observations that describe what is currently on screen. File/app discovery
# results are not screen state and are deliberately excluded.
SCREEN_OBSERVATION_TOOLS = frozenset({"read_screen", "inspect_window", "inspect_web_page", "read_web_page", "observe_window"})

# Normalized similarity at or above this means "the screen did not change".
# OCR of an unchanged window varies slightly between captures (cursor, clock).
UNCHANGED_SIMILARITY = 0.95
MAX_STALLS = 3
_MAX_CHARS = 4000


def new_state() -> dict[str, Any]:
    return {"actions": 0, "stalls": 0, "last": {}}


def normalize_observation(output: Any) -> str:
    if isinstance(output, str):
        text = output
    else:
        try:
            text = json.dumps(output, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(output)
    text = text.casefold()
    # Clock times, counters, and video positions change without the UI moving.
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_CHARS]


def record_action(state: dict[str, Any]) -> None:
    state["actions"] = int(state.get("actions", 0)) + 1


def record_observation(state: dict[str, Any], tool: str, output: Any) -> dict[str, Any] | None:
    """Record a screen observation; return a progress verdict when one applies.

    Returns None when no state-changing action happened since the previous
    observation of this kind (nothing to judge), otherwise a dict with
    ``screen_changed``, ``actions_without_change`` and ``stalls``.
    """
    if tool not in SCREEN_OBSERVATION_TOOLS:
        return None
    current = normalize_observation(output)
    last_by_tool: dict[str, Any] = state.setdefault("last", {})
    previous = last_by_tool.get(tool)
    actions = int(state.get("actions", 0))
    last_by_tool[tool] = {"text": current, "actions": actions}
    if not previous:
        return None
    actions_between = actions - int(previous.get("actions", 0))
    if actions_between <= 0:
        return None
    similarity = difflib.SequenceMatcher(None, previous.get("text", ""), current, autojunk=False).ratio()
    changed = similarity < UNCHANGED_SIMILARITY
    state["stalls"] = 0 if changed else int(state.get("stalls", 0)) + 1
    return {
        "screen_changed": changed,
        "actions_without_change": 0 if changed else actions_between,
        "stalls": state["stalls"],
        "similarity": round(similarity, 3),
    }


def is_stuck(state: dict[str, Any]) -> bool:
    return int(state.get("stalls", 0)) >= MAX_STALLS
