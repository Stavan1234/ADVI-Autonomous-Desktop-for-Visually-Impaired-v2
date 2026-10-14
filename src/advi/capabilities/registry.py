from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
from datetime import datetime, timezone
import logging
import os

from advi.core.action_contracts import get_action_contract
from advi.capabilities.desktop.context import DesktopContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionSpec:
    """Canonical runtime view of one executable ADVI action.

    This is composed from the authoritative capability registry plus the
    canonical parameter contract. Consumers should use this object instead
    of rebuilding action semantics themselves.
    """

    action: str
    capability: str
    description: str
    side_effect: str
    requires_confirmation: bool
    verification: str
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)

    def required_parameters(self) -> list[str]:
        return [name for name, spec in self.parameters.items() if spec.get("required")]

    def to_prompt_line(self) -> str:
        return (
            f"- {self.action}: {self.description} | side_effect={self.side_effect} | "
            f"confirmation={str(self.requires_confirmation).lower()} | verification={self.verification} | "
            f"required_parameters={','.join(self.required_parameters()) or 'none'}"
        )


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AvailabilitySnapshot:
    """Runtime availability evidence for one capability."""

    capability: str
    status: CapabilityStatus
    reason: str
    checked_at: str


@dataclass
class Capability:
    """Definition of a concrete capability supported by ADVI."""
    name: str
    description: str
    supported_actions: set[str]
    status: CapabilityStatus = CapabilityStatus.AVAILABLE
    requires_permission: bool = False
    handler: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    availability_probe: Callable[["Capability"], tuple[CapabilityStatus, str]] | None = None
    availability_reason: str = "registered"
    last_checked_at: str | None = None
    availability_ttl_seconds: float = 5.0

    def action_metadata(self, action: str) -> dict[str, Any]:
        """Return normalized semantics for one supported action."""
        if action not in self.supported_actions:
            return {}
        defaults = _ACTION_SEMANTICS.get(action, {})
        contract = get_action_contract(action)
        parameters = {}
        if contract is not None:
            for name, rule in contract.parameters.items():
                parameters[name] = {
                    "required": rule.required,
                    "kind": rule.kind,
                    "aliases": list(rule.aliases),
                }
        explicit = self.metadata.get("actions", {}).get(action, {}) if isinstance(self.metadata.get("actions"), dict) else {}
        return {
            "action": action,
            "capability": self.name,
            "description": defaults.get("description", action.replace("_", " ")),
            "side_effect": defaults.get("side_effect", "low"),
            "requires_confirmation": self.requires_permission or defaults.get("requires_confirmation", False),
            "verification": defaults.get("verification", "none"),
            "parameters": parameters,
            **explicit,
        }

    def action_spec(self, action: str) -> ActionSpec | None:
        """Build the canonical executable specification for ``action``."""
        metadata = self.action_metadata(action)
        if not metadata:
            return None
        return ActionSpec(
            action=action,
            capability=self.name,
            description=metadata["description"],
            side_effect=str(metadata["side_effect"]),
            requires_confirmation=bool(metadata["requires_confirmation"]),
            verification=str(metadata["verification"]),
            parameters=dict(metadata.get("parameters") or {}),
        )

    def is_available(self) -> bool:
        return self.status in {CapabilityStatus.AVAILABLE, CapabilityStatus.PARTIAL}

    def availability_snapshot(self) -> AvailabilitySnapshot:
        return AvailabilitySnapshot(
            capability=self.name,
            status=self.status,
            reason=self.availability_reason,
            checked_at=self.last_checked_at or "never",
        )


