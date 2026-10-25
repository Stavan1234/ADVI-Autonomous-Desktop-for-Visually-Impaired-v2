"""The wake listener: a small background program that starts ADVI on "wake up ADVI".

It runs separately from ADVI (fourth live session, 2026-10-25: the wake word
must work when ADVI is closed, and after a restart, with nobody touching the PC):
- started at every Windows sign-in (autostart.py) and by ADVI itself, so it is
  already listening when "sleep" or "exit" closes ADVI;
- one copy at a time (a named mutex);
- a supervisor process restarts the listening worker if it ever crashes, with a
  growing pause between restarts; the worker ends when its supervisor is gone;
- the microphone is reopened when it stops delivering audio (a headset
  unplugged, the default device changed).

While ADVI is closed:
- the wake engine (engines.py) listens: by default Silero VAD finds short
  utterances (0.3-4 s of speech) and local Whisper tiny.en checks them on this
  PC (nothing goes online); optionally Porcupine;
- "wake up ADVI", "hey ADVI" or "ADVI" plays the wake sound and starts
  run_advi.bat in its own window;
- the ADVI hotkey (Control Alt Z) starts ADVI too.
While ADVI runs, the listener lets go of the microphone and the hotkey, and
checks every second whether ADVI has closed.

Heard text is not logged unless it was the wake phrase.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from advi.wake.phrase import is_wake_phrase

log = logging.getLogger(__name__)

WAKE_MODEL = "tiny.en"
STARTUP_GRACE_SECONDS = 120.0  # ADVI takes 10-60 s to start; don't start a second copy meanwhile


@dataclass(frozen=True)
class WakeRules:
    min_speech: float = 0.3
    max_speech: float = 4.0
    stalled_after: int = 3  # this many silent windows in a row with no audio at all: reopen the mic


def endpointing():
    from advi.voice.recorder import Endpointing

    # Short phrases: end quickly; long talk is cut at 6 s and never transcribed.
    return Endpointing(silence_seconds=0.6, long_silence_seconds=0.8, no_speech_seconds=10.0, max_seconds=6.0,
                       min_speech_seconds=0.25)


def project_root() -> Path:
    from advi.core.config import PROJECT_ROOT

    return PROJECT_ROOT


def launch_advi(root: Path | None = None) -> bool:
    """Start ADVI the way the user would: run_advi.bat in its own console window."""
    root = root or project_root()
    launcher = root / "run_advi.bat"
    if not launcher.exists():
        log.error("Cannot start ADVI: %s is missing.", launcher)
        return False
    try:
        subprocess.Popen(["cmd.exe", "/c", str(launcher)], cwd=str(root),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return True
    except OSError as exc:
        log.error("Cannot start ADVI: %s", exc)
        return False


class WakeListener:
    def __init__(
        self,
        *,
        transcriber: Any,
        open_recorder: Callable[[], Any],
        app_running: Callable[[], bool],
        launch: Callable[[], bool],
        detector: Any = None,
        earcons: Any = None,
        hotkey_factory: Callable[[str, Callable[[], None]], Any] | None = None,
        hotkeys: tuple[str, ...] = (),
        rules: WakeRules = WakeRules(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transcriber = transcriber
        self.open_recorder = open_recorder
        self.app_running = app_running
        self.launch = launch
        self.detector = detector
        self.earcons = earcons
        self.hotkey_factory = hotkey_factory
        self.hotkeys = hotkeys
        self.rules = rules
        self.clock = clock
        self.recorder: Any = None
        self.hotkey: Any = None
        self._capturing = False
        self._empty_windows = 0
        self._launched_at: float | None = None
        self._lock = threading.Lock()

    # --- resources -------------------------------------------------------------------

    def _release(self) -> None:
        if self._capturing and self.recorder is not None:
            self.recorder.source.end()
        self._capturing = False
        self._empty_windows = 0
        for name in ("recorder", "hotkey"):
            part = getattr(self, name)
            setattr(self, name, None)
            if part is not None:
                try:
                    part.close()
                except Exception:
                    log.debug("Closing %s failed.", name, exc_info=True)

    def _ensure_hotkey(self) -> None:
        if self.hotkey is not None or self.hotkey_factory is None:
            return
        for text in self.hotkeys:
            try:
                hotkey = self.hotkey_factory(text, self.wake)
                if hotkey.start():
                    self.hotkey = hotkey
                    return
            except Exception:
                log.debug("Wake hotkey %s not registered.", text, exc_info=True)

    def close(self) -> None:
        self._release()
        close = getattr(self.detector, "close", None)
        if callable(close):
            close()

    # --- one step of the loop ------------------------------------------------------------

    def _starting(self) -> bool:
        return self._launched_at is not None and self.clock() - self._launched_at < STARTUP_GRACE_SECONDS

    def _stalled(self, got_audio: bool) -> bool:
        self._empty_windows = 0 if got_audio else self._empty_windows + 1
        if self._empty_windows < self.rules.stalled_after:
            return False
        log.warning("Wake listener: the microphone stopped delivering audio; reopening it.")
        self._release()
        return True

    def step(self) -> str:
        """Listen for one utterance (or ~1 s of keyword frames); returns what happened."""
        if self.app_running():
            self._launched_at = None
            self._release()
            return "app_running"
        if self._starting():
            self._release()
            return "starting"
        if self.recorder is None:
            try:
                self.recorder = self.open_recorder()
            except Exception as exc:
                log.warning("Wake listener: no microphone (%s); trying again later.", exc)
                return "no_microphone"
        self._ensure_hotkey()
        if self.detector is not None:
            return self._step_keyword()
        return self._step_phrase()

    def _step_phrase(self) -> str:
        from advi.voice.recorder import FRAME

        recording = self.recorder.record(cancelled=self.app_running)
        if recording.reason == "cancelled":
            return "silence"
        # A dead stream returns no frames at all (only the short pre-roll).
        if self._stalled(recording.audio.size > FRAME * 12):
            return "stalled"
        if recording.reason != "done":
            return "silence"
        if not self.rules.min_speech <= recording.speech_seconds <= self.rules.max_speech:
            return "skipped"
        text = self.transcriber.transcribe(recording.audio)
        if not is_wake_phrase(text):
            return "not_wake"
        log.info("Wake phrase heard: %r", text)
        self.wake()
        return "woke"

    def _step_keyword(self) -> str:
        source = self.recorder.source
        if not self._capturing:
            source.begin()
            self._capturing = True
        got_audio = False
        for _ in range(32):  # about one second of 32 ms frames, then check on ADVI again
            frame = source.read(timeout=0.5)
            if frame is None:
                continue
            got_audio = True
            if self.detector.process(frame):
                log.info("Wake word detected by the keyword engine.")
                self.wake()
                return "woke"
            if self.app_running():
                break
        if self._stalled(got_audio):
            return "stalled"
        return "listening"

    def wake(self) -> None:
        """Start ADVI once (the wake phrase or the hotkey)."""
        with self._lock:
            if self.app_running() or self._starting():
                return
            self._launched_at = self.clock()
        if self.earcons is not None:
            try:
                self.earcons.play("wake")
            except Exception:
                log.debug("Wake sound failed.", exc_info=True)
        if not self.launch():
            self._launched_at = None

    def run_forever(self, stop: threading.Event | None = None, keep_going: Callable[[], bool] = lambda: True) -> None:
        stop = stop or threading.Event()
        pause = {"app_running": 1.0, "starting": 1.0, "no_microphone": 30.0, "stalled": 2.0, "error": 5.0}
        while not stop.is_set() and keep_going():
            try:
                outcome = self.step()
            except Exception:
                log.exception("Wake listener step failed; reopening the microphone.")
                self._release()
                outcome = "error"
            delay = pause.get(outcome, 0.0)
            if delay:
                stop.wait(delay)
        self.close()


# --- processes -------------------------------------------------------------------------

def _configure_logging(name: str) -> None:
    from logging.handlers import RotatingFileHandler

    from advi.core.config import LOG_ROOT

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_ROOT / "wake.log", maxBytes=2 * 1024 * 1024, backupCount=1,
                                  encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter(f"%(asctime)s [%(levelname)s] {name}: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # faster-whisper logs every utterance it checks; that would record when people talked.
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)


def _lower_priority() -> None:
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def _parent_alive(pid: int) -> Callable[[], bool]:
    def alive() -> bool:
        try:
            import psutil

            return psutil.pid_exists(pid)
        except Exception:
            return True

    return alive


def worker(supervisor_pid: int | None = None) -> int:
    """The listening process."""
    from advi.core.config import ASSET_ROOT
    from advi.voice.earcons import Earcons
    from advi.voice.hotkey import GlobalHotkey
    from advi.voice.listener import HOTKEY_CHOICES
    from advi.voice.recorder import open_microphone
    from advi.voice.transcriber import LocalTranscriber
    from advi.wake.engines import porcupine_from_env
    from advi.wake.presence import app_running

    _configure_logging("wake-worker")
    _lower_priority()
    keep_going = _parent_alive(supervisor_pid) if supervisor_pid else (lambda: True)
    detector = porcupine_from_env(ASSET_ROOT)
    transcriber = None
    if detector is None:
        transcriber = LocalTranscriber(os.getenv("ADVI_WAKE_MODEL", WAKE_MODEL).strip() or WAKE_MODEL,
                                       download_root=ASSET_ROOT / "whisper", language="en", hotwords="ADVI")
        while not transcriber.preload():
            log.warning("Wake listener: speech model %s not available yet; retrying in 60 s.", transcriber.model_name)
            if not keep_going():
                return 0
            time.sleep(60)
    configured = os.getenv("ADVI_VOICE_HOTKEY", "").strip()
    listener = WakeListener(
        transcriber=transcriber,
        open_recorder=lambda: open_microphone(endpointing()),
        app_running=app_running,
        launch=launch_advi,
        detector=detector,
        earcons=Earcons(),
        hotkey_factory=GlobalHotkey,
        hotkeys=(configured,) if configured else HOTKEY_CHOICES,
    )
    log.info("Wake listener started (%s).",
             "Porcupine" if detector is not None else f"Whisper {transcriber.model_name} phrase check")
    listener.run_forever(keep_going=keep_going)
    return 0


def supervise(spawn: Callable[[], Any] | None = None, *, max_restarts: int | None = None,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    """Hold the single-instance mutex and keep one worker alive, restarting it after a crash."""
    from advi.wake.presence import WAKE_MUTEX, hold

    _configure_logging("wake")
    handle, already = hold(WAKE_MUTEX)
    if already:
        log.info("Wake listener is already running.")
        return 0
    if spawn is None:
        def spawn():
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            return subprocess.Popen([sys.executable, "-m", "advi.wake", "worker", str(os.getpid())],
                                    creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
    backoff, restarts = 5.0, 0
    while max_restarts is None or restarts <= max_restarts:
        started = clock()
        code = spawn().wait()
        if clock() - started > 600:
            backoff = 5.0  # it ran fine for a while: this is a fresh problem
        log.warning("Wake worker ended (exit code %s); restarting in %.0f s.", code, backoff)
        sleep(backoff)
        backoff = min(backoff * 2, 300.0)
        restarts += 1
    return 0
