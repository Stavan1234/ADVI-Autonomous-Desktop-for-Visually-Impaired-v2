from _pytest import cacheprovider
from advi.core.config import load_settings
from advi.core.runtime import Runtime
from advi.io.tts import clean_for_speech
from advi.io.output import AdviResponse, OutputManager
from advi.providers import LLMProvider, LLMResponse
from unittest.mock import Mock, patch
from advi.core.conversation import ConversationEngine
from advi.memory.long_term import LongTermMemory
from advi.memory.short_term import ShortTermMemory
from advi.memory.session import SessionBuffer
from advi.memory.retriever import MemoryRetriever

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
    assert provider.messages[-1] == {
        "role": "user",
        "content": "Hello Advi.",
    }

    assert provider.messages[0]["role"] == "system"
    assert "You are ADVI" in provider.messages[0]["content"]
    assert "Be concise by default" in provider.messages[0]["content"]


def test_conversation_engine_ignores_empty_input():
    provider = FakeProvider()
    engine = ConversationEngine(provider)

    response = engine.respond("   ")

    assert response.text == ""
    assert provider.messages == []

def test_long_term_memory_persists(tmp_path):
    database = tmp_path / "memory.db"

    memory = LongTermMemory(database)

    memory.remember(
        "user",
        "name",
        "Stavan",
    )

    # Simulate ADVI shutting down.
    memory = LongTermMemory(database)

    result = memory.recall(
        "user",
        "name",
    )

    assert result is not None
    assert result.value == "Stavan"    


def test_long_term_memory_updates_existing_value(tmp_path):
    database = tmp_path / "memory.db"

    memory = LongTermMemory(database)

    memory.remember(
        "user",
        "name",
        "Stavan",
    )

    memory.remember(
        "user",
        "name",
        "Stavan Kalkumbe",
    )

    result = memory.recall(
        "user",
        "name",
    )

    assert result is not None
    assert result.value == "Stavan Kalkumbe"

def test_session_buffer_stages_evicted_messages():
    memory = ShortTermMemory(max_messages=2)
    buffer = SessionBuffer()

    buffer.add(
        memory.add("user", "one")
    )

    buffer.add(
        memory.add("assistant", "two")
    )

    evicted = memory.add("user", "three")
    buffer.add(evicted)

    assert memory.get_messages() == [
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]

    assert buffer.messages == [
        {"role": "user", "content": "one"},
    ]   
    
def test_memory_search_handles_possessive_query(tmp_path):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "family",
        "father",
        "Devdan Kalkumbe",
        "The user's father is Devdan Kalkumbe.",
        0.98,
    )

    results = memory.search(
        "What is my father's name?"
    )

    assert any(
        item.key == "father"
        and item.value == "Devdan Kalkumbe"
        for item in results
    )     

def test_memory_search_handles_question_punctuation(tmp_path):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "family",
        "father",
        "Devdan Kalkumbe",
        "The user's father is Devdan Kalkumbe.",
        0.98,
    )

    results = memory.search(
        "father?"
    )

    assert any(
        item.key == "father"
        for item in results
    )

def test_memory_update_preserves_history(tmp_path):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "institution",
        "FCRIT Vashi",
        "The user studies at FCRIT Vashi.",
        0.98,
    )

    memory.remember(
        "user",
        "institution",
        "VJTI",
        "The user studies at VJTI.",
        0.98,
    )

    current = memory.recall(
        "user",
        "institution",
    )

    assert current is not None
    assert current.value == "VJTI"

    history = memory.history(
        "user",
        "institution",
    )

    assert len(history) == 1
    assert history[0].value == "FCRIT Vashi"

def test_memory_update_does_not_create_history_for_same_fact(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "name",
        "Stavan Kalkumbe",
        "The user's name is Stavan Kalkumbe.",
        0.98,
    )

    memory.remember(
        "user",
        "name",
        "Stavan Kalkumbe",
        "The user's name is Stavan Kalkumbe.",
        0.98,
    )

    history = memory.history(
        "user",
        "name",
    )

    assert history == []       

