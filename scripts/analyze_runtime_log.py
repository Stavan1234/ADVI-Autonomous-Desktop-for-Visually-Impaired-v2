from __future__ import annotations

"""Analyze an ADVI runtime log for the failure patterns the acceptance suite guards."""

import argparse
import json
import re
from pathlib import Path

PATTERNS = {
    "missing_tool_contract": re.compile(r"not in request\.tools", re.I),
    "provider_request_error": re.compile(r"provider_session\.(?:request_error|send_user\.error|send_tool_results\.error)", re.I),
    "provider_failover": re.compile(r"gateway\.provider_session\.failover", re.I),
    "unsafe_tool_limit": re.compile(r"safe tool-call limit", re.I),
    "unverified_typing": re.compile(r"text_effect_not_observed|Typed \d+ characters", re.I),
    "unknown_parameter": re.compile(r"Unknown parameter", re.I),
    "cross_domain_observation": re.compile(r"active_application.*Chrome.*target.*WhatsApp.*(?:YouTube Music|music\.youtube\.com)|target.*WhatsApp.*browser.*(?:YouTube Music|music\.youtube\.com)", re.I),
    "david_trace_error": re.compile(r"trace\(\) got multiple values for argument ['\"]stage['\"]", re.I),
    "wrong_artifact_reuse": re.compile(r"advi\.pdf.*reused its existing window", re.I),
    "uncertain_open_file": re.compile(r"name=['\"]open_file['\"].*verified=['\"]False['\"].*verification_status=['\"]uncertain", re.I),
}


def analyze(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    counts = {name: sum(1 for line in lines if pattern.search(line)) for name, pattern in PATTERNS.items()}
    turns = len(re.findall(r"User input received:", text))
    provider_attempts = len(re.findall(r"gateway\.provider_session\.(?:send_user\.request|send_tool_results\.request)", text))
    return {
        "log": str(path),
        "lines": len(lines),
        "user_turns": turns,
        "provider_requests": provider_attempts,
        "failure_pattern_counts": counts,
        "total_flagged_events": sum(counts.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = analyze(args.log)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"ADVI runtime log: {result['log']}")
    print(f"Lines: {result['lines']} | user turns: {result['user_turns']} | provider requests: {result['provider_requests']}")
    for name, count in result["failure_pattern_counts"].items():
        print(f"{name}: {count}")
    print(f"Total flagged events: {result['total_flagged_events']}")


if __name__ == "__main__":
    main()
