from __future__ import annotations

import pytest

from pathlib import Path

from advi.core import runtime as runtime_module
from advi.core.runtime import Runtime


class Resource:
    def __init__(self, name: str, events: list[str], fail: bool = False):
        self.name = name
        self.events = events
        self.fail = fail

    def close(self) -> None:
        self.events.append(self.name)
        if self.fail:
            raise RuntimeError(f"close failed: {self.name}")


def test_shutdown_closes_all_resources_after_one_fails():
    runtime = Runtime(settings=object(), started=True)
    events: list[str] = []
    runtime.register_resource(Resource("first", events))
    runtime.register_resource(Resource("second", events, fail=True))
    runtime.register_resource(Resource("third", events))

    runtime.shutdown()
    runtime.shutdown()

    assert events == ["third", "second", "first"]
    assert runtime.started is False
    assert runtime.last_shutdown_errors


def test_startup_exception_leaves_runtime_stopped(monkeypatch):
    def explode(_):
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(runtime_module, "configure_logging", explode)
    runtime = Runtime(settings=object())

    with pytest.raises(RuntimeError, match="logging unavailable"):
        runtime.start()

    assert runtime.started is False
    assert "RuntimeError" in (runtime.last_start_error or "")


def test_runtime_context_manager_always_shutdowns():
    events: list[str] = []
    settings = type("Settings", (), {"piper_exe": Path("piper"), "piper_model": Path("model"), "groq_api_key": None, "gemini_api_key": None})()
    with Runtime(settings=settings) as runtime:
        runtime.register_resource(Resource("only", events))
        assert runtime.started is True

    assert runtime.started is False
    assert events == ["only"]
