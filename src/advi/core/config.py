from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


def _find_project_root() -> Path:
    """Find the source project root without assuming a fixed package depth."""
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    # For an installed package, assets/.env live with the launched project when
    # ADVI is started from its project directory (the supported Windows layout).
    return Path.cwd()


PROJECT_ROOT = _find_project_root()
ASSET_ROOT = PROJECT_ROOT / "assets"
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
LOG_ROOT = PROJECT_ROOT / "logs"
MEMORY_ROOT = RUNTIME_ROOT / "memory"
MEMORY_DATABASE = MEMORY_ROOT / "advi_memory.db"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    groq_api_key: str | None
    groq_api_keys: tuple[str, ...]
    groq_key_groups: tuple[str, ...]
    gemini_api_key: str | None
    gemini_api_keys: tuple[str, ...]
    gemini_key_groups: tuple[str, ...]

    groq_model: str
    gemini_model: str

    piper_exe: Path
    piper_model: Path
    tts_output: Path
    environment_full_inventory: bool
    environment_process_poll_seconds: float
    mcp_config: Path




def _load_groq_api_keys() -> tuple[str, ...]:
    """Load up to three Groq keys while preserving the legacy primary variable."""
    candidates: list[str] = []
    for name in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3"):
        value = os.getenv(name)
        if value:
            candidates.append(value.strip())
    extra = os.getenv("GROQ_API_KEYS", "")
    if extra:
        candidates.extend(part.strip() for part in extra.split(","))
    result: list[str] = []
    for key in candidates:
        if key and key not in result:
            result.append(key)
    return tuple(result[:3])


def _load_groq_key_groups(key_count: int) -> tuple[str, ...]:
    """Load optional Groq quota groups used by the provider circuit breaker.

    Leave unset to route each configured key independently. Set groups when keys
    belong to the same Groq project/quota scope so one exhausted scope blocks its
    sibling keys together instead of wasting retries.
    """
    if key_count <= 0:
        return ()
    raw = os.getenv("GROQ_KEY_GROUPS", "").strip()
    if raw:
        groups = tuple(part.strip() or f"groq-{i + 1}" for i, part in enumerate(raw.split(",")))
    else:
        groups = tuple(f"groq-{i + 1}" for i in range(key_count))
    if len(groups) < key_count:
        groups = groups + tuple(groups[-1] for _ in range(key_count - len(groups)))
    return groups[:key_count]


def _load_gemini_api_keys() -> tuple[str, ...]:
    """Load up to three Gemini keys without logging or exposing their values.

    GEMINI_API_KEY remains the primary/backwards-compatible variable.
    Additional keys may be supplied as GEMINI_API_KEY_2 and _3, or via
    GEMINI_API_KEYS as a comma-separated list. Empty/duplicate values are removed.
    """
    candidates: list[str] = []
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        value = os.getenv(name)
        if value:
            candidates.append(value.strip())
    extra = os.getenv("GEMINI_API_KEYS", "")
    if extra:
        candidates.extend(part.strip() for part in extra.split(","))
    result: list[str] = []
    for key in candidates:
        if key and key not in result:
            result.append(key)
    return tuple(result[:3])


def _load_gemini_key_groups(key_count: int) -> tuple[str, ...]:
    raw = os.getenv("GEMINI_KEY_GROUPS", "").strip()
    if raw:
        groups = tuple(part.strip() or f"gemini-{i + 1}" for i, part in enumerate(raw.split(",")))
    else:
        # Conservative default: all keys are considered one quota scope until the
        # user explicitly proves they belong to independent projects. This avoids
        # wasting calls by rotating three keys that share the same project quota.
        groups = ("gemini-default",)
    if len(groups) < key_count:
        groups = groups + tuple(groups[-1] for _ in range(key_count - len(groups)))
    return groups[:key_count]


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
    mcp_config = Path(os.getenv("ADVI_MCP_CONFIG") or (PROJECT_ROOT / "mcp_servers.json"))

    groq_api_keys = _load_groq_api_keys()
    gemini_api_keys = _load_gemini_api_keys()

    return Settings(
        groq_api_key=groq_api_keys[0] if groq_api_keys else None,
        groq_api_keys=groq_api_keys,
        groq_key_groups=_load_groq_key_groups(len(groq_api_keys)),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_api_keys=gemini_api_keys,
        gemini_key_groups=_load_gemini_key_groups(len(gemini_api_keys)),
        groq_model=os.getenv(
            "ADVI_GROQ_MODEL",
            "openai/gpt-oss-120b",
        ),
        gemini_model=os.getenv(
            "ADVI_GEMINI_MODEL",
            "gemini-3.8-flash",
        ),
        piper_exe=piper_exe,
        piper_model=piper_model,
        tts_output=tts_output,
        environment_full_inventory=os.getenv("ADVI_ENV_FULL_INVENTORY", "0").strip().lower() in {"1", "true", "yes", "on"},
        environment_process_poll_seconds=float(os.getenv("ADVI_ENV_PROCESS_POLL_SECONDS", "10")),
        mcp_config=mcp_config,
    )