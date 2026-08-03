"""Reconstructed GB-INP-005 Decimal harness with a no-rescan assertion."""

from __future__ import annotations

from decimal import Decimal
import importlib
from time import perf_counter


def main() -> int:
    canonical = importlib.import_module("graphblocks.canonical")
    original_dumps = canonical.json.dumps

    class TrackingString(str):
        replace_calls = 0

        def replace(self, old: str, new: str, count: int = -1) -> TrackingString:
            type(self).replace_calls += 1
            return type(self)(super().replace(old, new, count))

    def tracking_dumps(*args: object, **kwargs: object) -> TrackingString:
        return TrackingString(original_dumps(*args, **kwargs))

    canonical.json.dumps = tracking_dumps
    try:
        values = [Decimal("1.25") for _ in range(16_000)]
        started = perf_counter()
        encoded = canonical.canonical_dumps(values)
        elapsed = perf_counter() - started
    finally:
        canonical.json.dumps = original_dumps
    if TrackingString.replace_calls != 0 or len(encoded) != 80_001:
        raise SystemExit("Decimal encoding rescanned output or changed its canonical form")
    print(16_000, f"{elapsed:.6f}", len(encoded), "replace_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
