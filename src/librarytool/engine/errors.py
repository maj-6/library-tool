"""Transport-independent engine errors with machine-readable details."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class EngineError(Exception):
    """Base error exposed by application services.

    ``code`` is stable API data.  ``message`` is suitable for a human, while
    ``details`` contains values a transport or client can act on.  HTTP status
    selection intentionally belongs to the HTTP adapter, not this package.
    """

    default_code = "engine_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = str(code or self.default_code)
        self.details = dict(details or {})
        self.retryable = bool(retryable)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            value["details"] = dict(self.details)
        return value


class ValidationError(EngineError):
    default_code = "invalid_command"


class NotFoundError(EngineError):
    default_code = "not_found"


class PreconditionRequiredError(EngineError):
    default_code = "precondition_required"


class ConflictError(EngineError):
    default_code = "revision_conflict"


class RepositoryError(EngineError):
    default_code = "repository_error"
