"""Save As must not save under a name the user did not ask for (log 2026-10-14 turn 25)."""
from __future__ import annotations

import pytest

from advi.capabilities.desktop.context import DesktopContext
from advi.capabilities.desktop.executor import DesktopCapability
from advi.tools.contracts import ToolCall


class FakePyAutoGUI:
    def __init__(self):
        self.pressed = []

    def hotkey(self, *keys):
        self.pressed.append("+".join(keys))

    def press(self, key):
        self.pressed.append(key)


class FakeEdit:
    def __init__(self, accepts):
        self.accepts = accepts
        self.value = "Whispers of dawn in quiet light,.txt"

    def set_edit_text(self, text):
        if self.accepts:
            self.value = text

    def click_input(self):
        pass

    def get_value(self):
        return self.value


class FakeButton:
    def __init__(self, on_click):
        self.clicked = 0
        self.on_click = on_click

    def click_input(self):
        self.clicked += 1
        self.on_click()


def _capability(monkeypatch, *, accepts, writes_file, target, title):
    gui = FakePyAutoGUI()
    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: gui)
    monkeypatch.setattr("pywinauto.keyboard.send_keys", lambda *a, **k: None)
    cap = DesktopCapability(context=DesktopContext(current_application="Notepad"))
    cap.context.target_hwnd = 5
    edit = FakeEdit(accepts)
    button = FakeButton(lambda: target.write_text("poem") if writes_file else None)
    monkeypatch.setattr(cap, "_ensure_target_window", lambda **kwargs: (True, {}))
    monkeypatch.setattr(cap, "_find_foreground_dialog_for_pid", lambda pid: 99)
    monkeypatch.setattr(cap, "_get_uia_window", lambda hwnd: object())
    monkeypatch.setattr(cap, "_find_dialog_edit", lambda dialog, preferred_titles=(): edit)
    monkeypatch.setattr(cap, "_find_dialog_button", lambda dialog, titles=(): button)
    monkeypatch.setattr(cap, "_accept_overwrite_confirmation", lambda pid: None)
    monkeypatch.setattr(cap, "_window_title", lambda hwnd: title)
    monkeypatch.setattr("advi.capabilities.desktop.executor.time.sleep", lambda s: None)
    return cap, gui, button


def test_unconfirmed_filename_cancels_instead_of_saving(monkeypatch, tmp_path):
    target = tmp_path / "TwinklePoem.txt"
    cap, gui, button = _capability(monkeypatch, accepts=False, writes_file=False, target=target,
                                   title="*Untitled - Notepad")
    result = cap.execute(ToolCall("s", "save_document_as", {"path": str(target)}))
    assert not result.success and "cancelled" in result.error
    assert button.clicked == 0 and "esc" in gui.pressed


def test_confirmed_filename_saves_and_verifies(monkeypatch, tmp_path):
    target = tmp_path / "TwinklePoem.txt"
    cap, _gui, button = _capability(monkeypatch, accepts=True, writes_file=True, target=target,
                                    title="TwinklePoem.txt - Notepad")
    result = cap.execute(ToolCall("s", "save_document_as", {"path": str(target)}))
    assert result.success and result.verified and button.clicked == 1


def test_save_under_another_name_is_reported_honestly(monkeypatch, tmp_path):
    target = tmp_path / "TwinklePoem.txt"
    cap, _gui, _button = _capability(monkeypatch, accepts=True, writes_file=False, target=target,
                                     title="Whispers of dawn in quiet light,.txt - Notepad")
    result = cap.execute(ToolCall("s", "save_document_as", {"path": str(target)}))
    assert not result.verified
    assert "Whispers of dawn in quiet light,.txt" in result.human_readable