_ACTION_SEMANTICS: dict[str, dict[str, Any]] = {
    "open_application": {"description": "Launch a desktop application.", "side_effect": "medium", "verification": "window_presence"},
    "find_application": {"description": "Locate an installed or running desktop application by name.", "side_effect": "low", "verification": "application_inventory"},
    "new_document": {"description": "Create a new blank document in the active desktop application.", "side_effect": "medium", "verification": "window_state"},
    "save_document": {"description": "Save the current document in the active desktop application.", "side_effect": "high", "verification": "document_saved"},
    "save_document_as": {"description": "Save the current document to a specified path through the application.", "side_effect": "high", "verification": "document_saved_as"},
    "close_document": {"description": "Close the current document without discarding unsaved changes silently.", "side_effect": "medium", "verification": "document_closed"},
    "focus_window": {"description": "Focus an existing application window.", "side_effect": "low", "verification": "foreground_window"},
    "close_window": {"description": "Close the targeted application window.", "side_effect": "medium", "verification": "window_absence"},
    "close_application": {"description": "Gracefully close a named installed/running Windows application and verify its visible windows are gone.", "side_effect": "medium", "verification": "application_windows_absence"},
    "list_applications": {"description": "List known installed applications and their current running state.", "side_effect": "low", "verification": "application_inventory"},
    "list_windows": {"description": "List visible desktop windows and their current process/application identity.", "side_effect": "low", "verification": "window_inventory"},
    "inspect_window": {"description": "Inspect the current target window, dialogs, and visible controls.", "side_effect": "low", "verification": "window_inspection"},
    "read_screen": {"description": "Read visible text from the current screen using OCR.", "side_effect": "low", "verification": "ocr_result"},
    "type_text": {"description": "Type text into the current target.", "side_effect": "high", "verification": "target_state_or_observation"},
    "press_key": {"description": "Press a keyboard key in the current target.", "side_effect": "high", "verification": "target_state_or_observation"},
    "hotkey": {"description": "Press a keyboard shortcut in the current target.", "side_effect": "high", "verification": "target_state_or_observation"},
    "click": {"description": "Click the targeted desktop element or coordinates.", "side_effect": "high", "verification": "target_state_or_observation"},
    "double_click": {"description": "Double-click the targeted desktop element.", "side_effect": "high", "verification": "target_state_or_observation"},
    "right_click": {"description": "Right-click the targeted desktop element.", "side_effect": "high", "verification": "target_state_or_observation"},
    "scroll": {"description": "Scroll the active desktop surface.", "side_effect": "medium", "verification": "observation"},
    "wait": {"description": "Wait for a duration or until the active application reaches a readiness or stability condition.", "side_effect": "low", "verification": "application_state_or_timing"},
    "navigate": {"description": "Open a URL in ADVI's browser and wait for the page to finish loading.", "side_effect": "medium", "verification": "url"},
    "open_browser": {"description": "Open ADVI's own browser window (optionally at a URL). Use this, not open_application, for Chrome or 'the browser'.", "side_effect": "medium", "verification": "browser_page"},
    "search": {"description": "Search within the controlled browser context.", "side_effect": "medium", "verification": "page_state"},
    "click_web_element": {"description": "Click a page element by its ref from the latest inspect_web_page (pass the ref as selector). Waits until the element can be clicked, then returns what changed, whether media is playing, and a fresh snapshot.", "side_effect": "high", "verification": "page_state"},
    "inspect_web_page": {"description": "Snapshot the current page: its visible interactive elements (links, buttons, fields, players, including inside iframes), each with a ref like e12 or f1e2 for click_web_element/type_web_element.", "side_effect": "low", "verification": "page_interactive_elements"},
    "type_web_element": {"description": "Fill a page field by its ref (pass the ref as selector); submit=true presses Enter afterwards. Returns the field's value and a fresh snapshot.", "side_effect": "high", "verification": "input_value"},
    "read_web_page": {"description": "Read the current browser page.", "side_effect": "low", "verification": "page_content"},
    "go_back": {"description": "Go back one page in the controlled browser history.", "side_effect": "medium", "verification": "url"},
    "go_forward": {"description": "Go forward one page in the controlled browser history.", "side_effect": "medium", "verification": "url"},
    "reload_page": {"description": "Reload the current page in the controlled browser.", "side_effect": "medium", "verification": "page_state"},
    "research_web": {"description": "Search the web, open a relevant result, and read the resulting page.", "side_effect": "medium", "verification": "page_content"},
    "save_file": {"description": "Write content to a local file.", "side_effect": "high", "verification": "file_exists_and_content"},
    "create_file": {"description": "Create a new local file. For .pdf/.docx/.xlsx/.pptx ALWAYS generate the real binary container using PyMuPDF/python-docx/openpyxl/python-pptx, never plain text. For PDF use text content and optional pages; for PPTX use slide text blocks and optional slides; for XLSX provide CSV-style rows; for DOCX provide paragraphs.", "side_effect": "high", "verification": "structured_file_parse"},
    "append_file": {"description": "Append content to an existing local file.", "side_effect": "high", "verification": "file_contains_appended_content"},
    "replace_file_text": {"description": "Replace an exact text occurrence in an existing local file.", "side_effect": "high", "verification": "file_contains_replacement"},
    "read_file": {"description": "Read content from a local file.", "side_effect": "low", "verification": "file_read"},
    "delete_file": {"description": "Delete a local file.", "side_effect": "high", "requires_confirmation": False, "verification": "file_absence"},
    "copy_file": {"description": "Copy a local file to another path.", "side_effect": "high", "verification": "destination_exists"},
    "move_file": {"description": "Move a local file to another path.", "side_effect": "high", "verification": "source_absent_destination_exists"},
    "rename_file": {"description": "Rename a local file within its folder.", "side_effect": "high", "verification": "renamed"},
    "copy_folder": {"description": "Copy a local folder and its contents to another path.", "side_effect": "high", "verification": "directory_copied"},
    "move_folder": {"description": "Move a local folder and its contents to another path.", "side_effect": "high", "verification": "directory_moved"},
    "rename_folder": {"description": "Rename a local folder within its parent directory.", "side_effect": "high", "verification": "directory_renamed"},
    "delete_folder": {"description": "Delete a local folder, optionally including its contents.", "side_effect": "high", "requires_confirmation": False, "verification": "directory_absence"},
    "list_files": {"description": "List local files/folders with optional extension, scope, recursion, and result limit filters.", "side_effect": "low", "verification": "listing"},
    "find_file": {"description": "Find a local file or folder by exact name across the user computer or selected folders.", "side_effect": "low", "verification": "file_search"},
    "search_file_content": {"description": "Search readable content inside a file or folder.", "side_effect": "low", "verification": "content_search"},
    "create_folder": {"description": "Create a local folder.", "side_effect": "high", "verification": "directory_exists"},
    "open_file": {"description": "Open a local file with its associated application.", "side_effect": "medium", "verification": "file_open_requested"},
    "open_folder": {"description": "Open a local folder in the system file manager.", "side_effect": "medium", "verification": "folder_open_requested"},
    "email_draft_create": {"description": "Create an email draft.", "side_effect": "medium", "verification": "draft_exists"},
    "email_draft_read": {"description": "Read an email draft.", "side_effect": "low", "verification": "draft_content"},
    "email_draft_update": {"description": "Update an existing email draft.", "side_effect": "medium", "verification": "draft_content"},
    "observe_window": {"description": "List the interactive elements (buttons, fields, list items, tabs, links) of an application window with IDs, roles, names, values and focus. Use this to see a desktop app before acting; read_screen is only for reading text.", "side_effect": "low", "verification": "uia_interactive_elements"},
    "interact": {"description": "Act on one element from the latest observe_window by its ID: click, type, set_value, select, toggle, expand, or focus. Returns whether the window changed plus a fresh observation.", "side_effect": "high", "verification": "uia_state_change"},
    "play_media": {"description": "Play a song or video on YouTube or YouTube Music in one verified step: searches, opens the best result, and confirms playback started. Use this instead of navigate/search/click for any play request.", "side_effect": "medium", "verification": "media_playing"},
    "whatsapp_send_message": {"description": "Send a WhatsApp message to a chat or group in WhatsApp Desktop as one verified operation: opens WhatsApp, finds the chat, types the exact text, and sends it. Use this instead of click/type_text for WhatsApp.", "side_effect": "critical", "requires_confirmation": True, "verification": "whatsapp_composer_cleared"},
    "whatsapp_open_chat": {"description": "Open a WhatsApp chat or group in WhatsApp Desktop and verify its conversation is showing.", "side_effect": "medium", "verification": "whatsapp_chat_header"},
    "email_send": {"description": "Send an email through the connected account.", "side_effect": "critical", "requires_confirmation": True, "verification": "message_id"},
    "email_reply": {"description": "Reply to a specific email in its existing Gmail thread.", "side_effect": "critical", "requires_confirmation": True, "verification": "message_id"},
    "email_read": {"description": "Read email from the connected account. With limit greater than 1, return message summaries; use limit=1 when the user needs the full body.", "side_effect": "low", "verification": "message_content"},
    "get_system_info": {"description": "Read local operating-system, CPU, memory, disk, and runtime information.", "side_effect": "low", "verification": "system_probe"},
    "get_system_status": {"description": "Read current local CPU, memory, disk, uptime, and battery status.", "side_effect": "low", "verification": "system_probe"},
    "get_battery_status": {"description": "Read the current local battery percentage and charging state.", "side_effect": "low", "verification": "system_probe"},
    "memory_retrieval": {"description": "Retrieve durable memory relevant to the request.", "side_effect": "low", "verification": "memory_result"},
    "contact_lookup": {"description": "Look up trusted contact details such as email address by contact name.", "side_effect": "low", "verification": "contact_result"},
    "memory_update": {"description": "Store a durable memory fact.", "side_effect": "medium", "verification": "memory_presence"},
    "memory_forget": {"description": "Remove a durable memory fact.", "side_effect": "high", "requires_confirmation": True, "verification": "memory_absence"},
    "david_fallback": {"description": "Delegate a task to the isolated David desktop/browser fallback when the primary capability path cannot handle it.", "side_effect": "medium", "verification": "fallback_result"},
    "mcp_list_tools": {"description": "List tools exposed by configured Model Context Protocol servers.", "side_effect": "low", "verification": "mcp_tool_list"},
    "mcp_call": {"description": "Call one exact tool on a configured Model Context Protocol server.", "side_effect": "high", "verification": "mcp_result"},
}


