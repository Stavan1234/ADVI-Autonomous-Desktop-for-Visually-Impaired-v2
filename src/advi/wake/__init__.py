"""Wake word: a background listener that starts ADVI on "wake up ADVI", even when ADVI is closed."""

from advi.wake.phrase import is_wake_phrase

__all__ = ["is_wake_phrase"]
