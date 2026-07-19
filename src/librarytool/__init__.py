"""Headless Library Tool engine packages.

The package deliberately has no dependency on the current Flask transport or
the transitional flat modules in ``tools``.  Application adapters supply the
policies and repositories needed by each service.
"""

from .engine.errors import EngineError

__all__ = ["EngineError"]
