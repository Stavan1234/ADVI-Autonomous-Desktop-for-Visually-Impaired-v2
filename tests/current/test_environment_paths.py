from pathlib import PosixPath

from advi.environment import environment


def test_windows_known_folder_paths_are_authoritative(monkeypatch, tmp_path):
    monkeypatch.setattr(environment.os, "name", "nt")
    # Keep pathlib usable on the Linux test runner while simulating Windows logic.
    monkeypatch.setattr(environment, "Path", PosixPath)
    home = tmp_path / "RealUser"
    home.mkdir()
    monkeypatch.setattr(environment.Path, "home", classmethod(lambda cls: home))
    # Keep the simulated USERPROFILE POSIX-shaped because this test deliberately
    # swaps pathlib.Path for PosixPath while exercising the Windows branch.
    monkeypatch.setenv("USERPROFILE", "/simulated/Public")
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))

    known = {
        "B4BFCC3A-DB2C-424C-B029-7FE99A87C641": home / "DesktopReal",
        "FDD39AD0-238F-46AF-ADB4-6C85480369C7": home / "DocumentsReal",
        "374DE290-123F-4565-9164-39C4925E467B": home / "DownloadsReal",
    }
    monkeypatch.setattr(environment, "_windows_known_folder", lambda guid: known[guid])
    paths = environment.get_environment_paths()

    assert paths.user_home == home.resolve()
    assert paths.desktop == (home / "DesktopReal").resolve()
    assert paths.documents == (home / "DocumentsReal").resolve()
    assert paths.downloads == (home / "DownloadsReal").resolve()
