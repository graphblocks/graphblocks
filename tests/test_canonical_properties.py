from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from graphblocks.canonical import (
    MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH,
    canonical_dumps,
    canonical_hash,
    canonical_loads,
)
from graphblocks.loader import load_documents
from graphblocks.schema import SchemaId, SchemaIdError

_SURROGATE_CATEGORY: tuple[Literal["Cs"], ...] = ("Cs",)
_UNICODE_SCALARS = st.characters(exclude_categories=_SURROGATE_CATEGORY)
_JSON_KEYS = st.text(_UNICODE_SCALARS, max_size=24)
_YAML_PRINTABLE_TEXT = st.text(
    st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    max_size=48,
)
_YAML_KEYS = _YAML_PRINTABLE_TEXT
_JSON_SCALARS: SearchStrategy[object] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(1 << 255), max_value=1 << 255),
    st.floats(
        allow_nan=False,
        allow_infinity=False,
        width=64,
    ),
    st.text(_UNICODE_SCALARS, max_size=48),
)
_JSON_VALUES: SearchStrategy[object] = st.recursive(
    _JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=8),
        st.dictionaries(_JSON_KEYS, children, max_size=8),
    ),
    max_leaves=40,
)
_YAML_JSON_VALUES: SearchStrategy[object] = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(1 << 63), max_value=(1 << 63) - 1),
        _YAML_PRINTABLE_TEXT,
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=8),
        st.dictionaries(_YAML_KEYS, children, max_size=8),
    ),
    max_leaves=40,
)
_SCHEMA_NAMES = st.from_regex(
    r"[A-Za-z][A-Za-z0-9._/-]{0,31}",
    fullmatch=True,
)


@settings(max_examples=250, deadline=None)
@given(value=_JSON_VALUES)
def test_canonical_round_trip_and_hash_are_stable(value: object) -> None:
    encoded = canonical_dumps(value)
    decoded = canonical_loads(encoded)

    assert canonical_dumps(decoded) == encoded
    assert canonical_hash(decoded) == canonical_hash(value)


@settings(max_examples=150, deadline=None)
@given(value=st.dictionaries(_JSON_KEYS, _JSON_VALUES, max_size=12))
def test_canonical_identity_is_independent_of_mapping_insertion_order(
    value: dict[str, object],
) -> None:
    reversed_value = dict(reversed(tuple(value.items())))

    assert canonical_dumps(reversed_value) == canonical_dumps(value)
    assert canonical_hash(reversed_value) == canonical_hash(value)


@settings(max_examples=100, deadline=None)
@given(
    key=_JSON_KEYS,
    left=st.integers(min_value=-(1 << 63), max_value=(1 << 63) - 1),
    right=st.integers(min_value=-(1 << 63), max_value=(1 << 63) - 1),
)
def test_canonical_parser_rejects_generated_duplicate_object_keys(
    key: str,
    left: int,
    right: int,
) -> None:
    encoded_key = json.dumps(key, ensure_ascii=False)
    payload = f"{{{encoded_key}:{left},{encoded_key}:{right}}}"

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        canonical_loads(payload)


@settings(max_examples=100, deadline=None)
@given(
    depth=st.integers(
        min_value=MAX_CANONICAL_JSON_DEPTH - 4,
        max_value=MAX_CANONICAL_JSON_DEPTH + 4,
    ),
    use_objects=st.booleans(),
)
def test_canonical_depth_budget_holds_for_generated_container_shapes(
    depth: int,
    use_objects: bool,
) -> None:
    value: object = 0
    for _ in range(depth):
        value = {"value": value} if use_objects else [value]

    if depth <= MAX_CANONICAL_JSON_DEPTH:
        assert canonical_loads(canonical_dumps(value)) == value
    else:
        with pytest.raises(ValueError, match="nesting"):
            canonical_dumps(value)


@settings(max_examples=12, deadline=None)
@given(
    digit_count=st.integers(
        min_value=MAX_CANONICAL_INTEGER_DIGITS - 4,
        max_value=MAX_CANONICAL_INTEGER_DIGITS + 4,
    ),
    first_digit=st.integers(min_value=1, max_value=9),
    negative=st.booleans(),
)
def test_canonical_integer_token_budget_holds_near_the_boundary(
    digit_count: int,
    first_digit: int,
    negative: bool,
) -> None:
    token = f"{'-' if negative else ''}{first_digit}{'7' * (digit_count - 1)}"

    if digit_count <= MAX_CANONICAL_INTEGER_DIGITS:
        assert canonical_dumps(canonical_loads(token)) == token
    else:
        with pytest.raises(ValueError, match="decimal digits"):
            canonical_loads(token)


@settings(max_examples=300, deadline=None)
@given(text=st.text(max_size=512))
def test_canonical_parser_handles_arbitrary_text_without_unexpected_errors(
    text: str,
) -> None:
    try:
        value = canonical_loads(text)
    except (TypeError, UnicodeError, ValueError):
        return

    assert canonical_dumps(canonical_loads(canonical_dumps(value))) == canonical_dumps(
        value
    )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(document=st.dictionaries(_YAML_KEYS, _YAML_JSON_VALUES, max_size=12))
def test_yaml_loader_preserves_generated_json_documents(
    tmp_path: Path,
    document: dict[str, Any],
) -> None:
    source = tmp_path / "generated-document.yaml"
    source.write_text(canonical_dumps(document), encoding="utf-8")

    loaded = load_documents(source)

    assert canonical_dumps(loaded) == canonical_dumps([document])


@settings(max_examples=100, deadline=None)
@given(
    name=_SCHEMA_NAMES,
    major=st.integers(min_value=1, max_value=(1 << 32) - 1),
)
def test_schema_id_accepts_generated_canonical_identities(
    name: str,
    major: int,
) -> None:
    schema_id = SchemaId.parse(f"{name}@{major}")

    assert schema_id.name == name
    assert schema_id.major_version == major
    assert schema_id.as_str() == f"{name}@{major}"


@settings(max_examples=100, deadline=None)
@given(
    name=_SCHEMA_NAMES,
    major=st.integers(min_value=1, max_value=999_999),
)
def test_schema_id_rejects_generated_leading_zero_versions(
    name: str,
    major: int,
) -> None:
    with pytest.raises(SchemaIdError, match="leading zeroes"):
        SchemaId.parse(f"{name}@0{major}")
