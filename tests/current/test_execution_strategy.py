from types import SimpleNamespace

import fitz

from advi.agent.session import AgentSession
from advi.core.execution_strategy import (
    ExecutionMode,
    OperationFamily,
    infer_execution_strategy,
    terminal_evidence_matches,
)
from advi.core.verification import ToolVerifier
from advi.tools.contracts import ToolCall, ToolResult, VerificationStatus


def test_youtube_music_is_deliberate_media_workflow():
    strategy = infer_execution_strategy('open youtube music, search for "Never Say Never" and play it')
    assert strategy.mode is ExecutionMode.DELIBERATE
    assert strategy.family is OperationFamily.MEDIA_PLAYBACK
    assert strategy.max_side_effects_per_round == 1
    assert strategy.checkpoint_after_side_effect


def test_rich_pdf_is_deliberate_structured_document():
    strategy = infer_execution_strategy('create a beautiful pdf about Water with hierarchy and tables')
    assert strategy.mode is ExecutionMode.DELIBERATE
    assert strategy.family is OperationFamily.STRUCTURED_DOCUMENT


def test_unknown_action_orients_before_committing():
    strategy = infer_execution_strategy('configure this strange application for my workflow')
    assert strategy.mode is ExecutionMode.EXPLORATORY
    assert strategy.family is OperationFamily.EXPLORATION
    assert 'read_screen' in strategy.observation_tools
    assert strategy.max_side_effects_per_round == 1


def test_known_simple_operations_do_not_become_exploratory():
    cases = [
        ('open Notepad', OperationFamily.DESKTOP_WORKFLOW),
        ('open Downloads', OperationFamily.FILE_WORKFLOW),
        ('press Enter', OperationFamily.GENERIC),
        ('go to example.com', OperationFamily.BROWSER_WORKFLOW),
        ('send an email to david@example.com', OperationFamily.EMAIL_WORKFLOW),
    ]
    for text, family in cases:
        strategy = infer_execution_strategy(text)
        expected_mode = ExecutionMode.DELIBERATE if family is OperationFamily.EMAIL_WORKFLOW else ExecutionMode.SIMPLE
        assert strategy.mode is expected_mode, text
        assert strategy.family is family, text


def test_media_terminal_evidence_requires_actual_playback():
    strategy = infer_execution_strategy('play "Never Say Never" on YouTube Music')
    search_result = ToolResult(
        call_id='1', name='search', success=True, verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={'search_result': True},
    )
    click_not_playing = ToolResult(
        call_id='2', name='click_web_element', success=True, verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={'playback': {'playing': False}},
    )
    click_playing = ToolResult(
        call_id='3', name='click_web_element', success=True, verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={'playback': {'playing': True, 'title': 'Never Say Never'}},
    )
    assert not terminal_evidence_matches(strategy, 'search', search_result, strategy_text := 'play "Never Say Never" on YouTube Music')
    assert not terminal_evidence_matches(strategy, 'click_web_element', click_not_playing, strategy_text)
    assert terminal_evidence_matches(strategy, 'click_web_element', click_playing, strategy_text)


def test_structured_document_terminal_evidence_rejects_content_mismatch():
    strategy = infer_execution_strategy('create a pdf about Water with beautiful styling')
    bad = ToolResult(
        call_id='1', name='save_file', success=True, verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={
            'exists': True,
            'content_matches': False,
            'verification_mode': 'native_structure_plus_content_warning',
            'style_requested': True,
            'visual_sanity': {'valid': True},
        },
    )
    good = ToolResult(
        call_id='2', name='save_file', success=True, verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={
            'exists': True,
            'content_matches': True,
            'verification_mode': 'native_structure_plus_content_warning',
            'style_requested': True,
            'visual_sanity': {'valid': True},
        },
    )
    no_visual = ToolResult(
        call_id='3', name='save_file', success=True, verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={
            'exists': True,
            'content_matches': True,
            'verification_mode': 'native_structure_plus_content_warning',
            'style_requested': True,
            'visual_sanity': {'valid': False},
        },
    )
    assert not terminal_evidence_matches(strategy, 'save_file', bad, 'create a pdf about Water with beautiful styling')
    assert terminal_evidence_matches(strategy, 'save_file', good, 'create a pdf about Water with beautiful styling')
    assert not terminal_evidence_matches(strategy, 'save_file', no_visual, 'create a pdf about Water with beautiful styling')


def test_structured_terminal_evidence_tolerates_serialized_visual_sanity():
    strategy = infer_execution_strategy('create a beautiful pdf about Water')
    result = ToolResult(
        call_id='1', name='create_file', success=True, verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={
            'exists': True, 'content_matches': True, 'style_requested': True,
            'visual_sanity': '{"valid":true}', 'verification_mode': 'exact',
        },
    )
    assert terminal_evidence_matches(strategy, 'create_file', result, strategy_text := 'create a beautiful pdf about Water') is True


def test_create_folder_is_terminal_when_verified():
    strategy = infer_execution_strategy('create a folder called doremon in downloads')
    result = ToolResult(
        call_id='1', name='create_folder', success=True, verified=True,
        verification_status=VerificationStatus.VERIFIED,
        verification_details={'path': r'C:\Users\HP\Downloads\doremon', 'exists': True},
    )
    assert terminal_evidence_matches(strategy, 'create_folder', result, 'create a folder called doremon in downloads') is True


def test_pdf_visual_sanity_is_reported_for_styled_document(tmp_path):
    pdf_path = tmp_path / 'styled.pdf'
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), 'Water is essential for life and ecosystems.')
    document.save(str(pdf_path))
    document.close()

    verifier = ToolVerifier()
    action = ToolCall(
        call_id='pdf-1',
        name='save_file',
        arguments={
            'path': str(pdf_path),
            'content': 'Water is essential for life and ecosystems.',
            'style': 'beautiful fonts and hierarchy',
        },
    )
    result = ToolResult(call_id='pdf-1', name='save_file', success=True, output=str(pdf_path))
    verified = verifier.verify(action, result)

    assert verified.success
    details = dict(verified.verification_details)
    assert details['style_requested'] is True
    assert details['visual_sanity']['valid'] is True
    assert details['visual_sanity']['checked_pages'][0]['text_chars'] > 12


def test_pdf_visual_sanity_detects_blank_page(tmp_path):
    pdf_path = tmp_path / 'blank.pdf'
    document = fitz.open()
    document.new_page()
    document.save(str(pdf_path))
    document.close()

    verifier = ToolVerifier()
    action = ToolCall(
        call_id='pdf-2',
        name='save_file',
        arguments={
            'path': str(pdf_path),
            'content': 'Water',
            'style': 'beautiful design',
        },
    )
    result = ToolResult(call_id='pdf-2', name='save_file', success=True, output=str(pdf_path))
    verified = verifier.verify(action, result)

    details = dict(verified.verification_details)
    assert details['style_requested'] is True
    assert details['visual_sanity']['valid'] is False


def test_click_web_element_verifies_explicit_playback_evidence():
    from advi.core.verification import ToolVerifier
    from advi.tools.contracts import ToolCall, ToolResult
    verifier = ToolVerifier()
    action = ToolCall("play", "click_web_element", {"selector": "a#first-result"})
    result = ToolResult(
        call_id="play", name="click_web_element", success=True,
        output="https://www.youtube.com/watch?v=test",
        verification_details={"method": "youtube_result_to_player", "playing": True, "current_time": 0.4},
        metadata={"tab_id": "tab-1"},
    )
    verified = verifier.verify(action, result)
    assert verified.success is True
    assert verified.verified is True
    assert verified.verification_details["playback_verified"] is True
