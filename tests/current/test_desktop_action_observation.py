from __future__ import annotations

from advi.capabilities.desktop.executor import DesktopCapability
from advi.capabilities.desktop.context import DesktopContext
from advi.capabilities.desktop.perception import PerceptionResult
from advi.core.verification import ToolVerifier
from advi.tools import ToolCall


class FakePyAutoGUI:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, value: str, interval: float = 0.0) -> None:
        self.writes.append(value)

    def click(self, *coords) -> None:
        pass

    def doubleClick(self, *coords) -> None:
        pass

    def rightClick(self, *coords) -> None:
        pass


class FakeEdit:
    def __init__(self) -> None:
        self.value = "hello"
        self.focused = False
        self.focus_calls = 0

    def get_value(self) -> str:
        return self.value

    def set_focus(self) -> None:
        self.focus_calls += 1
        self.focused = True

    def has_focus(self) -> bool:
        return self.focused


class FakeButton:
    def __init__(self) -> None:
        self.focused = False
        self.selected = False

    def has_focus(self) -> bool:
        return self.focused

    def is_selected(self) -> bool:
        return self.selected

    def click_input(self) -> None:
        self.focused = True
        self.selected = True


def test_type_text_targets_control_and_records_value_delta(monkeypatch):
    control = FakeEdit()
    pyautogui = FakePyAutoGUI()
    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: pyautogui)
    monkeypatch.setattr("advi.capabilities.desktop.executor.time.sleep", lambda *_: None)

    class Perception:
        def find(self, *, target, action, application):
            assert action == "type_text"
            return PerceptionResult(found=True, target=control, method="uia")

    capability = DesktopCapability(perception=Perception())
    original_write = pyautogui.write

    def write(value: str, interval: float = 0.0) -> None:
        original_write(value, interval)
        control.value += value

    pyautogui.write = write
    result = capability.execute(ToolCall("1", "type_text", {"target": "editor", "value": " world"}))

    observation = result.metadata["observation"]
    assert result.success is True
    assert observation["focused_for_typing"] is True
    assert observation["before_text"] == "hello"
    assert observation["after_text"] == "hello world"
    assert observation["text_delta_observed"] is True

    verified = ToolVerifier().verify(ToolCall("1", "type_text", {"target": "editor", "value": " world"}), result)
    assert verified.verified is True


def test_click_uses_state_delta_instead_of_current_focus(monkeypatch):
    control = FakeButton()
    pyautogui = FakePyAutoGUI()
    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: pyautogui)
    monkeypatch.setattr("advi.capabilities.desktop.executor.time.sleep", lambda *_: None)

    class Perception:
        def find(self, *, target, action, application):
            return PerceptionResult(found=True, target=control, method="uia")

    capability = DesktopCapability(perception=Perception())
    result = capability.execute(ToolCall("1", "click", {"target": "Save"}))
    observation = result.metadata["observation"]

    assert result.success is True
    assert observation["state_changed"] is True
    verified = ToolVerifier().verify(ToolCall("1", "click", {"target": "Save"}), result)
    assert verified.verified is True


def test_click_without_effect_remains_uncertain(monkeypatch):
    class NoopButton:
        def has_focus(self) -> bool:
            return True

        def click_input(self) -> None:
            pass

    control = NoopButton()
    pyautogui = FakePyAutoGUI()
    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: pyautogui)
    monkeypatch.setattr("advi.capabilities.desktop.executor.time.sleep", lambda *_: None)

    class Perception:
        def find(self, *, target, action, application):
            return PerceptionResult(found=True, target=control, method="uia")

    capability = DesktopCapability(perception=Perception())
    result = capability.execute(ToolCall("1", "click", {"target": "Save"}))
    verified = ToolVerifier().verify(ToolCall("1", "click", {"target": "Save"}), result)
    assert verified.success is True
    assert verified.verified is False
    assert str(verified.verification_status).lower().endswith("uncertain")


def test_rebind_target_window_prefers_single_recreated_window(monkeypatch):
    context = DesktopContext(
        current_application="Notepad",
        target_hwnd=100,
        target_pid=22,
        target_title="notes.txt - Notepad",
        target_process_name="notepad.exe",
    )
    capability = DesktopCapability(context=context)
    monkeypatch.setattr(capability, "_find_windows_for_process", lambda pid: [200])
    monkeypatch.setattr(capability, "_window_title", lambda hwnd: "notes.txt - Notepad")
    monkeypatch.setattr(capability, "_process_name", lambda pid: "notepad.exe")
    rebound = capability._rebind_target_window()
    assert rebound == 200
    assert context.target_hwnd == 200
    assert context.target_pid == 22


