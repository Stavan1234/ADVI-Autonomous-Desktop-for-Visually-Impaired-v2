from __future__ import annotations

from collections.abc import Callable
import threading

# One lock for every console line. With voice, the answer, progress lines and the
# next "You (voice) ›" come from different threads; a long answer was cut in half
# by the next command in the live log (2026-10-24).
PRINT_LOCK = threading.RLock()


def print_line(text: str) -> None:
    with PRINT_LOCK:
        print(text, flush=True)


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
        print_line(f"\nADVI › {message}")
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
        self._index = 0

        # The first message is said right away, before the heavy preparation. It is
        # spoken on the announcer's thread: through ADVI's own audio stream speaking
        # takes as long as the sentence, and startup must not wait for that.
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
        self._emit(self._MESSAGES[0])
        self._index = 1
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


class InputHub:
    """One queue for typed lines and spoken commands, consumed by the main loop.

    Typing keeps working while voice is on: ``input()`` runs on its own thread,
    and voice puts its transcript in the same queue. Items are (source, text);
    source is "typed", "voice" or "eof".
    """

    EXIT_WORDS = {"exit", "quit", "sleep"}

    def __init__(self, reader: Callable[[], str] = read_line, on_exit: Callable[[], None] | None = None) -> None:
        import queue

        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._reader = reader
        self._thread: threading.Thread | None = None
        self.busy = False  # set by the main loop while ADVI works on a request
        self.on_exit = on_exit

    def start(self) -> None:
        self._thread = threading.Thread(target=self._read_typed, name="advi-typed-input", daemon=True)
        self._thread.start()

    def _read_typed(self) -> None:
        while True:
            try:
                line = self._reader()
            except (EOFError, KeyboardInterrupt, OSError):
                self._queue.put(("eof", ""))
                return
            if line.strip(" .!").casefold() in self.EXIT_WORDS:
                # Typed exit works even mid-task: the main loop may be busy for minutes
                # (live log 2026-10-24: "exit" and "sleep" were typed and nothing happened).
                self.request_exit()
                return
            self._queue.put(("typed", line))

    def request_exit(self) -> None:
        self._queue.put(("exit", ""))
        if self.on_exit is not None:
            self.on_exit()

    def put_voice(self, text: str) -> None:
        self.busy = True  # taken: a second spoken command now hears "still working"
        print_line(f"\nYou (voice) › {text}")
        self._queue.put(("voice", text))

    def get(self) -> tuple[str, str]:
        import queue

        # Short waits keep Ctrl+C working on the main thread (a bare get() blocks it on Windows).
        while True:
            try:
                return self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

    def close(self) -> None:
        self._queue.put(("eof", ""))


def print_shutdown() -> None:
    print("\nADVI › Shutting down.")
