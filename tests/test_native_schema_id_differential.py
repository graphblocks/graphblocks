from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import graphblocks_runtime
from graphblocks.schema import SchemaId, SchemaIdError


_SCHEMA_NAMES = st.from_regex(
    r"[A-Za-z][A-Za-z0-9._/-]{0,63}",
    fullmatch=True,
)


@settings(max_examples=250, deadline=None)
@given(
    name=_SCHEMA_NAMES,
    major_version=st.integers(min_value=1, max_value=(1 << 32) - 1),
)
def test_native_schema_id_matches_python_reference(
    name: str,
    major_version: int,
) -> None:
    raw = f"{name}@{major_version}"
    reference = SchemaId.parse(raw)

    assert graphblocks_runtime.parse_schema_id(raw) == {
        "canonical": reference.as_str(),
        "majorVersion": reference.major_version,
        "name": reference.name,
    }


@pytest.mark.parametrize(
    "raw",
    (
        "",
        "schemas/Message",
        "@1",
        "schemas/Message@0",
        "schemas/Message@01",
        " schemas/Message@1",
        "schemas/Message @1",
        "schemas/Message@legacy@1",
        "schemas/Message@4294967296",
        "schemas/Message@-1",
        "schemas/Message@１",
        "schemas/\ud800@1",
    ),
)
def test_native_schema_id_rejects_python_reference_rejections(raw: str) -> None:
    with pytest.raises(SchemaIdError):
        SchemaId.parse(raw)
    with pytest.raises((TypeError, UnicodeError, ValueError)):
        graphblocks_runtime.parse_schema_id(raw)


def test_native_schema_id_bridge_normalizes_hostile_string_subclass() -> None:
    class HostileString(str):
        def __str__(self) -> str:
            raise RuntimeError("implementation detail")

        def __iter__(self) -> Any:
            raise RuntimeError("implementation detail")

    assert graphblocks_runtime.parse_schema_id(
        HostileString("schemas/Message@1")
    ) == {
        "canonical": "schemas/Message@1",
        "majorVersion": 1,
        "name": "schemas/Message",
    }
