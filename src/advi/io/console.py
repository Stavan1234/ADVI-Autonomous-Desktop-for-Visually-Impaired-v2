from __future__ import annotations

from collections.abc import Callable
import threading


class StartupAnnouncer:
    """Keep the user informed while ADVI performs real startup initialization.

    The first message is emitted synchronously so the configured voice path is
    exercised before expensive initialization begins. Later messages are spaced
    out while startup continues. The announcer never talks to an LLM.
    """

    _MESSAGES = (
        "Just preparing everything, sir...",
        "Getting ADVI ready, sir...",
        "Preparing your workspace...",
        "Almost ready, sir...",
    )

    def __init__(
        self,
        *,
        interval_seconds: float = 7.0,
        speaker: Callable[[str], bool] | None = None,
    ) -> None:
        self.interval_seconds = max(2.0, float(interval_seconds))
        self.speaker = speaker
        self._stop = threading.Event()
        self._speech_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._index = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, message: str) -> None:
        print(f"\nADVI › {message}", flush=True)
        if self.speaker is None:
            return
        with self._speech_lock:
            try:
                self.speaker(message)
            except Exception:
                # Startup voice is best-effort; text progress must continue.
                pass

    def start(self) -> None:
        if self.running:
            return

        self._stop.clear()
        self._index = 1

        # This is deliberate: the first spoken progress message is emitted before
        # the heavy local preparation starts, proving that the voice path is alive.
        self._emit(self._MESSAGES[0])

        self._thread = threading.Thread(
            target=self._run,
            name="advi-startup-announcer",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, final_message: str | None = None) -> None:
        thread = self._thread
        self._stop.set()
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        if final_message:
            self._emit(final_message)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if self._index >= len(self._MESSAGES):
                continue
            self._emit(self._MESSAGES[self._index])
            self._index += 1


def print_banner() -> None:
    print("╭──────────────────────────────────────────────────────────────╮")
    print("│ ADVI — Autonomous Desktop for Visually Impaired              │")
    print("│ Runtime online                                                │")
    print("╰──────────────────────────────────────────────────────────────╯")


def read_line() -> str:
    return input("\nYou › ").strip()


def print_shutdown() -> None:
    print("\nADVI › Shutting down.")
