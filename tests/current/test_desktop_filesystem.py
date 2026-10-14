from __future__ import annotations

from pathlib import Path

from advi.capabilities.desktop.executor import DesktopCapability
from advi.capabilities.files.executor import FileCapability
from advi.environment.watchtower import ApplicationRecord
from advi.capabilities.desktop.context import DesktopContext
from advi.core.verification import ToolVerifier
from advi.tools.contracts import ToolCall


class FakeWatchtower:
    def __init__(self, records=None):
        self.records = records or []
        self.refreshes = 0

    def refresh_applications(self):
        self.refreshes += 1

    def list_applications(self, *, limit=100):
        return [
            {
                "name": record.name,
                "source": record.source,
                "executable": record.executable,
                "path": record.path,
                "running": record.name.casefold() == "notepad",
            }
            for record in self.records[:limit]
        ]

    def find_application(self, name):
        needle = name.casefold()
        return next((r for r in self.records if r.name.casefold() == needle), None)

    def find_running_application(self, name):
        return self.find_application(name)


def test_file_capability_creates_and_opens_folder_and_file(tmp_path, monkeypatch):
    cap = FileCapability()
    cap.desktop_dir = tmp_path

    folder = cap.execute(ToolCall("f1", "create_folder", {"path": "Projects/ADVI"}))
    assert folder.success
    assert (tmp_path / "Projects" / "ADVI").is_dir()
    assert ToolVerifier().verify(ToolCall("f1", "create_folder", {"path": "Projects/ADVI"}), folder).verified

    file_call = ToolCall("f2", "create_file", {"path": "Projects/ADVI/notes.txt", "content": "hello"})
    created = cap.execute(file_call)
    assert created.success and (tmp_path / "Projects" / "ADVI" / "notes.txt").read_text() == "hello"

    opened = []
    monkeypatch.setattr(cap, "_open_with_default_application", lambda path: opened.append(Path(path)) or None)
    # Opening a real Windows resource is verified through the associated window.
    # The test must therefore simulate that observation rather than silently
    # disabling the OS opener and expecting production verification to succeed.
    monkeypatch.setattr(
        cap,
        "_bind_opened_window",
        lambda path, previous_foreground_hwnd=None: (4242, path.name, "notepad.exe", True),
    )
    file_open = cap.execute(ToolCall("f3", "open_file", {"path": "Projects/ADVI/notes.txt"}))
    folder_open = cap.execute(ToolCall("f4", "open_folder", {"path": "Projects/ADVI"}))
    assert file_open.success and folder_open.success
    assert opened == [tmp_path / "Projects" / "ADVI" / "notes.txt", tmp_path / "Projects" / "ADVI"]
    assert ToolVerifier().verify(ToolCall("f3", "open_file", {"path": "Projects/ADVI/notes.txt"}), file_open).verified
    assert ToolVerifier().verify(ToolCall("f4", "open_folder", {"path": "Projects/ADVI"}), folder_open).verified


def test_file_resolution_supports_canonical_user_folders(tmp_path):
    cap = FileCapability()
    cap.desktop_dir = tmp_path / "Desktop"
    assert cap.resolve_path("Desktop/a.txt") == cap.desktop_dir / "a.txt"
    assert cap.resolve_path("Documents/a.txt").parts[-2:] == ("Documents", "a.txt")
    assert cap.resolve_path("Downloads/a.txt").parts[-2:] == ("Downloads", "a.txt")


def test_list_applications_uses_watchtower_catalog():
    records = [
        ApplicationRecord(name="Notepad", source="start_menu", executable=r"C:\Windows\System32\notepad.exe"),
        ApplicationRecord(name="Calculator", source="registry", executable=r"C:\Windows\System32\calc.exe"),
    ]
    watchtower = FakeWatchtower(records)
    cap = DesktopCapability(context=DesktopContext(), environment_watchtower=watchtower)
    result = cap.execute(ToolCall("a1", "list_applications", {}))
    assert result.success
    assert result.verified
    assert [row["name"] for row in result.data] == ["Notepad", "Calculator"]
    assert watchtower.refreshes == 1


def test_open_application_reuses_existing_window_before_launch(monkeypatch):
    record = ApplicationRecord(name="Notepad", source="start_menu", executable=r"C:\Windows\System32\notepad.exe")
    watchtower = FakeWatchtower([record])
    cap = DesktopCapability(context=DesktopContext(), environment_watchtower=watchtower)
    monkeypatch.setattr(cap, "_find_existing_application_window", lambda *_args: 4242)
    monkeypatch.setattr(cap, "_activate_window", lambda hwnd: hwnd == 4242)
    result = cap.execute(ToolCall("a2", "open_application", {"application": "Notepad"}))
    assert result.success
    assert result.metadata["already_running"] is True
    assert result.metadata["hwnd"] == 4242


def test_desktop_app_inventory_action_is_advertised_by_registry():
    from advi.capabilities.registry import Capability, CapabilityRegistry

    registry = CapabilityRegistry()
    registry.register(Capability(
        name="desktop_control",
        description="desktop",
        supported_actions={"list_applications"},
    ))
    spec = registry.get_action_spec("list_applications")
    assert spec is not None
    assert spec.verification == "application_inventory"


def test_folder_copy_and_move_to_existing_directory(tmp_path):
    from advi.capabilities.files.executor import FileCapability
    from advi.tools.contracts import ToolCall
    src = tmp_path / "Source"
    src.mkdir()
    (src / "note.txt").write_text("hello", encoding="utf-8")
    container = tmp_path / "Container"
    container.mkdir()
    cap = FileCapability()

    copied = cap.execute(ToolCall("c1", "copy_folder", {"source": str(src), "destination": str(container)}))
    assert copied.success
    assert (container / "Source" / "note.txt").read_text(encoding="utf-8") == "hello"

    destination_container = tmp_path / "Destination"
    destination_container.mkdir()
    moved = cap.execute(ToolCall("m1", "move_folder", {"source": str(container / "Source"), "destination": str(destination_container)}))
    assert moved.success
    assert not (container / "Source").exists()
    assert (destination_container / "Source" / "note.txt").exists()

