from pathlib import Path

from advi.capabilities.files.executor import FileCapability
from advi.tools.contracts import ToolCall


def test_normal_user_file_operation_is_not_marked_for_extra_confirmation(tmp_path):
    cap = FileCapability()
    cap.desktop_dir = tmp_path
    result = cap.execute(
        ToolCall(
            call_id="c1",
            name="create_file",
            arguments={"path": "notes.txt", "content": "hello"},
        )
    )
    assert result.success
    assert result.requires_confirmation is False


def test_path_policy_detects_children_of_protected_root(tmp_path):
    protected = tmp_path / "system"
    nested = protected / "startup" / "advi.lnk"
    assert FileCapability._is_within(nested, [protected])
    assert not FileCapability._is_within(tmp_path / "Documents" / "note.txt", [protected])


def test_normal_file_resolution_stays_under_desktop(tmp_path):
    cap = FileCapability()
    cap.desktop_dir = tmp_path
    assert cap.resolve_path("note.txt") == tmp_path / "note.txt"
