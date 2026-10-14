from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import re
import threading

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
        self._speech_lock = threading.Lock()
        self._speech_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="advi-speech")
        self._progress_thread: threading.Thread | None = None
        self._progress_stop: threading.Event | None = None
        self._progress_generation = 0
        self._state_lock = threading.RLock()
        self._closed = False
        # Progress narration is "latest only": a phrase not yet spoken when the
        # next phrase or the final answer arrives is dropped, so speech never
        # trails behind what ADVI is actually doing.
        self._narration_generation = 0
        self._last_narration: str | None = None

    def close(self) -> None:
        """Release output-channel resources without racing background feedback."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self.stop_progress()
        # Every actual TTS invocation goes through this one executor. Waiting here
        # therefore guarantees no speech call is still using the TTS backend when
        # it is closed below.
        self._speech_executor.shutdown(wait=True, cancel_futures=True)
        if self.tts is not None:
            self.tts.close()

    def start_progress(self, message: str = "Working on that.", *, delay: float = 1.5) -> None:
        """Queue delayed progress speech without creating a second TTS channel."""
        self.stop_progress()
        with self._state_lock:
            if self._closed or self.tts is None:
                return
            self._progress_generation += 1
            generation = self._progress_generation
            stop = threading.Event()
            self._progress_stop = stop

        def speak_worker() -> None:
            with self._speech_lock:
                with self._state_lock:
                    if self._closed or stop.is_set() or generation != self._progress_generation or self.tts is None:
                        return
                try:
                    self.tts.speak(message)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).warning("Speech progress output failed: %s", exc)

        def scheduler() -> None:
            if stop.wait(max(0.0, float(delay))):
                return
            with self._state_lock:
                if self._closed or stop.is_set() or generation != self._progress_generation or self.tts is None:
                    return
            try:
                self._speech_executor.submit(speak_worker)
            except RuntimeError:
                return

        thread = threading.Thread(target=scheduler, name="advi-progress-feedback", daemon=True)
        with self._state_lock:
            self._progress_thread = thread
        thread.start()

    def narrate(self, message: str) -> None:
        """Announce a step as it starts (display + non-blocking speech)."""
        message = str(message or "").strip()
        with self._state_lock:
            if self._closed or not message or message == self._last_narration:
                return
            self._last_narration = message
            self._narration_generation += 1
            generation = self._narration_generation
        # A concrete step replaces the generic "Working on that."
        self.stop_progress()
        print(f"  ... {message}")
        if self.tts is None:
            return

        def speak_worker() -> None:
            with self._speech_lock:
                with self._state_lock:
                    if self._closed or generation != self._narration_generation or self.tts is None:
                        return
                try:
                    self.tts.speak(message)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).warning("Progress narration failed: %s", exc)

        try:
            self._speech_executor.submit(speak_worker)
        except RuntimeError:
            pass

    def stop_progress(self) -> None:
        """Invalidate delayed progress speech and join only the lightweight scheduler."""
        with self._state_lock:
            self._progress_generation += 1
            stop = self._progress_stop
            thread = self._progress_thread
            self._progress_stop = None
            self._progress_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def deliver(self, response: AdviResponse, *, display: bool = True) -> bool:
        """Display the response and attempt voice output.

        Returns True when the response was successfully delivered
        to the available output channels.
        """
        with self._state_lock:
            if self._closed:
                return False
        display_text = response.for_display()

        if display and display_text:
            print(f"Advi: {display_text}")

        # Text output succeeded even when voice is unavailable.
        if self.tts is None:
            return bool(display_text)

        speech_text = response.for_speech()

        # The answer supersedes any progress phrase still waiting to be spoken.
        with self._state_lock:
            self._narration_generation += 1
            self._last_narration = None

        if not speech_text:
            return bool(display_text)

        self.stop_progress()

        # Speech is a side channel. Do not let a stalled Windows audio service
        # block the agent's response or convert a successful task into a failed one.
        def speak_worker() -> None:
            with self._speech_lock:
                try:
                    self.tts.speak(speech_text)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).warning("Speech output failed: %s", exc)

        try:
            self._speech_executor.submit(speak_worker)
        except RuntimeError:
            pass
        return bool(display_text)