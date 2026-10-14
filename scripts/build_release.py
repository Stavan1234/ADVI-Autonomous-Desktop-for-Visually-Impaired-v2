from __future__ import annotations

from pathlib import Path
import hashlib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    import re
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']\s*$', text, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not determine project version from pyproject.toml")
    return match.group(1)

VERSION = _project_version()
ARCHIVE_ROOT = f"ADVI_{VERSION}"
OUTPUT = ROOT.parent / f"ADVI_COMPLETE_PROJECT_{VERSION}.zip"

EXCLUDED_DIRS = {
    ".git", ".venv", ".pytest_cache", "__pycache__", "logs", "runtime",
    "build", "dist", "*.egg-info",
}
EXCLUDED_FILES = {".env", "mcp_servers.json"}
EXCLUDED_TOP_LEVEL = {OUTPUT.name}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
EXCLUDED_SECRETS_DIRS = {"credentials", "secrets"}


def excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    if any(
        part in EXCLUDED_DIRS
        or part in EXCLUDED_SECRETS_DIRS
        or part.endswith(".egg-info")
        for part in parts
    ):
        return True
    if path.name in EXCLUDED_FILES or path.name in EXCLUDED_TOP_LEVEL:
        return True
    if path.suffix in EXCLUDED_SUFFIXES:
        return True
    return False


def main() -> None:
    files = [p for p in ROOT.rglob("*") if p.is_file() and not excluded(p)]
    files.sort(key=lambda p: p.relative_to(ROOT).as_posix().casefold())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            archive_name = f"{ARCHIVE_ROOT}/{path.relative_to(ROOT).as_posix()}"
            zf.write(path, archive_name)
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    print(f"Created: {OUTPUT}")
    print(f"Files: {len(files)}")
    print(f"SHA256: {digest}")


if __name__ == "__main__":
    main()
