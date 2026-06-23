from __future__ import annotations

try:
    from ._native import __version__, binding_version
except ImportError:
    __version__ = "0.1.0"

    def binding_version() -> str:
        return __version__


__all__ = ["__version__", "binding_version"]
