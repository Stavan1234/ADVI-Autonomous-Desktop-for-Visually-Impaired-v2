"""Is this short transcript someone calling ADVI ("wake up ADVI", "hey ADVI", "ADVI")?

Local Whisper spells the made-up name many ways. Measured on 2026-10-25 with
tiny.en, base.en and distil-small.en on Windows' two voices, plus the user's
own sessions: "ADVI", "Advi", "Adhvi", "Atwi", "adv", "ad-v", "Add V", "at V",
"Edv". It also writes "advice", which must not count.

To keep TV and conversation from starting ADVI, the name only counts in a short
utterance: on its own or with a greeting (at most 3 words), or together with
"wake" (at most 7 words). A name Whisper split in two ("at V") also needs
"wake" or a greeting, since "look at V" is ordinary English.
"""

from __future__ import annotations

import re

# a/e + d/t sound (+h) + v/w + optional ee sound: advi, adhvi, atwi, adv, edv, addv, advee.
_NAME = re.compile(r"^(?:a|aa|e)(?:d|dd|t|tt)h?(?:v|w)(?:i|ii|ee|ie|y|e|ey)?$")
# Whisper sometimes splits the name: "Add V", "at V", "ad vee".
_SECOND_HALF = {"v", "vi", "vee", "ve", "vy", "we", "wee", "wi"}
_FIRST_HALF = {"ad", "add", "at", "ed", "adh", "aad"}
_SHORT = 3
_WITH_WAKE = 7


def _words(text: str) -> list[str]:
    plain = str(text or "").casefold().replace("-", "")
    return re.findall(r"[a-z]+", plain)


_GREETINGS = {"wake", "hey", "hi", "hello", "ok", "okay"}


def _name_match(words: list[str]) -> str:
    """'whole' (advi, adhvi, atwi...), 'split' ("add V", "at V") or ''."""
    for index, word in enumerate(words):
        if _NAME.fullmatch(word):
            return "whole"
        nxt = words[index + 1] if index + 1 < len(words) else ""
        if word in _FIRST_HALF and nxt in _SECOND_HALF:
            return "split"
    return ""


def name_in(words: list[str]) -> bool:
    return bool(_name_match(words))


def is_wake_phrase(text: str) -> bool:
    words = _words(text)
    match = _name_match(words)
    if not match:
        return False
    # "at V" is also ordinary English ("Look at V" woke it in testing): a split name
    # counts only on its own or with "wake" or a greeting.
    if match == "split" and len(words) > 2 and not _GREETINGS & set(words):
        return False
    return len(words) <= _SHORT or ("wake" in words and len(words) <= _WITH_WAKE)


def without_name(text: str) -> str:
    """The words around the name: 'Wake up, ADVI.' -> 'wake up'."""
    words = _words(text)
    kept: list[str] = []
    skip = False
    for index, word in enumerate(words):
        if skip:
            skip = False
            continue
        nxt = words[index + 1] if index + 1 < len(words) else ""
        if _NAME.fullmatch(word):
            continue
        if word in _FIRST_HALF and nxt in _SECOND_HALF:
            skip = True
            continue
        kept.append(word)
    return " ".join(kept)
