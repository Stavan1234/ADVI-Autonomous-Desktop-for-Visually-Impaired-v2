from __future__ import annotations

import logging
from pathlib import Path
import re
import subprocess

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
        logger.warning("Audio playback failed: %s", exc)
        return False


class PiperTTS:
    def __init__(
        self,
        executable: Path,
        model: Path,
        output_wav: Path,
    ) -> None:
        self.executable = executable
        self.model = model
        self.output_wav = output_wav

    def available(self) -> bool:
        return (
            self.executable.exists()
            and self.model.exists()
            and self.model.with_suffix(".onnx.json").exists()
        )

    def synthesize(self, text: str) -> bool:
        """Convert text into a WAV file without playing it."""
        cleaned = clean_for_speech(text)

        if not cleaned:
            logger.debug("No speech generated because text is empty.")
            return False

        if not self.available():
            logger.warning("Piper assets are not available.")
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
                timeout=120,
                check=False,
            )

            if process.returncode != 0:
                stderr = process.stderr.decode(
                    "utf-8",
                    errors="replace",
                )
                logger.warning(
                    "Piper synthesis failed with exit code %s: %s",
                    process.returncode,
                    stderr.strip(),
                )
                return False

            return self.output_wav.exists()

        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Piper execution failed: %s", exc)
            return False

    def speak(self, text: str) -> bool:
        """Synthesize speech and play it immediately."""
        if not self.synthesize(text):
            return False

        return play_wav(self.output_wav)