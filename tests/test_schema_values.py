from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from graphblocks import SchemaId, SchemaIdError, SchemaManifest, SchemaManifestError, TypedValue
from graphblocks.canonical import canonical_hash


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


def test_typed_value_rejects_non_json_values_at_construction() -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Message@1", object())

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Score@1", math.nan)

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.from_value({"schema": "schemas/Message@1", "value": object()})


def test_typed_value_copies_payload_and_canonical_value() -> None:
    payload = {"z": 1, "a": [True]}
    value = TypedValue.new("schemas/Message@1", payload)

    payload["a"].append(False)
    payload["z"] = 2

    assert value.canonical_value() == {
        "schema": "schemas/Message@1",
        "value": {"z": 1, "a": [True]},
    }
    envelope = value.canonical_value()
    envelope["value"]["a"].append(False)

    assert value.to_json() == '{"schema":"schemas/Message@1","value":{"a":[true],"z":1}}'


def test_python_typed_value_matches_shared_tck_cases() -> None:
    fixture = Path(__file__).resolve().parents[1] / "tck" / "schema" / "typed-values.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))

    for case in cases:
        expected = case["expected"]
        if "error" in expected:
            with pytest.raises(SchemaIdError):
                TypedValue.new(case["schema"], case["value"])
            continue

        value = TypedValue.new(case["schema"], case["value"])

        assert value.canonical_value() == expected["canonical_value"], case["name"]
        assert value.to_json() == expected["canonical_json"], case["name"]


def test_schema_manifest_scans_schema_documents_deterministically(tmp_path: Path) -> None:
    schema_b = tmp_path / "v1" / "b.schema.json"
    schema_a = tmp_path / "v1" / "a.schema.json"
    schema_b.parent.mkdir()
    document_b = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "example.com/v1/b.schema.json",
        "title": "B",
        "type": "object",
        "properties": {"z": {"type": "string"}},
    }
    document_a = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "example.com/v1/a.schema.json",
        "title": "A",
        "type": "object",
        "properties": {"a": {"type": "integer"}},
    }
    schema_b.write_text(
        '{"properties":{"z":{"type":"string"}},"type":"object","title":"B",'
        '"$id":"example.com/v1/b.schema.json","$schema":"https://json-schema.org/draft/2020-12/schema"}',
        encoding="utf-8",
    )
    schema_a.write_text(
        '{"type":"object","properties":{"a":{"type":"integer"}},"$id":"example.com/v1/a.schema.json",'
        '"title":"A","$schema":"https://json-schema.org/draft/2020-12/schema"}',
        encoding="utf-8",
    )

    manifest = SchemaManifest.from_directory(tmp_path)

    assert [entry.schema_id for entry in manifest.entries] == [
        "example.com/v1/a.schema.json",
        "example.com/v1/b.schema.json",
    ]
    assert [entry.path for entry in manifest.entries] == ["v1/a.schema.json", "v1/b.schema.json"]
    assert [entry.digest for entry in manifest.entries] == [
        canonical_hash(document_a),
        canonical_hash(document_b),
    ]
    assert manifest.manifest_contract() == {
        "manifestVersion": 1,
        "schemas": [
            {
                "schemaId": "example.com/v1/a.schema.json",
                "path": "v1/a.schema.json",
                "digest": canonical_hash(document_a),
                "draft": "https://json-schema.org/draft/2020-12/schema",
                "title": "A",
            },
            {
                "schemaId": "example.com/v1/b.schema.json",
                "path": "v1/b.schema.json",
                "digest": canonical_hash(document_b),
                "draft": "https://json-schema.org/draft/2020-12/schema",
                "title": "B",
            },
        ],
    }
    assert manifest.content_digest() == canonical_hash(manifest.manifest_contract())


def test_schema_manifest_rejects_missing_and_duplicate_schema_ids(tmp_path: Path) -> None:
    missing = tmp_path / "missing.schema.json"
    missing.write_text('{"type":"object"}', encoding="utf-8")

    with pytest.raises(SchemaManifestError, match="must declare a string \\$id"):
        SchemaManifest.from_directory(tmp_path)

    missing.unlink()
    (tmp_path / "first.schema.json").write_text('{"$id":"example.com/Duplicate.schema.json"}', encoding="utf-8")
    (tmp_path / "second.schema.json").write_text('{"$id":"example.com/Duplicate.schema.json"}', encoding="utf-8")

    with pytest.raises(SchemaManifestError, match="duplicate schema id example.com/Duplicate.schema.json"):
        SchemaManifest.from_directory(tmp_path)


def test_checked_in_schema_manifest_digest_is_golden() -> None:
    schema_root = Path(__file__).resolve().parents[1] / "schemas"

    manifest = SchemaManifest.from_directory(schema_root)

    assert [entry.schema_id for entry in manifest.entries] == [
        "graphblocks.ai/v1alpha1/application.schema.json",
        "graphblocks.ai/v1alpha1/binding.schema.json",
        "graphblocks.ai/v1alpha1/plugin-manifest.schema.json",
        "graphblocks.ai/v1alpha3/graph.schema.json",
    ]
    assert manifest.content_digest() == "sha256:3bcd67f34d6c22940158b7c3d3290fb33620fa32de72c177533d7f20188a013e"
