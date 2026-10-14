from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
from typing import Any

import shutil


@dataclass(frozen=True)
class LaunchTarget:
    name: str
    executable: Path | None = None
    shortcut: Path | None = None
    shell_target: str | None = None


# Only these model-facing aliases are directly executable without an installed-app inventory.
BUILTIN_APPLICATIONS: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "cmd": "cmd.exe",
    "explorer": "explorer.exe",
    "paint": "mspaint.exe",
    "word": "winword.exe",
    "microsoft word": "winword.exe",
    "excel": "excel.exe",
    "microsoft excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "microsoft powerpoint": "powerpnt.exe",
}

_COMMON_WINDOWS_APP_PATHS: dict[str, tuple[str, ...]] = {
    "winword.exe": tuple(
        path for version in ("16", "15", "14")
        for path in (rf"Microsoft Office\root\Office{version}\WINWORD.EXE", rf"Microsoft Office\Office{version}\WINWORD.EXE")
    ),
    "excel.exe": tuple(
        path for version in ("16", "15", "14")
        for path in (rf"Microsoft Office\root\Office{version}\EXCEL.EXE", rf"Microsoft Office\Office{version}\EXCEL.EXE")
    ),
    "powerpnt.exe": tuple(
        path for version in ("16", "15", "14")
        for path in (rf"Microsoft Office\root\Office{version}\POWERPNT.EXE", rf"Microsoft Office\Office{version}\POWERPNT.EXE")
    ),
}


def _builtin_executable(name: str) -> Path | None:
    executable = BUILTIN_APPLICATIONS.get(_normalize(name))
    if executable is None or os.name != "nt":
        return None

    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / executable
    try:
        if system32.is_file():
            return system32.resolve(strict=True)
    except (OSError, RuntimeError):
        pass

    # Office applications are not stored in System32. Check the canonical
    # Microsoft Office installation roots before relying on the installed-app
    # inventory.
    program_roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
    ]
    for root in program_roots:
        for relative in _COMMON_WINDOWS_APP_PATHS.get(executable, ()):
            candidate = root / relative
            try:
                if candidate.is_file():
                    return candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
    return None

_ALLOWED_EXECUTABLE_SUFFIXES = {".exe", ".com"}


def normalize_application_name(name: str) -> str:
    """Canonical human/application name used by inventory and launch resolution."""
    value = str(name).strip().casefold().replace("_", " ")
    if value.endswith(".exe"):
        value = value[:-4]
    return " ".join(value.split())


def _normalize(name: str) -> str:
    return normalize_application_name(name)


def _clean_registry_executable(value: str | None) -> Path | None:
    if not value:
        return None
    raw = str(value).strip().strip('"')
    # Windows uninstall DisplayIcon commonly appends an icon index: app.exe,0
    raw = raw.split(",", 1)[0].strip().strip('"')
    candidate = Path(os.path.expandvars(raw)).expanduser()
    if candidate.suffix.casefold() not in _ALLOWED_EXECUTABLE_SUFFIXES:
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_file() else None


def resolve_launch_target(name: str, application_record: Any = None) -> LaunchTarget | None:
    normalized = _normalize(name)
    if not normalized:
        return None

    builtin = _builtin_executable(name)
    if builtin is not None:
        return LaunchTarget(name=str(name).strip(), executable=builtin)

    # PATH is another legitimate Windows application source (portable tools,
    # developer applications, command-installed apps). Only accept a concrete
    # executable resolved by the OS; never turn arbitrary model text into a
    # shell command.
    if os.name == "nt":
        command_name = normalized + ".exe"
        resolved = shutil.which(command_name) or shutil.which(normalized)
        if resolved:
            candidate = Path(resolved)
            if candidate.is_file() and candidate.suffix.casefold() == ".exe":
                try:
                    return LaunchTarget(name=str(name).strip(), executable=candidate.resolve(strict=True))
                except (OSError, RuntimeError):
                    pass

    if application_record is not None:
        shortcut = getattr(application_record, "path", None)
        if shortcut and str(shortcut).casefold().endswith(".lnk"):
            shortcut_path = Path(shortcut)
            try:
                if shortcut_path.is_file():
                    return LaunchTarget(name=str(name).strip(), shortcut=shortcut_path.resolve())
            except OSError:
                pass

        executable = _clean_registry_executable(getattr(application_record, "executable", None))
        if executable is not None:
            return LaunchTarget(name=str(name).strip(), executable=executable)

        shell_target = getattr(application_record, "command", None)
        if shell_target and str(shell_target).casefold().startswith("shell:appsFolder\\"):
            return LaunchTarget(name=str(name).strip(), shell_target=str(shell_target))

    return None


def launch(target: LaunchTarget) -> tuple[subprocess.Popen[Any] | None, str]:
    if os.name == "nt" and target.shortcut is not None:
        os.startfile(str(target.shortcut))  # type: ignore[attr-defined]
        return None, "start_menu_shortcut"

    if os.name == "nt" and target.shell_target:
        os.startfile(target.shell_target)  # type: ignore[attr-defined]
        return None, "windows_apps_folder"

    if target.executable is None:
        raise ValueError("No executable launch target is available.")

    # Never invoke a model-controlled application through a shell. Passing argv
    # as a sequence also avoids shell metacharacter interpretation.
    executable = str(target.executable)
    proc = subprocess.Popen(
        [executable],
        shell=False,
        creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
    )
    return proc, "executable"
