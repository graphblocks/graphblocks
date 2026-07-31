"""Narrow serialization primitives shared by integration adapters."""

from __future__ import annotations

import json


def canonical_json_dumps(value: object) -> str:
    """Return the compact, sorted JSON form used by deployment adapters."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = ["canonical_json_dumps"]
