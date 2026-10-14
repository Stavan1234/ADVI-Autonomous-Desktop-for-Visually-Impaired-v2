from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


def clean_for_speech(text: str) -> str:
    if not text:
        return ""

    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    cleaned = re.sub(r"[*#_`~]", "", cleaned)
    cleaned = re.sub(r"\.\.\.+|…", ", ", cleaned)

    return re.sub(r"\s+", " ", cleaned).strip()


def play_wav(wav_path: Path) -> bool:
    """Play a WAV file using Windows' native audio playback."""
    if not wav_path.exists():
        logger.warning("Audio file does not exist: %s", wav_path)
        return False

    try:
        import winsound

        winsound.PlaySound(
            str(wav_path),
            winsound.SND_FILENAME,
        )
        return True

    except (OSError, RuntimeError) as exc:
        logger.info("Audio playback failed: %s", exc)
        return False


class PiperTTS:
    """Speech output with bundled Piper first and a persistent Windows SAPI worker fallback."""

    _SAPI_SENTINEL = "__ADVI_EXIT__"

    def __init__(
        self,
        executable: Path,
        model: Path,
        output_wav: Path,
    ) -> None:
        self.executable = executable
        self.model = model
        self.output_wav = output_wav
        self._piper_disabled = False
        self._sapi_process: subprocess.Popen[str] | None = None
        self._sapi_reader: threading.Thread | None = None
        self._sapi_ready = threading.Event()
        self._sapi_lock = threading.RLock()
        self._sapi_retry_after = 0.0
        self._sapi_failure_logged = False

    def available(self) -> bool:
        return (
            not self._piper_disabled
            and self.executable.exists()
            and self.model.exists()
            and self.model.with_suffix(".onnx.json").exists()
        )

    def _sapi_command(self) -> str | None:
        if os.name != "nt":
            return None
        return shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")

    def sapi_available(self) -> bool:
        """Return whether Windows built-in System.Speech is callable."""
        return self._sapi_command() is not None

    def preflight(self) -> bool:
        """Validate and warm the speech path before interactive mode starts."""
        self.output_wav.parent.mkdir(parents=True, exist_ok=True)
        if self.available():
            # Warm the actual Piper model once. This makes the first startup
            # announcement a real voice test instead of discovering a broken
            # model only after interactive mode begins.
            warmed = self.synthesize("ADVI voice ready.")
            try:
                if self.output_wav.exists():
                    self.output_wav.unlink()
            except OSError:
                pass
            if warmed:
                return True
        if not self.sapi_available():
            return False
        with self._sapi_lock:
            process = self._ensure_sapi_process(wait_ready=True)
            return bool(process is not None and process.poll() is None and self._sapi_ready.is_set())

    def synthesize(self, text: str) -> bool:
        """Convert text into a WAV file without playing it."""
        cleaned = clean_for_speech(text)

        if not cleaned:
            logger.debug("No speech generated because text is empty.")
            return False

        if not self.available():
            logger.debug("Piper assets are unavailable or Piper is disabled for this process.")
            return False

        self.output_wav.parent.mkdir(parents=True, exist_ok=True)

        try:
            process = subprocess.run(
                [
                    str(self.executable),
                    "--model",
                    str(self.model),
                    "--output_file",
                    str(self.output_wav),
                ],
                input=cleaned.encode("utf-8"),
                capture_output=True,
                timeout=8,
                check=False,
            )

            if process.returncode != 0:
                stderr = process.stderr.decode("utf-8", errors="replace")
                logger.warning("Piper synthesis failed with exit code %s: %s", process.returncode, stderr.strip())
                self._piper_disabled = True
                return False

            return self.output_wav.exists()

        except (OSError, subprocess.SubprocessError) as exc:
            logger.info("Piper execution failed; switching voice fallback: %s", exc)
            self._piper_disabled = True
            return False

    def _sapi_worker_script(self) -> str:
        return r'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice.Rate = 0
