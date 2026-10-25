"""Phase 9: vision as the last-resort sense (look_at_screen)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from advi.agent.contracts import TaskState
from advi.agent.session import AgentSession
from advi.capabilities.desktop import screen_describer
from advi.capabilities.desktop.executor import DesktopCapability
from advi.capabilities.desktop.screen_describer import (
    ScreenDescriber,
    format_screen_description,
    wait_for_stable_frame,
)
from advi.core.execution_strategy import is_observation_tool
from advi.tools.contracts import ToolCall, ToolResult


class _Client:
    def __init__(self, answers):
        self.answers = answers
        self.prompts = []
        self.models = self

    def generate_content(self, *, model, contents, config=None):
        self.prompts.append(contents[0])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(text=answer)


def _describer(*clients):
    by_key = {f"k{i}": client for i, client in enumerate(clients)}
    return ScreenDescriber(api_keys=list(by_key), model="m", client_factory=by_key.__getitem__)


def _image(color="white"):
    return Image.new("RGB", (320, 200), color)


def test_question_reaches_the_model_and_intro_is_stripped():
    client = _Client(["Here's what's on your screen: An Amazon order page saying 'Order placed'."])
    answer, cached = _describer(client).describe(_image(), "Is an order confirmation visible?")
    assert answer == "An Amazon order page saying 'Order placed'."
    assert not cached
    assert "Question: Is an order confirmation visible?" in client.prompts[0]
    assert "Never give pixel coordinates" in client.prompts[0]


def test_quota_on_one_key_moves_to_the_next():
    first = _Client([RuntimeError("429 RESOURCE_EXHAUSTED")])
    second = _Client(["Telegram is open on the chat list."])
    answer, _ = _describer(first, second).describe(_image())
    assert answer == "Telegram is open on the chat list."


def test_every_key_failing_raises_a_speakable_reason():
    with pytest.raises(RuntimeError, match="^the vision service's daily quota is used up$"):
        _describer(_Client([RuntimeError("429 {'error': ...}")])).describe(_image())


def test_a_busy_service_is_not_retried_on_every_key():
    # Live 2026-10-23: 504 DEADLINE_EXCEEDED on three keys took 60 s per look.
    busy = _Client([RuntimeError("504 DEADLINE_EXCEEDED")])
    spare = _Client(["unused"])
    with pytest.raises(RuntimeError, match="busy or too slow"):
        _describer(busy, spare).describe(_image())
    assert spare.prompts == []


def test_same_screen_and_question_is_not_asked_twice():
    client = _Client(["A blank page."])
    describer = _describer(client)
    describer.describe(_image(), "what is it?")
    answer, cached = describer.describe(_image(), "what is it?")
    assert cached and answer == "A blank page."
    assert len(client.prompts) == 1


def test_settle_waits_for_a_stable_frame_and_reports_a_changing_one():
    frames = iter([_image("black"), _image("white"), _image("white")])
    frame, settled = wait_for_stable_frame(lambda: next(frames), interval=0)
    assert settled and frame.getpixel((0, 0)) == (255, 255, 255)

    colors = iter(["black", "white"] * 50)
    _, settled = wait_for_stable_frame(lambda: _image(next(colors)), timeout=0.01, interval=0.005)
    assert not settled


def test_format_keeps_plain_text():
    assert format_screen_description("  Notepad   is open. ") == "Notepad is open."


# ---- the desktop tool ----------------------------------------------------


class _Perception:
    def __init__(self, ocr_text="Order #123"):
        self.ocr_text = ocr_text
        self.asked = []

    def window_image(self, application=None, *, settle=True):
        self.asked.append(application)
        return {"image": _image(), "settled": True, "scope": "full_screen", "window": None}

    def read_screen_text(self, application=None):
        return {"text": self.ocr_text, "screenshot_path": "x.png"}


def _desktop(describer, perception=None):
    perception = perception or _Perception()
    cap = DesktopCapability(perception=perception, screen_describer=describer)
    cap._describer_loaded = True
    return cap, perception


def test_look_at_screen_answers_from_vision():
    cap, perception = _desktop(_describer(_Client(["Chrome shows a sign-in page."])))
    result = cap.execute(ToolCall("c", "look_at_screen", {"question": "what is showing?"}))
    assert result.success and result.output == "Chrome shows a sign-in page."
    assert result.verification_details["evidence"] == "visual_observation_only"
    assert perception.asked == ["full_screen"]


def test_without_vision_the_user_still_gets_ocr_text():
    cap, _ = _desktop(None)
    result = cap.execute(ToolCall("c", "look_at_screen", {}))
    assert result.success
    assert "couldn't get a picture description" in result.output and "Order #123" in result.output


def test_automatic_look_without_vision_does_not_repeat_ocr():
    cap, _ = _desktop(None)
    result = cap.execute(ToolCall("c", "look_at_screen", {"foreground": True}))
    assert not result.success and result.metadata["vision_error"]


def test_look_at_screen_is_an_observation_tool():
    assert is_observation_tool("look_at_screen")


# ---- automatic escalation ------------------------------------------------


class _Tools:
    def __init__(self, side_effects):
        self.side_effects = side_effects

    def get(self, name):
        if name == "look_at_screen" or name in self.side_effects:
            return SimpleNamespace(side_effect=self.side_effects.get(name, "low"))
        return None


class _Runner:
    def __init__(self, answer="A 'Select delivery address' dialog covers the page."):
        self.calls = []
        self.answer = answer

    def run(self, call, approved=False):
        self.calls.append(call)
        return ToolResult(call_id=call.call_id, name=call.name, success=True, output=self.answer, verified=True)


def _session(side_effects=None):
    session = AgentSession.__new__(AgentSession)
    session.task = TaskState(goal="buy it", task_id="t")
    session.tools = _Tools(side_effects or {})
    session.tool_runner = _Runner()
    return session


def _observed(output, success=True):
    return ToolResult(call_id="o", name="observe_window", success=success, output=output, verified=success)


def _done(verified=True, success=True):
    return ToolResult(call_id="a", name="click", success=success, verified=verified)


def test_no_vision_when_structure_saw_the_screen():
    session = _session()
    rich = {"elements": [{"id": f"e{i}"} for i in range(10)]}
    assert session._vision_trigger(ToolCall("a", "click", {}), _done(False), _observed(rich), {"screen_changed": True}) is None


def test_vision_triggers():
    session = _session({"click_web_element": "critical"})
    click = ToolCall("a", "click", {"target": "Proceed to checkout"})
    blind = {"elements": [{"id": "e1"}, {"id": "e2"}]}  # window chrome only (e.g. Telegram's Qt UI)
    assert session._vision_trigger(click, _done(False), _observed(blind), None) == "structure_blind"
    assert session._vision_trigger(click, _done(False), _observed(None, success=False), None) == "structure_blind"
    rich = {"elements": [{"id": f"e{i}"} for i in range(10)]}
    assert session._vision_trigger(click, _done(False), _observed(rich), {"screen_changed": False}) == "stalled"
    buy = ToolCall("a", "click_web_element", {"target": "Place your order"})
    assert session._vision_trigger(buy, _done(False), _observed([{"label": "x"}]), None) == "uncertain_final_state"
    assert session._vision_trigger(buy, _done(True), _observed([{"label": "x"}]), None) is None


def test_automatic_look_asks_a_specific_question_and_is_bounded():
    session = _session()
    click = ToolCall("a", "click", {"target": "Proceed to checkout"})
    first = session._look_at_screen_for(click, "stalled")
    assert "dialog covers the page" in first["answer"]
    assert "not proof the task succeeded" in first["note"]
    call = session.tool_runner.calls[0]
    assert call.arguments["foreground"] is True
    assert "Proceed to checkout" in call.arguments["question"] and "did not seem to change" in call.arguments["question"]
    assert session._look_at_screen_for(click, "stalled") is not None
    assert session._look_at_screen_for(click, "stalled") is None  # budget of 2 per task
    assert len(session.tool_runner.calls) == 2


def test_typed_text_is_not_sent_to_vision():
    session = _session()
    session._look_at_screen_for(ToolCall("a", "type_text", {"text": "my secret message"}), "stalled")
    assert "secret" not in session.tool_runner.calls[0].arguments["question"]


def test_vision_answer_never_backs_a_sent_claim():
    from advi.agent.session import _SEND_TOOLS

    assert "look_at_screen" not in _SEND_TOOLS


def test_vision_is_off_without_keys_or_when_disabled(monkeypatch):
    monkeypatch.setenv("ADVI_VISION", "off")
    assert screen_describer.describer_from_settings() is None
    assert not ScreenDescriber(api_keys=[], model="m").available