def test_rebind_target_window_refuses_ambiguous_windows(monkeypatch):
    context = DesktopContext(
        current_application="Notepad",
        target_hwnd=100,
        target_pid=22,
        target_title="old.txt - Notepad",
        target_process_name="notepad.exe",
    )
    capability = DesktopCapability(context=context)
    monkeypatch.setattr(capability, "_find_windows_for_process", lambda pid: [200, 300])
    monkeypatch.setattr(capability, "_window_title", lambda hwnd: {200: "first.txt - Notepad", 300: "second.txt - Notepad"}[hwnd])
    assert capability._rebind_target_window() is None
    assert context.target_hwnd == 100


def test_rebind_target_window_refuses_ambiguous_windows_without_windows_api(monkeypatch):
    context = DesktopContext(
        current_application="Notepad",
        target_hwnd=100,
        target_pid=22,
        target_title="old.txt - Notepad",
        target_process_name="notepad.exe",
    )
    capability = DesktopCapability(context=context)
    monkeypatch.setattr(capability, "_find_windows_for_process", lambda pid: [200, 300])
    monkeypatch.setattr(capability, "_window_title", lambda hwnd: {200: "first.txt - Notepad", 300: "second.txt - Notepad"}[hwnd])
    assert capability._rebind_target_window() is None


def test_paste_unicode_text_uses_native_write_off_windows(monkeypatch):
    class FakePyAutoGUI:
        def __init__(self): self.writes = []
        def write(self, value, interval=0.0): self.writes.append((value, interval))

    monkeypatch.setattr("advi.capabilities.desktop.executor.os.name", "posix")
    pg = FakePyAutoGUI()
    result = DesktopCapability._paste_unicode_text("नमस्ते\nADVI 🚀", pg)
    assert result["method"] == "pyautogui_write"
    assert pg.writes[0][0] == "नमस्ते\nADVI 🚀"


def test_inspect_window_has_a_native_contract():
    cap = DesktopCapability(context=DesktopContext())
    assert "inspect_window" in cap.SUPPORTED_ACTIONS


def test_save_document_without_known_path_reports_save_as_uncertainty(monkeypatch):
    class FakePyAutoGUI:
        def hotkey(self, *keys):
            self.keys = keys

    fake = FakePyAutoGUI()
    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: fake)
    cap = DesktopCapability(context=DesktopContext(current_application="Notepad"))
    monkeypatch.setattr(cap, "_ensure_target_window", lambda **kwargs: (True, {"target_hwnd": 101}))
    monkeypatch.setattr(cap, "_window_title", lambda hwnd: "Untitled - Notepad")
    monkeypatch.setattr(cap, "_find_foreground_dialog_for_pid", lambda pid: None)
    result = cap.execute(ToolCall("save", "save_document", {}))
    assert result.success is True
    assert result.verified is False
    assert str(result.verification_status).lower().endswith("uncertain")


def test_context_tracks_opened_document_and_clears_path_for_new_document():
    context = DesktopContext(current_application="Word")
    context.set_document(path="C:/Docs/report.docx", title="report.docx - Word", dirty=False)
    assert context.observation_snapshot()["document"]["path"] == "C:/Docs/report.docx"
    context.clear_document()
    context.set_document(title="Document2 - Word", dirty=False)
    assert context.observation_snapshot()["document"]["path"] is None


def test_save_document_as_requires_destination_path(monkeypatch):
    class FakePyAutoGUI:
        def hotkey(self, *keys):
            self.keys = keys

    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: FakePyAutoGUI())
    cap = DesktopCapability(context=DesktopContext(current_application="Word"))
    result = cap.execute(ToolCall("saveas", "save_document_as", {}))
    assert not result.success
    assert "destination path" in str(result.error).lower()


def test_context_document_lifecycle():
    context = DesktopContext(current_application="Word")
    context.set_document(path="C:/Docs/report.docx", title="report.docx - Word", dirty=True)
    snap = context.observation_snapshot()
    assert snap["document"]["path"].endswith("report.docx")
    assert snap["document"]["dirty"] is True
    context.set_document(path="C:/Docs/report.docx", title="report.docx - Word", dirty=False)
    assert context.observation_snapshot()["document"]["dirty"] is False


def test_close_document_without_windows_target_fails_cleanly(monkeypatch):
    class FakePyAutoGUI:
        def hotkey(self, *keys):
            self.keys = keys
    monkeypatch.setattr("advi.capabilities.desktop.executor._get_pyautogui", lambda: FakePyAutoGUI())
    cap = DesktopCapability(context=DesktopContext(current_application="Word"))
    monkeypatch.setattr(cap, "_ensure_target_window", lambda **kwargs: (False, {"blocking_dialog": False, "target_hwnd": None}))
    result = cap.execute(ToolCall("close", "close_document", {}))
    assert not result.success
    assert "not available" in str(result.error).lower()
