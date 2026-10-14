from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..providers import LLMProvider
from .action_plan_schema import ActionPlan
from .execution_loop import ExecutionLoop, ExecutionLoopResult
from .intent_schema import Intent
from .intent_service import IntentService
from .planner_service import PlannerService
from advi.core.pipeline_trace import trace


logger = logging.getLogger(__name__)


class FallbackService:
    """
    Standalone agentic desktop fallback pipeline.

    Intent generation → action planning → execution.

    This service does not interact with ADVI's built-in
    planner, executor, or task coordinator.
    """

    def __init__(
        self,
        provider: LLMProvider,
        intent_prompt_path: str | Path,
        planner_prompt_path: str | Path,
        execution_loop: ExecutionLoop | None = None,
    ) -> None:
        self.intent_service = IntentService(
            provider=provider,
            prompt_path=intent_prompt_path,
        )

        self.planner_service = PlannerService(
            provider=provider,
            prompt_path=planner_prompt_path,
        )

        self.execution_loop = (
            execution_loop
            or ExecutionLoop()
        )

    def process(
        self,
        user_input: str,
        *,
        approved: bool = False,
        stage: str | None = None,
        operation_intent: str | None = None,
        protected_parameters: dict[str, Any] | None = None,
    ) -> ExecutionLoopResult:
        logger.info(
            "Fallback processing request: %r",
            user_input,
        )
        trace(
            "gateway.fallback_service.process.enter",
            source_component="FallbackService",
            user_input=user_input,
            approved=approved,
        )

        if stage == "send_prepared":
            parameters = dict(protected_parameters or {})
            result = self.execution_loop.send_prepared_email(
                recipient=str(parameters.get("recipient") or parameters.get("to") or ""),
                subject=str(parameters.get("subject") or ""),
                body=str(parameters.get("body") or parameters.get("content") or ""),
            )
            trace(
                "gateway.fallback_service.process.exit",
                source_component="FallbackService",
                success=result.success,
                fallback_stage=stage,
                text=getattr(result, "text", None),
                metadata=getattr(result, "metadata", None),
            )
            return result

        normalized_parameters = dict(protected_parameters or {})
        authoritative_intent = str(operation_intent or "").strip()
        if authoritative_intent:
            # The primary ADVI brain already chose the operation. David must not
            # reinterpret that operation from scratch; its LLM remains responsible
            # for planning the GUI execution inside the supplied intent.
            intent_goal = {
                "email_draft_create": "send_email",
                "email_send": "send_email",
                "email_reply": "email_reply",
                "search": "web_search",
            }.get(authoritative_intent, authoritative_intent)
            intent = Intent(
                goal=intent_goal,
                entities=normalized_parameters,
                constraints=[],
                confirmation_required=(intent_goal == "send_email"),
                missing_information=[],
            )
        else:
            intent = self.intent_service.extract_intent(user_input)
            if normalized_parameters:
                # Merge exact protected values without allowing the fallback LLM
                # to rewrite them. Its interpretation can fill missing fields, but
                # an already-captured literal always wins.
                intent.entities = {**dict(intent.entities or {}), **normalized_parameters}

        logger.info(
            "Fallback intent: %s",
            intent.model_dump_json(),
        )
        trace(
            "gateway.fallback_service.intent",
            source_component="FallbackService",
            intent=intent.model_dump(),
        )

        if intent.missing_information:
            logger.info(
                "Fallback intent is missing information: %s",
                intent.missing_information,
            )
            result = ExecutionLoopResult(
                success=False,
                results=[],
            )
            trace("gateway.fallback_service.process.exit", source_component="FallbackService", success=False, reason="missing_information", missing=intent.missing_information)
            return result

        # Email is deliberately handled without David's generic planner. The
        # planner previously generated a final "click Send" action, so a click
        # target could fail after the compose page was already successfully built.
        # Prepare the draft before the single irreversible-send confirmation. The
        # primary ADVI confirmation is therefore about the actual send, not about
        # creating the draft.
        if intent.goal == "send_email":
            entities = dict(intent.entities or {})
            recipient = str(entities.get("recipient_email") or entities.get("to") or entities.get("recipient") or "").strip()
            subject = str(entities.get("subject") or "").strip()
            body = str(entities.get("content") or entities.get("body") or "").strip()
            prepared = self.execution_loop.prepare_email_draft(
                recipient=recipient, subject=subject, body=body
            )
            return prepared

        if intent.confirmation_required and not approved:
            logger.info(
                "Fallback intent requires confirmation."
            )

            result = ExecutionLoopResult(
                success=False,
                results=[],
                metadata={
                    "awaiting_confirmation": True,
                    "goal": intent.goal,
                    "entities": intent.entities,
                    "reason": "confirmation_required",
                },
            )
            trace("gateway.fallback_service.process.exit", source_component="FallbackService", success=False, reason="confirmation_required", metadata=result.metadata)
            return result

        plan = self.planner_service.generate_plan(
            intent,
            request_text=user_input,
        )

        logger.info(
            "Fallback action plan: %s",
            plan.model_dump_json(),
        )
        trace(
            "gateway.fallback_service.plan",
            source_component="FallbackService",
            plan=plan.model_dump(),
        )

        executable_actions = [
            action for action in plan.actions
            if getattr(action, "action", "") != "finish"
        ]
        if not executable_actions:
            reason = plan.reason or "planner_returned_no_execution_actions"
            logger.warning(
                "David planner returned no executable actions; refusing false success. reason=%s",
                reason,
            )
            result = ExecutionLoopResult(
                success=False,
                results=[],
                metadata={
                    "fallback_failed": True,
                    "reason": reason,
                    "goal": intent.goal,
                },
            )
            trace(
                "gateway.fallback_service.process.exit",
                source_component="FallbackService",
                success=False,
                reason=reason,
                goal=intent.goal,
            )
            return result

        loop_result = self.execution_loop.run(
            plan
        )
        trace(
            "gateway.fallback_service.process.exit",
            source_component="FallbackService",
            success=loop_result.success,
            results=loop_result.results,
            text=getattr(loop_result, "text", None),
            metadata=getattr(loop_result, "metadata", None),
        )
        return loop_result