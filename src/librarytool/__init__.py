"""Headless Library Tool engine packages.

The package deliberately has no dependency on the current Flask transport or
the transitional flat modules in ``tools``.  Application adapters supply the
policies and repositories needed by each service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine.errors import EngineError

__all__ = ["EngineError"]


def __getattr__(name: str) -> Any:
    """Keep lightweight subpackages independent of the full engine graph."""

    if name == "EngineError":
        from .engine.errors import EngineError

        return EngineError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
