from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

def _get_desktop_class():
    """Load pywinauto only when fallback UI perception is requested."""
    from pywinauto import Desktop
    return Desktop


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerceptionResult:
    found: bool
    target: Any = None
    method: str | None = None
    error: str | None = None


class ScreenPerception:
    """
    Resolve semantic UI targets.

    Resolution order:

        1. Windows UI Automation
        2. Vision fallback

    UIA is preferred whenever the target can be resolved
    reliably.

    Vision is used only when UIA cannot resolve the target.

    Website search fields are handled carefully so that a
    browser address bar is never returned as a website
    search field merely because both are editable controls.
    """

    def __init__(
        self,
        vision=None,
    ) -> None:

        self.vision = vision

        if self.vision is None:

            from .vision import VisionPerception

            self.vision = VisionPerception()

    def find(
        self,
        target: str,
        action: str | None = None,
        application: str | None = None,
    ) -> PerceptionResult:

        if not target:

            return PerceptionResult(
                found=False,
                error="Target was not specified.",
            )

        # =====================================================
        # CDP FAST-PATH FOR CHROME BROWSER ACTIONS
        # =====================================================
        if application and application.lower() in {"chrome", "google chrome"}:
            target_lower = target.strip().lower()
            if action in {"search", "navigate", "click", "submit"}:
                if not ("address bar" in target_lower or "addressbar" in target_lower):
                    logger.info("CDP fast-path resolving target '%s' for action '%s'.", target, action)
                    return PerceptionResult(
                        found=True,
                        target=target,
                        method="cdp",
                    )


        # =====================================================
        # WINDOW OPERATIONS
        # =====================================================

        if action in {
            "focus_window",
            "close_window",
        }:

            window_result = self._find_window(
                target
            )

            if window_result.found:
                return window_result

            logger.info(
                "UIA could not resolve window '%s'. "
                "Trying vision fallback.",
                target,
            )

        # =====================================================
        # UIA
        # =====================================================

        uia_result = self._find_with_uia(
            target=target,
            action=action,
            application=application,
        )

        if uia_result.found:
            return uia_result

        logger.info(
            "UIA could not find '%s'. "
            "Trying vision fallback.",
            target,
        )

        # =====================================================
        # VISION FALLBACK
        # =====================================================

        vision_result = self.vision.find(
            target=target,
            action=action,
            application=application,
        )

        if vision_result.found:

            return PerceptionResult(
                found=True,
                target=vision_result.target,
                method="vision",
            )

        return PerceptionResult(
            found=False,
            method="vision",
            error=(
                vision_result.error
                or uia_result.error
                or "Target could not be found."
            ),
        )

    # =========================================================
    # WINDOW RESOLUTION
    # =========================================================

    def _find_window(
        self,
        target: str,
    ) -> PerceptionResult:

        try:

            Desktop = _get_desktop_class()
            Desktop = _get_desktop_class()
            desktop = Desktop(
                backend="uia"
            )

            target_lower = (
                target.strip().lower()
            )

            normalized_target = (
                target_lower
                .replace(
                    " application window",
                    "",
                )
                .replace(
                    " app window",
                    "",
                )
                .replace(
                    " main window",
                    "",
                )
                .replace(
                    " window",
                    "",
                )
                .strip()
            )

            if not normalized_target:

                return PerceptionResult(
                    found=False,
                    method="uia",
                    error=(
                        "Window target was empty "
                        "after normalization."
                    ),
                )

            for window in desktop.windows():

                try:

                    if not window.is_visible():
                        continue

                    if not window.is_enabled():
                        continue

                    title = (
                        window.window_text()
                        or ""
                    ).strip()

                    if not title:
                        continue

                    title_lower = title.lower()

                    if normalized_target in title_lower:

                        logger.info(
                            "Resolved window '%s' "
                            "to '%s'.",
                            target,
                            title,
                        )

                        return PerceptionResult(
                            found=True,
                            target=window,
                            method="uia",
                        )

                except Exception:
                    continue

            return PerceptionResult(
                found=False,
                method="uia",
                error=(
                    f"Window could not be found: "
                    f"{target}"
                ),
            )

        except Exception as exc:

            logger.exception(
                "Window perception failed."
            )

            return PerceptionResult(
                found=False,
                method="uia",
                error=str(exc),
            )

    # =========================================================
    # GENERAL UIA RESOLUTION
    # =========================================================

    def _find_with_uia(
        self,
        target: str,
        action: str | None = None,
        application: str | None = None,
    ) -> PerceptionResult:

        try:

            Desktop = _get_desktop_class()
            Desktop = _get_desktop_class()
            desktop = Desktop(
                backend="uia"
            )

            target_lower = (
                target.strip().lower()
            )

            semantic_text_actions = {
                "type_text",
                "click",
                "double_click",
                "right_click",
            }

            # =================================================
            # SEARCH TOP-LEVEL WINDOWS
            # =================================================

            for window in desktop.windows():

                try:

                    if not window.is_visible():
                        continue

                    if not window.is_enabled():
                        continue

                    title = (
                        window.window_text()
                        or ""
                    ).strip()

                    if not title:
                        continue

                    title_lower = title.lower()

                    # -------------------------------------------------
                    # APPLICATION CONTEXT
                    # -------------------------------------------------
                    # When the execution loop already knows which
                    # application owns the current action, never
                    # resolve a normal UI target from another
                    # application such as VS Code.
                    #
                    # Search targets keep their existing behaviour
                    # below because their own application hint logic
                    # is already working.
                    # -------------------------------------------------
                    if (
                        application
                        and action != "search"
                        and not self._window_matches_application(
                            title_lower,
                            application,
                        )
                    ):
                        continue

                    # -----------------------------------------
                    # Focus window
                    # -----------------------------------------

                    if action == "focus_window":

                        if not self._looks_like_window_target(
                            target_lower
                        ):
                            continue

                        normalized_target = (
                            target_lower
                            .replace(
                                " application window",
                                "",
                            )
                            .replace(
                                " app window",
                                "",
                            )
                            .replace(
                                " main window",
                                "",
                            )
                            .replace(
                                " window",
                                "",
                            )
                            .strip()
                        )

                        if (
                            normalized_target
                            and normalized_target
                            in title_lower
                        ):

                            return PerceptionResult(
                                found=True,
                                target=window,
                                method="uia",
                            )

                    # -----------------------------------------
                    # Website/application search box
                    # -----------------------------------------

                    if (
                        action == "search"
                        and self._looks_like_search_target(
                            target_lower
                        )
                    ):

                        application_hint = (
                            self._extract_application_hint(
                                target_lower
                            )
                        )

                        if (
                            application_hint
                            and not self._window_matches_application(
                                title_lower,
                                application_hint,
                            )
                        ):
                            continue

                        search_control = (
                            self._find_search_control(
                                window
                            )
                        )

                        if search_control is not None:

                            logger.info(
                                "Resolved search target "
                                "'%s' to actual search "
                                "control inside '%s'.",
                                target,
                                title,
                            )

                            return PerceptionResult(
                                found=True,
                                target=search_control,
                                method="uia",
                            )

                    # -----------------------------------------
                    # Text / editing area
                    # -----------------------------------------

                    if (
                        action in semantic_text_actions
                        and self._looks_like_text_target(
                            target_lower
                        )
                        and not (
                            action == "search"
                            and self._looks_like_search_target(
                                target_lower
                            )
                        )
                    ):

                        application_hint = (
                            self._extract_application_hint(
                                target_lower
                            )
                        )

                        if (
                            application_hint
                            and not self._window_matches_application(
                                title_lower,
                                application_hint,
                            )
                        ):
                            continue

                        editable = (
                            self._find_editable_control(
                                window
                            )
                        )

                        if editable is not None:

                            logger.info(
                                "Resolved semantic target "
                                "'%s' to editable control "
                                "inside '%s'.",
                                target,
                                title,
                            )

                            return PerceptionResult(
                                found=True,
                                target=editable,
                                method="uia",
                            )

                    # -----------------------------------------
                    # Normal window target
                    # -----------------------------------------

                    if (
                        target_lower in title_lower
                        and title
                    ):

                        return PerceptionResult(
                            found=True,
                            target=window,
                            method="uia",
                        )

                except Exception:
                    continue

            # =================================================
            # INDIVIDUAL UI CONTROLS
            # =================================================

            for window in desktop.windows():

                try:

                    if not window.is_visible():
                        continue

                    if not window.is_enabled():
                        continue

                    # Keep normal target resolution inside the
                    # application established by the execution loop.
                    # This prevents a matching control in VS Code or
                    # another visible application from being selected.
                    if (
                        application
                        and action != "search"
                        and not self._window_matches_application(
                            (window.window_text() or "").strip().lower(),
                            application,
                        )
                    ):
                        continue

                    controls = window.descendants()

                except Exception:
                    continue

                for control in controls:

                    try:

                        if not control.is_visible():
                            continue

                        if not control.is_enabled():
                            continue

                        name = (
                            control.window_text()
                            or ""
                        ).strip()

                        if not name:
                            continue

                        name_lower = name.lower()

                        # -------------------------------------
                        # Search targets
                        #
                        # Do NOT allow a generic control name
                        # match to accidentally select Chrome's
                        # address bar.
                        # -------------------------------------

                        if (
                            action == "search"
                            and self._looks_like_search_target(
                                target_lower
                            )
                        ):

                            if self._control_looks_like_search(
                                control
                            ):

                                logger.info(
                                    "Resolved search target "
                                    "'%s' to UIA control '%s'.",
                                    target,
                                    name,
                                )

                                return PerceptionResult(
                                    found=True,
                                    target=control,
                                    method="uia",
                                )

                            continue

                        # -------------------------------------
                        # Normal semantic control
                        # -------------------------------------
                        # For mouse clicks, never treat an editable
                        # browser control as a generic semantic target.
                        # Chrome's address/search field is an Edit control
                        # and must not become the target for a page result.
                        if action in {
                            "click",
                            "double_click",
                            "right_click",
                        }:
                            try:
                                control_type = (
                                    getattr(
                                        control.element_info,
                                        "control_type",
                                        "",
                                    )
                                    or ""
                                ).strip().lower()
                            except Exception:
                                control_type = ""

                            if control_type in {
                                "edit",
                                "combobox",
                                "document",
                            }:
                                continue

                        if target_lower in name_lower:

                            logger.info(
                                "Resolved target '%s' "
                                "to UIA control '%s'.",
                                target,
                                name,
                            )

                            return PerceptionResult(
                                found=True,
                                target=control,
                                method="uia",
                            )

                    except Exception:
                        continue

            # =================================================
            # EDITABLE FALLBACK
            # =================================================

            # IMPORTANT:
            #
            # Never use the generic editable fallback for a
            # website search target.
            #
            # Otherwise Chrome's address bar can be returned
            # because it is also an Edit control.

            if (
                action in semantic_text_actions
                and self._looks_like_text_target(
                    target_lower
                )
                and not (
                    action == "search"
                    and self._looks_like_search_target(
                        target_lower
                    )
                )
            ):

                for window in desktop.windows():

                    try:

                        if (
                            application
                            and not self._window_matches_application(
                                (window.window_text() or "").strip().lower(),
                                application,
                            )
                        ):
                            continue

                        editable = (
                            self._find_editable_control(
                                window
                            )
                        )

                        if editable is not None:

                            logger.info(
                                "Using editable UIA fallback "
                                "for target '%s'.",
                                target,
                            )

                            return PerceptionResult(
                                found=True,
                                target=editable,
                                method="uia",
                            )

                    except Exception:
                        continue

            return PerceptionResult(
                found=False,
                method="uia",
                error=(
                    f"UIA could not find target: "
                    f"{target}"
                ),
            )

        except Exception as exc:

            logger.exception(
                "UI Automation perception failed."
            )

            return PerceptionResult(
                found=False,
                method="uia",
                error=str(exc),
            )

    # =========================================================
    # SEMANTIC TARGET HELPERS
    # =========================================================

    @staticmethod
    def _looks_like_window_target(
        target: str,
    ) -> bool:

        window_keywords = {
            "window",
            "application window",
            "app window",
            "main window",
        }

        return any(
            keyword in target
            for keyword in window_keywords
        )

    @staticmethod
    def _looks_like_search_target(
        target: str,
    ) -> bool:

        search_keywords = {
            "search box",
            "search field",
            "search bar",
            "search input",
            "search textbox",
            "search text box",
        }

        return any(
            keyword in target
            for keyword in search_keywords
        )

    @staticmethod
    def _looks_like_text_target(
        target: str,
    ) -> bool:

        text_keywords = {
            "text",
            "document",
            "editing area",
            "editor",
            "body",
            "field",
            "input",
            "textbox",
            "text box",
            "search box",
            "search field",
            "message",
            "content",
        }

        return any(
            keyword in target
            for keyword in text_keywords
        )

    # =========================================================
    # APPLICATION HINT
    # =========================================================

    @staticmethod
    def _extract_application_hint(
        target: str,
    ) -> str | None:

        text_markers = (
            " search box",
            " search field",
            " search bar",
            " search input",
            " search textbox",
            " search text box",
            " document editing area",
            " editing area",
            " document editor",
            " text box",
            " textbox",
            " editor",
        )

        for marker in text_markers:

            if marker in target:

                prefix = target.split(
                    marker,
                    1,
                )[0].strip()

                if prefix:
                    return prefix

        return None

    # =========================================================
    # APPLICATION MATCHING
    # =========================================================

    @staticmethod
    def _window_matches_application(
        title: str,
        application: str,
    ) -> bool:

        application = (
            application.strip().lower()
        )

        title = (
            title.strip().lower()
        )

        if not application:
            return True

        if application in title:
            return True

        normalized_application = (
            application
            .replace(" ", "")
            .replace("-", "")
            .replace("_", "")
        )

        normalized_title = (
            title
            .replace(" ", "")
            .replace("-", "")
            .replace("_", "")
        )

        if normalized_application in normalized_title:
            return True

        # Common browser naming variations.
        aliases = {
            "chrome": {
                "chrome",
                "google chrome",
            },
            "google chrome": {
                "chrome",
                "google chrome",
            },
            "edge": {
                "edge",
                "microsoft edge",
            },
            "microsoft edge": {
                "edge",
                "microsoft edge",
            },
            "firefox": {
                "firefox",
                "mozilla firefox",
            },
            "mozilla firefox": {
                "firefox",
                "mozilla firefox",
            },
        }

        for alias in aliases.get(
            application,
            set(),
        ):

            if alias in title:
                return True

        return False

    # =========================================================
    # SEARCH CONTROL RESOLUTION
    # =========================================================

    @staticmethod
    def _find_search_control(
        window: Any,
    ) -> Any | None:
        """
        Find an actual search input through UIA.

        IMPORTANT:

        A browser address bar is also an Edit control.

        Therefore a generic Edit control is NEVER accepted
        as a website search field.

        UIA search resolution only succeeds when the control
        itself exposes search-related semantic information.

        If no such control exists, this method returns None
        and ScreenPerception.find() automatically falls back
        to Vision.
        """

        try:

            descendants = window.descendants()

        except Exception:

            return None

        search_candidates = []

        for control in descendants:

            try:

                if not control.is_visible():
                    continue

                if not control.is_enabled():
                    continue

                element_info = (
                    control.element_info
                )

                control_type = (
                    getattr(
                        element_info,
                        "control_type",
                        "",
                    )
                    or ""
                ).strip().lower()

                # ---------------------------------------------
                # Never select obvious non-input controls.
                # ---------------------------------------------

                if control_type in {
                    "button",
                    "hyperlink",
                    "menuitem",
                    "splitbutton",
                    "image",
                    "text",
                }:
                    continue

                name = (
                    control.window_text()
                    or ""
                ).strip().lower()

                automation_id = (
                    getattr(
                        element_info,
                        "automation_id",
                        "",
                    )
                    or ""
                ).strip().lower()

                class_name = (
                    getattr(
                        element_info,
                        "class_name",
                        "",
                    )
                    or ""
                ).strip().lower()

                # Try to read additional UIA properties when
                # available. Some browser controls expose
                # useful semantic information through these.
                localized_control_type = (
                    getattr(
                        element_info,
                        "localized_control_type",
                        "",
                    )
                    or ""
                ).strip().lower()

                search_text = (
                    f"{name} "
                    f"{automation_id} "
                    f"{class_name} "
                    f"{localized_control_type}"
                )

                # ---------------------------------------------
                # Only accept controls with explicit search
                # semantics.
                # ---------------------------------------------

                if "search" not in search_text:
                    continue

                # ---------------------------------------------
                # Prefer editable controls.
                # ---------------------------------------------

                score = 0

                if control_type == "edit":
                    score += 100

                if "search" in name:
                    score += 50

                if "search" in automation_id:
                    score += 40

                if "search" in class_name:
                    score += 30

                if "search" in localized_control_type:
                    score += 20

                search_candidates.append(
                    (
                        score,
                        control,
                    )
                )

            except Exception:
                continue

        if not search_candidates:
            logger.info(
                "UIA found no explicitly searchable "
                "control. Vision fallback will be used."
            )

            return None

        search_candidates.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        selected = (
            search_candidates[0][1]
        )

        logger.info(
            "Selected UIA search control: "
            "name='%s', automation_id='%s', "
            "control_type='%s'.",
            (
                selected.window_text()
                or ""
            ),
            (
                getattr(
                    selected.element_info,
                    "automation_id",
                    "",
                )
                or ""
            ),
            (
                getattr(
                    selected.element_info,
                    "control_type",
                    "",
                )
                or ""
            ),
        )

        return selected

    # =========================================================
    # CONTROL SEARCH SEMANTICS
    # =========================================================

    @staticmethod
    def _control_looks_like_search(
        control: Any,
    ) -> bool:

        try:

            element_info = (
                control.element_info
            )

            control_type = (
                getattr(
                    element_info,
                    "control_type",
                    "",
                )
                or ""
            ).strip().lower()

            if control_type in {
                "button",
                "hyperlink",
                "menuitem",
                "splitbutton",
                "image",
                "text",
            }:
                return False

            name = (
                control.window_text()
                or ""
            ).strip().lower()

            automation_id = (
                getattr(
                    element_info,
                    "automation_id",
                    "",
                )
                or ""
            ).strip().lower()

            class_name = (
                getattr(
                    element_info,
                    "class_name",
                    "",
                )
                or ""
            ).strip().lower()

            localized_control_type = (
                getattr(
                    element_info,
                    "localized_control_type",
                    "",
                )
                or ""
            ).strip().lower()

            searchable_text = (
                f"{name} "
                f"{automation_id} "
                f"{class_name} "
                f"{localized_control_type}"
            )

            return (
                "search"
                in searchable_text
            )

        except Exception:
            return False

    # =========================================================
    # EDITABLE CONTROL RESOLUTION
    # =========================================================

    @staticmethod
    def _find_editable_control(
        window: Any,
    ) -> Any | None:

        try:

            descendants = window.descendants()

        except Exception:

            return None

        document_candidates = []
        edit_candidates = []

        for control in descendants:

            try:

                if not control.is_visible():
                    continue

                if not control.is_enabled():
                    continue

                control_type = (
                    control.element_info.control_type
                )

                if control_type == "Document":

                    document_candidates.append(
                        control
                    )

                elif control_type == "Edit":

                    edit_candidates.append(
                        control
                    )

            except Exception:
                continue

        if document_candidates:
            return document_candidates[0]

        if edit_candidates:
            return edit_candidates[0]

        return None