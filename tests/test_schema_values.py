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

    with pytest.raises(SchemaIdError, match="name is not canonical"):
        SchemaId.parse("schemas/Message@legacy@1")


def test_schema_id_rejects_non_string_and_surrogate_values_cleanly() -> None:
    with pytest.raises(SchemaIdError, match="must be a string"):
        SchemaId.parse(None)  # type: ignore[arg-type]
    with pytest.raises(SchemaIdError, match="Unicode scalar values"):
        SchemaId.parse("schemas/\ud800@1")


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


def test_typed_value_rejects_malformed_direct_construction_and_envelopes() -> None:
    with pytest.raises(TypeError, match="schema_id must be a SchemaId"):
        TypedValue("schemas/Message@1", {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="envelope must be a mapping"):
        TypedValue.from_value([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="contains unknown fields"):
        TypedValue.from_value(
            {
                "schema": "schemas/Message@1",
                "value": {},
                "unexpected": True,
            }
        )


def test_typed_value_rejects_non_json_values_at_construction() -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Message@1", object())

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Score@1", math.nan)

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.from_value({"schema": "schemas/Message@1", "value": object()})

    with pytest.raises(ValueError, match="canonical JSON"):
        TypedValue.new("schemas/Message@1", "\ud800")


def test_typed_value_snapshots_stateful_mappings_once() -> None:
    class StatefulDict(dict[str, object]):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def items(self) -> object:
            self.calls += 1
            if self.calls == 1:
                return (("stable", 1),)
            return (("changed", 2),)

    source = StatefulDict()

    value = TypedValue.new("schemas/Message@1", source)

    assert value.value == {"stable": 1}
    assert source.calls == 1


def test_typed_value_deeply_freezes_state_and_returns_mutable_projections() -> None:
    source = {"nested": {"items": [1]}}
    value = TypedValue.new("schemas/Message@1", source)

    source["nested"]["items"].append(2)

    assert value.value == {"nested": {"items": [1]}}
    with pytest.raises(TypeError, match="frozen list"):
        value.value["nested"]["items"].append(3)  # type: ignore[index,union-attr]

    projection = value.canonical_value()
    projection["value"]["nested"]["items"].append(4)  # type: ignore[index,union-attr]
    assert value.value == {"nested": {"items": [1]}}


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


def test_schema_manifest_rejects_malformed_entry_collections_cleanly() -> None:
    with pytest.raises(SchemaManifestError, match="non-empty schema id"):
        SchemaManifestEntry(  # type: ignore[arg-type]
            schema_id=1,
            path="schemas/schema.json",
            digest="sha256:" + "0" * 64,
        )

    with pytest.raises(SchemaManifestError, match="SchemaManifestEntry"):
        SchemaManifest(("not-an-entry",))  # type: ignore[arg-type]

    with pytest.raises(SchemaManifestError, match="Unicode scalar string"):
        SchemaManifest(
            (
                SchemaManifestEntry(
                    schema_id="example.com/schema.json",
                    path="schemas/schema.json",
                    digest="sha256:" + "0" * 64,
                    title="\ud800",
                ),
            )
        )


def test_schema_manifest_validates_sort_fields_before_sorting() -> None:
    digest = "sha256:" + "0" * 64
    with pytest.raises(SchemaManifestError, match="non-empty schema id"):
        SchemaManifest(
            (
                SchemaManifestEntry(
                    schema_id="example.com/valid.json",
                    path="valid.json",
                    digest=digest,
                ),
                SchemaManifestEntry(  # type: ignore[arg-type]
                    schema_id=1,
                    path="invalid.json",
                    digest=digest,
                ),
            )
        )
    with pytest.raises(SchemaManifestError, match="relative path"):
        SchemaManifest(
            (
                SchemaManifestEntry(
                    schema_id="example.com/surrogate.json",
                    path="schema-\ud800.json",
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


def test_compile_graph_rejects_malformed_top_level_collaborators_cleanly() -> None:
    with pytest.raises(TypeError, match="graph document must be a mapping"):
        compile_graph([])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="block_catalog must be a BlockCatalog"):
        compile_graph(
            {
                "apiVersion": "graphblocks.ai/v1",
                "kind": "Graph",
                "metadata": {"name": "bad-catalog"},
                "spec": {"nodes": {}},
            },
            block_catalog=object(),  # type: ignore[arg-type]
        )


def test_compile_graph_reports_non_string_api_versions_without_type_errors() -> None:
    plan = compile_graph(
        {
            "apiVersion": ["graphblocks.ai/v1"],
            "kind": "Graph",
            "metadata": {"name": "bad-version"},
            "spec": {"nodes": {}},
        }
    )

    assert [(item.code, item.path) for item in plan.diagnostics.diagnostics] == [
        ("GB0002", "$.apiVersion")
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


def test_compile_graph_preserves_spec_valid_arbitrary_size_integers() -> None:
    huge_integer = 10**5_000
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "huge-integer"},
        "spec": {
            "nodes": {
                "source": {
                    "block": "example.source@1",
                    "config": {"huge": huge_integer},
                }
            },
        },
    }

    plan = compile_graph(document, allow_unknown_blocks=True)

    assert plan.ok
    assert plan.normalized["spec"]["nodes"]["source"]["config"]["huge"] == huge_integer
    assert canonical_hash(plan.normalized).startswith("sha256:")


def test_compile_graph_reports_invalid_fields_with_arbitrary_size_integers() -> None:
    plan = compile_graph(
        {
            "apiVersion": "graphblocks.ai/v1",
            "kind": "Graph",
            "metadata": {"name": 10**5_000},
            "spec": {"nodes": {}},
        }
    )

    assert not plan.ok
    assert any(
        item.path == "$.metadata.name"
        for item in plan.diagnostics.diagnostics
    )


def test_compile_graph_reports_arbitrary_size_integer_enum_as_diagnostic() -> None:
    huge_integer = 10**5_000
    plan = compile_graph(
        {
            "apiVersion": "graphblocks.ai/v1",
            "kind": "Graph",
            "metadata": {"name": "huge-enum"},
            "spec": {
                "nodes": {"source": {"block": "example.source@1"}},
                "bindings": {
                    "tools": {
                        "createTicket": {
                            "definition": {
                                "name": "ticket.create",
                                "description": "Create a support ticket.",
                                "inputSchema": "schemas/TicketCreateRequest@1",
                            },
                            "implementation": {
                                "kind": "block",
                                "block": "example.tool@1",
                            },
                            "cancellation": huge_integer,
                        }
                    }
                },
            },
        },
        allow_unknown_blocks=True,
    )

    assert not plan.ok
    assert any(
        item.path == "$.spec.bindings.tools.createTicket.cancellation"
        for item in plan.diagnostics.diagnostics
    )


def test_compile_graph_reports_arbitrary_size_integer_combinator_as_diagnostic() -> None:
    plan = compile_graph(
        {
            "apiVersion": "graphblocks.ai/v1",
            "kind": "Graph",
            "metadata": {"name": "huge-combinator"},
            "spec": {
                "nodes": {
                    "source": {
                        "block": "example.source@1",
                        "flow": {"retry": 10**5_000},
                    }
                }
            },
        },
        allow_unknown_blocks=True,
    )

    assert not plan.ok
    assert any(
        item.path == "$.spec.nodes.source.flow.retry"
        for item in plan.diagnostics.diagnostics
    )


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
    assert manifest.content_digest() == "sha256:5cd2e5fe720b79e3f0585c0124025bb4cc0ae8a4521f4484e557590547f694c9"


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
