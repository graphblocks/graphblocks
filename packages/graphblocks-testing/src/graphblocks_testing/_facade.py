from __future__ import annotations

import sys
from typing import TypeVar


_T = TypeVar("_T")


def facade_dependency(name: str, default: _T) -> _T:
    package = sys.modules.get("graphblocks_testing")
    if package is None:
        return default
    return getattr(package, name, default)
