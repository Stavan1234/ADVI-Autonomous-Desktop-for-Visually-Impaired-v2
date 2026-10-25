"""Names heard by voice: spelled letters, sound-alike matching, corrections.

Whisper writes some names as letters: "Siyu" came out as "CU" (2026-10-24).
Two simple remedies, both local:
- compare what was heard with names ADVI already knows, by how they sound;
- let the user spell just the name ("S I Y U") and join the letters.
"""

from __future__ import annotations

import difflib
import re
from typing import Iterable

# How letter names sound, so "CU" (see-you) can match "Siyu".
_LETTER_SOUNDS = {
    "a": "ay", "b": "bee", "c": "see", "d": "dee", "e": "ee", "f": "ef", "g": "jee", "h": "aych",
    "i": "eye", "j": "jay", "k": "kay", "l": "el", "m": "em", "n": "en", "o": "oh", "p": "pee",
    "q": "kyu", "r": "ar", "s": "es", "t": "tee", "u": "you", "v": "vee", "w": "dubleyu",
    "x": "eks", "y": "why", "z": "zed",
}
_SPOKEN_LETTERS = {"double u": "w", "ex": "x", "why": "y", "you": "u", "see": "c", "bee": "b", "pee": "p", "tee": "t"}


def phonetic_key(name: str) -> str:
    """A rough sound key: 'Siyu', 'See You' and 'CU' all become 'siyu'."""
    words = re.findall(r"[A-Za-z]+", str(name or ""))
    parts = []
    for word in words:
        # Short all-capital words are usually letters Whisper wrote down ("CU", "AJ").
        if word.isupper() and len(word) <= 3:
            parts.extend(_LETTER_SOUNDS[ch] for ch in word.casefold())
        else:
            parts.append(word.casefold())
    key = "".join(parts)
    for pattern, repl in (
        (r"ph", "f"), (r"ck", "k"), (r"c(?=[eiy])", "s"), (r"[cq]", "k"), (r"z", "s"), (r"x", "ks"),
        (r"(?<=[^aeiou])h", ""), (r"ee|ea|ie|ey|y(?=[^aeiou]|$)", "i"), (r"oo|ou|ew", "u"),
        (r"w", "v"), (r"you|yoo", "yu"), (r"(.)\1+", r"\1"),
    ):
        key = re.sub(pattern, repl, key)
    return key


def sounds_like(heard: str, known: str, threshold: float = 0.8) -> float:
    """Similarity of two names by sound, 0..1 (0 below the threshold)."""
    a, b = phonetic_key(heard), phonetic_key(known)
    if not a or not b:
        return 0.0
    score = difflib.SequenceMatcher(None, a, b).ratio()
    return score if score >= threshold else 0.0


def best_sound_alike(heard: str, names: Iterable[str], threshold: float = 0.8) -> str | None:
    """The one known name that sounds like ``heard``; None if none or a tie."""
    scored = sorted(((sounds_like(heard, n, threshold), n) for n in names), reverse=True)
    scored = [(s, n) for s, n in scored if s > 0]
    if not scored or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05):
        return None
    return scored[0][1]


def resolve_chat(heard: str, known: Iterable[tuple[str, str]]) -> tuple[str, str]:
    """Map a heard chat name onto a known one: (name, how), how = exact | alias | sounds_like | new."""
    known = list(known)
    plain = re.sub(r"\W", "", heard).casefold()
    for name, _ in known:
        if re.sub(r"\W", "", name).casefold() == plain:
            return name, "exact"
    for name, heard_as in known:
        if heard_as and re.sub(r"\W", "", heard_as).casefold() == plain:
            return name, "alias"
    match = best_sound_alike(heard, {name for name, _ in known})
    if match:
        return match, "sounds_like"
    return heard, "new"


_TOKEN = re.compile(r"[A-Za-z]+|[.,\-]|\s+")


def spelled_names(text: str) -> list[str]:
    """Names spelled letter by letter: 'It is C U. S I Y U, not C U' -> ['Cu', 'Siyu', 'Cu'] with 'not' runs last.

    Letters join across spaces, commas, hyphens and "and". A full stop joins
    them only in dotted spelling ("S. I. Y. U."); otherwise it ends the name,
    so "C U. S I Y U." is two names, not one.
    """
    text = str(text or "")
    found: list[tuple[bool, str]] = []
    run: list[str] = []
    start = 0
    dotted = False
    pending_dot = False

    def close() -> None:
        if len(run) >= 2:
            before = text[max(0, start - 12):start].casefold()
            negated = bool(re.search(r"\bnot(?: the)?\s*$", before))
            found.append((negated, "".join(run).capitalize()))

    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if token.isalpha():
            if len(token) == 1:
                if pending_dot and run and not dotted and len(run) > 1:
                    close()
                    run = []
                if not run:
                    start, dotted = match.start(), False
                elif pending_dot:
                    dotted = len(run) == 1 or dotted
                run.append(token)
                pending_dot = False
            elif token.casefold() == "and" and run:
                continue
            else:
                close()
                run, pending_dot = [], False
        elif token == ".":
            pending_dot = True
        elif token.isspace() or token in {",", "-"}:
            continue
    close()
    return [name for _negated, name in sorted(found, key=lambda item: item[0])]


