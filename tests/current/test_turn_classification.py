from __future__ import annotations

from advi.agent.contracts import TaskStatus
from advi.agent.turns import TurnKind, classify_turn


def test_greeting_is_conversation_even_with_failed_task() -> None:
    result = classify_turn("hello", task_status=TaskStatus.FAILED)
    assert result.kind is TurnKind.CONVERSATION


def test_simple_state_question_is_query_state() -> None:
    result = classify_turn("what is my email")
    assert result.kind is TurnKind.QUERY_STATE


def test_task_status_question_is_query_state() -> None:
    result = classify_turn("what task are active", task_status=TaskStatus.FAILED)
    assert result.kind is TurnKind.QUERY_STATE


def test_yes_only_confirms_when_action_is_pending() -> None:
    assert classify_turn("yes").kind is TurnKind.CONVERSATION
    assert classify_turn("yes", has_pending_confirmation=True).kind is TurnKind.CONFIRM_PENDING_ACTION


def test_try_again_only_continues_an_existing_task() -> None:
    assert classify_turn("try again").kind is TurnKind.CONVERSATION
    assert classify_turn("try again", task_status=TaskStatus.FAILED).kind is TurnKind.CONTINUE_TASK
    assert classify_turn("try again", task_status=TaskStatus.UNCERTAIN).kind is TurnKind.CONTINUE_TASK


def test_cancel_is_only_task_control_when_task_exists() -> None:
    assert classify_turn("cancel this task").kind is TurnKind.CONVERSATION
    assert classify_turn("cancel this task", task_status=TaskStatus.FAILED).kind is TurnKind.CANCEL_TASK
    assert classify_turn("please cancel", has_pending_david_send=True).kind is TurnKind.CANCEL_TASK


def test_email_payload_cannot_turn_send_request_into_capability_query() -> None:
    result = classify_turn(
        "send email to me saying, this is a testing email. in body include your capabilities list advi. send email using david fallback method"
    )
    assert result.kind is TurnKind.NEW_TASK


def test_correction_with_action_remains_actionable() -> None:
    result = classify_turn("I meant save those email and details")
    assert result.kind is TurnKind.NEW_TASK


def test_new_task_is_detected_from_imperative_prefix_not_payload_words() -> None:
    result = classify_turn("create a beautiful pdf and include a capabilities section")
    assert result.kind is TurnKind.NEW_TASK


def test_continuation_is_not_confused_with_new_action_words_in_payload() -> None:
    result = classify_turn("try again and open the same page", task_status=TaskStatus.FAILED)
    assert result.kind is TurnKind.CONTINUE_TASK


def test_capability_fast_path_does_not_match_email_payload() -> None:
    from advi.agent.session import AgentSession

    assert AgentSession._is_local_capabilities_request("List your capabilities in short")
    assert not AgentSession._is_local_capabilities_request(
        "send email to me and include your capabilities list in the body"
    )


def test_system_fast_path_does_not_match_email_payload() -> None:
    from advi.agent.session import AgentSession

    assert AgentSession._is_local_system_request("what is my system information") == "get_system_info"
    assert AgentSession._is_local_system_request(
        "send an email and include your system information in the body"
    ) is None