class CapabilityRegistry:
    """
    Authoritative registry of what ADVI can actually perform.
    Enforces real-world capability boundaries and provides capability context to the LLM.
    """

    def __init__(self, desktop_context: DesktopContext | None = None) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._action_to_capability: dict[str, str] = {}
        self.desktop_context = desktop_context or DesktopContext()

    def register(self, capability: Capability) -> None:
        self._capabilities[capability.name] = capability
        for action in capability.supported_actions:
            self._action_to_capability[action] = capability.name

    def set_status(
        self,
        name: str,
        status: CapabilityStatus,
        *,
        reason: str = "runtime status update",
    ) -> bool:
        """Update runtime availability without changing capability semantics."""
        capability = self._capabilities.get(name)
        if capability is None:
            return False
        capability.status = status
        capability.availability_reason = reason
        capability.last_checked_at = datetime.now(timezone.utc).isoformat()
        return True

    def refresh_capability(self, name: str) -> AvailabilitySnapshot | None:
        """Probe one capability and publish the result to the registry."""
        capability = self._capabilities.get(name)
        if capability is None:
            return None
        probe = capability.availability_probe
        if probe is None:
            return capability.availability_snapshot()
        try:
            status, reason = probe(capability)
        except Exception as exc:
            status, reason = CapabilityStatus.UNAVAILABLE, f"availability probe failed: {exc}"
        self.set_status(capability.name, status, reason=reason)
        return capability.availability_snapshot()

    def refresh_all(self, *, force: bool = True) -> list[AvailabilitySnapshot]:
        """Refresh registered capabilities and publish runtime availability snapshots."""
        snapshots = []
        for name in list(self._capabilities):
            snapshot = self.refresh_capability(name) if force else self.refresh_stale(name)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def refresh_stale(self, name: str | None = None) -> list[AvailabilitySnapshot] | AvailabilitySnapshot | None:
        """Refresh only capabilities whose probe result is stale.

        Probes are intentionally cached for a short interval so planning does not
        repeatedly hit the OS, browser, or authentication layer on every prompt.
        """
        names = [name] if name is not None else list(self._capabilities)
        refreshed: list[AvailabilitySnapshot] = []
        now = datetime.now(timezone.utc)
        for cap_name in names:
            capability = self._capabilities.get(cap_name)
            if capability is None:
                continue
            if capability.availability_probe is None:
                continue
            stale = capability.last_checked_at is None
            if not stale:
                try:
                    checked = datetime.fromisoformat(capability.last_checked_at)
                    if checked.tzinfo is None:
                        checked = checked.replace(tzinfo=timezone.utc)
                    stale = (now - checked).total_seconds() >= max(0.0, capability.availability_ttl_seconds)
                except (TypeError, ValueError):
                    stale = True
            if stale:
                snapshot = self.refresh_capability(cap_name)
                if snapshot is not None:
                    refreshed.append(snapshot)
        if name is not None:
            return refreshed[0] if refreshed else self.availability(name)
        return refreshed

    def refresh_for_action(self, action: str, *, force: bool = False) -> AvailabilitySnapshot | None:
        """Refresh the capability owning ``action`` when its runtime state is stale."""
        capability = self.get_capability_for_action(action)
        if capability is None:
            return None
        if force:
            return self.refresh_capability(capability.name)
        return self.refresh_stale(capability.name)

    def availability(self, name: str) -> AvailabilitySnapshot | None:
        capability = self._capabilities.get(name)
        return capability.availability_snapshot() if capability else None

    def availability_summary(self) -> str:
        """Compact authoritative runtime availability summary."""
        lines = []
        for capability in self._capabilities.values():
            lines.append(
                f"- {capability.name}: {capability.status.value} ({capability.availability_reason})"
            )
        return "\n".join(lines) or "- (no capabilities registered)"

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def get_capability_for_action(self, action: str) -> Capability | None:
        cap_name = self._action_to_capability.get(action)
        if cap_name:
            return self._capabilities.get(cap_name)
        return None

    def list_all(self) -> list[Capability]:
        return list(self._capabilities.values())

    def list_available(self) -> list[Capability]:
        return [c for c in self._capabilities.values() if c.is_available()]

    def is_action_supported(self, action: str) -> bool:
        cap = self.get_capability_for_action(action)
        return cap is not None and cap.is_available()

    def get_action_metadata(self, action: str) -> dict[str, Any] | None:
        spec = self.get_action_spec(action)
        if spec is None:
            return None
        return {
            "action": spec.action,
            "capability": spec.capability,
            "description": spec.description,
            "side_effect": spec.side_effect,
            "requires_confirmation": spec.requires_confirmation,
            "verification": spec.verification,
            "parameters": spec.parameters,
        }

    def get_action_spec(self, action: str) -> ActionSpec | None:
        cap = self.get_capability_for_action(action)
        if cap is None or not cap.is_available():
            return None
        return cap.action_spec(action)

    def get_registered_action_spec(self, action: str) -> ActionSpec | None:
        """Return an action contract even when its capability is temporarily unavailable.

        Model routing needs to know that an operation exists so a transient readiness
        probe failure does not turn a valid request into a tool-less LLM turn. The
        execution engine still enforces actual capability availability before running it.
        """
        cap = self.get_capability_for_action(action)
        if cap is None:
            return None
        return cap.action_spec(action)

    def list_action_specs(self, *, available_only: bool = True) -> list[ActionSpec]:
        specs: list[ActionSpec] = []
        capabilities = self.list_available() if available_only else self.list_all()
        for cap in capabilities:
            for action in sorted(cap.supported_actions):
                spec = cap.action_spec(action)
                if spec is not None:
                    specs.append(spec)
        return specs

    def actions_for_prompt(self) -> str:
        """Detailed, authoritative action catalogue for planning/replanning."""
        rows = [spec.to_prompt_line() for spec in self.list_action_specs()]
        return "\n".join(rows) or "- (no executable actions currently available)"

    def readiness_report(self):
        """Run a deterministic consistency audit for registered capabilities."""
        from advi.core.capability_readiness import CapabilityReadinessChecker
        return CapabilityReadinessChecker().check(self)

    def summary_for_prompt(self) -> str:
        """Full capability context for the LLM – includes unavailable capabilities so the
        Brain can accurately tell users what ADVI can and cannot do."""
        available_lines = []
        unavailable_lines = []
        for cap in self._capabilities.values():
            actions_list = ", ".join(sorted(cap.supported_actions)) if cap.supported_actions else "none"
            if cap.is_available():
                available_lines.append(
                    f"- **{cap.name}** [{cap.status.value}]: {cap.description} | reason={cap.availability_reason}"
                )
            else:
                unavailable_lines.append(
                    f"- **{cap.name}** [unavailable]: {cap.description} | reason={cap.availability_reason}"
                )
        parts = []
        if available_lines:
            parts.append("Available:\n" + "\n".join(available_lines))
        if unavailable_lines:
            parts.append("Unavailable (do not claim these work):\n" + "\n".join(unavailable_lines))
        return "\n\n".join(parts)


