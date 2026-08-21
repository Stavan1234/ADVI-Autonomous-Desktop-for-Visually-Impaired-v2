from advi.core.config import load_settings
from advi.core.runtime import Runtime
from advi.io.tts import clean_for_speech
from advi.io.output import AdviResponse, OutputManager

def test_response_has_separate_speech_representation():
    response = AdviResponse(
        "## Today's plan\n\n"
        "| Task | Time |\n"
        "|---|---|\n"
        "| Study | 2 PM |\n"
        "| Gym | 6 PM |\n"
        "\n"
        "**Priority:** Study first."
    )

    assert "##" in response.for_display()
    assert "|" in response.for_display()

    speech = response.for_speech()

    assert "Today's plan" in speech
    assert "Study" in speech
    assert "2 PM" in speech
    assert "Gym" in speech
    assert "6 PM" in speech
    assert "##" not in speech

def test_settings_load_without_secrets(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    settings = load_settings()

    assert settings.groq_api_key is None
    assert settings.gemini_api_key is None
    assert settings.piper_exe.name == "piper.exe"


def test_runtime_start_and_shutdown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    runtime = Runtime.create()
    runtime.start()

    assert runtime.started is True

    runtime.shutdown()

    assert runtime.started is False


def test_speech_cleaning():
    assert clean_for_speech(
        "**Hello** [Advi](https://example.com)..."
    ) == "Hello Advi,"


def test_response_speech_removes_markdown():
    response = AdviResponse(
        "## Hello\n\n"
        "This is **important** and [useful](https://example.com).\n\n"
        "- First item\n"
        "- Second item"
    )

    speech = response.for_speech()

    assert "##" not in speech
    assert "**" not in speech
    assert "https://" not in speech
    assert "First item" in speech
    assert "Second item" in speech


def test_response_speech_handles_table():
    response = AdviResponse(
        "| Task | Time |\n"
        "|---|---|\n"
        "| Study | 2 PM |\n"
        "| Gym | 6 PM |"
    )

    speech = response.for_speech()

    assert "Task: Study; Time: 2 PM." in speech
    assert "Task: Gym; Time: 6 PM." in speech


def test_empty_response_produces_no_speech():
    response = AdviResponse("")

    assert response.for_display() == ""
    assert response.for_speech() == ""

class FakeTTS:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.spoken: list[str] = []

    def speak(self, text: str) -> bool:
        self.spoken.append(text)
        return self.result


def test_output_manager_reports_tts_failure():
    fake_tts = FakeTTS(False)
    output = OutputManager(tts=fake_tts)

    result = output.deliver(
        AdviResponse("Hello. This is a test.")
    )

    assert result is False
    assert fake_tts.spoken == ["Hello. This is a test."]


def test_output_manager_reports_success():
    fake_tts = FakeTTS(True)
    output = OutputManager(tts=fake_tts)

    result = output.deliver(
        AdviResponse("Hello. This is a test.")
    )

    assert result is True
    assert fake_tts.spoken == ["Hello. This is a test."]