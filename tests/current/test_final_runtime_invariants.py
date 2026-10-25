from __future__ import annotations

from pathlib import Path

from advi.capabilities.files.executor import FileCapability
from advi.tools.contracts import ToolCall
from advi.core.verification import ToolVerifier


def test_create_file_never_overwrites(tmp_path: Path) -> None:
    capability = FileCapability()
    capability.desktop_dir = tmp_path
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")

    result = capability.execute(
        ToolCall(
            call_id="create-existing",
            name="create_file",
            arguments={"path": "existing.txt", "content": "replacement"},
        )
    )

    assert not result.success
    assert "already exists" in (result.error or "").lower()
    assert target.read_text(encoding="utf-8") == "original"


def test_verifier_summary_has_no_retired_finish_action() -> None:
    summary = ToolVerifier.summarize([])
    assert summary["status"] == "verified"


def test_app_interactive_exit_words_are_terminal_only() -> None:
    app_source = Path(__file__).resolve().parents[2] / "src" / "advi" / "app.py"
    text = app_source.read_text(encoding="utf-8")
    # Typed exit / quit / sleep close ADVI, even mid-task (read on the input thread,
    # 2026-10-24); a spoken "sleep" only stops listening.
    from advi.io.console import InputHub

    assert InputHub.EXIT_WORDS == {"exit", "quit", "sleep"}
    assert 'source in {"eof", "exit"}' in text
    assert 'agent.respond(user_input)' in text


def test_pending_confirmation_cancel_is_local() -> None:
    session_source = (
        Path(__file__).resolve().parents[2] / "src" / "advi" / "agent" / "session.py"
    ).read_text(encoding="utf-8")
    block = session_source.split("def _resolve_pending_confirmation", 1)[1].split("def ", 1)[0]
    cancel = block.split('if _CANCEL_RE.fullmatch(text):', 1)[1].split('if not _CONFIRM_RE', 1)[0]
    assert "send_user" not in cancel
