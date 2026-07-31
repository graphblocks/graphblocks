"""Narrow JSON decoding primitives shared across package boundaries."""

from __future__ import annotations

from typing import TypeVar


_ValueT = TypeVar("_ValueT")


def reject_duplicate_json_keys(
    pairs: list[tuple[str, _ValueT]],
) -> dict[str, _ValueT]:
    result: dict[str, _ValueT] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


__all__ = ["reject_duplicate_json_keys"]
