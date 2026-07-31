from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from importlib import import_module


def resolve_lazy_export(
    *,
    module_name: str,
    package_name: str | None,
    namespace: MutableMapping[str, object],
    export_modules: Mapping[str, str],
    name: str,
) -> object:
    target_module = export_modules.get(name)
    if target_module is None:
        raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
    value = getattr(import_module(target_module, package_name), name)
    namespace[name] = value
    return value


__all__ = ["resolve_lazy_export"]
