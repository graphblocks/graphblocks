"""Reconstructed GB-INP-004 oversized-integer harness from captured timings."""

from __future__ import annotations

from time import perf_counter

from graphblocks.canonical import MAX_CANONICAL_INTEGER_DIGITS, canonical_loads


def main() -> int:
    for size in (MAX_CANONICAL_INTEGER_DIGITS + 1, 100_000, 300_000):
        started = perf_counter()
        try:
            canonical_loads("9" * size)
        except ValueError as error:
            if "decimal digits" not in str(error):
                raise
        else:
            raise SystemExit(f"oversized {size}-digit integer was accepted")
        print(size, "rejected", f"{perf_counter() - started:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
