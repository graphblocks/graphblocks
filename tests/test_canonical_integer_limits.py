from __future__ import annotations

import pytest

from graphblocks.canonical import (
    MAX_CANONICAL_INTEGER_DIGITS,
    canonical_dumps,
    canonical_loads,
)


def test_canonical_integer_limit_accepts_exact_decimal_boundary() -> None:
    digits = "9" * MAX_CANONICAL_INTEGER_DIGITS

    assert canonical_dumps(canonical_loads(digits)) == digits
    assert canonical_dumps(canonical_loads(f"-{digits}")) == f"-{digits}"
    assert canonical_dumps(10 ** (MAX_CANONICAL_INTEGER_DIGITS - 1)) == (
        "1" + "0" * (MAX_CANONICAL_INTEGER_DIGITS - 1)
    )


@pytest.mark.parametrize(
    "digit_count",
    (MAX_CANONICAL_INTEGER_DIGITS + 1, 100_000, 300_000),
)
def test_canonical_loads_rejects_oversized_integer_tokens(
    digit_count: int,
) -> None:
    with pytest.raises(
        ValueError,
        match=f"must not exceed {MAX_CANONICAL_INTEGER_DIGITS} decimal digits",
    ):
        canonical_loads("9" * digit_count)


def test_canonical_dumps_rejects_oversized_python_integers() -> None:
    oversized = 10**MAX_CANONICAL_INTEGER_DIGITS

    with pytest.raises(
        ValueError,
        match=f"must not exceed {MAX_CANONICAL_INTEGER_DIGITS} decimal digits",
    ):
        canonical_dumps(oversized)
    with pytest.raises(
        ValueError,
        match=f"must not exceed {MAX_CANONICAL_INTEGER_DIGITS} decimal digits",
    ):
        canonical_dumps(-oversized)
