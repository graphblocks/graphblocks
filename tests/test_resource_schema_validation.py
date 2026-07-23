from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil

import pytest
import yaml

import graphblocks
from graphblocks.canonical import canonical_dumps, canonical_loads
import graphblocks.schema as schema_module
from graphblocks.schema import (
    RESOURCE_SCHEMA_PATHS,
    ResourceValidationError,
    SchemaManifestError,
    load_resource_schema,
    resource_schema_errors,
    validate_resource,
)


ROOT = Path(__file__).resolve().parents[1]


def test_resource_validation_api_is_available_from_supported_package_facade() -> None:
    assert graphblocks.resource_schema_errors is resource_schema_errors
    assert graphblocks.validate_resource is validate_resource
    assert graphblocks.RESOURCE_SCHEMA_PATHS is RESOURCE_SCHEMA_PATHS


def test_checked_in_resource_schemas_are_valid_draft_2020_12() -> None:
    loaded = {
        pair: load_resource_schema(*pair, schema_root=ROOT / "schemas")["$id"]
        for pair in RESOURCE_SCHEMA_PATHS
    }

    assert loaded == {
        ("graphblocks.ai/v1", "Graph"): "graphblocks.ai/v1/graph.schema.json",
        ("graphblocks.ai/v1", "PluginManifest"): "graphblocks.ai/v1/plugin-manifest.schema.json",
        ("graphblocks.ai/v1alpha3", "Graph"): "graphblocks.ai/v1alpha3/graph.schema.json",
        ("graphblocks.ai/v1alpha1", "Application"): "graphblocks.ai/v1alpha1/application.schema.json",
        ("graphblocks.ai/v1alpha1", "Binding"): "graphblocks.ai/v1alpha1/binding.schema.json",
        ("graphblocks.ai/v1alpha1", "PluginManifest"): "graphblocks.ai/v1alpha1/plugin-manifest.schema.json",
        (
            "graphblocks.ai/composition/v1alpha1",
            "GraphFragment",
        ): "graphblocks.ai/composition/v1alpha1/graph-fragment.schema.json",
    }


def test_v1alpha3_graph_schema_rejects_dotted_composition_import_alias() -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "dotted-import"},
        "spec": {
            "nodes": {},
            "composition": {
                "apiVersion": "graphblocks.ai/composition/v1alpha1",
                "imports": {"lib.v2": {"path": "fragment.yaml"}},
                "slots": {},
            },
        },
    }

    violations = resource_schema_errors(document, schema_root=ROOT / "schemas")

    assert [(item.path, item.keyword) for item in violations] == [
        ("$.spec.composition.imports", "pattern")
    ]


def test_stable_plugin_manifest_schema_requires_version() -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "PluginManifest",
        "metadata": {"name": "missing-version"},
        "spec": {"pluginId": "missing-version", "blocks": []},
    }

    violations = resource_schema_errors(document, schema_root=ROOT / "schemas")

    assert [(item.path, item.keyword) for item in violations] == [
        ("$.spec", "required")
    ]


@pytest.mark.parametrize(
    ("api_version", "kind"),
    [
        ("graphblocks.ai/v1", "Graph"),
        ("graphblocks.ai/v1alpha3", "Graph"),
        ("graphblocks.ai/composition/v1alpha1", "GraphFragment"),
    ],
)
@pytest.mark.parametrize(
    ("major_version", "expected_valid"),
    [("4294967295", True), ("4294967296", False)],
)
def test_resource_schemas_match_schema_id_u32_version_bounds(
    api_version: str,
    kind: str,
    major_version: str,
    expected_valid: bool,
) -> None:
    spec: dict[str, object] = {
        "nodes": {"worker": {"block": f"example.worker@{major_version}"}},
    }
    if kind == "GraphFragment":
        spec["interface"] = {"inputs": {}, "outputs": {}}
    document = {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": "schema-id-version-bound"},
        "spec": spec,
    }

    violations = resource_schema_errors(document, schema_root=ROOT / "schemas")

    if expected_valid:
        assert violations == ()
    else:
        assert [(item.path, item.keyword) for item in violations] == [
            ("$.spec.nodes.worker.block", "pattern")
        ]


