from advi.core.config import load_settings
from advi.core.runtime import Runtime
from advi.io.tts import clean_for_speech
from advi.io.output import AdviResponse, OutputManager
from advi.providers import LLMProvider, LLMResponse
from unittest.mock import Mock, patch
from advi.core.conversation import ConversationEngine

from groq import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from advi.providers import (
    GroqProvider,
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)

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



def test_llm_response_contract():
    response = LLMResponse(
        text="Hello.",
        provider="test",
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        latency_ms=42.5,
        finish_reason="stop",
        request_id="test-request",
    )

    assert response.text == "Hello."
    assert response.provider == "test"
    assert response.model == "test-model"
    assert response.input_tokens == 10
    assert response.output_tokens == 5
    assert response.latency_ms == 42.5
    assert response.finish_reason == "stop"
    assert response.request_id == "test-request"

def test_llm_provider_is_abstract():
    assert issubclass(LLMProvider, object)


def test_groq_provider_properties():
    provider = GroqProvider(
        api_key="test-key",
        model="test-model",
    )

    assert provider.name == "groq"
    assert provider.model == "test-model"


def test_groq_authentication_error_is_normalized():
    provider = GroqProvider(
        api_key="test-key",
        model="test-model",
    )

    with patch.object(
        provider._client.chat.completions,
        "create",
        side_effect=AuthenticationError(
            "bad key",
            response=Mock(status_code=401),
            body=None,
        ),
    ):
        try:
            provider.chat(
                [{"role": "user", "content": "hello"}]
            )
            assert False
        except LLMAuthenticationError as exc:
            assert "authentication" in str(exc).lower()


def test_groq_rate_limit_error_is_normalized():
    provider = GroqProvider(
        api_key="test-key",
        model="test-model",
    )

    with patch.object(
        provider._client.chat.completions,
        "create",
        side_effect=RateLimitError(
            "rate limited",
            response=Mock(status_code=429),
            body=None,
        ),
    ):
        try:
            provider.chat(
                [{"role": "user", "content": "hello"}]
            )
            assert False
        except LLMRateLimitError as exc:
            assert "rate limit" in str(exc).lower()


def test_groq_timeout_error_is_normalized():
    provider = GroqProvider(
        api_key="test-key",
        model="test-model",
    )

    with patch.object(
        provider._client.chat.completions,
        "create",
        side_effect=APITimeoutError(
            request=Mock(),
        ),
    ):
        try:
            provider.chat(
                [{"role": "user", "content": "hello"}]
            )
            assert False
        except LLMTimeoutError as exc:
            assert "timed out" in str(exc).lower()


def test_groq_connection_error_is_normalized():
    provider = GroqProvider(
        api_key="test-key",
        model="test-model",
    )

    with patch.object(
        provider._client.chat.completions,
        "create",
        side_effect=APIConnectionError(
            request=Mock(),
        ),
    ):
        try:
            provider.chat(
                [{"role": "user", "content": "hello"}]
            )
            assert False
        except LLMUnavailableError as exc:
            assert "connect" in str(exc).lower()


def test_groq_success_is_normalized():
    provider = GroqProvider(
        api_key="test-key",
        model="test-model",
    )

    mock_response = Mock()

    mock_response.choices = [
        Mock(
            message=Mock(content="Hello, Advi."),
            finish_reason="stop",
        )
    ]

    mock_response.usage = Mock(
        prompt_tokens=12,
        completion_tokens=7,
    )

    mock_response.id = "req-test-123"

    with patch.object(
        provider._client.chat.completions,
        "create",
        return_value=mock_response,
    ):
        response = provider.chat(
            [{"role": "user", "content": "hello"}]
        )

    assert response.text == "Hello, Advi."
    assert response.provider == "groq"
    assert response.model == "test-model"
    assert response.input_tokens == 12
    assert response.output_tokens == 7
    assert response.finish_reason == "stop"
    assert response.request_id == "req-test-123"
    assert response.latency_ms is not None            


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def chat(self, messages):
        self.messages = messages

        return LLMResponse(
            text="Hello from the fake model.",
            provider=self.name,
            model=self.model,
        )


def test_conversation_engine_creates_response():
    provider = FakeProvider()
    engine = ConversationEngine(provider)

    response = engine.respond("Hello Advi.")

    assert response.text == "Hello from the fake model."
    assert provider.messages == [
        {
            "role": "user",
            "content": "Hello Advi.",
        }
    ]


def test_conversation_engine_ignores_empty_input():
    provider = FakeProvider()
    engine = ConversationEngine(provider)

    response = engine.respond("   ")

    assert response.text == ""
    assert provider.messages == []
