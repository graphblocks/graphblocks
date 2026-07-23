from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from graphblocks import canonical_hash, compile_graph, migrate_document, normalize_graph
from graphblocks.canonical import canonical_loads
from graphblocks.cli import main
from graphblocks.migration import GRAPH_API_VERSION, MigrationError
from graphblocks.schema import validate_resource


ROOT = Path(__file__).resolve().parents[1]


def test_shared_migration_cases_are_exact_deterministic_and_non_mutating() -> None:
    cases = canonical_loads(
        (ROOT / "tck" / "migration" / "cases.json").read_text(encoding="utf-8")
    )

    for case in cases:
        source = deepcopy(case["document"])
        expected = case["expected"]
        if "error" in expected:
            with pytest.raises(MigrationError) as captured:
                migrate_document(case["document"])
            assert captured.value.code == expected["error"]["code"], case["name"]
            assert captured.value.path == expected["error"]["path"], case["name"]
        else:
            migrated = migrate_document(case["document"])
            assert migrated == expected["document"], case["name"]
            assert canonical_hash(migrated) == canonical_hash(
                migrate_document(case["document"])
            ), case["name"]
            validate_resource(migrated, schema_root=ROOT / "schemas")
            if "normalized" in expected:
                assert normalize_graph(case["document"]) == expected["normalized"], case["name"]
        assert case["document"] == source, case["name"]


def test_alpha_and_native_v1_graphs_compile_with_equivalent_semantics() -> None:
    stable = {
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "equivalent"},
        "spec": {"nodes": {}},
    }
    alpha = deepcopy(stable)
    alpha["apiVersion"] = "graphblocks.ai/v1alpha3"

    stable_plan = compile_graph(stable)
    alpha_plan = compile_graph(alpha)

    assert stable_plan.ok
    assert alpha_plan.ok
    assert stable_plan.normalized["spec"] == alpha_plan.normalized["spec"]
    assert stable_plan.diagnostics.to_list() == alpha_plan.diagnostics.to_list()
    assert stable_plan.graph_hash != alpha_plan.graph_hash
    assert (
        alpha_plan.normalized["metadata"]["annotations"]["graphblocks.ai/migratedFrom"]
        == "graphblocks.ai/v1alpha3"
    )


def test_preview_alpha_graph_is_not_relabelled_as_stable_v1() -> None:
    preview = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "preview-state"},
        "spec": {"nodes": {}, "state": {"attackerDefinedFutureField": True}},
    }

    with pytest.raises(MigrationError) as migrated:
        migrate_document(preview)
    assert migrated.value.code == "GB0002"
    assert migrated.value.path == "$.spec"
    with pytest.raises(MigrationError, match="cannot be represented by the stable wire schema"):
        normalize_graph(preview)

    plan = compile_graph(preview)

    assert plan.ok
    assert plan.normalized["apiVersion"] == "graphblocks.ai/v1alpha3"
    assert "graphblocks.ai/migratedFrom" not in plan.normalized["metadata"].get(
        "annotations", {}
    )


def test_current_stable_resources_must_already_satisfy_the_stable_schema() -> None:
    invalid_graph = {
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "invalid-stable-graph"},
        "spec": {"nodes": {}, "state": {"previewOnly": True}},
    }
    invalid_plugin = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "PluginManifest",
        "metadata": {"name": "invalid-stable-plugin"},
        "spec": {
            "pluginId": "example.invalid",
            "version": "1.0.0",
            "blocks": [{"typeId": "example.invalid", "version": "1.0.0"}],
        },
    }

    with pytest.raises(MigrationError) as graph_error:
        migrate_document(invalid_graph)
    assert graph_error.value.code == "GB0002"
    assert graph_error.value.path == "$.spec"

    with pytest.raises(MigrationError) as plugin_error:
        migrate_document(invalid_plugin)
    assert plugin_error.value.code == "GB2018"
    assert plugin_error.value.path.startswith("$.spec.blocks[0]")


def test_migration_rejects_invalid_root_and_recursive_documents_cleanly() -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        migrate_document([])  # type: ignore[arg-type]

    recursive: dict[str, object] = {
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
    }
    recursive["spec"] = recursive
    with pytest.raises(ValueError, match="canonical JSON values"):
        migrate_document(recursive)


def test_migration_normalizes_hostile_mapping_traversal_errors() -> None:
    class ExplodingMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError("hostile lookup")

        def __iter__(self) -> Iterator[str]:
            return iter(("apiVersion",))

        def __len__(self) -> int:
            return 1

    with pytest.raises(
        ValueError,
        match="migration document must contain canonical JSON values",
    ) as captured:
        migrate_document(ExplodingMapping())  # type: ignore[arg-type]

    assert isinstance(captured.value.__cause__, RuntimeError)


def test_migration_reports_non_string_resource_versions() -> None:
    document = {
        "apiVersion": ["graphblocks.ai/v1"],
        "kind": "Graph",
        "metadata": {"name": "bad-version"},
        "spec": {"nodes": {}},
    }

    with pytest.raises(MigrationError) as captured:
        migrate_document(document)

    assert captured.value.code == "GB0002"
    assert captured.value.path == "$.apiVersion"


def test_migrate_cli_emits_normalized_v1_graph(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "legacy"},
                "spec": {
                    "nodes": {
                        "echo": {
                            "block": "example.echo@1",
                            "inputs": {"message": "$input.message"},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["migrate", str(path)]) == 0
    migrated = yaml.safe_load(capsys.readouterr().out)

    assert migrated["apiVersion"] == GRAPH_API_VERSION
    assert "inputs" not in migrated["spec"]["nodes"]["echo"]
    assert migrated["spec"]["edges"] == [
        {"from": "$input.message", "to": "echo.message"}
    ]


def test_migrate_cli_rejects_unknown_stable_resource_versions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "future.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "graphblocks.ai/v2",
                "kind": "Graph",
                "metadata": {"name": "future"},
                "spec": {"nodes": {}},
            }
        ),
        encoding="utf-8",
    )

    assert main(["migrate", str(path)]) == 1
    assert "GB0002 $.apiVersion" in capsys.readouterr().out


@pytest.mark.parametrize("api_version", [GRAPH_API_VERSION, "graphblocks.ai/v1alpha3"])
def test_direct_compile_validates_migrated_source_before_normalization(
    api_version: str,
) -> None:
    graph = {
        "apiVersion": api_version,
        "kind": "Graph",
        "metadata": {"name": "closed-source"},
        "spec": {
            "nodes": {},
            "edges": [
                {
                    "from": "$input.value",
                    "to": "$output.value",
                    "mystery": True,
                }
            ],
        },
    }

    plan = compile_graph(graph)

    assert any(
        item.code == "GB0014" and item.path == "$.spec.edges[0]"
        for item in plan.diagnostics.diagnostics
    )