$voice.Volume = 100
[Console]::WriteLine("__ADVI_READY__")
[Console]::Out.Flush()
try {
    while (($line = [Console]::In.ReadLine()) -ne $null) {
        if ($line -eq "__ADVI_EXIT__") { break }
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try {
            $bytes = [Convert]::FromBase64String($line)
            $text = [System.Text.Encoding]::UTF8.GetString($bytes)
            if (-not [string]::IsNullOrWhiteSpace($text)) { $voice.Speak($text) }
        } catch {
            # One malformed utterance must not kill the persistent worker.
        }
    }
} finally {
    $voice.Dispose()
}
'''

    def _ensure_sapi_process(self, *, wait_ready: bool = False) -> subprocess.Popen[str] | None:
        powershell = self._sapi_command()
        if not powershell or os.name != "nt":
            return None

        now = time.monotonic()
        if self._sapi_process is not None and self._sapi_process.poll() is None:
            if wait_ready and not self._sapi_ready.wait(timeout=4.0):
                return None
            return self._sapi_process
        if now < self._sapi_retry_after:
            return None

        self._sapi_ready.clear()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", self._sapi_worker_script()],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._sapi_retry_after = now + 30.0
            if not self._sapi_failure_logged:
                logger.info("Windows SAPI worker could not start: %s", exc)
                self._sapi_failure_logged = True
            return None

        self._sapi_process = process
        self._sapi_failure_logged = False

        def read_ready() -> None:
            stream = process.stdout
            if stream is None:
                return
            try:
                for line in stream:
                    if line.strip() == "__ADVI_READY__":
                        self._sapi_ready.set()
                        break
            except (OSError, ValueError):
                pass
            finally:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        self._sapi_reader = threading.Thread(
            target=read_ready,
            name="advi-sapi-ready-reader",
            daemon=True,
        )
        self._sapi_reader.start()
        if wait_ready and not self._sapi_ready.wait(timeout=4.0):
            try:
                process.terminate()
            except OSError:
                pass
            self._sapi_process = None
            self._sapi_retry_after = time.monotonic() + 30.0
            return None
        return process

    def close(self) -> None:
        """Release the speech worker and generated audio state."""
        with self._sapi_lock:
            process = self._sapi_process
            self._sapi_process = None
            self._sapi_ready.clear()
            if process is not None:
                try:
                    if process.poll() is None and process.stdin is not None:
                        process.stdin.write(self._SAPI_SENTINEL + "\n")
                        process.stdin.flush()
                        process.stdin.close()
                    process.wait(timeout=2.0)
                except (OSError, subprocess.SubprocessError):
                    try:
                        process.terminate()
                        process.wait(timeout=1.0)
                    except (OSError, subprocess.SubprocessError):
                        try:
                            process.kill()
                        except OSError:
                            pass
        try:
            if self.output_wav.exists():
                self.output_wav.unlink()
        except OSError as exc:
            logger.warning("Could not remove generated TTS audio: %s", exc)

    def _speak_windows_sapi(self, cleaned: str) -> bool:
        with self._sapi_lock:
            process = self._ensure_sapi_process(wait_ready=True)
            if process is None or process.stdin is None or not self._sapi_ready.is_set():
                return False
            payload = base64.b64encode(cleaned.encode("utf-8")).decode("ascii")
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError) as exc:
                try:
                    process.terminate()
                except OSError:
                    pass
                self._sapi_process = None
                self._sapi_retry_after = time.monotonic() + 15.0
                if not self._sapi_failure_logged:
                    logger.info("Windows SAPI worker stopped: %s", exc)
                    self._sapi_failure_logged = True
                return False

    def speak(self, text: str) -> bool:
        """Use Piper when healthy, otherwise queue text into one persistent SAPI worker."""
        cleaned = clean_for_speech(text)
        if not cleaned:
            return False
        if self.available():
            if self.synthesize(cleaned) and play_wav(self.output_wav):
                return True
        return self._speak_windows_sapi(cleaned)
