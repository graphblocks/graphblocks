from __future__ import annotations

import math
from pathlib import Path

import pytest

from graphblocks import (
    SchemaId,
    SchemaIdError,
    SchemaManifest,
    SchemaManifestEntry,
    SchemaManifestError,
    TypedValue,
    compile_graph,
    resource_schema_errors,
)
from graphblocks.canonical import canonical_hash, canonical_loads


def test_schema_id_accepts_canonical_major_version_reference() -> None:
    schema_id = SchemaId.parse("schemas/Message@1")

    assert schema_id.as_str() == "schemas/Message@1"
    assert schema_id.name == "schemas/Message"
    assert schema_id.major_version == 1
    assert str(schema_id) == "schemas/Message@1"


def test_schema_id_accepts_maximum_u32_major_version() -> None:
    schema_id = SchemaId.parse("schemas/Message@4294967295")

    assert schema_id.major_version == 4294967295


@pytest.mark.parametrize(
    "version",
    ["4294967296", "9" * 10_000],
    ids=["u32-overflow", "conversion-limit"],
)
def test_schema_id_rejects_major_versions_outside_u32(version: str) -> None:
    with pytest.raises(
        SchemaIdError,
        match="schema id major version must be a positive integer",
    ):
        SchemaId.parse(f"schemas/Message@{version}")


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

    with pytest.raises(SchemaIdError, match="name is not canonical"):
        SchemaId.parse("schemas/Message Type@1")


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

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Message@1", "\ud800")


def test_typed_value_rejects_python_only_json_like_values() -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Message@1", ("not", "a", "json", "array"))

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Message@1", {1: "not a json object key"})


@pytest.mark.parametrize("container_kind", ["mapping", "array"])
def test_typed_value_rejects_recursive_values(container_kind: str) -> None:
    if container_kind == "mapping":
        value: object = {}
        value["self"] = value  # type: ignore[index]
    else:
        value = []
        value.append(value)  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Message@1", value)


def test_typed_value_rejects_excessive_nesting_without_recursion_errors() -> None:
    value: object = 0
    for _ in range(1_100):
        value = [value]

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Message@1", value)


@pytest.mark.parametrize(
    ("invalid_value", "expected_path", "expected_keyword"),
    [
        ({1: "not a JSON object key"}, "$.spec.extensions", "jsonObjectKey"),
        ("\ud800", "$.spec.extensions", "unicodeScalar"),
        ({"\udfff": "value"}, "$.spec.extensions", "unicodeScalar"),
        (math.nan, "$.spec.extensions", "finiteNumber"),
        (math.inf, "$.spec.extensions", "finiteNumber"),
        (-math.inf, "$.spec.extensions", "finiteNumber"),
    ],
)
def test_resource_validation_rejects_values_outside_the_json_domain(
    invalid_value: object,
    expected_path: str,
    expected_keyword: str,
) -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "invalid-json-domain"},
        "spec": {"nodes": {}, "extensions": invalid_value},
    }
    errors = resource_schema_errors(document)
    plan = compile_graph(document)

    assert [(error.path, error.keyword) for error in errors] == [
        (expected_path, expected_keyword)
    ]
    assert not plan.ok
    assert [diagnostic.code for diagnostic in plan.diagnostics.diagnostics] == [
        "GB0014"
    ]


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
    cases = canonical_loads(fixture.read_text(encoding="utf-8"))

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


def test_schema_manifest_rejects_non_standard_json_constants(tmp_path: Path) -> None:
    schema = tmp_path / "bad.schema.json"
    schema.write_text('{"$id":"example.com/Bad.schema.json","type":"object","ignored":NaN}', encoding="utf-8")

    with pytest.raises(SchemaManifestError, match="strict JSON"):
        SchemaManifest.from_directory(tmp_path)


def test_schema_manifest_rejects_symlinked_schema_documents(tmp_path: Path, symlink_or_skip) -> None:
    root = tmp_path / "schemas"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"$id":"example.com/Outside.schema.json"}', encoding="utf-8")
    symlink_or_skip(root / "inside.json", outside)

    with pytest.raises(SchemaManifestError, match="regular non-symlinked files"):
        SchemaManifest.from_directory(root)


