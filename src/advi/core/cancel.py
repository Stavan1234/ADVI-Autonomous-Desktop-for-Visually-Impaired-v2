"""A process-wide "the user said stop" flag that long-running tools check.

AgentSession.request_stop() sets it (a voice "stop" while ADVI works); each new
request clears it. A computer-wide file search ran for four minutes while the
user kept saying "stop" (live log 2026-10-24), because the stop was only
checked between tool calls.
"""

from __future__ import annotations

import threading

STOP = threading.Event()


def requested() -> bool:
    return STOP.is_set()