@pytest.mark.parametrize("api_version", ["graphblocks.ai/v1", "graphblocks.ai/v1alpha1"])
@pytest.mark.parametrize("direction", ["inputs", "outputs"])
def test_plugin_manifest_schema_rejects_noncanonical_endpoint_names(
    api_version: str,
    direction: str,
) -> None:
    document = {
        "apiVersion": api_version,
        "kind": "PluginManifest",
        "metadata": {"name": "bad-endpoint"},
        "spec": {
            "pluginId": "bad-endpoint",
            "version": "1.0.0",
            "blocks": [
                {
                    "typeId": "bad.endpoint",
                    "version": 1,
                    "capabilities": [],
                    "configSchema": {"type": "object"},
                    direction: [{"name": "nested.value", "type": "Any"}],
                }
            ],
        },
    }

    violations = resource_schema_errors(document, schema_root=ROOT / "schemas")

    assert [(item.path, item.keyword) for item in violations] == [
        (f"$.spec.blocks[0].{direction}[0].name", "pattern")
    ]


def test_python_resource_validator_matches_shared_tck_cases() -> None:
    cases = canonical_loads((ROOT / "tck" / "schema" / "resources.json").read_text(encoding="utf-8"))

    for case in cases:
        errors = resource_schema_errors(case["document"], schema_root=ROOT / "schemas")
        actual = [
            {"code": error.code, "path": error.path, "keyword": error.keyword}
            for error in errors
        ]

        assert (not errors) is case["expected"]["valid"], case["name"]
        assert actual == case["expected"].get("errors", []), case["name"]


def test_shipped_examples_satisfy_each_matching_resource_schema() -> None:
    validated: list[str] = []
    roots = (ROOT / "examples", ROOT / "src" / "graphblocks" / "data")
    for root in roots:
        for path in sorted(root.rglob("*.yaml")):
            for index, document in enumerate(yaml.safe_load_all(path.read_text(encoding="utf-8"))):
                if not isinstance(document, dict):
                    continue
                pair = (document.get("apiVersion"), document.get("kind"))
                if pair not in RESOURCE_SCHEMA_PATHS:
                    continue

                errors = resource_schema_errors(document, schema_root=ROOT / "schemas")
                assert not errors, f"{path} document {index}: {errors}"
                validated.append(f"{path.relative_to(ROOT)}#{index}")

    assert len(validated) >= 30


def test_default_lookup_prefers_schemas_embedded_in_installed_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "installed" / "graphblocks"
    relative_path = RESOURCE_SCHEMA_PATHS[("graphblocks.ai/v1alpha3", "Graph")]
    installed_schema = package_root / "schemas" / relative_path
    installed_schema.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "schemas" / relative_path, installed_schema)
    monkeypatch.setattr(schema_module.resources, "files", lambda package: package_root)

    validate_resource(
        {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "installed-schema"},
            "spec": {"nodes": {}},
        }
    )


def test_resource_validation_error_preserves_structured_violations() -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "Binding",
        "metadata": {"name": "missing-resources"},
        "spec": {},
    }

    with pytest.raises(ResourceValidationError) as captured:
        validate_resource(document, schema_root=ROOT / "schemas")

    assert [(item.path, item.keyword) for item in captured.value.violations] == [
        ("$.spec", "required")
    ]
    assert "GB0014 $.spec" in str(captured.value)


def test_resource_validation_rejects_excessive_predicate_depth_without_recursing() -> None:
    required_when: object = {"phase": "resumed"}
    for _ in range(200):
        required_when = {"not": required_when}
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "PluginManifest",
        "metadata": {"name": "deep-predicate"},
        "spec": {
            "pluginId": "deep-predicate",
            "version": "1.0.0",
            "blocks": [
                {
                    "typeId": "deep.predicate",
                    "version": 1,
                    "inputs": [],
                    "outputs": [
                        {
                            "name": "value",
                            "type": "schemas/Any@1",
                            "required": False,
                            "requiredWhen": required_when,
                        }
                    ],
                }
            ],
        },
    }

    violations = resource_schema_errors(document, schema_root=ROOT / "schemas")
    assert len(violations) == 1
    assert violations[0].code == "GB0014"
    assert violations[0].keyword == "maxDepth"
    with pytest.raises(ResourceValidationError, match="resource nesting"):
        validate_resource(document, schema_root=ROOT / "schemas")