def test_schema_manifest_rejects_non_regular_json_candidates(tmp_path: Path) -> None:
    (tmp_path / "directory.json").mkdir()

    with pytest.raises(SchemaManifestError, match="regular non-symlinked files"):
        SchemaManifest.from_directory(tmp_path)


@pytest.mark.parametrize(
    "path",
    ["../schema.json", "/schema.json", "schemas//schema.json", "schemas\\schema.json", "C:/schema.json"],
)
def test_schema_manifest_rejects_unsafe_or_noncanonical_paths(path: str) -> None:
    with pytest.raises(SchemaManifestError, match="safe canonical relative path"):
        SchemaManifest(
            (
                SchemaManifestEntry(
                    schema_id="example.com/schema.json",
                    path=path,
                    digest="sha256:" + "0" * 64,
                ),
            )
        )


@pytest.mark.parametrize(
    "digest",
    ["sha256:abcd", "sha256:" + "A" * 64, "sha512:" + "0" * 64],
)
def test_schema_manifest_rejects_noncanonical_sha256_digests(digest: str) -> None:
    with pytest.raises(SchemaManifestError, match="requires a sha256 digest"):
        SchemaManifest(
            (
                SchemaManifestEntry(
                    schema_id="example.com/schema.json",
                    path="schemas/schema.json",
                    digest=digest,
                ),
            )
        )


@pytest.mark.parametrize("value", [b"bytes", {"set"}, object()])
def test_compile_graph_reports_non_json_values_as_diagnostics(value: object) -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "invalid-json-value"},
        "spec": {"nodes": {}, "extensions": value},
    }

    plan = compile_graph(document)

    assert not plan.ok
    assert [(item.code, item.path) for item in plan.diagnostics.diagnostics] == [
        ("GB0014", "$.spec.extensions")
    ]


def test_compile_graph_reports_excessive_depth_as_a_diagnostic() -> None:
    nested: dict[str, object] = {}
    current = nested
    for _ in range(65):
        child: dict[str, object] = {}
        current["next"] = child
        current = child
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "too-deep"},
        "spec": {"nodes": {}, "extensions": nested},
    }

    plan = compile_graph(document)

    assert not plan.ok
    assert [(item.code, item.message) for item in plan.diagnostics.diagnostics] == [
        ("GB0014", "resource nesting must not exceed 64 levels")
    ]


def test_checked_in_schema_manifest_digest_is_golden() -> None:
    schema_root = Path(__file__).resolve().parents[1] / "schemas"

    manifest = SchemaManifest.from_directory(schema_root)

    assert [entry.schema_id for entry in manifest.entries] == [
        "graphblocks.ai/composition/v1alpha1/graph-fragment.schema.json",
        "graphblocks.ai/v1/graph.schema.json",
        "graphblocks.ai/v1/plugin-manifest.schema.json",
        "graphblocks.ai/v1alpha1/application.schema.json",
        "graphblocks.ai/v1alpha1/binding.schema.json",
        "graphblocks.ai/v1alpha1/plugin-manifest.schema.json",
        "graphblocks.ai/v1alpha3/graph.schema.json",
    ]
    assert manifest.content_digest() == "sha256:2a4966634df9fc043fb21d2a887b58b3d1004fa3be15bd3fe33315864ba11a40"


def test_rust_schema_package_mirrors_canonical_resource_schemas() -> None:
    root = Path(__file__).resolve().parents[1]
    canonical_root = root / "schemas"
    rust_root = root / "crates" / "graphblocks-schema" / "schemas"

    assert {
        path.relative_to(canonical_root).as_posix(): path.read_bytes()
        for path in canonical_root.rglob("*.json")
    } == {
        path.relative_to(rust_root).as_posix(): path.read_bytes()
        for path in rust_root.rglob("*.json")
    }
