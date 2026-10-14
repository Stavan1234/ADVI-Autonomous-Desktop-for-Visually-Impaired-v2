from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    SIMPLE = "simple"
    DELIBERATE = "deliberate"
    EXPLORATORY = "exploratory"


class OperationFamily(str, Enum):
    GENERIC = "generic"
    MEDIA_PLAYBACK = "media_playback"
    STRUCTURED_DOCUMENT = "structured_document"
    DESKTOP_WORKFLOW = "desktop_workflow"
    BROWSER_WORKFLOW = "browser_workflow"
    EMAIL_WORKFLOW = "email_workflow"
    FILE_WORKFLOW = "file_workflow"
    EXPLORATION = "exploration"


@dataclass(frozen=True)
class ExecutionStrategy:
    """Bounded pacing/observation policy; the LLM remains the planner."""

    mode: ExecutionMode
    family: OperationFamily
    pace_seconds: float
    checkpoint_after_side_effect: bool
    max_side_effects_per_round: int
    observation_tools: tuple[str, ...]
    reason: str

    @property
    def guidance(self) -> str:
        if self.mode is ExecutionMode.SIMPLE:
            return "pace=direct; use the minimum actions needed."
        if self.mode is ExecutionMode.EXPLORATORY:
            return (
                "pace=deliberate; orient to the live environment before acting; "
                "prefer read-only discovery; do not guess an application, target, or control; "
                "after a state change, observe again and revise from the new state."
            )
        return (
            "pace=deliberate; use one state-changing action at a time; allow the environment "
            "to settle; observe after meaningful state changes; if observation contradicts "
            "the previous plan, re-plan instead of forcing it."
        )


