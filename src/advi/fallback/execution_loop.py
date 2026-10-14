from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
import urllib.parse
from typing import Any

from .action_plan_schema import Action, ActionPlan
from .execution_context import ExecutionContext
from .executor import ActionResult, FallbackExecutor
from .perception import ScreenPerception

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionLoopResult:
    success: bool
    results: list[ActionResult]
    metadata: dict[str, Any] = field(default_factory=dict)
    text: str = ""


class ExecutionLoop:
    """
    Execute a fallback action plan in one continuous run.

    Key Requirements & Architecture:
    1. Target Ownership: Maintains persistent ExecutionContext (target_tab_id, target_hwnd,
       current_application) across the entire plan.
    2. Continuous Execution: Keeps execution attached to the target Chrome tab/window
       without losing focus or jumping between tabs.
    3. State Awareness & Idempotency: Inspects state before retrying or re-executing actions.
    4. Structured Logging: Logs detailed execution info (action, target, focus, success, error, attempt).
    """

    def __init__(
        self,
        perception: ScreenPerception | None = None,
        executor: FallbackExecutor | None = None,
        context: ExecutionContext | None = None,
    ) -> None:
        self.perception = perception or ScreenPerception()
        self.context = context or ExecutionContext()
        self.executor = executor or FallbackExecutor(context=self.context)
        # David is intentionally isolated from the native AgentSession watchdog,
        # so keep a small local budget here as well. The outer task sees one
        # david_fallback call, while this loop may execute many GUI actions.
        self.max_actions = 40
        self.max_failures = 6
        self.max_elapsed_seconds = 300.0

    @property
    def current_application(self) -> str | None:
        return self.context.current_application

    @current_application.setter
    def current_application(self, value: str | None) -> None:
        self.context.current_application = value

    def prepare_email_draft(self, *, recipient: str, subject: str, body: str) -> ExecutionLoopResult:
        """Open Gmail compose and verify the draft fields; never send at this stage."""
        results: list[ActionResult] = []
        self.context.reset()
        open_result = self.executor.execute_action(Action(action="open_application", focus="Chrome", parameters={"application": "Chrome"}), context=self.context)
        results.append(open_result)
        if not open_result.success:
            return ExecutionLoopResult(False, results, {"stage": "draft_prepare_failed", "reason": open_result.error or "Chrome did not open."}, text=open_result.error or "I couldn't open Chrome for the email draft.")

        query = urllib.parse.urlencode({"view": "cm", "fs": "1", "to": recipient, "su": subject, "body": body})
        navigate = self.executor.execute_action(Action(action="navigate", focus="Chrome", parameters={"url": "https://mail.google.com/mail/?" + query}), context=self.context)
        results.append(navigate)
        if not navigate.success:
            return ExecutionLoopResult(False, results, {"stage": "draft_prepare_failed", "reason": navigate.error or "Gmail compose navigation failed."}, text=navigate.error or "I couldn't open Gmail compose.")

        verified = self.executor.cdp_browser.verify_gmail_compose(
            self.context.target_tab_id or "", recipient=recipient, subject=subject, body=body
        ) if self.context.target_tab_id else False
        if not verified:
            return ExecutionLoopResult(False, results, {"stage": "draft_prepare_failed", "reason": "gmail_compose_not_verified", "recipient": recipient, "subject": subject}, text="I opened Gmail, but I couldn't verify the draft fields.")

        return ExecutionLoopResult(
            True,
            results,
            {"stage": "draft_prepared", "awaiting_send_confirmation": True, "recipient": recipient, "subject": subject, "body": body, "tab_id": self.context.target_tab_id},
            text=f"Draft created and ready to send to {recipient}. Should I send it, sir?",
        )

    def send_prepared_email(
        self,
        *,
        recipient: str = "",
        subject: str = "",
        body: str = "",
    ) -> ExecutionLoopResult:
        """Re-verify the prepared Gmail draft, then send it through the controlled UI."""
        tab_id = self.context.target_tab_id
        if not tab_id:
            return ExecutionLoopResult(
                False, [],
                {"stage": "send_failed", "reason": "prepared_gmail_tab_missing"},
                text="The prepared Gmail draft is no longer open.",
            )

        # The user can edit the visible draft between preparation and confirmation.
        # Never send an altered recipient, subject, or body just because the Send
        # button is still present. Re-check the exact prepared fields immediately
        # before the irreversible click.
        if any(value != "" for value in (recipient, subject, body)):
            fields_verified = self.executor.cdp_browser.verify_gmail_compose(
                tab_id, recipient=recipient, subject=subject, body=body, timeout=5.0
            )
            if not fields_verified:
                return ExecutionLoopResult(
                    False, [],
                    {"stage": "send_failed", "reason": "prepared_gmail_fields_changed",
                     "recipient": recipient, "subject": subject},
                    text="The Gmail draft changed before sending, so I did not send it.",
                )

        ok = self.executor.cdp_browser.send_gmail_compose(tab_id)
        if not ok:
            return ExecutionLoopResult(
                False, [],
                {"stage": "send_failed", "reason": "gmail_send_not_verified"},
                text="The draft was open, but I couldn't verify the Send action.",
            )
        return ExecutionLoopResult(
            True, [], {"stage": "sent", "send_verified": True},
            text="The email was sent successfully through the Gmail interface.",
        )

    def run(self, plan: ActionPlan) -> ExecutionLoopResult:
        results: list[ActionResult] = []

        # Reset execution context for every new plan
        self.context.reset()

        total_actions = len(plan.actions)
        started = time.monotonic()
        failures = 0
        if total_actions > self.max_actions:
            logger.warning("David plan exceeds action budget: %d > %d", total_actions, self.max_actions)
            return ExecutionLoopResult(
                success=False,
                results=[],
                metadata={"reason": "maximum_action_count_reached", "total_actions": total_actions, "max_actions": self.max_actions},
            )
        logger.info("Starting ExecutionLoop for plan with %d actions.", total_actions)

        for index, action in enumerate(plan.actions, start=1):
            if time.monotonic() - started > self.max_elapsed_seconds:
                logger.warning("David ExecutionLoop timed out before Action #%d.", index)
                return ExecutionLoopResult(
                    success=False,
                    results=results,
                    metadata={"reason": "task_timeout", "failed_action_index": index, "elapsed_seconds": time.monotonic() - started},
                )
            logger.info("=" * 60)
            logger.info(
                "Action #%d/%d: action=%r | focus=%r | parameters=%s",
                index,
                total_actions,
                action.action,
                action.focus,
                action.parameters,
            )

            result = self._execute_action(action)
            results.append(result)

            logger.info(
                "Result Action #%d (%s): success=%s | data=%r | error=%r",
                index,
                action.action,
                result.success,
                result.data,
                result.error,
            )

            if not result.success:
                failures += 1
                logger.warning("ExecutionLoop stopped at Action #%d (%s): %s", index, action.action, result.error)
                if failures >= self.max_failures:
                    return ExecutionLoopResult(
                        success=False,
                        results=results,
                        metadata={"failed_action_index": index, "failed_action": action.action, "reason": "maximum_failure_count_reached", "failures": failures},
                    )
                return ExecutionLoopResult(
                    success=False,
                    results=results,
                    metadata={"failed_action_index": index, "failed_action": action.action, "failures": failures},
                )

            self._update_context(action)

        logger.info("ExecutionLoop completed successfully.")
        return ExecutionLoopResult(
            success=True,
            results=results,
            metadata={"total_actions": total_actions},
        )

    def _ensure_current_application_focus(self, action_name: str) -> ActionResult | None:
        """
        Ensure application focus context is active.
        """
        if action_name in {"open_application", "finish"}:
            return None

        if not self.context.current_application:
            return None

        logger.info("Ensuring foreground application context: %s", self.context.current_application)
        self.executor.ensure_application_focus(self.context.current_application)
        return None

    def _execute_action(self, action: Any) -> ActionResult:
        # Update focus context if explicitly provided
        action_focus = getattr(action, "focus", None)
        if action_focus:
            requested_focus = str(action_focus).strip()
            if requested_focus:
                self.context.set_application(requested_focus)

        # 1. Restore OS focus when necessary for non-CDP actions
        focus_error = self._ensure_current_application_focus(action.action)
        if focus_error is not None:
            return focus_error

        parameters = dict(action.parameters)
        target = parameters.get("target")

        # 2. Web actions targeting an active Chrome CDP tab: CDP executes directly in-page
        if self.context.is_chrome_target() and action.action in {"search", "navigate", "click", "submit"}:
            return self.executor.execute_action(action, context=self.context)

        # 3. Actions without UI target
        if not target:
            return self.executor.execute_action(action, context=self.context)

        target_string = str(target).strip()
        if not target_string:
            return ActionResult(
                action=action.action,
                success=False,
                error="Target parameter was empty.",
            )

        # 4. Perception target resolution for desktop / UIA / Vision targets
        logger.info("Resolving perception target %r for action %r in app %r", target_string, action.action, self.context.current_application)
        perception_result = self.perception.find(
            target=target_string,
            action=action.action,
            application=self.context.current_application,
        )

        if not perception_result.found:
            return ActionResult(
                action=action.action,
                success=False,
                error=perception_result.error or f"Target {target_string!r} could not be resolved.",
            )

        # Restore focus again after perception (perception captures screenshots / inspects windows)
        focus_error = self._ensure_current_application_focus(action.action)
        if focus_error is not None:
            return focus_error

        parameters["resolved_target"] = perception_result.target
        action = action.model_copy(update={"parameters": parameters})

        return self.executor.execute_action(action, context=self.context)

    def _update_context(self, action: Any) -> None:
        """Update context state following successful action execution."""
        if action.action == "open_application":
            app = action.parameters.get("application")
            if app:
                self.context.set_application(str(app).strip())