from __future__ import annotations

from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    Decimal,
    DecimalException,
    ROUND_CEILING,
    localcontext,
)
import math
from typing import Any


MAX_DURATION_MILLISECONDS = (1 << 64) - 1


def parse_duration_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
        except (OverflowError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text.isascii() or "_" in text:
            return None
        multiplier = 1.0
        for suffix, candidate_multiplier in (
            ("ms", 0.001),
            ("s", 1.0),
            ("m", 60.0),
            ("h", 3600.0),
            ("d", 86400.0),
        ):
            if text.endswith(suffix):
                text = text[: -len(suffix)]
                multiplier = candidate_multiplier
                break
        try:
            seconds = float(text) * multiplier
        except ValueError:
            return None
    else:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def parse_duration_milliseconds(value: Any) -> int | None:
    """Parse a duration using integer milliseconds and string/float seconds.

    Integer duration fields are the SDK's legacy millisecond form. Other
    numeric values and unit-suffixed strings use the seconds grammar shared by
    ``parse_duration_seconds``. Positive sub-millisecond values round up so an
    explicit duration can never become an unset or already-expired timeout.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0 or value > MAX_DURATION_MILLISECONDS:
            return None
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text.isascii() or "_" in text:
            return None
        multiplier = Decimal("1000")
        for suffix, candidate_multiplier in (
            ("ms", Decimal("1")),
            ("s", Decimal("1000")),
            ("m", Decimal("60000")),
            ("h", Decimal("3600000")),
            ("d", Decimal("86400000")),
        ):
            if text.endswith(suffix):
                text = text[: -len(suffix)]
                multiplier = candidate_multiplier
                break
        try:
            amount = Decimal(text)
            if not amount.is_finite() or amount <= 0:
                return None
            precision = (
                len(amount.as_tuple().digits)
                + len(multiplier.as_tuple().digits)
                + 1
            )
            with localcontext() as context:
                context.prec = precision
                context.Emax = MAX_EMAX
                context.Emin = MIN_EMIN
                milliseconds_decimal = amount * multiplier
                if (
                    not milliseconds_decimal.is_finite()
                    or milliseconds_decimal > MAX_DURATION_MILLISECONDS
                ):
                    return None
                milliseconds = int(
                    milliseconds_decimal.to_integral_value(rounding=ROUND_CEILING)
                )
        except DecimalException:
            return None
        return max(1, milliseconds)
    seconds = parse_duration_seconds(value)
    if seconds is None:
        return None
    raw_milliseconds = seconds * 1000
    if not math.isfinite(raw_milliseconds) or raw_milliseconds <= 0:
        return None
    milliseconds = math.ceil(raw_milliseconds)
    if milliseconds > MAX_DURATION_MILLISECONDS:
        return None
    return max(1, milliseconds)