def corrected_name(text: str, current: str) -> str | None:
    """A new name from a correction such as 'it's S I Y U, not C U' or 'send it to Siyu instead'."""
    # Compared by spelling, not sound: the correction usually *sounds* the same ("CU" -> "Siyu").
    plain_current = re.sub(r"\W", "", current).casefold()
    for name in spelled_names(text):
        if name.casefold() != plain_current:
            return name
    patterns = (
        r"\b(?:send|sent)\s+(?:it|this|that|the message)\s+to\s+(.+?)(?:\s+instead)?[.!?]*$",
        r"\bto\s+(.+?)\s+instead\b",
        r"\b(?:the )?(?:name|contact|chat) is\s+(.+?)[.!?]*$",
        r"\b(?:it'?s|it is)\s+(.+?)[,.]?\s+not\b",
        r"\bnot\s+.+?[,.]?\s+(?:it'?s|but)\s+(.+?)[.!?]*$",
    )
    for pattern in patterns:
        match = re.search(pattern, str(text or ""), re.I)
        if match:
            name = match.group(1).strip(" .,!?\"'")
            if name and len(name.split()) <= 4 and re.sub(r"\W", "", name).casefold() != plain_current:
                return name
    return None


_ADVI_SOUNDS = {"advi", "aadvi", "adhvi", "adwi", "atwi", "advee", "adivi"}


def word_correction(text: str) -> tuple[str, str] | None:
    """(new, old) from "it's aadvi, not aadri", "not Adri, ADVI", "replace Adri with ADVI"."""
    text = str(text or "").strip()
    patterns = (
        (r"^(?:no,?\s+)?(?:it'?s|it is|that'?s|that is|i said)\s+(?P<new>[\w'-]+(?:\s+[\w'-]+){0,2}),?\s+not\s+(?P<old>[\w'-]+(?:\s+[\w'-]+){0,2})[.!]*$", False),
        (r"^(?:no,?\s+)?not\s+(?P<old>[\w'-]+(?:\s+[\w'-]+){0,2}),?\s+(?:it'?s\s+|it is\s+|but\s+)?(?P<new>[\w'-]+(?:\s+[\w'-]+){0,2})[.!]*$", False),
        (r"\breplace\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)[.!]*$", False),
        (r"\bchange\s+(?:the\s+word\s+)?(?P<old>[\w'-]+(?:\s+[\w'-]+){0,2})\s+to\s+(?P<new>[\w'-]+(?:\s+[\w'-]+){0,2})[.!]*$", True),
    )
    for pattern, risky in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        old, new = match.group("old").strip(" .,'\""), match.group("new").strip(" .,'\"")
        if risky and re.match(r"(?:the\s+)?(?:body|message|subject|text|name|recipient)\b", old, re.I):
            return None  # "change the message to ..." is a whole-field edit, not a word swap
        if old and new and old.casefold() != new.casefold():
            if new.casefold().replace(" ", "") in _ADVI_SOUNDS:
                new = "ADVI"  # the assistant's own name, however Whisper spelled it
            return new, old
    return None


def replace_word(value: str, old: str, new: str) -> str | None:
    """Replace ``old`` in ``value`` exactly, or else the one word that sounds like it; None if absent."""
    pattern = re.compile(rf"(?<!\w){re.escape(old)}(?!\w)", re.I)
    if pattern.search(value):
        return pattern.sub(new, value)
    words = re.findall(r"[A-Za-z][\w'-]*", value)
    matches = {w for w in words if sounds_like(w, old, 0.75)}
    if len(matches) == 1:
        heard = matches.pop()
        return re.sub(rf"(?<!\w){re.escape(heard)}(?!\w)", new, value)
    return None


def corrected_text(text: str) -> str | None:
    """A new message body: 'change the message to X', 'say X instead', 'make it X'."""
    patterns = (
        r"\b(?:change|make)\s+(?:the\s+)?(?:message|text)\s+(?:to|say|into)\s+(.+?)[.!]*$",
        r"\b(?:say|write)\s+(.+?)\s+instead[.!]*$",
        r"\bmessage should (?:say|be)\s+(.+?)[.!]*$",
    )
    for pattern in patterns:
        match = re.search(pattern, str(text or ""), re.I)
        if match:
            value = match.group(1).strip(" \"'“”")
            if value:
                return value
    return None
