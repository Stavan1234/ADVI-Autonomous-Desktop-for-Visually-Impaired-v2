from __future__ import annotations

import json
import logging
import re
import urllib.parse
from pathlib import Path

from ..providers import LLMProvider
from .action_plan_schema import Action, ActionPlan
from .intent_schema import Intent


logger = logging.getLogger(__name__)


class PlannerService:
    """
    Generate a semantic desktop action plan from an intent.

    The LLM is responsible for:
        - deciding the ordered action sequence
        - choosing the semantic action parameters
        - describing UI targets semantically

    Deterministic validation is responsible for:
        - ensuring explicit Intent actions are preserved
        - preventing unsupported action types
        - ensuring required parameters exist
        - adding missing explicit Intent actions when necessary
        - ensuring finish is the final action
        - maintaining the application/window focus for each action

    The planner does NOT perform website-specific grounding.

    For example, it does not decide that:
        - the first search must use Chrome's address bar
        - a YouTube search must use "YouTube search box"
        - an Amazon search must use "Amazon search box"

    UI grounding is handled later by the execution/perception layer.
    """

    SUPPORTED_ACTIONS = {
        "open_application",
        "open_file",
        "open_folder",
        "focus_window",
        "click",
        "double_click",
        "right_click",
        "type_text",
        "press_key",
        "hotkey",
        "select_file",
        "attach_file",
        "open_folder",
        "wait",
        "scroll",
        "search",
        "navigate",
        "submit",
        "close_window",
        "finish",
    }

    ACTIONS_REQUIRING_TARGET = {
        "click",
        "double_click",
        "right_click",
        "type_text",
        "select_file",
        "attach_file",
        "open_folder",
        "submit",
        "close_window",
        "focus_window",
    }

    def __init__(
        self,
        provider: LLMProvider,
        prompt_path: str | Path,
    ) -> None:
        self.provider = provider

        prompt_file = Path(prompt_path)

        with prompt_file.open(
            "r",
            encoding="utf-8",
        ) as file:
            self.prompt = file.read()

    # =========================================================
    # PLAN GENERATION
    # =========================================================

    def generate_plan(
        self,
        intent: Intent,
        request_text: str | None = None,
    ) -> ActionPlan:
        """
        Generate an executor-ready action plan from a validated Intent.
        """

        # -----------------------------------------------------
        # Missing information
        # -----------------------------------------------------

        if getattr(
            intent,
            "missing_information",
            None,
        ):
            return ActionPlan(
                actions=[],
                reason="missing_information",
            )

        # -----------------------------------------------------
        # Deterministic recovery plans for operations where a second
        # planner adds failure risk but the action sequence is simple.
        # David remains the execution/fallback subsystem; these plans
        # only prevent planner-quota/format failures from stopping it.
        # -----------------------------------------------------
        deterministic = self._build_deterministic_plan(intent, request_text=request_text)
        if deterministic is not None:
            return deterministic

        # -----------------------------------------------------
        # Generate initial plan using the LLM.
        # -----------------------------------------------------

        full_prompt = (
            f"{self.prompt}\n\n"
            "Intent JSON:\n\n"
            f"{intent.model_dump_json(indent=2)}"
        )

        result = self.provider.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are ADVI's agentic desktop "
                        "action planning component. "
                        "Return only valid JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": full_prompt,
                },
            ]
        )

        response = result.text.strip()

        plan_dict = json.loads(response)

        plan = ActionPlan(
            **plan_dict
        )

        # -----------------------------------------------------
        # Deterministic validation.
        # -----------------------------------------------------

        plan = self._validate_and_complete_plan(
            intent=intent,
            plan=plan,
        )

        return plan

    def _build_deterministic_plan(self, intent: Intent, *, request_text: str | None = None) -> ActionPlan | None:
        goal = str(getattr(intent, "goal", "") or "").casefold().strip()
        entities = getattr(intent, "entities", {})
        if not isinstance(entities, dict):
            entities = {}

        if goal == "web_search":
            query = str(entities.get("query") or "").strip()
            if query:
                url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
                return ActionPlan(goal="web_search", actions=[
                    Action(action="open_application", focus="Chrome", parameters={"application":"Chrome"}),
                    Action(action="navigate", focus="Chrome", parameters={"url":url}),
                    Action(action="finish", focus=None, parameters={}),
                ])

        if goal == "send_email":
            to = str(entities.get("recipient_email") or entities.get("to") or entities.get("recipient") or "").strip()
            subject = str(entities.get("subject") or "").strip()
            body = str(entities.get("content") or entities.get("body") or "").strip()
            if to and body:
                query = urllib.parse.urlencode({"view":"cm", "fs":"1", "to":to, "su":subject, "body":body}, doseq=False)
                url = "https://mail.google.com/mail/?" + query
                return ActionPlan(
                    goal="send_email",
                    actions=[
                        Action(action="open_application", focus="Chrome", parameters={"application":"Chrome"}),
                        Action(action="navigate", focus="Chrome", parameters={"url":url}),
                        Action(action="wait", focus=None, parameters={"seconds":2}),
                        Action(action="finish", focus=None, parameters={}),
                    ],
                )

        if goal in {"open_folder", "open_directory"} or goal.startswith("open_folder"):
            path = str(entities.get("path") or entities.get("directory") or entities.get("folder") or "").strip()
            if path:
                return ActionPlan(goal="open_folder", actions=[
                    Action(action="open_folder", focus=None, parameters={"path": path}),
                    Action(action="finish", focus=None, parameters={}),
                ])

        if goal in {"open_file", "open_document"} or goal.startswith("open_file"):
            path = str(entities.get("path") or entities.get("file_path") or entities.get("filename") or entities.get("file") or "").strip()
            if path:
                return ActionPlan(
                    goal=goal or "open_file",
                    actions=[
                        Action(action="open_file", focus=None, parameters={"path":path}),
                        Action(action="finish", focus=None, parameters={}),
                    ],
                )

        # David's intent extractor is useful but should not be a single point of
        # failure for an explicit local-file request. If the request contains a
        # concrete Windows path/file name, recover that artifact deterministically
        # instead of spending another LLM call on a task the executor already knows
        # how to perform. This is intentionally limited to opening local files.
        raw_request = str(request_text or "").strip()
        if raw_request and any(token in goal for token in ("unsupported", "open", "read")):
            path_match = re.search(r'(?i)([A-Za-z]:\\[^\r\n,;]+?\.(?:pdf|txt|md|csv|json|docx|xlsx|pptx|rtf|jpg|jpeg|png|gif|webp))', raw_request)
            if path_match:
                path = path_match.group(1).strip().rstrip('.,)\"\'')
                return ActionPlan(
                    goal="open_file",
                    actions=[
                        Action(action="open_file", focus=None, parameters={"path":path}),
                        Action(action="finish", focus=None, parameters={}),
                    ],
                )
        return None

    # =========================================================
    # PLAN VALIDATION
    # =========================================================

    def _validate_and_complete_plan(
        self,
        intent: Intent,
        plan: ActionPlan,
    ) -> ActionPlan:
        """
        Validate the LLM-generated plan against the Intent.

        Important:
            The LLM's semantic parameters are preserved.

        This method does NOT reconstruct search targets from
        website names and does NOT impose browser-specific
        behavior.

        Focus is maintained separately from semantic target
        resolution. Once an application is opened, subsequent
        actions inherit that application's focus unless an action
        explicitly specifies another focus.
        """

        entities = getattr(
            intent,
            "entities",
            {},
        )

        if not isinstance(
            entities,
            dict,
        ):
            return self._finalize_plan(
                plan.actions
            )

        secondary_actions = entities.get(
            "actions",
            [],
        )

        if not isinstance(
            secondary_actions,
            list,
        ):
            secondary_actions = []

        # -----------------------------------------------------
        # If Intent contains no explicit secondary actions,
        # only validate the LLM plan.
        # -----------------------------------------------------

        if not secondary_actions:
            return self._finalize_plan(
                plan.actions
            )

        # -----------------------------------------------------
        # Normalize the LLM actions.
        # -----------------------------------------------------

        llm_actions = [
            action
            for action in plan.actions
            if action.action != "finish"
        ]

        corrected_actions: list[Action] = []

        # -----------------------------------------------------
        # Preserve planner-generated setup actions.
        #
        # Example:
        #
        # Intent:
        #   search YouTube
        #
        # Planner:
        #   open_application Chrome
        #   search ...
        #
        # The open_application action is not itself an
        # Intent secondary action, so it is preserved.
        # -----------------------------------------------------

        secondary_index = 0

        for action in llm_actions:

            if action.action == "open_application":
                corrected_actions.append(
                    action
                )
                continue

            if secondary_index >= len(
                secondary_actions
            ):
                # Extra LLM action not represented in Intent.
                #
                # Keep it only if it is a legitimate action
                # explicitly supported by the planner.
                if self._is_valid_action(
                    action
                ):
                    corrected_actions.append(
                        action
                    )

                continue

            secondary = secondary_actions[
                secondary_index
            ]

            if not isinstance(
                secondary,
                dict,
            ):
                secondary_index += 1
                continue

            expected_action = secondary.get(
                "action"
            )

            # -------------------------------------------------
            # The action type generated by the LLM must match
            # the explicit Intent action.
            # -------------------------------------------------

            if (
                expected_action
                and action.action
                == expected_action
            ):
                validated_action = (
                    self._validate_action(
                        action
                    )
                )

                if validated_action is not None:
                    corrected_actions.append(
                        validated_action
                    )

                secondary_index += 1
                continue

            # -------------------------------------------------
            # LLM changed the explicit action type.
            #
            # Example:
            #
            # Intent:
            #   search
            #
            # LLM:
            #   type_text
            #
            # Do not silently accept that.
            #
            # Instead construct the required action using
            # Intent parameters as a fallback.
            # -----------------------------------------------------

            logger.warning(
                "Planner changed Intent action "
                "from '%s' to '%s'. "
                "Restoring the Intent action type.",
                expected_action,
                action.action,
            )

            fallback_action = (
                self._build_action_from_intent(
                    secondary
                )
            )

            if fallback_action is not None:
                corrected_actions.append(
                    fallback_action
                )

            secondary_index += 1

        # -----------------------------------------------------
        # Add any explicit Intent actions that the LLM omitted.
        # -----------------------------------------------------

        while secondary_index < len(
            secondary_actions
        ):

            secondary = secondary_actions[
                secondary_index
            ]

            if isinstance(
                secondary,
                dict,
            ):
                missing_action = (
                    self._build_action_from_intent(
                        secondary
                    )
                )

                if missing_action is not None:
                    corrected_actions.append(
                        missing_action
                    )

            secondary_index += 1

        return self._finalize_plan(
            corrected_actions
        )

    # =========================================================
    # ACTION VALIDATION
    # =========================================================

    def _validate_action(
        self,
        action: Action,
        current_focus: str | None = None,
    ) -> Action | None:
        """
        Validate one LLM-generated Action without replacing
        its semantic target.

        Focus handling:
            - Explicit action.focus is preserved.
            - If focus is missing, the current application focus
              is inherited.
            - open_application establishes a new application
              focus from its application parameter.
            - finish has no focus.

        The planner is allowed to say things such as:

            "search box on the current website"

        or:

            "site search field"

        These remain semantic targets.

        Actual UI grounding happens later.
        """

        action_name = action.action

        if action_name not in self.SUPPORTED_ACTIONS:
            logger.warning(
                "Unsupported planner action: %s",
                action_name,
            )
            return None

        parameters = dict(
            action.parameters
        )

        # -----------------------------------------------------
        # Determine focus.
        #
        # Explicit focus from the LLM is preserved.
        # Otherwise inherit the current application focus.
        # -----------------------------------------------------

        focus = getattr(
            action,
            "focus",
            None,
        )

        if not focus:
            focus = current_focus

        # -----------------------------------------------------
        # OPEN APPLICATION
        # -----------------------------------------------------

        if action_name == "open_application":

            application = parameters.get(
                "application"
            )

            if not application:
                logger.warning(
                    "open_application has no application."
                )
                return None

            # Opening an application establishes the focus.
            resolved_focus = str(
                application
            )

            return Action(
                action="open_application",
                focus=resolved_focus,
                parameters={
                    "application": application,
                },
            )

        # -----------------------------------------------------
        # SEARCH
        # -----------------------------------------------------

        if action_name == "search":

            query = parameters.get(
                "query"
            )

            target = parameters.get(
                "target"
            )

            if not query:
                logger.warning(
                    "Search action has no query."
                )
                return None

            if not target:
                logger.warning(
                    "Search action has no semantic target."
                )
                return None

            return Action(
                action="search",
                focus=focus,
                parameters={
                    "target": str(target),
                    "query": query,
                },
            )

        # -----------------------------------------------------
        # NAVIGATE
        # -----------------------------------------------------

        if action_name == "navigate":

            url = parameters.get(
                "url"
            )

            if not url:
                logger.warning(
                    "Navigate action has no URL."
                )
                return None

            return Action(
                action="navigate",
                focus=focus,
                parameters={
                    "url": url,
                },
            )

        # -----------------------------------------------------
        # TARGET-BASED ACTIONS
        # -----------------------------------------------------

        if action_name in self.ACTIONS_REQUIRING_TARGET:

            target = parameters.get(
                "target"
            )

            if not target:
                logger.warning(
                    "%s action has no target.",
                    action_name,
                )
                return None

            cleaned_parameters = {
                "target": target,
            }

            # type_text additionally requires value.
            if action_name == "type_text":

                if "value" not in parameters:
                    logger.warning(
                        "type_text action has no value."
                    )
                    return None

                cleaned_parameters[
                    "value"
                ] = parameters[
                    "value"
                ]

            return Action(
                action=action_name,
                focus=focus,
                parameters=cleaned_parameters,
            )

        # -----------------------------------------------------
        # PRESS KEY
        # -----------------------------------------------------

        if action_name == "press_key":

            key = parameters.get(
                "key"
            )

            if not key:
                return None

            return Action(
                action="press_key",
                focus=focus,
                parameters={
                    "key": str(key).upper(),
                },
            )

        # -----------------------------------------------------
        # HOTKEY
        # -----------------------------------------------------

        if action_name == "hotkey":

            keys = parameters.get(
                "keys"
            )

            if not isinstance(
                keys,
                list,
            ):
                return None

            if len(keys) < 2:
                return None

            return Action(
                action="hotkey",
                focus=focus,
                parameters={
                    "keys": [
                        str(key).upper()
                        for key in keys
                    ],
                },
            )

        # -----------------------------------------------------
        # WAIT
        # -----------------------------------------------------

        if action_name == "wait":

            seconds = parameters.get(
                "seconds"
            )

            if seconds is None:
                return None

            return Action(
                action="wait",
                focus=focus,
                parameters={
                    "seconds": seconds,
                },
            )

        # -----------------------------------------------------
        # SCROLL
        # -----------------------------------------------------

        if action_name == "scroll":

            amount = parameters.get(
                "amount"
            )

            if amount is None:
                return None

            return Action(
                action="scroll",
                focus=focus,
                parameters={
                    "amount": amount,
                },
            )

        # -----------------------------------------------------
        # FINISH
        # -----------------------------------------------------

        if action_name == "finish":

            return Action(
                action="finish",
                focus=None,
                parameters={},
            )

        # -----------------------------------------------------
        # Any other supported action
        # -----------------------------------------------------

        return Action(
            action=action_name,
            focus=focus,
            parameters=parameters,
        )

    # =========================================================
    # INTENT ACTION FALLBACK
    # =========================================================

    def _build_action_from_intent(
        self,
        source: dict,
    ) -> Action | None:
        """
        Build an Action directly from an Intent secondary action.

        This is only a safety fallback when the LLM:
            - changes an explicit action type
            - omits an explicit action

        No website-specific assumptions are introduced here.
        """

        action_name = source.get(
            "action"
        )

        if not action_name:
            return None

        if action_name not in self.SUPPORTED_ACTIONS:
            logger.warning(
                "Unsupported Intent action: %s",
                action_name,
            )
            return None

        parameters = self._build_parameters(
            action_name,
            source,
        )

        if parameters is None:
            return None

        # -----------------------------------------------------
        # Use explicit focus from the Intent if available.
        #
        # If the Intent does not contain focus, the final
        # normalization pass will inherit the current focus.
        # -----------------------------------------------------

        focus = source.get(
            "focus"
        )

        return Action(
            action=action_name,
            focus=focus,
            parameters=parameters,
        )

    # =========================================================
    # PARAMETER BUILDING
    # =========================================================

    def _build_parameters(
        self,
        action_name: str,
        source: dict,
    ) -> dict | None:
        """
        Convert an Intent secondary action into a minimal
        executable parameter structure.

        IMPORTANT:
            Search does NOT assume Chrome, YouTube, Amazon,
            Reddit, or any other website.

        If the Intent itself provides a target, preserve it.

        Otherwise, use a generic semantic target so that the
        perception layer can resolve the actual UI control.
        """

        # -----------------------------------------------------
        # SEARCH
        # -----------------------------------------------------

        if action_name == "search":

            query = source.get(
                "query"
            )

            if not query:
                return None

            target = source.get(
                "target"
            )

            if target:
                return {
                    "target": str(target),
                    "query": query,
                }

            # Generic semantic target.
            #
            # This is deliberately NOT:
            #
            #     Chrome address bar
            #
            # and NOT:
            #
            #     YouTube search box
            #
            # because grounding belongs to perception.
            return {
                "target": "search field",
                "query": query,
            }

        # -----------------------------------------------------
        # NAVIGATE
        # -----------------------------------------------------

        if action_name == "navigate":

            url = source.get(
                "url"
            )

            if not url:
                return None

            return {
                "url": url,
            }

        # -----------------------------------------------------
        # OPEN APPLICATION
        # -----------------------------------------------------

        if action_name == "open_application":

            application = source.get(
                "application"
            )

            if not application:
                return None

            return {
                "application": application,
            }

        # -----------------------------------------------------
        # FOCUS WINDOW
        # -----------------------------------------------------

        if action_name == "focus_window":

            target = source.get(
                "target"
            )

            if not target:
                return None

            return {
                "target": target,
            }

        # -----------------------------------------------------
        # CLICK ACTIONS
        # -----------------------------------------------------

        if action_name in {
            "click",
            "double_click",
            "right_click",
        }:

            target = source.get(
                "target"
            )

            if not target:
                return None

            return {
                "target": target,
            }

        # -----------------------------------------------------
        # TYPE TEXT
        # -----------------------------------------------------

        if action_name == "type_text":

            target = source.get(
                "target"
            )

            value = source.get(
                "value"
            )

            if not target or value is None:
                return None

            return {
                "target": target,
                "value": value,
            }

        # -----------------------------------------------------
        # PRESS KEY
        # -----------------------------------------------------

        if action_name == "press_key":

            key = source.get(
                "key"
            )

            if not key:
                return None

            return {
                "key": str(key).upper(),
            }

        # -----------------------------------------------------
        # HOTKEY
        # -----------------------------------------------------

        if action_name == "hotkey":

            keys = source.get(
                "keys"
            )

            if not isinstance(
                keys,
                list,
            ):
                return None

            if len(keys) < 2:
                return None

            return {
                "keys": [
                    str(key).upper()
                    for key in keys
                ],
            }

        # -----------------------------------------------------
        # WAIT
        # -----------------------------------------------------

        if action_name == "wait":

            seconds = source.get(
                "seconds"
            )

            if seconds is None:
                return None

            return {
                "seconds": seconds,
            }

        # -----------------------------------------------------
        # SCROLL
        # -----------------------------------------------------

        if action_name == "scroll":

            amount = source.get(
                "amount"
            )

            if amount is None:
                return None

            return {
                "amount": amount,
            }

        # -----------------------------------------------------
        # SELECT FILE
        # -----------------------------------------------------

        if action_name == "select_file":

            target = source.get(
                "target"
            )

            if not target:
                return None

            return {
                "target": target,
            }

        # -----------------------------------------------------
        # ATTACH FILE
        # -----------------------------------------------------

        if action_name == "attach_file":

            target = source.get(
                "target"
            )

            if not target:
                return None

            return {
                "target": target,
            }

        # -----------------------------------------------------
        # SUBMIT
        # -----------------------------------------------------

        if action_name == "submit":

            target = source.get(
                "target"
            )

            if not target:
                return None

            return {
                "target": target,
            }

        # -----------------------------------------------------
        # CLOSE WINDOW
        # -----------------------------------------------------

        if action_name == "close_window":

            target = source.get(
                "target"
            )

            if not target:
                return None

            return {
                "target": target,
            }

        # -----------------------------------------------------
        # FINISH
        # -----------------------------------------------------

        if action_name == "finish":
            return {}

        # -----------------------------------------------------
        # UNKNOWN ACTION
        # -----------------------------------------------------

        logger.warning(
            "Unsupported Intent action: %s",
            action_name,
        )

        return None

    # =========================================================
    # FINAL PLAN NORMALIZATION
    # =========================================================

    def _finalize_plan(
        self,
        actions: list[Action],
    ) -> ActionPlan:
        """
        Final normalization of the plan.

        Ensures:
            - unsupported actions are removed
            - invalid actions are removed
            - focus is propagated through the action sequence
            - finish appears exactly once
            - finish is always last
        """

        validated_actions: list[Action] = []

        current_focus: str | None = None

        for action in actions:

            if action.action == "finish":
                continue

            validated_action = (
                self._validate_action(
                    action,
                    current_focus=current_focus,
                )
            )

            if validated_action is None:
                logger.warning(
                    "Dropping invalid action: %s",
                    action.action,
                )
                continue

            validated_actions.append(
                validated_action
            )

            # -------------------------------------------------
            # Update focus for all subsequent actions.
            # -------------------------------------------------

            if validated_action.focus:
                current_focus = (
                    validated_action.focus
                )

        # -----------------------------------------------------
        # finish must always be final.
        # -----------------------------------------------------

        validated_actions.append(
            Action(
                action="finish",
                focus=None,
                parameters={},
            )
        )

        return ActionPlan(
            actions=validated_actions
        )

    # =========================================================
    # HELPERS
    # =========================================================

    def _is_valid_action(
        self,
        action: Action,
    ) -> bool:
        """
        Check whether an action belongs to the supported
        semantic action vocabulary.
        """

        if action.action not in self.SUPPORTED_ACTIONS:
            return False

        return (
            self._validate_action(
                action
            )
            is not None
        )