"""Application skills: typed, verified, single-call operations for frequent apps."""
from __future__ import annotations

import os
from typing import Any, Callable

from advi.tools.contracts import ToolCall, ToolResult, VerificationStatus

from .whatsapp import SkillOutcome, WhatsAppSkill


class MessagingCapability:
    """WhatsApp Desktop skills exposed as model tools.

    One tool call runs the whole verified flow locally, instead of the model
    driving click/type/read_screen one LLM round trip at a time.
    """

    SUPPORTED_ACTIONS = {"whatsapp_send_message", "whatsapp_open_chat"}

    def __init__(self, skill_factory: Callable[[], WhatsAppSkill] | None = None) -> None:
        self._skill_factory = skill_factory or _default_skill
        self._skill: WhatsAppSkill | None = None

    def is_available(self) -> bool:
        return os.name == "nt"

    def _get_skill(self) -> WhatsAppSkill:
        if self._skill is None:
            self._skill = self._skill_factory()
        return self._skill

    def _execute_whatsapp_send_message(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        return self.execute(call, approval_granted=approval_granted)

    def _execute_whatsapp_open_chat(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        return self.execute(call, approval_granted=approval_granted)

    def execute(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        action = call.action
        chat = str(call.parameters.get("chat") or "").strip()
        if not chat:
            return ToolResult(call_id=call.call_id, name=action, success=False, error="Which WhatsApp chat? No chat name was given.")
        try:
            skill = self._get_skill()
            if action == "whatsapp_open_chat":
                outcome = skill.open_chat(chat)
            elif action == "whatsapp_send_message":
                text = str(call.parameters.get("text") or "")
                if not text.strip():
                    return ToolResult(call_id=call.call_id, name=action, success=False, error="No message text was given.")
                outcome = skill.send_message(chat, text)
            else:
                return ToolResult(call_id=call.call_id, name=action, success=False, error=f"Unsupported messaging action: {action}")
        except Exception as exc:
            return ToolResult(call_id=call.call_id, name=action, success=False, error=f"WhatsApp automation failed: {exc}")
        return _to_result(call, outcome)


class DesktopUiCapability:
    """observe_window / interact: structured perception and verified actions for any app."""

    SUPPORTED_ACTIONS = {"observe_window", "interact"}

    def __init__(
        self,
        desktop_context: Any = None,
        observer_factory: Callable[[], "UiObserver"] | None = None,
    ) -> None:
        self.desktop_context = desktop_context
        self._observer_factory = observer_factory or self._default_observer
        self._observer: "UiObserver | None" = None

    def _default_observer(self) -> "UiObserver":
        from .ui_observer import UiObserver
        from .uia import WindowsUiaDriver

        return UiObserver(
            WindowsUiaDriver(),
            target_hwnd=lambda: getattr(self.desktop_context, "target_hwnd", None),
        )

    def is_available(self) -> bool:
        return os.name == "nt"

    def _get_observer(self) -> "UiObserver":
        if self._observer is None:
            self._observer = self._observer_factory()
        return self._observer

    def _execute_observe_window(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        return self.execute(call, approval_granted=approval_granted)

    def _execute_interact(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        return self.execute(call, approval_granted=approval_granted)

    def execute(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        action = call.action
        try:
            observer = self._get_observer()
            if action == "observe_window":
                application = str(call.parameters.get("application") or "").strip() or None
                observation = observer.observe(application)
                if observation is None:
                    target = f"a window titled '{application}'" if application else "an application window to observe"
                    return ToolResult(call_id=call.call_id, name=action, success=False, error=f"I couldn't find {target}.")
                payload = observation.payload()
                return ToolResult(
                    call_id=call.call_id, name=action, success=True, output=payload,
                    human_readable=f"Observed {len(observation.elements)} interactive elements in '{observation.window.name}'.",
                    verified=True, verification_status=VerificationStatus.VERIFIED,
                    verification_details={"method": "uia_interactive_elements", "state_hash": observation.state_hash},
                    metadata={"read_only": True},
                )
            if action == "interact":
                outcome = observer.interact(
                    str(call.parameters.get("element_id") or ""),
                    str(call.parameters.get("action") or "click"),
                    call.parameters.get("text"),
                )
                details = dict(outcome.details)
                observation = details.pop("observation", None)
                return ToolResult(
                    call_id=call.call_id, name=action, success=outcome.ok or bool(details.get("method")),
                    output={"result": outcome.message, "screen_changed": details.get("screen_changed"), "observation": observation},
                    human_readable=outcome.message,
                    error=None if outcome.ok or details.get("method") else outcome.message,
                    verified=outcome.ok,
                    verification_status=VerificationStatus.VERIFIED if outcome.ok else (
                        VerificationStatus.UNCERTAIN if details.get("method") else VerificationStatus.FAILED
                    ),
                    verification_details={"method": "uia_interact", **details},
                )
        except Exception as exc:
            return ToolResult(call_id=call.call_id, name=action, success=False, error=f"UI automation failed: {exc}")
        return ToolResult(call_id=call.call_id, name=action, success=False, error=f"Unsupported action: {action}")


def _default_skill() -> WhatsAppSkill:
    from .uia import WindowsUiaDriver

    return WhatsAppSkill(WindowsUiaDriver())


def _to_result(call: ToolCall, outcome: SkillOutcome) -> ToolResult:
    details: dict[str, Any] = {"method": "whatsapp_uia_skill", "stage": outcome.stage, **outcome.evidence}
    if outcome.candidates:
        details["candidates"] = outcome.candidates
    # The skill only fails *before* the irreversible step, or reports that it
    # could not confirm the send. Neither may be retried blindly.
    if not outcome.ok and outcome.stage == "sent":
        details["side_effect_may_have_happened"] = True
    return ToolResult(
        call_id=call.call_id,
        name=call.action,
        success=outcome.ok,
        output={"stage": outcome.stage, "chat": outcome.evidence.get("chat_title")},
        human_readable=outcome.message,
        error=None if outcome.ok else outcome.message,
        verified=outcome.ok,
        verification_status=VerificationStatus.VERIFIED if outcome.ok else (
            VerificationStatus.UNCERTAIN if outcome.stage == "sent" else VerificationStatus.FAILED
        ),
        verification_details=details,
        metadata={"stage": outcome.stage, "app": "whatsapp"},
    )