def _probe_always_available(capability: Capability) -> tuple[CapabilityStatus, str]:
    return CapabilityStatus.AVAILABLE, "built-in capability"


def _probe_desktop(capability: Capability) -> tuple[CapabilityStatus, str]:
    if capability.handler is None:
        return CapabilityStatus.UNAVAILABLE, "desktop handler is unavailable"
    try:
        import pyautogui
        if os.name != "nt" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return CapabilityStatus.UNAVAILABLE, "no desktop display session detected"
        _ = pyautogui.size()
        return CapabilityStatus.AVAILABLE, "desktop input/display is reachable"
    except Exception as exc:
        return CapabilityStatus.UNAVAILABLE, f"desktop environment unavailable: {exc}"


def _probe_browser(capability: Capability) -> tuple[CapabilityStatus, str]:
    """Treat browser connectivity as lazy; do not contact Chrome during startup health checks."""
    if capability.handler is None:
        return CapabilityStatus.UNAVAILABLE, "browser handler is unavailable"
    cdp = getattr(capability.handler, "cdp", None)
    if cdp is None:
        return CapabilityStatus.UNAVAILABLE, "browser CDP client is unavailable"
    # Starting ADVI should not require Chrome to already expose port 9222. The
    # browser capability owns its launch/attach path and will establish CDP when
    # the first browser action actually needs it.
    if getattr(capability.handler, "_ensure_chrome_running", None) is not None:
        return CapabilityStatus.PARTIAL, "Chrome/CDP is lazy; it will be started or attached on the first browser task"
    return CapabilityStatus.UNAVAILABLE, "browser launch/attach handler is unavailable"


