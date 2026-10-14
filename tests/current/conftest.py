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