def test_resource_schema_errors_are_stable_across_mapping_order() -> None:
    first = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "stable-errors", "second": 2, "first": 1},
        "spec": {"nodes": {}, "zeta": True, "alpha": True},
    }
    second = deepcopy(first)
    second["metadata"] = {"first": 1, "second": 2, "name": "stable-errors"}
    second["spec"] = {"alpha": True, "zeta": True, "nodes": {}}

    assert resource_schema_errors(first, schema_root=ROOT / "schemas") == resource_schema_errors(
        second,
        schema_root=ROOT / "schemas",
    )


def test_excessive_depth_diagnostics_are_stable_across_mapping_order() -> None:
    def deep_branch() -> object:
        value: object = None
        for _ in range(65):
            value = {"next": value}
        return value

    left = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "stable-depth"},
        "spec": {
            "nodes": {},
            "extensions": {"a": deep_branch(), "b": deep_branch()},
        },
    }
    right = deepcopy(left)
    right["spec"]["extensions"] = {
        "b": deep_branch(),
        "a": deep_branch(),
    }

    left_violations = resource_schema_errors(left, schema_root=ROOT / "schemas")
    right_violations = resource_schema_errors(right, schema_root=ROOT / "schemas")

    assert left_violations == right_violations
    assert left_violations[0].keyword == "maxDepth"
    assert left_violations[0].path.startswith("$.spec.extensions.a")
    assert graphblocks.compile_graph(left).graph_hash == graphblocks.compile_graph(
        right
    ).graph_hash


def test_invalid_authoritative_schema_fails_closed(tmp_path: Path) -> None:
    relative_path = RESOURCE_SCHEMA_PATHS[("graphblocks.ai/v1alpha3", "Graph")]
    schema_path = tmp_path / relative_path
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text(
        '{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        '"$id":"graphblocks.ai/v1alpha3/graph.schema.json","type":"not-a-json-schema-type"}',
        encoding="utf-8",
    )

    with pytest.raises(SchemaManifestError, match="not valid draft 2020-12"):
        load_resource_schema("graphblocks.ai/v1alpha3", "Graph", schema_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        (
            "$id",
            "graphblocks.ai/v1alpha3/not-graph.schema.json",
            "must declare expected \\$id",
        ),
        (
            "$schema",
            "http://json-schema.org/draft-07/schema#",
            "must declare draft 2020-12",
        ),
    ),
)
def test_resource_schema_loader_rejects_mismatched_schema_identity(
    tmp_path: Path,
    field: str,
    replacement: str,
    message: str,
) -> None:
    relative_path = RESOURCE_SCHEMA_PATHS[("graphblocks.ai/v1alpha3", "Graph")]
    schema_path = tmp_path / relative_path
    schema_path.parent.mkdir(parents=True)
    document = canonical_loads(
        (ROOT / "schemas" / relative_path).read_text(encoding="utf-8")
    )
    document[field] = replacement
    schema_path.write_text(
        canonical_dumps(document),
        encoding="utf-8",
    )

    with pytest.raises(SchemaManifestError, match=message):
        load_resource_schema(
            "graphblocks.ai/v1alpha3",
            "Graph",
            schema_root=tmp_path,
        )


def test_resource_schema_loader_rejects_schema_symlink_escape(
    tmp_path: Path,
    symlink_or_skip,
) -> None:
    relative_path = RESOURCE_SCHEMA_PATHS[("graphblocks.ai/v1alpha3", "Graph")]
    schema_root = tmp_path / "schemas"
    schema_path = schema_root / relative_path
    schema_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.schema.json"
    outside.write_text(
        (ROOT / "schemas" / relative_path).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    symlink_or_skip(schema_path, outside)

    with pytest.raises(SchemaManifestError, match="non-symlinked file"):
        load_resource_schema(
            "graphblocks.ai/v1alpha3",
            "Graph",
            schema_root=schema_root,
        )
