"""Keep an existing installation in step with the code, run by run_advi.bat before every start.

After `git pull`, new code can need new packages (0.4.0 added faster-whisper,
sounddevice and livekit). Without them ADVI would start with voice silently off.
So the launcher compares pyproject.toml with what was installed last time and,
only when it changed, installs the new components once:
- `pip install -e .[dev]` (pyproject.toml's dependencies);
- the offline speech and wake-word models (a failure is only a warning);
- the wake word at Windows sign-in, unless ADVI_WAKE=off.

Standard library only, and fast when nothing changed (one file hash).
SETUP_ADVI.bat records the state at the end (`--mark`), so a fresh install isn't
installed twice.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".venv" / "advi_install_state.json"
_OFF = {"0", "off", "false", "no"}


def fingerprint() -> str:
    return hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()


def recorded() -> str:
    try:
        return str(json.loads(STATE.read_text(encoding="utf-8")).get("pyproject_sha256", ""))
    except (OSError, ValueError):
        return ""


def mark() -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"pyproject_sha256": fingerprint()}), encoding="utf-8")


def setting(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is not None:
        return value.strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip() == name:
                return val.strip()
    return default


def say(text: str) -> None:
    """Print, and say it with Windows' voice (best effort): the user may not see the console."""
    print(text, flush=True)
    if os.name != "nt" or setting("ADVI_VOICE", "on").casefold() in _OFF:
        return
    script = ("Add-Type -AssemblyName System.Speech; "
              f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text.replace(chr(39), '')}')")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", script],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def run(args: list[str], what: str, *, required: bool) -> bool:
    print(f"  - {what}...", flush=True)
    code = subprocess.call(args, cwd=str(ROOT))
    if code != 0:
        print(f"    {what} {'FAILED' if required else 'did not finish (not required)'} (exit code {code}).", flush=True)
    return code == 0


def update() -> int:
    say("ADVI was updated. Installing the new parts once; this takes a few minutes.")
    python = sys.executable
    # A running wake listener keeps package DLLs open, and pip can't replace open files.
    subprocess.call([python, "-m", "advi.wake", "stop"], cwd=str(ROOT),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", ".[dev]"],
               "Installing packages", required=True):
        print("\nADVI could not install its updated packages. Check the internet connection and run SETUP_ADVI.bat.")
        return 1
    preload = ("from pathlib import Path; from advi.voice.transcriber import LocalTranscriber; import sys; "
               "sys.exit(0 if LocalTranscriber(sys.argv[1], download_root=Path('assets/whisper')).preload() else 1)")
    from_env = setting("ADVI_STT_MODEL", "") or "distil-small.en"
    run([python, "-c", preload, from_env], "Downloading the offline speech model", required=False)
    if setting("ADVI_WAKE", "on").casefold() not in _OFF:
        run([python, "-c", preload, setting("ADVI_WAKE_MODEL", "") or "tiny.en"], "Downloading the wake-word model",
            required=False)
        run([python, "-m", "advi.wake", "install"], "Turning on the wake word", required=False)
    mark()
    print("Update installed.\n", flush=True)
    return 0


def main(argv: list[str]) -> int:
    if "--mark" in argv:
        mark()
        return 0
    if recorded() == fingerprint():
        return 0
    return update()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
