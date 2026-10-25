"""Shared test fixtures."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_real_recycle_bin(monkeypatch):
    """Deletes in tests remove temp files directly instead of filling the user's Recycle Bin."""
    from advi.capabilities.files.executor import FileCapability

    trashed: list[str] = []

    def fake_trash(path: Path):
        path = Path(path)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        trashed.append(str(path))
        return True, None

    monkeypatch.setattr(FileCapability, "_trash", staticmethod(fake_trash))
    return trashed


@pytest.fixture(autouse=True)
def _default_local_command_mode(monkeypatch):
    """A developer's .env (ADVI_LOCAL_COMMANDS=first/off) must not change test behaviour."""
    monkeypatch.delenv("ADVI_LOCAL_COMMANDS", raising=False)


@pytest.fixture(autouse=True)
def _no_real_clipboard(monkeypatch):
    """Tests never read or overwrite the user's clipboard; typing falls back to key presses."""
    from advi.capabilities.desktop.executor import DesktopCapability

    monkeypatch.setattr(DesktopCapability, "_windows_clipboard_text", staticmethod(lambda: None))
    monkeypatch.setattr(DesktopCapability, "_set_windows_clipboard_text", staticmethod(lambda text: False))


@pytest.fixture(autouse=True)
def _no_real_vision(monkeypatch):
    """Tests never screenshot the desktop or call Gemini vision unless they inject a describer."""
    monkeypatch.setenv("ADVI_VISION", "off")


@pytest.fixture(autouse=True)
def _no_real_voice(monkeypatch):
    """Tests never open the microphone, register the hotkey, load Whisper, or start the wake listener."""
    monkeypatch.setenv("ADVI_VOICE", "off")
    monkeypatch.setenv("ADVI_WAKE", "off")
