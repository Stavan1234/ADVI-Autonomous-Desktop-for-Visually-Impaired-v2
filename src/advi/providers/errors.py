from __future__ import annotations


class LLMError(Exception):
    """Base error for all ADVI LLM failures."""


class LLMAuthenticationError(LLMError):
    """Provider rejected the supplied credentials."""


class LLMRateLimitError(LLMError):
    """Provider rejected the request because of quota/rate limits."""


class LLMTimeoutError(LLMError):
    """Provider request timed out."""


class LLMUnavailableError(LLMError):
    """Provider is temporarily unavailable."""


class LLMResponseError(LLMError):
    """Provider returned an unusable response."""