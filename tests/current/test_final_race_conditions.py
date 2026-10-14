from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from advi.agent.provider_session import ProviderSession
from advi.io.output import OutputManager, AdviResponse
from advi.integrations.mcp.manager import _AsyncMCPRuntime
from advi.providers.errors import LLMTimeoutError


class _FakeNative:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProvider:
    name = "fake"
    model = "fake-model"


def test_provider_timeout_defers_native_close_until_worker_finishes() -> None:
    native = _FakeNative()
    session = ProviderSession(_FakeProvider(), "system", [], native=native, turn_deadline_seconds=0.03)

    def slow_call():
        time.sleep(0.12)
        return "late"

    with pytest.raises(LLMTimeoutError):
        session._run_with_deadline(slow_call)

    assert session.closed is True
    assert native.closed is False
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not native.closed:
        time.sleep(0.01)
    assert native.closed is True


def test_output_progress_cancel_does_not_speak_after_stop() -> None:
    class FakeTTS:
        def __init__(self) -> None:
            self.calls: list[str] = []
        def speak(self, text: str) -> bool:
            self.calls.append(text)
            return True
        def close(self) -> None:
            pass

    tts = FakeTTS()
    output = OutputManager(tts=tts)
    try:
        output.start_progress("working", delay=0.08)
        output.stop_progress()
        time.sleep(0.14)
        assert tts.calls == []
    finally:
        output.close()


def test_mcp_start_close_race_does_not_leave_runtime_thread() -> None:
    runtime = _AsyncMCPRuntime({})

    starter = threading.Thread(target=runtime._ensure_started)
    starter.start()
    runtime.close()
    starter.join(timeout=2.0)

    thread = runtime._thread
    assert starter.is_alive() is False
    assert thread is None or not thread.is_alive()


def test_mcp_close_never_constructs_cleanup_coroutine_in_caller(monkeypatch) -> None:
    runtime = _AsyncMCPRuntime({})

    class FakeLoop:
        def __init__(self) -> None:
            self.stop_requested = False

        def is_running(self):
            return True

        def call_soon_threadsafe(self, callback):
            self.stop_requested = True
            assert callback is self.stop

        def stop(self):
            pass

    loop = FakeLoop()
    runtime._loop = loop
    runtime._thread = threading.current_thread()

    async def fail_if_called():
        raise AssertionError("cleanup coroutine must be owned by the worker loop")

    monkeypatch.setattr(runtime, "_close_async", fail_if_called)
    runtime.close()

    assert loop.stop_requested is True
