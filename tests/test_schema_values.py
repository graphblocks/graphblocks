from __future__ import annotations

import pytest

from graphblocks import SchemaId, SchemaIdError, TypedValue


def test_schema_id_accepts_canonical_major_version_reference() -> None:
    schema_id = SchemaId.parse("schemas/Message@1")

    assert schema_id.as_str() == "schemas/Message@1"
    assert schema_id.name == "schemas/Message"
    assert schema_id.major_version == 1
    assert str(schema_id) == "schemas/Message@1"


def test_schema_id_rejects_missing_or_invalid_version() -> None:
    with pytest.raises(SchemaIdError, match="must not be empty"):
        SchemaId.parse("")

    with pytest.raises(SchemaIdError, match="include a major version"):
        SchemaId.parse("schemas/Message")

    with pytest.raises(SchemaIdError, match="name must not be empty"):
        SchemaId.parse("@1")

    with pytest.raises(SchemaIdError, match="positive integer"):
        SchemaId.parse("schemas/Message@0")

    with pytest.raises(SchemaIdError, match="leading zero"):
        SchemaId.parse("schemas/Message@01")


def test_typed_value_preserves_schema_id_and_round_trips_canonical_json() -> None:
    value = TypedValue.new("schemas/Message@1", {"z": 1, "a": [True]})

    assert value.schema_id.as_str() == "schemas/Message@1"
    assert value.canonical_value() == {
        "schema": "schemas/Message@1",
        "value": {"z": 1, "a": [True]},
    }
    assert value.to_json() == '{"schema":"schemas/Message@1","value":{"a":[true],"z":1}}'
    assert TypedValue.from_value({"value": {"z": 1, "a": [True]}, "schema": "schemas/Message@1"}) == value


def test_typed_value_rejects_invalid_schema_id() -> None:
    with pytest.raises(SchemaIdError, match="include a major version"):
        TypedValue.new("schemas/Message", {})
