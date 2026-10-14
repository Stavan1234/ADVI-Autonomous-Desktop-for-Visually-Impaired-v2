from __future__ import annotations

import ctypes
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _get_pyautogui():
    """Load pyautogui only when screen perception actually runs."""
    import pyautogui
    return pyautogui


def _get_desktop_class():
    """Load pywinauto only when window perception actually runs."""
    from pywinauto import Desktop
    return Desktop


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisionResult:
    found: bool
    target: Any = None
    screenshot_path: Path | None = None
    error: str | None = None


class VisionPerception:
    """
    Screenshot-based visual perception fallback.

    The resolver is website-agnostic.

    Resolution flow:

        1. Capture current screen
        2. Identify the relevant application/browser window
        3. Restrict vision to the webpage/application area
        4. OCR visible text
        5. Match the semantic target
        6. Convert local coordinates to screen coordinates
        7. Return screen coordinates to the executor

    Example:

        "YouTube search bar"
            -> locate YouTube browser window
            -> OCR webpage area
            -> find "Search"
            -> return absolute screen coordinates

        "Amazon search bar"
            -> locate Amazon browser window
            -> OCR webpage area
            -> find search field
            -> return absolute screen coordinates

    No website-specific coordinates are stored here.
    """

    def __init__(
        self,
        screenshot_dir: str | Path = "runtime/screenshots",
    ) -> None:
        self.screenshot_dir = Path(
            screenshot_dir
        )

    # =========================================================
    # SCREEN CAPTURE
    # =========================================================

    def capture_screen(self) -> Path:
        self.screenshot_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        screenshot_path = (
            self.screenshot_dir
            / "current_screen.png"
        )

        image = _get_pyautogui().screenshot()

        image.save(
            screenshot_path
        )

        return screenshot_path

    # =========================================================
    # MAIN VISION ENTRY POINT
    # =========================================================

    def find(
        self,
        target: str,
        action: str | None = None,
        application: str | None = None,
    ) -> VisionResult:

        if not target:
            return VisionResult(
                found=False,
                error="Target was not specified.",
            )

        try:
            screenshot_path = (
                self.capture_screen()
            )

            logger.info(
                "Vision fallback captured screen: %s",
                screenshot_path,
            )

            # -------------------------------------------------
            # Optional dependencies
            # -------------------------------------------------

            try:
                import pytesseract
            except ImportError:
                return VisionResult(
                    found=False,
                    screenshot_path=screenshot_path,
                    error=(
                        "pytesseract is not installed. "
                        "Install it before using visual "
                        "target detection."
                    ),
                )

            try:
                from PIL import Image
            except ImportError:
                return VisionResult(
                    found=False,
                    screenshot_path=screenshot_path,
                    error=(
                        "Pillow is required for visual "
                        "target detection."
                    ),
                )

            image = Image.open(
                screenshot_path
            )

            target_lower = (
                target.strip().lower()
            )

            application_hint = (
                self._extract_application_hint(
                    target_lower
                )
            )

            # The execution loop already knows which application owns
            # the current action. Use that context for generic targets
            # such as "Baby Shark video", which do not contain an
            # application name themselves.
            if not application_hint and application:
                application_hint = str(application).strip() or None

            # -------------------------------------------------
            # Find relevant application/browser window
            # -------------------------------------------------

            window_info = (
                self._find_relevant_window(
                    application_hint
                )
            )

            # -------------------------------------------------
            # Determine OCR image and coordinate offset
            # -------------------------------------------------

            if window_info is not None:

                left = window_info["left"]
                top = window_info["top"]
                right = window_info["right"]
                bottom = window_info["bottom"]

                logger.info(
                    "Vision restricting search to window: "
                    "'%s' [%s, %s, %s, %s]",
                    window_info["title"],
                    left,
                    top,
                    right,
                    bottom,
                )

                # ---------------------------------------------
                # Remove browser/application chrome from the
                # OCR region.
                #
                # The webpage/application content normally
                # starts below the title/tab/address area.
                # ---------------------------------------------

                content_top_offset = (
                    self._content_top_offset(
                        window_info
                    )
                )

                crop_left = max(
                    0,
                    left,
                )

                crop_top = max(
                    0,
                    top + content_top_offset,
                )

                crop_right = min(
                    image.width,
                    right,
                )

                crop_bottom = min(
                    image.height,
                    bottom,
                )

                if (
                    crop_right <= crop_left
                    or crop_bottom <= crop_top
                ):
                    logger.warning(
                        "Calculated window crop is invalid. "
                        "Falling back to full-screen OCR."
                    )

                    ocr_image = image
                    origin_x = 0
                    origin_y = 0

                else:

                    ocr_image = image.crop(
                        (
                            crop_left,
                            crop_top,
                            crop_right,
                            crop_bottom,
                        )
                    )

                    origin_x = crop_left
                    origin_y = crop_top

            else:

                logger.info(
                    "No matching application window found "
                    "for hint '%s'. Using full-screen vision.",
                    application_hint,
                )

                ocr_image = image
                origin_x = 0
                origin_y = 0

            # -------------------------------------------------
            # OCR
            # -------------------------------------------------

            try:
                ocr_data = (
                    pytesseract.image_to_data(
                        ocr_image,
                        output_type=(
                            pytesseract.Output.DICT
                        ),
                        config="--psm 11",
                    )
                )

            except Exception as exc:
                logger.exception(
                    "OCR failed."
                )

                return VisionResult(
                    found=False,
                    screenshot_path=screenshot_path,
                    error=(
                        "OCR failed. Make sure Tesseract "
                        "OCR is installed and accessible. "
                        f"Details: {exc}"
                    ),
                )

            candidates = []

            total_items = len(
                ocr_data.get(
                    "text",
                    [],
                )
            )

            for index in range(
                total_items
            ):

                text = (
                    ocr_data["text"][index]
                    or ""
                ).strip()

                if not text:
                    continue

                confidence = (
                    self._parse_confidence(
                        ocr_data
                        .get("conf", [])[index]
                    )
                )

                if confidence < 20:
                    continue

                local_x = int(
                    ocr_data["left"][index]
                )

                local_y = int(
                    ocr_data["top"][index]
                )

                width = int(
                    ocr_data["width"][index]
                )

                height = int(
                    ocr_data["height"][index]
                )

                # ---------------------------------------------
                # Convert OCR coordinates back to absolute
                # Windows screen coordinates.
                # ---------------------------------------------

                screen_x = (
                    origin_x
                    + local_x
                )

                screen_y = (
                    origin_y
                    + local_y
                )

                candidates.append(
                    {
                        "text": text,
                        "text_lower": text.lower(),

                        # Local coordinates
                        "local_x": local_x,
                        "local_y": local_y,

                        # Absolute screen coordinates
                        "x": screen_x,
                        "y": screen_y,

                        "width": width,
                        "height": height,

                        "center_x": (
                            screen_x
                            + width // 2
                        ),

                        "center_y": (
                            screen_y
                            + height // 2
                        ),

                        "confidence": confidence,
                        "block_num": ocr_data.get("block_num", [None] * total_items)[index],
                        "par_num": ocr_data.get("par_num", [None] * total_items)[index],
                        "line_num": ocr_data.get("line_num", [None] * total_items)[index],
                    }
                )

            if not candidates:
                return VisionResult(
                    found=False,
                    screenshot_path=screenshot_path,
                    error=(
                        "Vision could not detect any "
                        "usable text on the target "
                        "application/page."
                    ),
                )

            logger.info(
                "Vision OCR detected %s text candidates.",
                len(candidates),
            )

            # -------------------------------------------------
            # Target-specific visual resolution
            # -------------------------------------------------

            if self._looks_like_search_target(
                target_lower
            ):

                result = (
                    self._find_search_target(
                        candidates=candidates,
                        application_hint=application_hint,
                    )
                )

            else:

                result = (
                    self._find_generic_target(
                        candidates=candidates,
                        target=target_lower,
                        application_hint=application_hint,
                    )
                )

            if result is None:
                return VisionResult(
                    found=False,
                    screenshot_path=screenshot_path,
                    error=(
                        f"Vision could not resolve "
                        f"target: {target}"
                    ),
                )

            logger.info(
                "Vision resolved '%s' to screen "
                "coordinates (%s, %s).",
                target,
                result["x"],
                result["y"],
            )

            return VisionResult(
                found=True,
                target=result,
                screenshot_path=screenshot_path,
            )

        except Exception as exc:
            logger.exception(
                "Vision perception failed."
            )

            return VisionResult(
                found=False,
                error=str(exc),
            )

    # =========================================================
    # WINDOW RESOLUTION
    # =========================================================

    @staticmethod
    def _find_relevant_window(
        application_hint: str | None,
    ) -> dict[str, Any] | None:
        """
        Find the visible desktop window corresponding to
        the semantic application/site hint.

        Example:

            "YouTube search bar"
                -> hint = "youtube"

        The method does NOT contain website-specific logic.
        It simply compares the semantic hint against the
        current visible window titles.
        """

        if not application_hint:
            return None

        try:
            Desktop = _get_desktop_class()
            desktop = Desktop(
                backend="uia"
            )

            normalized_hint = (
                VisionPerception
                ._normalize_text(
                    application_hint
                )
            )

            if not normalized_hint:
                return None

            candidates = []

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

                    normalized_title = (
                        VisionPerception
                        ._normalize_text(
                            title
                        )
                    )

                    if (
                        normalized_hint
                        in normalized_title
                    ):

                        try:
                            rect = (
                                window.rectangle()
                            )

                            left = int(
                                rect.left
                            )

                            top = int(
                                rect.top
                            )

                            right = int(
                                rect.right
                            )

                            bottom = int(
                                rect.bottom
                            )

                        except Exception:
                            continue

                        area = max(
                            0,
                            right - left,
                        ) * max(
                            0,
                            bottom - top,
                        )

                        candidates.append(
                            {
                                "window": window,
                                "title": title,
                                "left": left,
                                "top": top,
                                "right": right,
                                "bottom": bottom,
                                "area": area,
                            }
                        )

                except Exception:
                    continue

            if not candidates:
                return None

            # Prefer the real foreground window when it belongs to the
            # requested application. This avoids selecting another Chrome
            # window merely because it is larger or appears first in UIA.
            foreground_hwnd = 0
            try:
                foreground_hwnd = int(
                    ctypes.windll.user32.GetForegroundWindow()
                )
            except Exception:
                pass

            foreground_matches = [
                item
                for item in candidates
                if foreground_hwnd
                and int(item["window"].handle) == foreground_hwnd
            ]

            if foreground_matches:
                selected = foreground_matches[0]
            else:
                candidates.sort(
                    key=lambda item: item["area"],
                    reverse=True,
                )
                selected = candidates[0]

            logger.info(
                "Vision matched semantic application "
                "hint '%s' to window '%s'.",
                application_hint,
                selected["title"],
            )

            return selected

        except Exception as exc:
            logger.warning(
                "Could not resolve application window "
                "for vision: %s",
                exc,
            )

            return None

    @staticmethod
    def _content_top_offset(
        window_info: dict[str, Any],
    ) -> int:
        """
        Estimate the amount of top browser/application chrome
        to exclude from OCR.

        This is intentionally generic.

        It does not depend on YouTube, Amazon, Chrome, etc.

        The goal is simply to avoid selecting things such as
        browser tabs, address bar text, or Windows UI when
        looking for webpage controls.
        """

        title = (
            window_info.get(
                "title",
                "",
            )
            or ""
        ).lower()

        # Chrome / Edge / Firefox style browser windows
        # normally have title + tabs + address bar above the
        # webpage content.
        browser_markers = {
            "google chrome",
            "chrome",
            "microsoft edge",
            "edge",
            "firefox",
            "mozilla firefox",
            "opera",
            "brave",
        }

        if any(
            marker in title
            for marker in browser_markers
        ):
            return 105

        # Generic application window.
        return 35

    # =========================================================
    # SEARCH TARGET
    # =========================================================

    @staticmethod
    def _find_search_target(
        candidates: list[dict[str, Any]],
        application_hint: str | None,
    ) -> dict[str, Any] | None:

        scored = []

        for candidate in candidates:

            text = candidate["text_lower"]

            score = 0

            # -------------------------------------------------
            # Strong search indicators
            # -------------------------------------------------

            if text == "search":
                score += 120

            elif text in {
                "search...",
                "search…",
                "search here",
                "search products",
                "search amazon",
            }:
                score += 110

            elif "search" in text:
                score += 80

            # -------------------------------------------------
            # Application/site hint
            # -------------------------------------------------

            if application_hint:

                normalized_hint = (
                    VisionPerception
                    ._normalize_text(
                        application_hint
                    )
                )

                normalized_text = (
                    VisionPerception
                    ._normalize_text(
                        text
                    )
                )

                if (
                    normalized_hint
                    and normalized_hint
                    in normalized_text
                ):
                    score += 40

            # -------------------------------------------------
            # Avoid unrelated search text
            # -------------------------------------------------

            if any(
                word in text
                for word in {
                    "search results",
                    "searched",
                    "searching",
            }):
                score -= 50

            # -------------------------------------------------
            # Prefer larger OCR regions.
            #
            # Search-field placeholder text is normally
            # inside a reasonably sized input.
            # -------------------------------------------------

            width = candidate["width"]
            height = candidate["height"]

            if width >= 100:
                score += 10

            if height >= 15:
                score += 5

            if score <= 0:
                continue

            scored.append(
                (
                    score,
                    candidate,
                )
            )

        if not scored:
            return None

        # -----------------------------------------------------
        # Sort by semantic score first, OCR confidence second.
        # -----------------------------------------------------

        scored.sort(
            key=lambda item: (
                item[0],
                item[1]["confidence"],
            ),
            reverse=True,
        )

        best = scored[0][1]

        logger.info(
            "Vision selected search text '%s' at "
            "screen coordinates (%s, %s).",
            best["text"],
            best["center_x"],
            best["center_y"],
        )

        return {
            "x": best["center_x"],
            "y": best["center_y"],
            "width": best["width"],
            "height": best["height"],
            "source": "vision_ocr",
            "text": best["text"],
        }

    # =========================================================
    # GENERIC TARGET
    # =========================================================

    @staticmethod
    def _find_generic_target(
        candidates: list[dict[str, Any]],
        target: str,
        application_hint: str | None,
    ) -> dict[str, Any] | None:
        """
        Resolve a visible page target from OCR without allowing a
        single matching word to win when the user supplied a
        multi-word semantic target.

        For example, "Baby Shark video" should be resolved from the
        visible Baby + Shark result title, not from an unrelated
        occurrence of "video" elsewhere on the page.
        """
        target_words = VisionPerception._semantic_words(target)
        if not target_words:
            return None

        target_normalized = VisionPerception._normalize_text(" ".join(target_words))
        scored: list[tuple[int, dict[str, Any]]] = []

        # Group OCR words by their detected line. This lets us reason
        # about a visible result title rather than isolated OCR tokens.
        groups: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
        for candidate in candidates:
            key = (
                candidate.get("block_num"),
                candidate.get("par_num"),
                candidate.get("line_num"),
            )
            groups.setdefault(key, []).append(candidate)

        for group in groups.values():
            group.sort(key=lambda item: item["x"])
            joined = " ".join(item["text_lower"] for item in group)
            joined_normalized = VisionPerception._normalize_text(joined)

            present_words = {
                word
                for word in target_words
                if word in joined_normalized
            }

            # Require all meaningful target words to occur in the same
            # OCR line for a high-confidence phrase match.
            if len(present_words) == len(set(target_words)):
                score = 300
                if target_normalized and target_normalized in joined_normalized:
                    score += 120
            else:
                # Do not click on a partial one-word match for a
                # multi-word result target.
                if len(target_words) > 1:
                    continue
                score = 80 * len(present_words)

            confidence = max(
                float(item.get("confidence", 0))
                for item in group
            )
            score += int(confidence / 10)

            left = min(item["x"] for item in group)
            top = min(item["y"] for item in group)
            right = max(
                item["x"] + item["width"]
                for item in group
            )
            bottom = max(
                item["y"] + item["height"]
                for item in group
            )

            # Keep the clickable region around the matched text, rather
            # than returning an unrelated browser-control coordinate.
            candidate = {
                "x": left,
                "y": top,
                "width": max(1, right - left),
                "height": max(1, bottom - top),
                "center_x": left + max(1, right - left) // 2,
                "center_y": top + max(1, bottom - top) // 2,
                "confidence": confidence,
                "text": joined,
            }

            scored.append((score, candidate))

        if not scored:
            return None

        scored.sort(
            key=lambda item: (
                item[0],
                item[1]["confidence"],
            ),
            reverse=True,
        )

        best = scored[0][1]

        logger.info(
            "Vision selected generic target '%s' from OCR text '%s' "
            "at screen coordinates (%s, %s).",
            target,
            best["text"],
            best["center_x"],
            best["center_y"],
        )

        return {
            "x": best["center_x"],
            "y": best["center_y"],
            "width": best["width"],
            "height": best["height"],
            "source": "vision_ocr",
            "text": best["text"],
        }

    # =========================================================
    # HELPERS
    # =========================================================

    @staticmethod
    def _extract_application_hint(
        target: str,
    ) -> str | None:

        markers = (
            " search box",
            " search field",
            " search bar",
            " search input",
            " search textbox",
            " search text box",
        )

        for marker in markers:

            if marker in target:

                prefix = target.split(
                    marker,
                    1,
                )[0].strip()

                if prefix:
                    return prefix

        return None

    @staticmethod
    def _looks_like_search_target(
        target: str,
    ) -> bool:

        return any(
            keyword in target
            for keyword in {
                "search box",
                "search field",
                "search bar",
                "search input",
                "search textbox",
                "search text box",
            }
        )

    @staticmethod
    def _semantic_words(
        target: str,
    ) -> list[str]:

        stop_words = {
            "the",
            "a",
            "an",
            "this",
            "that",
            "inside",
            "on",
            "in",
            "at",
            "window",
            "application",
            "app",
            "box",
            "field",
            "bar",
            "input",
            "textbox",
            "text",
            "button",
            "area",
            "editing",
        }

        words = re.findall(
            r"[a-zA-Z0-9]+",
            target.lower(),
        )

        return [
            word
            for word in words
            if word not in stop_words
        ]

    @staticmethod
    def _normalize_text(
        value: str,
    ) -> str:

        return (
            re.sub(
                r"[^a-zA-Z0-9]+",
                "",
                value.lower(),
            )
        )

    @staticmethod
    def _parse_confidence(
        value: Any,
    ) -> float:

        try:
            return float(value)

        except Exception:
            return 0.0