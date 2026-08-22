from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CapabilityStatus(str, Enum):
    """Current operational status of an ADVI capability."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Capability:
    """Description of one capability known to ADVI."""

    name: str
    description: str
    status: CapabilityStatus


CAPABILITIES: dict[str, Capability] = {
    "conversation": Capability(
        name="conversation",
        description=(
            "Understand and respond to natural-language conversation."
        ),
        status=CapabilityStatus.AVAILABLE,
    ),
    "memory": Capability(
        name="memory",
        description=(
            "Remember and retrieve durable user information."
        ),
        status=CapabilityStatus.AVAILABLE,
    ),
    "session_continuity": Capability(
        name="session_continuity",
        description=(
            "Use useful context from previous sessions."
        ),
        status=CapabilityStatus.AVAILABLE,
    ),
    "speech_output": Capability(
        name="speech_output",
        description=(
            "Speak responses using Piper text-to-speech."
        ),
        status=CapabilityStatus.AVAILABLE,
    ),
    "web_search": Capability(
        name="web_search",
        description=(
            "Search the internet for current information."
        ),
        status=CapabilityStatus.UNAVAILABLE,
    ),
    "vision": Capability(
        name="vision",
        description=(
            "Understand visual information from the computer screen."
        ),
        status=CapabilityStatus.UNAVAILABLE,
    ),
    "desktop_control": Capability(
        name="desktop_control",
        description=(
            "Control desktop applications and system functions."
        ),
        status=CapabilityStatus.UNAVAILABLE,
    ),
}


def get_capability(
    name: str,
) -> Capability | None:
    """Return a capability by name."""

    return CAPABILITIES.get(
        name.strip().lower()
    )


def available_capabilities() -> list[Capability]:
    """Return capabilities that ADVI can currently use."""

    return [
        capability
        for capability in CAPABILITIES.values()
        if capability.status
        != CapabilityStatus.UNAVAILABLE
    ]


def unavailable_capabilities() -> list[Capability]:
    """Return capabilities that ADVI cannot currently use."""

    return [
        capability
        for capability in CAPABILITIES.values()
        if capability.status
        == CapabilityStatus.UNAVAILABLE
    ]


def capability_summary() -> str:
    """
    Produce a compact human-readable capability summary.

    This is intended for future model/planner context,
    not for direct user output.
    """

    available = available_capabilities()
    unavailable = unavailable_capabilities()

    lines = [
        "Available capabilities:"
    ]

    for capability in available:
        lines.append(
            f"- {capability.name}: {capability.description}"
        )

    lines.append("")
    lines.append("Unavailable capabilities:")

    for capability in unavailable:
        lines.append(
            f"- {capability.name}: {capability.description}"
        )

    return "\n".join(lines)

def can_use(name: str) -> bool:
    """Return True only when a capability is currently available."""

    capability = get_capability(name)

    return (
        capability is not None
        and capability.status == CapabilityStatus.AVAILABLE
    )


def capability_for_prompt() -> str:
    """
    Build compact capability context for the language model.

    Only capabilities that matter to self-awareness are exposed.
    """

    return (
        "ADVI capability state:\n"
        "- Conversation: available\n"
        "- Long-term memory: available\n"
        "- Session continuity: available\n"
        "- Speech output: available\n"
        "- Web search: unavailable\n"
        "- Screen/vision understanding: unavailable\n"
        "- Desktop control: unavailable\n\n"
        "Never claim to have an unavailable capability."
    )    