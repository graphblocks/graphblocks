"""Shared strict ISO-8601 datetime parsing.

One canonical parser replaces the per-module copies that had drifted apart
in accepted forms. The accepted shape is exactly::

    YYYY-MM-DDTHH:MM:SS[.f...](Z | +HH:MM | -HH:MM)

with no surrounding whitespace, at least one fractional digit when a fraction
is present, offset hours <= 23 and minutes <= 59. The result is normalized to
UTC. Callers wrap the generic ValueError with their own field context so
existing error messages stay stable.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_utc_iso_datetime(value: object) -> datetime:
    """Parse a strict ISO-8601 datetime string and normalize it to UTC."""

    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("must be an ISO datetime")
    if len(value) <= 19 or value[10] != "T":
        raise ValueError("must be an ISO datetime")
    suffix_start = 19
    if value[suffix_start] == ".":
        suffix_start += 1
        fraction_start = suffix_start
        while suffix_start < len(value) and value[suffix_start].isdigit():
            suffix_start += 1
        if suffix_start == fraction_start:
            raise ValueError("must be an ISO datetime")
    suffix = value[suffix_start:]
    if suffix == "Z":
        normalized = f"{value[:suffix_start]}+00:00"
    elif (
        len(suffix) == 6
        and suffix[0] in {"+", "-"}
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
        and int(suffix[1:3]) <= 23
        and int(suffix[4:6]) <= 59
    ):
        normalized = value
    else:
        raise ValueError("must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("must be an ISO datetime") from error
    return parsed.astimezone(timezone.utc)
