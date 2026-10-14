"""Desktop and OS Automation Capability.

Imports are intentionally lazy so non-GUI components (planning, execution policy, tests,
headless diagnostics) can use :class:`DesktopContext` without importing pyautogui/X11.
"""

from .context import ApplicationSession, ApplicationSessionState, DesktopContext

__all__ = [
    "DesktopCapability",
    "ApplicationSession",
    "ApplicationSessionState",
    "DesktopContext",
    "ScreenPerception",
    "PerceptionResult",
    "VisionPerception",
    "VisionResult",
]


def __getattr__(name: str):
    if name == "DesktopCapability":
        from .executor import DesktopCapability
        return DesktopCapability
    if name in {"ScreenPerception", "PerceptionResult"}:
        from .perception import PerceptionResult, ScreenPerception
        return {"ScreenPerception": ScreenPerception, "PerceptionResult": PerceptionResult}[name]
    if name in {"VisionPerception", "VisionResult"}:
        from .vision import VisionPerception, VisionResult
        return {"VisionPerception": VisionPerception, "VisionResult": VisionResult}[name]
    raise AttributeError(name)
