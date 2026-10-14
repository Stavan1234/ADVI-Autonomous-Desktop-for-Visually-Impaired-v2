from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
import inspect


@dataclass(frozen=True)
class DavidResult:
    success: bool
    text: str = ""
    results: list[Any] | None = None
    reason: str = ""
    metadata: dict[str, Any] | None = None


class DavidInterface(Protocol):
    def available(self) -> bool: ...

    def execute(
        self,
        user_input: str,
        *,
        reason: str = "",
        approved: bool = False,
        stage: str | None = None,
        operation_intent: str | None = None,
        protected_parameters: dict[str, Any] | None = None,
    ) -> DavidResult: ...


class DavidAdapter:
    """
    Narrow boundary between the primary ADVI system
    and the legacy David fallback system.
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    def available(self) -> bool:
        return self._service is not None

    def execute(
        self,
        user_input: str,
        *,
        reason: str = "",
        approved: bool = False,
        stage: str | None = None,
        operation_intent: str | None = None,
        protected_parameters: dict[str, Any] | None = None,
    ) -> DavidResult:
        if self._service is None:
            return DavidResult(
                success=False,
                reason="David fallback is unavailable.",
            )

        try:
            process = self._service.process
            kwargs = {
                "approved": approved,
                "stage": stage,
                "operation_intent": operation_intent,
                "protected_parameters": protected_parameters,
            }
            # David is intentionally isolated, so older/injected test services may
            # still expose the narrower legacy process signature. Pass only the
            # keywords that the concrete service accepts; the real implementation
            # receives the full structured contract.
            try:
                parameters = inspect.signature(process).parameters
            except (TypeError, ValueError):
                parameters = {}
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
            if not accepts_kwargs and parameters:
                kwargs = {key: value for key, value in kwargs.items() if key in parameters}
            result = process(user_input, **kwargs)

        except Exception as exc:
            return DavidResult(
                success=False,
                reason=f"David fallback failed: {exc}",
                metadata={},
            )

        text = str(getattr(result, "text", "") or "")

        results = list(
            getattr(result, "results", None) or []
        )

        if not text:
            successful = [
                item
                for item in results
                if getattr(item, "success", False)
            ]

            if successful:
                text = "David completed the fallback task."

        result_metadata = dict(getattr(result, "metadata", {}) or {})
        success = bool(getattr(result, "success", False))
        execution_reason = str(result_metadata.get("reason") or "").strip()
        if not execution_reason:
            execution_reason = "fallback execution failed" if not success else ""
        if reason:
            result_metadata.setdefault("handoff_reason", reason)

        return DavidResult(
            success=success,
            text=text,
            results=results,
            reason=execution_reason,
            metadata=result_metadata,
        )
