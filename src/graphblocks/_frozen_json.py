"""Shared frozen JSON container snapshots.

One canonical pair replaces the per-module copies in ``client``, ``webhooks``
and ``worker`` that had drifted apart in ``__repr__`` and equality phrasing.
The contract: a ``FrozenJsonObject`` is a read-only mapping snapshot, a
``FrozenJsonArray`` is a hashable tuple that compares equal to the list it
froze, ``copy`` returns the instance itself, and ``deepcopy``/pickle produce
detached mutable JSON projections.

Deliberately distinct frozen containers live elsewhere and must not be folded
into this pair: ``documents.FrozenDict``/``FrozenList`` (attribute-assignment
blocking, unhashable arrays), ``tools.FrozenJsonDict``/``FrozenJsonList``
(``dict`` subclass so admitted tool arguments stay directly serializable),
``application_event``'s payload containers (domain error messages), and
``integrations._wire`` (generic, integrations-facing).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import SupportsIndex


class FrozenJsonArray(tuple[object, ...]):
    """An immutable JSON array that stays hashable and list-comparable."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple(self) == tuple(other)
        return super().__eq__(other)

    __hash__ = tuple.__hash__

    def __copy__(self) -> FrozenJsonArray:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> list[object]:
        return [thaw_json_container(item) for item in self]

    def __reduce_ex__(
        self,
        protocol: SupportsIndex,
    ) -> tuple[type[FrozenJsonArray], tuple[tuple[object, ...]]]:
        del protocol
        return type(self), (tuple(self),)


class FrozenJsonObject(Mapping[str, object]):
    """An immutable mapping-backed JSON object snapshot."""

    __slots__ = ("__values",)

    def __init__(self, values: Mapping[str, object]) -> None:
        self.__values = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> object:
        return self.__values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    def __repr__(self) -> str:
        return repr(dict(self.__values))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self.__values) == dict(other)

    def __copy__(self) -> FrozenJsonObject:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
        return {
            key: thaw_json_container(item)
            for key, item in self.__values.items()
        }

    def __reduce_ex__(
        self,
        protocol: SupportsIndex,
    ) -> tuple[type[FrozenJsonObject], tuple[dict[str, object]]]:
        del protocol
        return type(self), (dict(self.__values),)


def freeze_json_container(value: object) -> object:
    """Recursively snapshot plain JSON containers into the frozen pair."""

    if isinstance(value, dict):
        return FrozenJsonObject(
            {key: freeze_json_container(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return FrozenJsonArray(freeze_json_container(item) for item in value)
    return value


def thaw_json_container(value: object) -> object:
    """Return a detached mutable JSON projection of a frozen snapshot."""

    if isinstance(value, Mapping):
        return {key: thaw_json_container(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json_container(item) for item in value]
    return value


__all__ = [
    "FrozenJsonArray",
    "FrozenJsonObject",
    "freeze_json_container",
    "thaw_json_container",
]