def test_memory_retriever_finds_paraphrased_fact(tmp_path):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "institution",
        "FCRIT Vashi",
        "The user studies at FCRIT Vashi.",
        0.98,
    )

    retriever = MemoryRetriever(memory)

    results = retriever.search(
        "where do I study",
        limit=5,
    )

    assert results

    assert any(
        result.memory.key == "institution"
        and result.memory.value == "FCRIT Vashi"
        for result in results
    )


def test_relationship_search_finds_direct_relationship(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember_relationship(
        "user",
        "father",
        "Devdan Kalkumbe",
        0.98,
    )

    results = memory.search_relationships(
        "What is my father's name?"
    )

    assert results
    assert any(
        relationship.object == "Devdan Kalkumbe"
        for relationship in results
    )


def test_relationship_search_supports_multi_hop_candidates(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember_relationship(
        "user",
        "uncle",
        "Pradeep",
        0.98,
    )

    memory.remember_relationship(
        "Pradeep",
        "daughter",
        "Ramanika",
        0.98,
    )

    memory.remember_relationship(
        "Ramanika",
        "younger sister",
        "Aaradhana",
        0.98,
    )

    results = memory.search_relationships(
        "Who is Ramanika?"
    )

    objects = {
        relationship.object
        for relationship in results
    }

    assert "Ramanika" in objects


def test_memory_update_keeps_current_value(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "institution",
        "FCRIT Vashi",
        "The user studies at FCRIT Vashi.",
        0.90,
    )

    memory.remember(
        "user",
        "institution",
        "VJTI",
        "The user studies at VJTI.",
        0.95,
    )

    current = memory.recall(
        "user",
        "institution"
    )

    assert current is not None
    assert current.value == "VJTI"

    history = memory.history(
        "user",
        "institution"
    )

    assert len(history) == 1
    assert history[0].value == "FCRIT Vashi"


def test_same_memory_does_not_create_history(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "name",
        "Stavan Kalkumbe",
        "The user's name is Stavan Kalkumbe.",
        0.98,
    )

    memory.remember(
        "user",
        "name",
        "Stavan Kalkumbe",
        "The user's name is Stavan Kalkumbe.",
        0.98,
    )

    assert memory.history(
        "user",
        "name"
    ) == [] 

def test_related_entities_supports_two_hop_relationships(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember_relationship(
        "user",
        "uncle",
        "Pradeep",
        0.98,
    )

    memory.remember_relationship(
        "Pradeep",
        "daughter",
        "Ramanika",
        0.98,
    )

    results = memory.related_entities(
        "user",
        max_hops=2,
    )

    triples = {
        (
            item.subject,
            item.relation,
            item.object,
        )
        for item in results
    }

    assert (
        ("user", "uncle", "Pradeep")
        in triples
    )

    assert (
        ("Pradeep", "daughter", "Ramanika")
        in triples
    )

def test_memory_conflict_prefers_higher_confidence(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "institution",
        "Old College",
        "The user studies at Old College.",
        0.95,
    )

    memory.remember(
        "user",
        "institution",
        "New College",
        "The user studies at New College.",
        0.40,
    )

    current = memory.recall(
        "user",
        "institution",
    )

    assert current is not None
    assert current.value == "Old College"

    assert memory.history(
        "user",
        "institution",
    ) == []

def test_memory_conflict_accepts_higher_confidence_update(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "institution",
        "Old College",
        "The user studies at Old College.",
        0.80,
    )

    memory.remember(
        "user",
        "institution",
        "New College",
        "The user studies at New College.",
        0.95,
    )

    current = memory.recall(
        "user",
        "institution",
    )

    assert current is not None
    assert current.value == "New College"

    history = memory.history(
        "user",
        "institution",
    )

    assert len(history) == 1
    assert history[0].value == "Old College"

def test_identical_memory_does_not_create_history(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "name",
        "Stavan",
        "The user's name is Stavan.",
        0.90,
    )

    memory.remember(
        "user",
        "name",
        "Stavan",
        "The user's name is Stavan.",
        0.95,
    )

    current = memory.recall(
        "user",
        "name",
    )

    assert current is not None
    assert current.confidence == 0.95

    assert memory.history(
        "user",
        "name",
    ) == []

def test_memory_conflict_equal_confidence_replaces_old(
    tmp_path,
):
    memory = LongTermMemory(
        tmp_path / "memory.db"
    )

    memory.remember(
        "user",
        "institution",
        "Old College",
        "The user studies at Old College.",
        0.90,
    )

    memory.remember(
        "user",
        "institution",
        "New College",
        "The user studies at New College.",
        0.90,
    )

    current = memory.recall(
        "user",
        "institution",
    )

    assert current is not None
    assert current.value == "New College"

    history = memory.history(
        "user",
        "institution",
    )

    assert len(history) == 1
    assert history[0].value == "Old College"       


def test_advi_identity_is_present():
    from advi.core.personality import build_advi_system_prompt

    prompt = build_advi_system_prompt()

    assert "ADVI" in prompt
    assert "Autonomous Desktop for the Visually Impaired" in prompt
    assert "Do not identify yourself as ChatGPT" in prompt


def test_advi_personality_is_concise_by_default():
    from advi.core.personality import build_advi_system_prompt

    prompt = build_advi_system_prompt()

    assert "Be concise by default" in prompt
    assert "Do not over-explain" in prompt


def test_advi_personality_is_speech_friendly():
    from advi.core.personality import build_advi_system_prompt

    prompt = build_advi_system_prompt()

    assert "speech-friendly" in prompt
    assert "Do not read Markdown syntax literally" in prompt             

def test_response_policy_defaults_to_concise():
    from advi.core.response_policy import detect_response_mode

    assert detect_response_mode("What is Python?") == "concise"


def test_response_policy_detects_detailed_request():
    from advi.core.response_policy import detect_response_mode

    assert detect_response_mode(
        "Explain Python in detail."
    ) == "detailed"


def test_response_policy_detects_brief_request():
    from advi.core.response_policy import detect_response_mode

    assert detect_response_mode(
        "What is Python? Keep it short."
    ) == "concise"


def test_response_policy_builds_instruction():
    from advi.core.response_policy import build_response_policy

    policy = build_response_policy("Explain this step by step.")

    assert "DETAILED" in policy

def test_capability_registry_contains_core_capabilities():
    from advi.core.capabilities import (
        CAPABILITIES,
        CapabilityStatus,
    )

    assert "conversation" in CAPABILITIES
    assert "memory" in CAPABILITIES
    assert "session_continuity" in CAPABILITIES
    assert "speech_output" in CAPABILITIES

    assert (
        CAPABILITIES["memory"].status
        == CapabilityStatus.AVAILABLE
    )


def test_capability_registry_marks_unimplemented_features_unavailable():
    from advi.core.capabilities import (
        CAPABILITIES,
        CapabilityStatus,
    )

    assert (
        CAPABILITIES["web_search"].status
        == CapabilityStatus.UNAVAILABLE
    )

    assert (
        CAPABILITIES["vision"].status
        == CapabilityStatus.UNAVAILABLE
    )

    assert (
        CAPABILITIES["desktop_control"].status
        == CapabilityStatus.UNAVAILABLE
    )


def test_get_capability_is_case_insensitive():
    from advi.core.capabilities import get_capability

    capability = get_capability(" MEMORY ")

    assert capability is not None
    assert capability.name == "memory"


def test_capability_summary_contains_status_groups():
    from advi.core.capabilities import capability_summary

    summary = capability_summary()

    assert "Available capabilities:" in summary
    assert "Unavailable capabilities:" in summary
    assert "memory:" in summary
    assert "web_search:" in summary


def test_capability_can_use_reports_actual_status():
    from advi.core.capabilities import can_use

    assert can_use("conversation")
    assert can_use("memory")
    assert not can_use("web_search")
    assert not can_use("vision")


def test_capability_prompt_contains_available_and_unavailable_state():
    from advi.core.capabilities import capability_for_prompt

    prompt = capability_for_prompt()

    assert "Conversation: available" in prompt
    assert "Long-term memory: available" in prompt
    assert "Web search: unavailable" in prompt
    assert "Screen/vision understanding: unavailable" in prompt
    assert "Never claim to have an unavailable capability." in prompt            