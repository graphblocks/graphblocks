"""Bounded immutable JSON snapshots shared by core contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ._canonical_reference import MAX_CANONICAL_JSON_DEPTH, canonical_dumps
from .documents import FrozenDict


JsonKeyValidator = Callable[[str, object], str]


def freeze_json_mapping(
    owner: str,
    field_name: str,
    value: object,
    *,
    key_validator: JsonKeyValidator,
    active_containers: set[int] | None = None,
    depth: int = 0,
) -> FrozenDict:
    """Validate and recursively freeze a strict JSON-compatible mapping."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} {field_name} must be a mapping")
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            f"{owner} {field_name} nesting must not exceed "
            f"{MAX_CANONICAL_JSON_DEPTH} levels"
        )
    active = set() if active_containers is None else active_containers
    identity = id(value)
    if identity in active:
        raise ValueError(f"{owner} {field_name} must not contain cyclic values")
    active.add(identity)
    try:
        snapshot = dict(value)
        return FrozenDict(
            {
                key_validator(owner, raw_key): _freeze_json_value(
                    owner,
                    field_name,
                    item,
                    key_validator=key_validator,
                    active_containers=active,
                    depth=depth + 1,
                )
                for raw_key, item in snapshot.items()
            }
        )
    finally:
        active.remove(identity)


def _freeze_json_value(
    owner: str,
    field_name: str,
    value: object,
    *,
    key_validator: JsonKeyValidator,
    active_containers: set[int],
    depth: int,
) -> object:
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            f"{owner} {field_name} nesting must not exceed "
            f"{MAX_CANONICAL_JSON_DEPTH} levels"
        )
    if isinstance(value, Mapping):
        return freeze_json_mapping(
            owner,
            field_name,
            value,
            key_validator=key_validator,
            active_containers=active_containers,
            depth=depth,
        )
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise ValueError(f"{owner} {field_name} must not contain cyclic values")
        active_containers.add(identity)
        try:
            return tuple(
                _freeze_json_value(
                    owner,
                    field_name,
                    item,
                    key_validator=key_validator,
                    active_containers=active_containers,
                    depth=depth + 1,
                )
                for item in value
            )
        finally:
            active_containers.remove(identity)
    try:
        canonical_dumps(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{owner} {field_name} must contain strict canonical JSON"
        ) from error
    return value


def thaw_json_value(value: object) -> object:
    """Return a detached mutable JSON projection of a frozen snapshot."""

    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


__all__ = ["JsonKeyValidator", "freeze_json_mapping", "thaw_json_value"]
