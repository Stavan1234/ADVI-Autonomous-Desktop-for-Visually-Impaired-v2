from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class EnvironmentPaths:
    """Canonical Windows/user filesystem locations used by all ADVI subsystems."""

    user_home: Path
    desktop: Path
    documents: Path
    downloads: Path
    appdata: Path
    local_appdata: Path
    startup: Path

    def summary(self) -> dict[str, str]:
        return {
            "user_home": str(self.user_home),
            "desktop": str(self.desktop),
            "documents": str(self.documents),
            "downloads": str(self.downloads),
            "appdata": str(self.appdata),
            "local_appdata": str(self.local_appdata),
            "startup": str(self.startup),
        }


def get_environment_paths() -> EnvironmentPaths:
    """Resolve real user folders from Windows known-folder APIs when available."""
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE")
        home = Path.home()
        if profile and not str(profile).replace("\\", "/").rstrip("/").casefold().endswith("/public"):
            try:
                home = Path(profile)
            except (NotImplementedError, OSError, TypeError):
                # Tests may simulate Windows while deliberately monkeypatching
                # pathlib.Path to PosixPath. Keep the test's canonical home
                # instead of constructing an incompatible path implementation.
                home = Path.home()
        # A service/wrapper can occasionally provide USERPROFILE=C:\\Users\\Public.
        # Path.home() is a better fallback when it identifies a different concrete
        # user profile. The Known Folder API remains authoritative for shell folders.
        if home.name.casefold() == "public" and Path.home().name.casefold() != "public":
            home = Path.home()

        appdata = _path_from_environment("APPDATA", home / "AppData" / "Roaming")
        local_appdata = _path_from_environment("LOCALAPPDATA", home / "AppData" / "Local")
        startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

        desktop = _prefer_user_known_folder(_windows_known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641"), home, "Desktop")
        documents = _prefer_user_known_folder(_windows_known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7"), home, "Documents")
        downloads = _prefer_user_known_folder(_windows_known_folder("374DE290-123F-4565-9164-39C4925E467B"), home, "Downloads")
    else:
        home = Path.home()
        appdata = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        local_appdata = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        startup = appdata / "autostart"
        desktop = _resolve_special_dir(home, "Desktop")
        documents = _resolve_special_dir(home, "Documents")
        downloads = _resolve_special_dir(home, "Downloads")

    return EnvironmentPaths(
        user_home=home.resolve(),
        desktop=desktop.resolve(),
        documents=documents.resolve(),
        downloads=downloads.resolve(),
        appdata=appdata.resolve(),
        local_appdata=local_appdata.resolve(),
        startup=startup.resolve(),
    )


def _path_from_environment(variable: str, fallback: Path) -> Path:
    """Convert an environment path without breaking simulated-OS tests."""
    value = os.environ.get(variable)
    if not value:
        return fallback
    try:
        return Path(value)
    except (NotImplementedError, OSError, TypeError):
        return fallback


def _windows_known_folder(guid_text: str) -> Path | None:
    """Return a Windows shell-known folder without relying on environment variables."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        parts = guid_text.strip("{}").split("-")
        if len(parts) != 5:
            return None
        data4 = bytes.fromhex(parts[3] + parts[4])
        guid = GUID(
            int(parts[0], 16),
            int(parts[1], 16),
            int(parts[2], 16),
            (ctypes.c_ubyte * 8).from_buffer_copy(data4),
        )
        path_ptr = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)
        ]
        shell32.SHGetKnownFolderPath.restype = wintypes.HRESULT
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        if hr != 0 or not path_ptr.value:
            return None
        try:
            return Path(path_ptr.value)
        finally:
            ole32.CoTaskMemFree(path_ptr)
    except Exception:
        return None


def _prefer_user_known_folder(candidate: Path | None, home: Path, name: str) -> Path:
    """Accept a shell-known folder unless Windows returned the Public profile by mistake."""
    if candidate is not None:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        public_profile = str(resolved).casefold().replace("/", "\\")
        if "\\users\\public\\" not in public_profile:
            return resolved
    return _resolve_special_dir(home, name, "OneDrive")


def _resolve_special_dir(home: Path, name: str, *_unused: str) -> Path:
    """Prefer the real user directory but tolerate systems with redirected folders."""
    direct = home / name
    if direct.exists():
        return direct.resolve()

    if os.name == "nt":
        one_drive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        if one_drive:
            redirected = Path(one_drive) / name
            if redirected.exists():
                return redirected.resolve()

    return direct.resolve()
