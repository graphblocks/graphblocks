from __future__ import annotations

import math
from typing import Any


def parse_duration_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        multiplier = 1.0
        for suffix, candidate_multiplier in (
            ("ms", 0.001),
            ("s", 1.0),
            ("m", 60.0),
            ("h", 3600.0),
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
