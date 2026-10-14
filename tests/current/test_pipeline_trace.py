from __future__ import annotations

import logging

from advi.core.pipeline_trace import (
    begin_turn,
    safe_repr,
    trace,
)


def test_begin_turn_sets_correlation_id():
    turn_id = begin_turn("hello")
    assert turn_id
    assert "-" in turn_id


def test_safe_repr_redacts_api_keys():
    payload = {
        "api_key": "super-secret-test-value",
        "message": "hello",
    }
    text = safe_repr(payload)
    assert "super-secret-test-value" not in text
    assert "<REDACTED>" in text


def test_trace_emits_structured_log(caplog):
    caplog.set_level(logging.INFO, logger="advi.trace")
    turn_id = begin_turn("Open Notepad")

    trace(
        "test.stage",
        tool="open_application",
        arguments={"application": "Notepad"},
        turn_id=turn_id,
    )

    assert any(
        "[TRACE]" in record.message
        and "test.stage" in record.message
        for record in caplog.records
    )


def test_trace_serializes_native_tool_result_without_legacy_types():
    turn_id = begin_turn("Open Notepad")
    payload = {
        "name": "open_application",
        "success": True,
        "verified": True,
        "verification_status": "verified",
        "turn_id": turn_id,
    }
    text = safe_repr(payload)
    assert "open_application" in text
    assert "legacy_action" not in text


def test_redacts_oauth_credentials_and_tokens():
    from advi.core.logging import AdviFileFormatter
    import logging

    test_samples = [
        ("Bearer ya29.a0AfH6SMD_dummy_access_token_12345", "<REDACTED>"),
        ("https://localhost:8080/?state=xyz&code=4/0Ad-dummy-oauth-code-67890", "code=<REDACTED>"),
        ('{"access_token": "ya29.test123456", "refresh_token": "1//04dummy_refresh"}', "<REDACTED>"),
        ("code=4/12345&client_secret=secret123", "code=<REDACTED>"),
    ]

    formatter = AdviFileFormatter()
    for sample, expected in test_samples:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg=sample,
            args=(),
            exc_info=None,
        )
        formatted = formatter.format(record)
        assert expected in formatted
        assert "dummy_access_token" not in formatted
        assert "dummy-oauth-code" not in formatted
        assert "04dummy_refresh" not in formatted
        assert "secret123" not in formatted

