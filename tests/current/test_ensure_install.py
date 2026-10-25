"""scripts/ensure_install.py: after `git pull`, run_advi.bat installs what the new code needs, once."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ensure_install.py"


@pytest.fixture
def updater(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ensure_install", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "pyproject.toml").write_text('dependencies = ["a"]\n', encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", tmp_path / ".venv" / "advi_install_state.json")
    monkeypatch.setattr(module, "say", lambda text: None)
    calls = []
    codes = {}

    def call(args, **kwargs):
        calls.append(args[1:])
        return codes.get(tuple(args[1:4]), 0)

    monkeypatch.setattr(module.subprocess, "call", call)
    monkeypatch.delenv("ADVI_WAKE", raising=False)
    return module, calls, codes, tmp_path


def test_nothing_changed_means_nothing_runs(updater):
    module, calls, _, _ = updater
    module.mark()
    assert module.main([]) == 0 and calls == []


def test_a_changed_pyproject_installs_once_in_order(updater):
    module, calls, _, root = updater
    module.mark()
    (root / "pyproject.toml").write_text('dependencies = ["a", "livekit"]\n', encoding="utf-8")
    assert module.main([]) == 0
    assert calls[0] == ["-m", "advi.wake", "stop"]  # the listener holds package DLLs open
    assert calls[1][:3] == ["-m", "pip", "install"] and calls[1][-2:] == ["-e", ".[dev]"]
    assert calls[-1] == ["-m", "advi.wake", "install"]
    assert sum(1 for c in calls if c and c[0] == "-c") == 2  # speech + wake models
    assert module.main([]) == 0 and len(calls) == 5  # recorded: not again


def test_an_old_checkout_without_any_record_is_updated(updater):
    module, calls, _, _ = updater
    assert module.main([]) == 0 and any(c[:2] == ["-m", "pip"] for c in calls)


def test_wake_off_skips_the_wake_word(updater, monkeypatch):
    module, calls, _, _ = updater
    monkeypatch.setenv("ADVI_WAKE", "off")
    module.main([])
    assert ["-m", "advi.wake", "install"] not in calls and sum(1 for c in calls if c[0] == "-c") == 1


def test_wake_off_in_dotenv_counts_too(updater):
    module, calls, _, root = updater
    (root / ".env").write_text("GROQ_API_KEY=\nADVI_WAKE=off\n", encoding="utf-8")
    module.main([])
    assert ["-m", "advi.wake", "install"] not in calls


def test_a_failed_package_install_stops_and_is_retried_next_time(updater):
    module, calls, codes, _ = updater
    codes[("-m", "pip", "install")] = 1
    assert module.main([]) == 1 and not module.STATE.exists()
    codes.clear()
    assert module.main([]) == 0 and module.STATE.exists()


def test_setup_marks_without_installing(updater):
    module, calls, _, _ = updater
    assert module.main(["--mark"]) == 0 and calls == [] and module.recorded() == module.fingerprint()
