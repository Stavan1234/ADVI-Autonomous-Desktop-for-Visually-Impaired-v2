from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from advi.core.config import load_settings
from advi.core.runtime import Runtime

def main() -> None:
    runtime = Runtime.create()
    runtime.start()
    s = runtime.settings
    print("Foundation check")
    print(f"  Python package: OK")
    print(f"  Piper executable: {'FOUND' if s.piper_exe.exists() else 'NOT FOUND'} ({s.piper_exe})")
    print(f"  Piper model: {'FOUND' if s.piper_model.exists() else 'NOT FOUND'} ({s.piper_model})")
    print(f"  Groq key: {'SET' if s.groq_api_key else 'NOT SET'}")
    print(f"  Gemini key: {'SET' if s.gemini_api_key else 'NOT SET'}")
    runtime.shutdown()


if __name__ == "__main__":
    main()