_ACTION_RE = re.compile(
    r"\b(?:open|launch|start|run|send|create|make|save|search|find|read|write|edit|"
    r"rename|move|copy|delete|play|watch|navigate|close|type|click|press|use|remember|forget)\b",
    re.I,
)
_COMPOUND_RE = re.compile(r"\b(?:and|then|after|once|on it|with it|using|followed by)\b", re.I)
_UNRESOLVED_ACTION_RE = re.compile(r"\b(?:configure|adjust|calibrate|customize|optimize|organize|set up|setup|troubleshoot|prepare|arrange|coordinate|handle|manage|fix|enable|disable)\b", re.I)
_URL_RE = re.compile(r"\bhttps?://|\b(?:www\.)?[a-z0-9-]+\.(?:com|org|net|io|dev|in|co\.in)\b", re.I)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def infer_execution_strategy(
    text: str | None = None,
    *,
    task_goal: str | None = None,
    operation_intent: str | None = None,
    last_tool: str | None = None,
    observation: dict[str, Any] | None = None,
    orientation_complete: bool = False,
) -> ExecutionStrategy:
    """Select a small execution policy from semantics and current environment."""
    combined = " ".join(
        str(value or "").casefold().strip()
        for value in (text, task_goal, operation_intent)
        if str(value or "").strip()
    )
    if not combined:
        return ExecutionStrategy(ExecutionMode.SIMPLE, OperationFamily.GENERIC, 0.0, False, 99, (), "no active operation")

    last = str(last_tool or "").casefold()
    browser_context = last in {
        "navigate", "search", "inspect_web_page", "read_web_page", "click_web_element",
        "type_web_element", "go_back", "go_forward", "reload_page",
    } or bool((observation or {}).get("browser"))
    last_browser_action = last in {
        "navigate", "search", "inspect_web_page", "read_web_page", "click_web_element",
        "type_web_element", "go_back", "go_forward", "reload_page",
    }
    last_file_action = last in {
        "find_file", "list_files", "read_file", "open_file", "open_folder", "create_file",
        "save_file", "append_file", "replace_file_text", "delete_file", "copy_file", "move_file",
        "rename_file", "copy_folder", "move_folder", "rename_folder", "delete_folder",
    }
    last_desktop_action = last in {
        "open_application", "new_document", "save_document", "save_document_as", "close_document",
        "close_window", "close_application", "focus_window", "type_text", "press_key", "hotkey",
        "click", "double_click", "right_click", "scroll", "inspect_window", "read_screen",
        "list_windows", "list_applications", "find_application", "wait",
    }
    last_email_action = last in {"email_read", "email_draft_create", "email_draft_read", "email_draft_update", "email_send", "email_reply", "contact_lookup", "david_fallback"}

    media = (
        _has_any(combined, ("youtube music", "youtube", "spotify", "song", "music", "track", "playback"))
        and _has_any(combined, ("play", "listen", "watch", "song", "track", "music"))
    )
    document = (
        _has_any(combined, ("pdf", "docx", "word document", "powerpoint", "pptx", "ppt", "presentation", "spreadsheet", "xlsx"))
        and _has_any(combined, ("create", "make", "write", "generate", "save", "build", "design"))
    )
    rich_document = document and _has_any(
        combined,
        ("beautiful", "beutiful", "pretty", "styled", "colorful", "professional", "modern", "attractive", "pages", "slides"),
    )
    email = last_email_action or _has_any(combined, ("email", "gmail", "inbox", "draft", "recipient", "reply", "mail"))
    file = last_file_action or _has_any(combined, ("file", "folder", "directory", "downloads", "desktop", "pdf", "docx", "xlsx", "pptx", "ppt", "txt", "csv"))
    browser = browser_context or last_browser_action or _has_any(combined, ("browser", "website", "web page", "youtube", "url", "navigate", "search", "google", "tab", "web")) or bool(_URL_RE.search(combined))
    desktop = last_desktop_action or _has_any(combined, ("application", "desktop app", "window", "notepad", "calculator", "chrome", "word", "excel", "powerpoint", "launch")) or bool(re.search(
        r"^\s*(?:please\s+)?(?:open|close|quit|exit|run|start|launch)\s+(?:the\s+)?[a-z0-9][a-z0-9 .&'()_-]{1,80}?(?:\s+(?:please|for me))?\s*$",
        combined, re.I,
    ))
    if desktop and _has_any(combined, ("pdf", "docx", "pptx", "ppt", "spreadsheet", "presentation", "file", "folder", "directory", "downloads", "website", "web page", "youtube")):
        desktop = False
    keyboard = bool(re.search(
        r"\b(?:press|hit|tap)\s+(?:enter|return|escape|esc|tab|space|backspace|delete|home|end|up|down|left|right|page\s+(?:up|down)|f(?:1[0-2]|[1-9]))\b|"
        r"\b(?:hotkey|keyboard\s+shortcut|shortcut)\b|\b(?:ctrl|control|alt|shift|windows?\s+key|cmd|command)\s*(?:\+|plus|and)\s*\w+",
        combined, re.I,
    ))
    deterministic = keyboard or _has_any(combined, ("remember", "forget", "memory", "system information", "system status", "battery status", "battery"))

    action_count = len(_ACTION_RE.findall(combined))
    multi_step = bool(_COMPOUND_RE.search(combined)) or action_count >= 2
    observation_present = bool(observation)
    unresolved_action = bool(_UNRESOLVED_ACTION_RE.search(combined)) and not bool(operation_intent)

    # Explicitly known local domains should never be routed into exploration merely
    # because the action happens to be short.
    if deterministic:
        return ExecutionStrategy(ExecutionMode.SIMPLE, OperationFamily.GENERIC, 0.0, False, 99, (), "deterministic local operation")

    if unresolved_action and not orientation_complete and not observation_present:
        return ExecutionStrategy(
            ExecutionMode.EXPLORATORY, OperationFamily.EXPLORATION, 0.50, True, 1,
            ("list_applications", "find_application", "list_windows", "inspect_window", "read_screen",
             "inspect_web_page", "read_web_page", "find_file", "list_files", "wait"),
            "unrecognized action semantics; observe the live environment before deciding how to execute it",
        )

    if media:
        return ExecutionStrategy(ExecutionMode.DELIBERATE, OperationFamily.MEDIA_PLAYBACK, 0.55, True, 1,
            ("inspect_web_page", "read_web_page", "read_screen", "wait"),
            "media target and playback state require semantic postcondition verification")

    if rich_document or document:
        return ExecutionStrategy(ExecutionMode.DELIBERATE, OperationFamily.STRUCTURED_DOCUMENT, 0.35, True, 1,
            ("inspect_window", "read_screen", "wait"),
            "structured document creation requires deliberate generation and verification")

    if email:
        deliberate = multi_step or _has_any(combined, ("send", "reply", "forward"))
        return ExecutionStrategy(
            ExecutionMode.DELIBERATE if deliberate else ExecutionMode.SIMPLE,
            OperationFamily.EMAIL_WORKFLOW,
            0.30 if deliberate else 0.0,
            deliberate,
            1 if deliberate else 99,
            ("inspect_web_page", "read_web_page", "wait"),
            "email workflow crosses stateful draft/confirmation/send boundaries" if deliberate else "simple email operation",
        )

    if file:
        return ExecutionStrategy(
            ExecutionMode.DELIBERATE if multi_step else ExecutionMode.SIMPLE,
            OperationFamily.FILE_WORKFLOW,
            0.20 if multi_step else 0.0,
            multi_step,
            1 if multi_step else 99,
            ("find_file", "read_file", "inspect_window", "read_screen", "wait"),
            "filesystem workflow should re-check state between dependent mutations" if multi_step else "simple filesystem operation",
        )

    if desktop:
        return ExecutionStrategy(
            ExecutionMode.DELIBERATE if multi_step else ExecutionMode.SIMPLE,
            OperationFamily.DESKTOP_WORKFLOW,
            0.45 if multi_step else 0.0,
            multi_step,
            1 if multi_step else 99,
            ("inspect_window", "read_screen", "list_windows", "wait"),
            "desktop UI workflow may change asynchronously" if multi_step else "simple desktop operation",
        )

    if browser:
        deliberate = multi_step
        return ExecutionStrategy(
            ExecutionMode.DELIBERATE if deliberate else ExecutionMode.SIMPLE,
            OperationFamily.BROWSER_WORKFLOW,
            0.25 if deliberate else 0.0,
            deliberate,
            1 if deliberate else 99,
            ("inspect_web_page", "read_web_page", "wait"),
            "browser state should be re-observed after dependent UI changes" if deliberate else "simple browser operation",
        )

    # Truly unresolved actionable work starts with orientation. After one verified
    # observation, normal bounded action selection is allowed again.
    if _ACTION_RE.search(combined):
        if not orientation_complete and not observation_present:
            return ExecutionStrategy(
                ExecutionMode.EXPLORATORY, OperationFamily.EXPLORATION, 0.45, True, 1,
                ("list_applications", "find_application", "list_windows", "inspect_window", "read_screen", "inspect_web_page", "read_web_page", "find_file", "list_files", "wait"),
                "unknown actionable intent; orient to the live environment first",
            )
        return ExecutionStrategy(
            ExecutionMode.EXPLORATORY, OperationFamily.EXPLORATION, 0.35, True, 1,
            ("list_applications", "find_application", "list_windows", "inspect_window", "read_screen", "inspect_web_page", "read_web_page", "find_file", "list_files", "wait"),
            "operation remains semantically unresolved; use observation before committing to a target",
        )

    return ExecutionStrategy(
        ExecutionMode.SIMPLE, OperationFamily.GENERIC, 0.0, False, 99, (), "conversation or non-actionable input"
    )


