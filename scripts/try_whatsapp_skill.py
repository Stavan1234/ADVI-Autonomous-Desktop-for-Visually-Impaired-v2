"""Try the WhatsApp skill on this machine without going through the LLM.

    .venv\\Scripts\\python.exe scripts\\try_whatsapp_skill.py --chat "New Chapter"
    .venv\\Scripts\\python.exe scripts\\try_whatsapp_skill.py --chat "New Chapter" --send "test from ADVI"

Without --send it only opens the chat (this marks its messages as read).
With --send it asks you to type YES before anything is typed into WhatsApp.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from advi.capabilities.apps.uia import WindowsUiaDriver  # noqa: E402
from advi.capabilities.apps.whatsapp import WhatsAppSkill  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chat", required=True, help="Chat or group name as it appears in WhatsApp")
    parser.add_argument("--send", help="Message text to send (asks for confirmation)")
    args = parser.parse_args()

    skill = WhatsAppSkill(WindowsUiaDriver())
    if args.send:
        answer = input(f"Send {args.send!r} to {args.chat!r} on WhatsApp? Type YES to continue: ")
        if answer.strip() != "YES":
            print("Cancelled.")
            return 1
        outcome = skill.send_message(args.chat, args.send)
    else:
        outcome = skill.open_chat(args.chat)

    print(f"{'OK' if outcome.ok else 'FAILED'} at stage '{outcome.stage}': {outcome.message}")
    print(json.dumps(outcome.evidence, indent=2, default=str))
    return 0 if outcome.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
