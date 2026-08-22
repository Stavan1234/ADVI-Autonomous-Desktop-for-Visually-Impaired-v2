from __future__ import annotations


def detect_response_mode(user_input: str) -> str:
    """
    Detect the requested response depth using simple deterministic rules.

    Returns:
        "concise"
        "detailed"
    """
    text = user_input.strip().lower()

    if not text:
        return "concise"

    detailed_phrases = (
        "explain in detail",
        "explain thoroughly",
        "explain deeply",
        "give me a detailed explanation",
        "give a detailed explanation",
        "tell me everything",
        "explain step by step",
        "step by step",
        "in depth",
        "deep dive",
        "elaborate",
    )

    detailed_starts = (
            "explain ",
            "describe ",
            "teach me ",
        )
        

    concise_phrases = (
        "be brief",
        "briefly",
        "keep it short",
        "short answer",
        "in one sentence",
        "one sentence",
        "just the answer",
        "don't explain",
        "do not explain",
    )

    has_detailed_suffix = (
            " in detail" in text
            or " in depth" in text
            or " thoroughly" in text
            or " deeply" in text
        )
        
    if (
        has_detailed_suffix
        or any(text.startswith(start) for start in detailed_starts)
        or any(phrase in text for phrase in detailed_phrases)
    ):
        return "detailed"

    if any(phrase in text for phrase in concise_phrases):
        return "concise"

    return "concise"


def build_response_policy(user_input: str) -> str:
    """Return response instructions appropriate for the request."""

    mode = detect_response_mode(user_input)

    if mode == "detailed":
        return (
            "Response mode: DETAILED.\n"
            "Provide enough explanation to satisfy the request. "
            "Use clear structure and avoid unnecessary repetition."
        )

    return (
        "Response mode: CONCISE.\n"
        "Give the shortest useful answer. "
        "Do not add unnecessary background, repetition, or filler."
    )