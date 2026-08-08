from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import graphblocks_runtime
from graphblocks.canonical import (
    MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH,
    canonical_dumps,
    canonical_hash,
    canonical_loads,
)


_UNICODE_SCALARS = st.characters(exclude_categories=("Cs",))
_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(1 << 255), max_value=1 << 255),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(_UNICODE_SCALARS, max_size=48),
)
_JSON_VALUES = st.recursive(
    _JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=8),
        st.dictionaries(st.text(_UNICODE_SCALARS, max_size=24), children, max_size=8),
    ),
    max_leaves=40,
)


@settings(max_examples=300, deadline=None)
@given(value=_JSON_VALUES)
def test_native_canonical_identity_matches_python_reference(value: object) -> None:
    reference_json = canonical_dumps(value)

    assert graphblocks_runtime.canonicalize_json(reference_json) == reference_json
    assert graphblocks_runtime.canonical_hash_json(reference_json) == canonical_hash(value)


@pytest.mark.parametrize(
    "source",
    (
        '{"b":2,"a":1}',
        "1.2300",
        "1e400",
        "-0",
        "-0.0",
        "1e-7",
        "10000000000000000.0",
        "[true,null,{\"z\":3,\"a\":2}]",
    ),
)
def test_native_canonical_identity_matches_reference_for_noncanonical_json(
    source: str,
) -> None:
    reference_value = canonical_loads(source)
    reference_json = canonical_dumps(reference_value)

    assert graphblocks_runtime.canonicalize_json(source) == reference_json
    assert graphblocks_runtime.canonical_hash_json(source) == canonical_hash(reference_value)


def test_native_canonical_identity_matches_reference_at_resource_limits() -> None:
    integer = "9" * MAX_CANONICAL_INTEGER_DIGITS
    nested = "0"
    for _ in range(MAX_CANONICAL_JSON_DEPTH):
        nested = f"[{nested}]"

    for source in (integer, nested):
        reference = canonical_dumps(canonical_loads(source))
        assert graphblocks_runtime.canonicalize_json(source) == reference
        assert graphblocks_runtime.canonical_hash_json(source) == canonical_hash(
            canonical_loads(source)
        )


@pytest.mark.parametrize(
    "source",
    (
        '{"value":1,"value":2}',
        "9" * (MAX_CANONICAL_INTEGER_DIGITS + 1),
        "[" * (MAX_CANONICAL_JSON_DEPTH + 1)
        + "0"
        + "]" * (MAX_CANONICAL_JSON_DEPTH + 1),
    ),
)
def test_native_canonical_identity_rejects_reference_rejections(source: str) -> None:
    with pytest.raises(ValueError):
        canonical_loads(source)
    with pytest.raises(ValueError):
        graphblocks_runtime.canonicalize_json(source)
    with pytest.raises(ValueError):
        graphblocks_runtime.canonical_hash_json(source)


@pytest.mark.parametrize(
    "value",
    (
        Decimal("1.2300"),
        Decimal("1e400"),
        Decimal("-0"),
        Decimal("1e-7"),
    ),
)
def test_native_canonical_value_bridge_preserves_exact_decimals(value: Decimal) -> None:
    assert graphblocks_runtime.canonicalize(value) == canonical_dumps(value)
    assert graphblocks_runtime.canonical_hash(value) == canonical_hash(value)
