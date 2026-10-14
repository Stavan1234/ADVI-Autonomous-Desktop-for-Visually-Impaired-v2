from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class UntrustedData:
    source: str
    content: Any

    def as_text(self) -> str:
        return f"[{self.source} DATA — UNTRUSTED]\n{self.content}"


class SecurityBoundary:
    """Lightweight boundary for untrusted model-visible data.

    Security is deliberately advisory for ordinary output: data is preserved and
    only compact flags are attached. Hard limits apply only to pathological size
    or depth, preventing a malicious tool result from consuming unbounded CPU or
    prompt space.
    """

    _injection_patterns = (
        re.compile(r"ignore (?:all|any|the) previous instructions", re.I),
        re.compile(r"system message", re.I),
        re.compile(r"developer message", re.I),
        re.compile(r"reveal (?:your|the) (?:system|developer) prompt", re.I),
        re.compile(r"follow these instructions", re.I),
        re.compile(r"you are now", re.I),
    )
    _directional_ranges = (("\u202a", "\u202e"), ("\u2066", "\u2069"))

    def wrap(self, source: str, content: Any, *, max_chars: int = 8000) -> UntrustedData:
        text = str(content)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return UntrustedData(source=source, content=text)

    def injection_flags(self, content: str) -> tuple[str, ...]:
        flags: list[str] = []
        if any(p.search(content) for p in self._injection_patterns):
            flags.append("prompt_injection_pattern")
        if any(
            start <= ch <= end
            for start, end in self._directional_ranges
            for ch in content
        ):
            flags.append("directional_unicode")
        return tuple(flags)

    def scan(self, value: Any, *, max_chars: int = 32768, max_strings: int = 256, max_depth: int = 8) -> tuple[str, ...]:
        """Scan bounded structured data without rewriting the underlying value."""
        flags: list[str] = []
        seen_strings = 0
        seen_chars = 0
        depth_limited = False

        def add(flag_values: tuple[str, ...]) -> None:
            for flag in flag_values:
                if flag not in flags:
                    flags.append(flag)

        def visit(item: Any, depth: int) -> None:
            nonlocal seen_strings, seen_chars, depth_limited
            if len(flags) >= 8 or seen_strings >= max_strings or seen_chars >= max_chars:
                return
            if depth > max_depth:
                depth_limited = True
                return
            if isinstance(item, str):
                remaining = max_chars - seen_chars
                if remaining <= 0:
                    return
                sample = item[:remaining]
                seen_chars += len(sample)
                seen_strings += 1
                add(self.injection_flags(sample))
                return
            if isinstance(item, dict):
                for key, value in item.items():
                    visit(key, depth + 1)
                    visit(value, depth + 1)
                    if len(flags) >= 8 or seen_strings >= max_strings or seen_chars >= max_chars:
                        break
            elif isinstance(item, (list, tuple, set, frozenset)):
                for value in item:
                    visit(value, depth + 1)
                    if len(flags) >= 8 or seen_strings >= max_strings or seen_chars >= max_chars:
                        break

        visit(value, 0)
        if depth_limited:
            flags.append("scan_depth_limited")
        if seen_strings >= max_strings or seen_chars >= max_chars:
            flags.append("scan_budget_reached")
        return tuple(flags)
