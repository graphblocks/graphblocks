"""Narrow validation primitives shared across core contracts."""

from __future__ import annotations

from collections.abc import Mapping

from .canonical import _has_unicode_surrogate


def snapshot_collection(
    owner: str,
    field_name: str,
    value: object,
) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise ValueError(f"{owner} {field_name} must be a collection")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except (TypeError, RuntimeError) as error:
        raise ValueError(f"{owner} {field_name} must be a collection") from error


def validate_non_empty_string(
    owner: str,
    field_name: str,
    value: object,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(
            f"{owner} {field_name} must not contain surrounding whitespace"
        )
    if _has_unicode_surrogate(value):
        raise ValueError(
            f"{owner} {field_name} must contain only Unicode scalar values"
        )
    return value


def validate_optional_non_empty_string(
    owner: str,
    field_name: str,
    value: object | None,
) -> str | None:
    if value is None:
        return None
    return validate_non_empty_string(owner, field_name, value)


__all__ = [
    "snapshot_collection",
    "validate_non_empty_string",
    "validate_optional_non_empty_string",
]