def is_observation_tool(name: str) -> bool:
    return str(name or "").casefold() in {
        "inspect_web_page", "read_web_page", "read_screen", "inspect_window", "observe_window",
        "list_windows", "list_applications", "find_application", "find_file", "list_files",
        "search_file_content", "read_file", "get_system_info", "get_system_status",
        "get_battery_status", "memory_search", "memory_retrieval", "contact_lookup", "wait",
    }


def is_state_changing_tool(name: str) -> bool:
    return not is_observation_tool(name)


def terminal_evidence_matches(strategy: ExecutionStrategy, tool_name: str, result: Any, task_goal: str = "") -> bool:
    """Operation-aware terminal evidence without making the strategy a planner."""
    name = str(tool_name or "").casefold()
    if not bool(getattr(result, "success", False)) or not bool(getattr(result, "verified", False)):
        return False
    details = dict(getattr(result, "verification_details", {}) or {})
    metadata = dict(getattr(result, "metadata", {}) or {})
    goal = str(task_goal or "").casefold()

    if strategy.family is OperationFamily.MEDIA_PLAYBACK:
        if name not in {"click_web_element", "play_media"}:
            return False
        playback = details.get("playback") or details.get("player") or {}
        return bool(
            details.get("playing") is True
            or details.get("playback_verified") is True
            or metadata.get("playback_verified") is True
            or isinstance(playback, dict) and playback.get("playing") is True
        )

    if strategy.family is OperationFamily.STRUCTURED_DOCUMENT:
        if name not in {"create_file", "save_file"}:
            return False
        if details.get("exists") is False:
            return False
        if details.get("definitive_failure") is True:
            return False
        if details.get("content_matches") is False:
            return False
        if details.get("style_requested"):
            visual_sanity = details.get("visual_sanity")
            # Verification details can cross provider/fallback serialization
            # boundaries and occasionally arrive as a string. Never let the
            # terminal-evidence check crash while deciding whether an already
            # verified document is complete. Only an explicit negative visual
            # result should block completion.
            if isinstance(visual_sanity, dict) and visual_sanity.get("valid") is False:
                return False
            if isinstance(visual_sanity, str) and visual_sanity.strip().casefold() in {
                "false", "invalid", "failed", "failure", "error",
            }:
                return False
        # "native_structure_and_content" is what ToolVerifier emits for verified
        # PDF/DOCX/XLSX/PPTX files. Omitting it here downgraded every verified
        # document creation in the runtime log to "couldn't verify".
        return details.get("verification_mode") in {
            None, "native_structure_and_content", "native_structure_plus_content_warning", "exact",
        }

    if strategy.family is OperationFamily.EMAIL_WORKFLOW:
        if name == "email_draft_create" and "send" in goal and "draft" not in goal:
            return False
        return name in {"email_draft_create", "email_draft_update", "email_send", "email_reply", "david_fallback"}

    if strategy.family is OperationFamily.EXPLORATION:
        return name == "david_fallback"

    if strategy.family is OperationFamily.BROWSER_WORKFLOW and name == "search":
        followup_after_search = re.search(
            r"\b(?:and|then|after|once|followed by)\s+(?:open|play|watch|listen|click|select|choose|buy|download|add|save|read)\b",
            goal,
        )
        return not bool(followup_after_search)

    # A browser search is terminal when the user's request is only to search.
    # It is non-terminal when another semantic action is explicitly requested
    # afterward (for example: "search X and open the first result").
    if name == "search":
        followup_after_search = re.search(
            r"\b(?:and|then|after|once|followed by)\s+(?:open|play|watch|listen|click|select|choose|buy|download|add|save|read)\b",
            goal,
        )
        return not bool(followup_after_search)

    # File discovery/list/read are independently complete when the user asked
    # only for discovery or content access. Dependent actions (open/read/copy/etc.)
    # remain non-terminal and are handled by the execution controller.
    if name == "list_files":
        return True
    if name == "find_file":
        return not bool(re.search(
            r"\b(?:and|then|after|once|followed by)\s+(?:open|read|show|display|delete|copy|move|rename|write|edit)\b",
            goal,
        ))
    if name == "read_file":
        return bool(re.search(r"\b(?:read|show|display|summari[sz]e|about|contents?)\b", goal))
    if name in {"memory_update", "memory_remember", "memory_forget"}:
        return True

    return name in {
        "create_file", "save_file", "append_file", "replace_file_text", "open_file", "open_folder", "create_folder",
        "delete_file", "copy_file", "move_file", "rename_file", "copy_folder", "move_folder",
        "rename_folder", "close_application", "close_window", "email_send", "email_reply",
        "search_file_content", "david_fallback",
    }
