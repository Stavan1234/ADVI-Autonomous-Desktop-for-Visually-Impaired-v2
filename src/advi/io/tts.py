from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
import re
from typing import Any
import shutil
import subprocess
import threading
import time
import wave

logger = logging.getLogger(__name__)


def clean_for_speech(text: str) -> str:
    if not text:
        return ""

    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    cleaned = re.sub(r"[*#_`~]", "", cleaned)
    cleaned = re.sub(r"\.\.\.+|…", ", ", cleaned)
    # A backslash is read out as "backslash" (live log 2026-10-25); paths are
    # simplified before this in AdviResponse.for_speech, anything left is a pause.
    cleaned = cleaned.replace("\\", " ")

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


def split_sentences(text: str, min_chars: int = 40) -> list[str]:
    """Sentences, short ones joined to the next, so playback can start before all of it is made."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?;:])\s+", text) if p.strip()]
    chunks: list[str] = []
    for part in parts:
        if chunks and len(chunks[-1]) < min_chars:
            chunks[-1] = f"{chunks[-1]} {part}"
        else:
            chunks.append(part)
    return chunks


def read_wav(path: Path) -> tuple[Any, int] | None:
    import numpy as np

    try:
        with wave.open(str(path), "rb") as source:
            rate, width, channels = source.getframerate(), source.getsampwidth(), source.getnchannels()
            data = source.readframes(source.getnframes())
    except (OSError, wave.Error) as exc:
        logger.info("Could not read synthesized speech: %s", exc)
        return None
    if width != 2:
        return None
    samples = np.frombuffer(data, dtype="<i2").astype("float32") / 32768
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


class PiperTTS:
    """Speech output with bundled Piper first and a persistent Windows SAPI worker fallback.

    With a player attached (voice on), speech is synthesized in memory, sentence by
    sentence, and played through ADVI's own audio stream, so the echo canceller
    knows exactly what ADVI said (voice/audio_io.py). Without one, SAPI and
    Piper play directly, as before.
    """

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
        self._state_lock = threading.Lock()
        self._sapi_pending = 0
        self._playing = False
        self.player: Any = None
        self._play_lock = threading.Lock()
        self._player_speaking = 0
        self._cut_generation = 0
        self._pcm_results: dict[str, str] = {}
        self._pcm_ready = threading.Condition()
        self._pcm_counter = 0

    def attach_player(self, player: Any) -> None:
        """Play all speech through ``player`` (voice/audio_io.DuplexAudio) from now on."""
        self.player = player

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
$pcm = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
[Console]::WriteLine("__ADVI_READY__")
[Console]::Out.Flush()
try {
    while (($line = [Console]::In.ReadLine()) -ne $null) {
        if ($line -eq "__ADVI_EXIT__") { break }
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.StartsWith("P:")) {
            # Synthesize to 16 kHz PCM in memory; ADVI plays it itself.
            $parts = $line.Split(":", 3)
            $audio = ""
            try {
                $text = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($parts[2]))
                $stream = New-Object System.IO.MemoryStream
                $voice.SetOutputToAudioStream($stream, $pcm)
                $voice.Speak($text)
                $voice.SetOutputToDefaultAudioDevice()
                $audio = [Convert]::ToBase64String($stream.ToArray())
            } catch {
                $voice.SetOutputToDefaultAudioDevice()
            }
            [Console]::WriteLine("__ADVI_PCM__ " + $parts[1] + " " + $audio)
            [Console]::Out.Flush()
            continue
        }
        try {
            $bytes = [Convert]::FromBase64String($line)
            $text = [System.Text.Encoding]::UTF8.GetString($bytes)
            if (-not [string]::IsNullOrWhiteSpace($text)) { $voice.Speak($text) }
        } catch {
            # One malformed utterance must not kill the persistent worker.
        }
        [Console]::WriteLine("__ADVI_SPOKE__")
        [Console]::Out.Flush()
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
                # The worker reports READY once, then SPOKE after each utterance,
                # which is how ADVI knows when it has stopped talking.
                for line in stream:
                    marker = line.strip()
                    if marker.startswith("__ADVI_PCM__"):
                        _, request_id, *payload = marker.split(" ", 2)
                        with self._pcm_ready:
                            self._pcm_results[request_id] = payload[0] if payload else ""
                            self._pcm_ready.notify_all()
                    elif marker == "__ADVI_READY__":
                        self._sapi_ready.set()
                    elif marker == "__ADVI_SPOKE__":
                        with self._state_lock:
                            self._sapi_pending = max(0, self._sapi_pending - 1)
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
                with self._state_lock:
                    self._sapi_pending += 1
                process.stdin.write(payload + "\n")
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError) as exc:
                with self._state_lock:
                    self._sapi_pending = 0
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

    def _sapi_pcm(self, text: str) -> tuple[Any, int] | None:
        """Synthesize one sentence with SAPI into 16 kHz samples (not played)."""
        import numpy as np

        with self._sapi_lock:
            process = self._ensure_sapi_process(wait_ready=True)
            if process is None or process.stdin is None or not self._sapi_ready.is_set():
                return None
            self._pcm_counter += 1
            request_id = str(self._pcm_counter)
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            try:
                process.stdin.write(f"P:{request_id}:{payload}\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                logger.info("Windows SAPI worker stopped: %s", exc)
                self._sapi_process = None
                return None
        deadline = time.monotonic() + 20.0
        with self._pcm_ready:
            while request_id not in self._pcm_results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._pcm_ready.wait(remaining)
            encoded = self._pcm_results.pop(request_id)
        if not encoded:
            return None
        return np.frombuffer(base64.b64decode(encoded), dtype="<i2").astype("float32") / 32768, 16_000

    def _synthesize_samples(self, text: str) -> tuple[Any, int] | None:
        if self.available() and self.synthesize(text):
            audio = read_wav(self.output_wav)
            if audio is not None:
                return audio
        return self._sapi_pcm(text)

    def _speak_through_player(self, cleaned: str) -> bool:
        """Make each sentence while the previous one plays; stop() cuts in between sentences too."""
        with self._state_lock:
            self._player_speaking += 1
            generation = self._cut_generation
        spoke = False
        try:
            with self._play_lock:
                current = None
                for sentence in split_sentences(cleaned):
                    audio = self._synthesize_samples(sentence)
                    if current is not None:
                        current.wait()
                    if generation != self._cut_generation:
                        return spoke
                    if audio is None:
                        continue
                    current = self.player.play(audio[0], audio[1], kind="speech")
                    spoke = True
                if current is not None:
                    current.wait()
            return spoke
        finally:
            with self._state_lock:
                self._player_speaking -= 1

    def speak(self, text: str) -> bool:
        """Use Piper when healthy, otherwise queue text into one persistent SAPI worker."""
        cleaned = clean_for_speech(text)
        if not cleaned:
            return False
        if self.player is not None:
            return self._speak_through_player(cleaned)
        if self.available():
            if self.synthesize(cleaned):
                self._playing = True
                try:
                    if play_wav(self.output_wav):
                        return True
                finally:
                    self._playing = False
        return self._speak_windows_sapi(cleaned)

    def pause(self) -> bool:
        """Hold speech where it is; False when this voice can't pause (no player)."""
        if self.player is None:
            return False
        self.player.pause("speech")
        return True

    def resume(self) -> None:
        if self.player is not None:
            self.player.resume("speech")

    def is_speaking(self) -> bool:
        with self._state_lock:
            if self._playing or self._sapi_pending > 0 or self._player_speaking > 0:
                return True
        return bool(self.player is not None and self.player.playing("speech"))

    def stop(self) -> None:
        """Cut speech off now (the user pressed the voice hotkey: barge-in)."""
        if self.player is not None:
            with self._state_lock:
                self._cut_generation += 1
            self.player.stop("speech")
            return
        if self._playing:
            try:
                import winsound

                winsound.PlaySound(None, winsound.SND_PURGE)
            except (ImportError, RuntimeError, OSError):
                pass
        with self._sapi_lock:
            process = self._sapi_process
            with self._state_lock:
                busy = self._sapi_pending > 0
                self._sapi_pending = 0
            if process is None or not busy:
                return
            # SAPI's Speak blocks its worker, so stopping means ending the worker;
            # a fresh one starts in the background for the next sentence.
            self._sapi_process = None
            self._sapi_ready.clear()
            try:
                process.terminate()
            except OSError:
                pass
        threading.Thread(target=self._restart_sapi, name="advi-sapi-restart", daemon=True).start()

    def _restart_sapi(self) -> None:
        with self._sapi_lock:
            self._ensure_sapi_process(wait_ready=True)
