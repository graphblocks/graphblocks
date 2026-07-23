from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Generic, TypeVar

from graphblocks.canonical import _canonical_dumps, canonical_loads


_ValueT = TypeVar("_ValueT")


class FrozenWireJsonObject(Mapping[str, _ValueT], Generic[_ValueT]):
    """An immutable mapping-backed JSON object snapshot."""

    __slots__ = ("__values",)

    def __init__(self, values: Mapping[str, _ValueT]) -> None:
        self.__values = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> _ValueT:
        return self.__values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    def __repr__(self) -> str:
        return repr(dict(self.__values))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self.__values) == dict(other)

    def __copy__(self) -> FrozenWireJsonObject:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
        return {
            key: thaw_wire_json(item)
            for key, item in self.__values.items()
        }

    def __reduce_ex__(
        self,
        protocol: int,
    ) -> tuple[type[FrozenWireJsonObject], tuple[dict[str, _ValueT]]]:
        del protocol
        return type(self), (dict(self.__values),)


class FrozenWireJsonArray(tuple[object, ...]):
    """An immutable JSON array that retains list-compatible equality."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple(self) == tuple(other)
        return super().__eq__(other)

    def __copy__(self) -> FrozenWireJsonArray:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> list[object]:
        return [thaw_wire_json(item) for item in self]

    def __reduce_ex__(
        self,
        protocol: int,
    ) -> tuple[type[FrozenWireJsonArray], tuple[tuple[object, ...]]]:
        del protocol
        return type(self), (tuple(self),)


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return FrozenWireJsonObject(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return FrozenWireJsonArray(_freeze_json(item) for item in value)
    return value


def snapshot_wire_json(value: object, *, field_name: str) -> object:
    """Validate and immutably snapshot a JSON wire value in one source traversal."""

    try:
        canonical = _canonical_dumps(value, reject_tuples=True)
        snapshot = canonical_loads(canonical)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must contain strict JSON values") from error
    return _freeze_json(snapshot)


def thaw_wire_json(value: object) -> object:
    """Return a mutable JSON projection suitable for downstream wire contracts."""

    if isinstance(value, Mapping):
        return {key: thaw_wire_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_wire_json(item) for item in value]
    return value


__all__ = [
    "FrozenWireJsonArray",
    "FrozenWireJsonObject",
    "snapshot_wire_json",
    "thaw_wire_json",
]
