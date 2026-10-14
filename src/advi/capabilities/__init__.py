"""Concrete ADVI capability definitions and their authoritative registry."""

from .registry import Capability, CapabilityRegistry, CapabilityStatus, get_default_registry


def __getattr__(name: str):
    if name == "default_registry":
        return get_default_registry()
    raise AttributeError(name)


__all__ = ["Capability", "CapabilityRegistry", "CapabilityStatus", "get_default_registry", "default_registry"]
