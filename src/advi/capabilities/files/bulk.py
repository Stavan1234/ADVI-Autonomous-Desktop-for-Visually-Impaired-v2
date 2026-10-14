"""Bulk file operations with an undo journal.

Moving files one tool call at a time cost one rate-limited LLM round per file;
"move all these PDFs into a PDF folder" died after the first file (runtime log,
2026-10-14). These operations run as one call, never overwrite (name clashes get
" (2)" suffixes), verify the result on disk, and record every move so the last
batch can be undone.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Documents": ("pdf", "doc", "docx", "odt", "rtf", "txt", "md", "xls", "xlsx", "csv", "ppt", "pptx", "epub"),
    "Images": ("jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "svg", "tif", "tiff"),
    "Videos": ("mp4", "mkv", "avi", "mov", "wmv", "webm", "m4v"),
    "Audio": ("mp3", "wav", "m4a", "flac", "aac", "ogg", "wma"),
    "Archives": ("zip", "rar", "7z", "tar", "gz", "bz2", "xz"),
    "Installers": ("exe", "msi", "msix", "appx", "msixbundle"),
}
_EXT_TO_CATEGORY = {ext: folder for folder, exts in CATEGORIES.items() for ext in exts}
_MAX_JOURNAL_BATCHES = 20
# Browsers still writing a download; moving it would break the download.
_IN_PROGRESS_SUFFIXES = (".crdownload", ".part", ".tmp", ".partial")


def journal_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ADVI"
    return base / "file_journal.json"


class FileJournal:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or journal_path())

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, batches: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(batches[-_MAX_JOURNAL_BATCHES:], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, operation: str, moves: list[tuple[str, str]], created_dirs: Iterable[str] = ()) -> str:
        batch_id = uuid.uuid4().hex[:10]
        batches = self._load()
        batches.append({
            "id": batch_id, "operation": operation, "time": time.time(),
            "moves": [list(pair) for pair in moves], "created_dirs": list(created_dirs),
        })
        self._save(batches)
        return batch_id

    def last(self) -> dict[str, Any] | None:
        batches = self._load()
        return batches[-1] if batches else None

    def drop_last(self) -> None:
        batches = self._load()
        if batches:
            self._save(batches[:-1])


def _free_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    for number in range(2, 1000):
        candidate = directory / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"No free name for {name} in {directory}")


def _normalize_extensions(extensions: Any) -> set[str]:
    if not extensions:
        return set()
    if isinstance(extensions, str):
        extensions = extensions.replace(";", ",").split(",")
    chosen: set[str] = set()
    for ext in extensions:
        word = str(ext).strip().lstrip("*").lstrip(".").casefold()
        if not word:
            continue
        category = _CATEGORY_WORDS.get(word)
        chosen.update(CATEGORIES[category] if category else (word,))
    return chosen


# "move all images / photos / videos / songs" name a kind of file, not an extension.
_CATEGORY_WORDS = {
    **{w: "Images" for w in ("image", "images", "photo", "photos", "picture", "pictures", "pic", "pics")},
    **{w: "Videos" for w in ("video", "videos", "movie", "movies")},
    **{w: "Audio" for w in ("audio", "music", "song", "songs", "mp3s")},
    **{w: "Documents" for w in ("document", "documents", "doc", "docs")},
    **{w: "Archives" for w in ("archive", "archives", "zips", "compressed")},
    **{w: "Installers" for w in ("installer", "installers", "setup", "setups")},
}


def select_files(source: Path, *, extensions: Any = None, name_contains: str | None = None) -> list[Path]:
    exts = _normalize_extensions(extensions)
    needle = (name_contains or "").casefold().strip()
    chosen = []
    for entry in sorted(source.iterdir(), key=lambda p: p.name.casefold()):
        if not entry.is_file() or entry.name.casefold().endswith(_IN_PROGRESS_SUFFIXES):
            continue
        if exts and entry.suffix.lstrip(".").casefold() not in exts:
            continue
        if needle and needle not in entry.name.casefold():
            continue
        chosen.append(entry)
    return chosen


def move_batch(plan: list[tuple[Path, Path]], *, operation: str, journal: FileJournal | None = None) -> dict[str, Any]:
    """Move each (file, destination_dir) pair; verify on disk; journal what moved."""
    journal = journal or FileJournal()
    moved: list[tuple[str, str]] = []
    created: list[str] = []
    failed: list[dict[str, str]] = []
    for source, directory in plan:
        try:
            if not directory.exists():
                directory.mkdir(parents=True)
                created.append(str(directory))
            target = _free_destination(directory, source.name)
            shutil.move(str(source), str(target))
            if target.exists() and not source.exists():
                moved.append((str(source), str(target)))
            else:
                failed.append({"file": source.name, "error": "not found at destination after move"})
        except OSError as exc:
            failed.append({"file": source.name, "error": str(exc)[:160]})
    batch_id = journal.record(operation, moved, created) if (moved or created) else None
    return {"moved": moved, "failed": failed, "created_dirs": created, "batch_id": batch_id}


def organize_plan(folder: Path) -> tuple[list[tuple[Path, Path]], dict[str, int], int]:
    """Top-level files grouped into category subfolders; unknown types stay put."""
    plan: list[tuple[Path, Path]] = []
    counts: dict[str, int] = {}
    left = 0
    for entry in select_files(folder):
        category = _EXT_TO_CATEGORY.get(entry.suffix.lstrip(".").casefold())
        if category is None:
            left += 1
            continue
        plan.append((entry, folder / category))
        counts[category] = counts.get(category, 0) + 1
    return plan, counts, left


def undo_last(journal: FileJournal | None = None) -> dict[str, Any]:
    journal = journal or FileJournal()
    batch = journal.last()
    if batch is None:
        return {"ok": False, "message": "There is no file operation to undo."}
    restored, failed = 0, []
    for source, target in reversed(batch.get("moves") or []):
        source_path, target_path = Path(source), Path(target)
        try:
            if not target_path.exists():
                failed.append({"file": target_path.name, "error": "no longer at its moved location"})
                continue
            if source_path.exists():
                failed.append({"file": source_path.name, "error": "original location is occupied"})
                continue
            source_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target_path), str(source_path))
            restored += 1
        except OSError as exc:
            failed.append({"file": target_path.name, "error": str(exc)[:160]})
    removed_dirs = 0
    for directory in reversed(batch.get("created_dirs") or []):
        try:
            Path(directory).rmdir()  # only if empty
            removed_dirs += 1
        except OSError:
            pass
    if not failed:
        journal.drop_last()
    return {"ok": not failed, "restored": restored, "failed": failed, "removed_dirs": removed_dirs,
            "operation": batch.get("operation"), "batch_id": batch.get("id")}


def send_to_recycle_bin(path: Path) -> tuple[bool, str | None]:
    """Move a file or folder to the Windows Recycle Bin. Never deletes permanently.

    delete_file / delete_folder used unlink() / rmtree(): a misheard voice
    command could destroy data for good. On non-fixed drives (USB, network)
    Windows silently deletes permanently even with "allow undo", so those
    are refused instead.
    """
    if os.name != "nt":
        return False, "Moving to the Recycle Bin is only supported on Windows."
    import ctypes
    from ctypes import wintypes

    path = Path(path).resolve()
    drive = path.drive + "\\"
    if ctypes.windll.kernel32.GetDriveTypeW(drive) != 3:  # DRIVE_FIXED
        return False, f"{drive} has no Recycle Bin, so deleting there would be permanent."

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FLAGS = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None, FLAGS, False, None, None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted:
        return False, f"Windows could not move it to the Recycle Bin (code {code})."
    return (not path.exists()), None if not path.exists() else "It is still there after the Recycle Bin move."