def _probe_handler(capability: Capability) -> tuple[CapabilityStatus, str]:
    if capability.handler is None:
        return CapabilityStatus.UNAVAILABLE, "capability handler is unavailable"
    checker = getattr(capability.handler, "is_available", None)
    if callable(checker):
        try:
            if checker():
                return CapabilityStatus.AVAILABLE, "handler reports capability available"
            return CapabilityStatus.UNAVAILABLE, "handler reports capability unavailable"
        except Exception as exc:
            return CapabilityStatus.UNAVAILABLE, f"handler availability check failed: {exc}"
    return CapabilityStatus.AVAILABLE, "handler is configured"


def build_default_registry(
    memory: Any = None,
    retriever: Any = None,
    gmail_service: Any = None,
    environment_watchtower: Any = None,
    david_fallback_service: Any = None,
    mcp_manager: Any = None,
    *,
    refresh: bool = False,
    desktop_context: DesktopContext | None = None,
) -> CapabilityRegistry:
    """Construct the authoritative capability registry and its shared desktop context."""
    registry = CapabilityRegistry(desktop_context=desktop_context)
    shared_context = registry.desktop_context

    # 1. Desktop Capability
    try:
        from advi.capabilities.desktop.executor import DesktopCapability
        desktop_cap = DesktopCapability(context=shared_context, environment_watchtower=environment_watchtower)
        registry.register(
            Capability(
                name="desktop_control",
                description="Control desktop applications, focus windows, type text, press keys, and mouse automation.",
                supported_actions=desktop_cap.SUPPORTED_ACTIONS,
                status=CapabilityStatus.AVAILABLE,
                handler=desktop_cap,
                availability_probe=_probe_desktop,
                availability_reason="initially registered; runtime probe pending",
            )
        )
    except Exception as exc:
        registry.register(
            Capability(
                name="desktop_control",
                description="Desktop automation unavailable.",
                supported_actions=set(),
                status=CapabilityStatus.UNAVAILABLE,
            )
        )

    # 2. Browser Capability
    try:
        from advi.capabilities.browser.executor import BrowserCapability
        browser_cap = BrowserCapability(context=shared_context)
        registry.register(
            Capability(
                name="browser_control",
                description="Automate Chrome via DevTools Protocol (CDP): navigate websites, search in-page, click elements.",
                supported_actions=browser_cap.SUPPORTED_ACTIONS,
                status=CapabilityStatus.AVAILABLE,
                handler=browser_cap,
                availability_probe=_probe_browser,
                availability_reason="initially registered; runtime probe pending",
            )
        )
    except Exception as exc:
        registry.register(
            Capability(
                name="browser_control",
                description="Browser automation unavailable.",
                supported_actions=set(),
                status=CapabilityStatus.UNAVAILABLE,
            )
        )

    # 3. Filesystem Capability
    try:
        from advi.capabilities.files.executor import FileCapability
        file_cap = FileCapability(environment_watchtower=environment_watchtower, desktop_context=shared_context)
        registry.register(
            Capability(
                name="file_management",
                description="Save, read, create, delete, and verify local files (especially on Desktop).",
                supported_actions=file_cap.SUPPORTED_ACTIONS,
                status=CapabilityStatus.AVAILABLE,
                handler=file_cap,
                availability_probe=_probe_handler,
                availability_reason="initially registered; runtime probe pending",
            )
        )
    except Exception as exc:
        registry.register(
            Capability(
                name="file_management",
                description="File capability unavailable.",
                supported_actions=set(),
                status=CapabilityStatus.UNAVAILABLE,
            )
        )

    # 4. Gmail Capability
    try:
        from advi.capabilities.gmail.executor import GmailCapability
        gmail_cap = GmailCapability(service=gmail_service)
        status = CapabilityStatus.AVAILABLE if gmail_service is not None else CapabilityStatus.UNAVAILABLE
        registry.register(
            Capability(
                name="email",
                description="Draft, read, update, and send email via connected Gmail account.",
                supported_actions=gmail_cap.SUPPORTED_ACTIONS,
                status=status,
                requires_permission=False,
                handler=gmail_cap,
                metadata={"optional": True},
                availability_probe=_probe_handler,
                availability_reason="initially registered; runtime probe pending",
            )
        )
    except Exception as exc:
        logger.warning("Gmail capability could not be registered: %s", exc)

    # 5. Local system information (read-only, deterministic, provider-independent)
    try:
        from advi.capabilities.system import SystemCapability
        system_cap = SystemCapability()
        registry.register(
            Capability(
                name="system",
                description="Read local system status and hardware/runtime information without a language-model call.",
                supported_actions=system_cap.SUPPORTED_ACTIONS,
                status=CapabilityStatus.AVAILABLE,
                handler=system_cap,
                availability_reason="local system probe available",
            )
        )
    except Exception as exc:
        logger.warning("System capability could not be registered: %s", exc)

    # 5b. App skills (WhatsApp Desktop via UI Automation)
    try:
        from advi.capabilities.apps import MessagingCapability
        messaging_cap = MessagingCapability()
        registry.register(
            Capability(
                name="messaging",
                description="Send messages and open chats in WhatsApp Desktop as verified single-call skills.",
                supported_actions=messaging_cap.SUPPORTED_ACTIONS,
                status=CapabilityStatus.AVAILABLE if messaging_cap.is_available() else CapabilityStatus.UNAVAILABLE,
                handler=messaging_cap,
                metadata={"optional": True},
                availability_probe=_probe_handler,
                availability_reason="initially registered; runtime probe pending",
            )
        )
    except Exception as exc:
        logger.warning("Messaging capability could not be registered: %s", exc)

    # 5c. Structured desktop perception (UI Automation element IDs)
    try:
        from advi.capabilities.apps import DesktopUiCapability
        ui_cap = DesktopUiCapability(desktop_context=shared_context)
        registry.register(
            Capability(
                name="desktop_ui",
                description="Observe application windows as interactive elements with IDs and act on them with verification.",
                supported_actions=ui_cap.SUPPORTED_ACTIONS,
                status=CapabilityStatus.AVAILABLE if ui_cap.is_available() else CapabilityStatus.UNAVAILABLE,
                handler=ui_cap,
                availability_probe=_probe_handler,
                availability_reason="initially registered; runtime probe pending",
            )
        )
    except Exception as exc:
        logger.warning("Desktop UI capability could not be registered: %s", exc)

    # 6. David fallback (supported subsystem, isolated from the primary brain)
    if david_fallback_service is not None:
        try:
            from advi.fallback.capability import DavidFallbackCapability
            from advi.fallback.david_adapter import DavidAdapter
            david_cap = DavidFallbackCapability(DavidAdapter(david_fallback_service))
            registry.register(
                Capability(
                    name="david_fallback",
                    description="Isolated David desktop/browser fallback for unsupported tasks and primary capability failures.",
                    supported_actions=david_cap.SUPPORTED_ACTIONS,
                    status=CapabilityStatus.AVAILABLE if david_cap.is_available() else CapabilityStatus.UNAVAILABLE,
                    handler=david_cap,
                    metadata={"optional": True},
                    availability_probe=_probe_handler,
                    availability_reason="David fallback configured.",
                )
            )
        except Exception as exc:
            logger.warning("David fallback capability could not be registered: %s", exc)

    # 6. MCP (optional; dormant unless configured and the SDK is installed)
    try:
        from advi.capabilities.mcp import MCPCapability
        mcp_cap = MCPCapability(manager=mcp_manager)
        registry.register(
            Capability(
                name="mcp",
                description="Connect to configured Model Context Protocol servers and call their tools.",
                supported_actions=mcp_cap.SUPPORTED_ACTIONS,
                status=CapabilityStatus.AVAILABLE if mcp_cap.is_available() else CapabilityStatus.UNAVAILABLE,
                handler=mcp_cap,
                metadata={"optional": True},
                availability_probe=_probe_handler,
                availability_reason="MCP configured and available." if mcp_cap.is_available() else "MCP not configured or SDK unavailable.",
            )
        )
    except Exception as exc:
        logger.warning("MCP capability could not be registered: %s", exc)

    # 7. Memory Capability
    try:
        from advi.capabilities.memory.executor import MemoryCapability
        mem_cap = MemoryCapability(memory=memory, retriever=retriever)
        status = CapabilityStatus.AVAILABLE if (memory is not None or retriever is not None) else CapabilityStatus.UNAVAILABLE
        registry.register(
            Capability(
                name="memory",
                description="Remember user facts, preferences, and retrieve durable contextual memories.",
                supported_actions=mem_cap.SUPPORTED_ACTIONS,
                status=status,
                handler=mem_cap,
                metadata={"optional": True},
                availability_probe=_probe_handler,
                availability_reason="initially registered; runtime probe pending",
            )
        )
    except Exception as exc:
        logger.warning("Memory capability could not be registered: %s", exc)

    # Runtime probes are opt-out because importing the module should never touch
    # browser/OS state or emit connection errors. Production startup explicitly
    # requests a fresh refresh; lazy consumers refresh their owning capability
    # immediately before execution.
    if refresh:
        registry.refresh_all()
    return registry


_default_registry: CapabilityRegistry | None = None


def get_default_registry() -> CapabilityRegistry:
    """Return the lazily constructed process-default registry.

    The native application passes its configured registry explicitly. This
    fallback exists for compatibility and tests without constructing desktop,
    browser, or fallback handlers at import time.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry(refresh=False)
    return _default_registry


def __getattr__(name: str):
    if name == "default_registry":
        return get_default_registry()
    raise AttributeError(name)
