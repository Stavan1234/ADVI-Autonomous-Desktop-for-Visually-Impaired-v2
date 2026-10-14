from __future__ import annotations

from pathlib import Path

from advi.core.build_info import source_fingerprint


def test_source_fingerprint_changes_when_production_source_changes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = root / "src" / "advi"
    source.mkdir(parents=True)
    (source / "a.py").write_text("value = 1\n", encoding="utf-8")
    (source / "core.py").write_text("value = 2\n", encoding="utf-8")

    first = source_fingerprint(root)
    (source / "a.py").write_text("value = 99\n", encoding="utf-8")
    second = source_fingerprint(root)

    assert first != second


def test_source_fingerprint_ignores_generated_pycache(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = root / "src" / "advi"
    source.mkdir(parents=True)
    (source / "a.py").write_text("value = 1\n", encoding="utf-8")
    first = source_fingerprint(root)
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-313.pyc").write_bytes(b"garbage")
    assert source_fingerprint(root) == first
