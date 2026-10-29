"""Errors with portable codes for applications and agents."""
from __future__ import annotations

from typing import Any
from functools import wraps


class ConfigurationError(ValueError):
    """An invalid public configuration, before any request is sent."""


class TranslationError(RuntimeError):
    """An operational error; details never intentionally contain credentials."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.code, self.details, self.retryable = code, details or {}, retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable,
                "details": self.details}


def operation(function):
    """Keep internal engine exception classes out of the facade contract."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        from translation.narrative.schemas import NarrativeError
        from translation.providers import ProviderError
        try:
            return function(*args, **kwargs)
        except (NarrativeError, ProviderError) as exc:
            details = {k: v for k, v in getattr(exc, "details", {}).items() if k != "text"}
            raise TranslationError(exc.kind, str(exc), details=details,
                                   retryable=getattr(exc, "retryable", False)) from exc
    return wrapped
