"""Narrow validation primitives shared across core contracts."""

from __future__ import annotations

from .canonical import _has_unicode_surrogate


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
    "validate_non_empty_string",
    "validate_optional_non_empty_string",
]
