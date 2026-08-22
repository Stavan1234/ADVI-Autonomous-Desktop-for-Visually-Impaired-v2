from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ASSET_ROOT = PROJECT_ROOT / "assets"
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
LOG_ROOT = PROJECT_ROOT / "logs"
MEMORY_ROOT = RUNTIME_ROOT / "memory"
MEMORY_DATABASE = MEMORY_ROOT / "advi_memory.db"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    groq_api_key: str | None
    gemini_api_key: str | None

    groq_model: str
    gemini_model: str

    piper_exe: Path
    piper_model: Path
    tts_output: Path


def load_settings() -> Settings:
    piper_exe = Path(
        os.getenv("ADVI_PIPER_EXE")
        or (ASSET_ROOT / "piper" / "piper.exe")
    )

    piper_model = Path(
        os.getenv("ADVI_PIPER_MODEL")
        or (ASSET_ROOT / "models" / "en_US-lessac-medium.onnx")
    )

    tts_output = Path(
        os.getenv("ADVI_TTS_OUTPUT")
        or (RUNTIME_ROOT / "audio" / "out.wav")
    )

    return Settings(
        groq_api_key=os.getenv("GROQ_API_KEY"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        groq_model=os.getenv(
            "ADVI_GROQ_MODEL",
            "openai/gpt-oss-120b",
        ),
        gemini_model=os.getenv(
            "ADVI_GEMINI_MODEL",
            "gemini-3.6-flash",
        ),
        piper_exe=piper_exe,
        piper_model=piper_model,
        tts_output=tts_output,
    )