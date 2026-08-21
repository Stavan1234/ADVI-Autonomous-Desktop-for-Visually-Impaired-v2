from __future__ import annotations

from dataclasses import dataclass
import re

from .tts import PiperTTS


@dataclass(frozen=True)
class AdviResponse:
    """A logical response with independent display and speech representations."""

    text: str

    def for_display(self) -> str:
        return self.text

    def for_speech(self) -> str:
        text = self.text.strip()

        if not text:
            return ""

        # Remove fenced code blocks entirely for speech.
        text = re.sub(r"```(?:\w+)?\s*\n.*?\n```", "", text, flags=re.DOTALL)

        # Markdown links -> visible label.
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

        lines = text.splitlines()
        spoken_parts: list[str] = []
        table_rows: list[list[str]] = []

        def flush_table() -> None:
            if not table_rows:
                return

            if len(table_rows) == 1:
                spoken_parts.append(
                    "Table: " + ", ".join(table_rows[0]) + "."
                )
            else:
                headers = table_rows[0]
                rows = table_rows[1:]

                for row in rows:
                    if len(row) == len(headers):
                        spoken_parts.append(
                            "; ".join(
                                f"{header}: {value}"
                                for header, value in zip(headers, row)
                            )
                            + "."
                        )
                    else:
                        spoken_parts.append(
                            ", ".join(row) + "."
                        )

            table_rows.clear()

        for line in lines:
            stripped = line.strip()

            if not stripped:
                continue

            # Markdown table row.
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [
                    cell.strip()
                    for cell in stripped.strip("|").split("|")
                ]

                # Ignore Markdown separator rows.
                if all(
                    re.fullmatch(r":?-+:?", cell or "")
                    for cell in cells
                ):
                    continue

                table_rows.append(cells)
                continue

            flush_table()

            # Heading.
            heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
            if heading:
                spoken_parts.append(heading.group(1).strip())
                continue

            # Bullet.
            bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
            if bullet:
                spoken_parts.append(bullet.group(1).strip())
                continue

            # Numbered item.
            numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
            if numbered:
                spoken_parts.append(numbered.group(1).strip())
                continue

            # Blockquote.
            stripped = re.sub(r"^>\s?", "", stripped)

            # Inline code.
            stripped = re.sub(r"`([^`]+)`", r"\1", stripped)

            # Basic emphasis.
            stripped = re.sub(r"[*_~]{1,3}", "", stripped)

            spoken_parts.append(stripped)

        flush_table()

        result = " ".join(spoken_parts)

        # Avoid speaking raw URLs.
        result = re.sub(
            r"https?://\S+",
            "",
            result,
        )

        result = re.sub(r"\s+", " ", result)
        result = re.sub(r"\s+([,.!?])", r"\1", result)

        return result.strip()


class OutputManager:
    """Delivers one logical response to available output channels."""

    def __init__(self, tts: PiperTTS | None = None) -> None:
        self.tts = tts

    def deliver(self, response: AdviResponse) -> bool:
        """Display the response and attempt voice output.

        Returns True when the response was successfully delivered
        to the available output channels.
        """
        display_text = response.for_display()

        if display_text:
            print(f"Advi: {display_text}")

        # Text output succeeded even when voice is unavailable.
        if self.tts is None:
            return bool(display_text)

        speech_text = response.for_speech()

        if not speech_text:
            return bool(display_text)

        speech_success = self.tts.speak(speech_text)

        if not speech_success:
            return False

        return bool(display_text)