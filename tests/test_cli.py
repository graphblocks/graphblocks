from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
from types import SimpleNamespace
import yaml

import graphblocks.cli as cli_module
import graphblocks.plugins as plugins_module
from graphblocks.cli import _loads_strict_json, main
from graphblocks.canonical import (
    canonical_dumps,
    canonical_hash,
    canonical_loads,
    normalize_graph,
)
from graphblocks.compiler import compile_graph
from graphblocks.diagnostics import Diagnostic, DiagnosticSet
from graphblocks.plugins import PluginRegistry
from graphblocks.runtime import SQLiteExecutionJournal
from graphblocks.run_store import SQLiteRunStore

RELEASE_DIGEST = "sha256:" + ("1" * 64)
SIGNATURE_DIGEST = "sha256:" + ("3" * 64)


def test_cli_strict_json_preserves_arbitrary_precision_numbers() -> None:
    payload = _loads_strict_json("--input-json", '{"huge":1e400}')

    assert canonical_dumps(payload) == '{"huge":1e+400}'


def _deployment_plan_payload(
    graph: dict[str, object],
    *,
    graph_hash: str | None = None,
) -> dict[str, object]:
    resolved_graph_hash = graph_hash or compile_graph(graph).graph_hash
    deployment_spec_hash = "sha256:" + ("5" * 64)
    resolved_binding_hash = "sha256:" + ("6" * 64)
    target_capability_hash = "sha256:" + ("7" * 64)
    physical_plan = {
        "graphHash": resolved_graph_hash,
        "packageLockHash": None,
        "defaultTarget": None,
        "targets": {},
        "placements": [],
    }
    plan_hash = canonical_hash(
        {
            "release_digest": RELEASE_DIGEST,
            "deployment_revision_id": "revision-1",
            "graph_hash": resolved_graph_hash,
            "package_lock_hash": None,
            "targets": [],
            "placements": [],
            "default_target": None,
        }
    )
    revision_content_digest = canonical_hash(
        {
            "release_digest": RELEASE_DIGEST,
            "deployment_spec_hash": deployment_spec_hash,
            "physical_plan_hash": plan_hash,
            "resolved_binding_hash": resolved_binding_hash,
            "target_capability_hash": target_capability_hash,
        }
    )
    return {
        "ok": True,
        "releaseDigest": RELEASE_DIGEST,
        "deploymentRevisionId": "revision-1",
        "deploymentSpecHash": deployment_spec_hash,
        "planHash": plan_hash,
        "deploymentRevision": {
            "revisionId": "revision-1",
            "releaseDigest": RELEASE_DIGEST,
            "deploymentSpecHash": deployment_spec_hash,
            "physicalPlanHash": plan_hash,
            "resolvedBindingHash": resolved_binding_hash,
            "targetCapabilityHash": target_capability_hash,
            "contentDigest": revision_content_digest,
        },
        "plan": physical_plan,
    }


def _render_plan_payload() -> dict[str, object]:
    release_digest = "sha256:" + ("8" * 64)
    deployment_spec_hash = "sha256:" + ("9" * 64)
    resolved_binding_hash = "sha256:" + ("a" * 64)
    target_capability_hash = "sha256:" + ("b" * 64)
    target = {
        "kind": "service",
        "executionHost": "rust",
        "capabilities": ["graph.coordinator"],
        "effects": [],
        "packageLock": None,
        "image": "registry.example.com/graphblocks/control@sha256:control",
    }
    plan = {
        "graphHash": "sha256:graph-turn",
        "packageLockHash": None,
        "defaultTarget": "control",
        "targets": {"control": target},
        "placements": [],
    }
    plan_hash = canonical_hash(
        {
            "release_digest": release_digest,
            "deployment_revision_id": "rev-1",
            "graph_hash": plan["graphHash"],
            "package_lock_hash": None,
            "targets": [
                {
                    "target_id": "control",
                    "kind": target["kind"],
                    "execution_host": target["executionHost"],
                    "capabilities": target["capabilities"],
                    "effects": target["effects"],
                    "package_lock": target["packageLock"],
                    "image": target["image"],
                }
            ],
            "placements": [],
            "default_target": "control",
        }
    )
    revision_content_digest = canonical_hash(
        {
            "release_digest": release_digest,
            "deployment_spec_hash": deployment_spec_hash,
            "physical_plan_hash": plan_hash,
            "resolved_binding_hash": resolved_binding_hash,
            "target_capability_hash": target_capability_hash,
        }
    )
    return {
        "ok": True,
        "deploymentId": "support-production",
        "deploymentRevisionId": "rev-1",
        "releaseDigest": release_digest,
        "deploymentSpecHash": deployment_spec_hash,
        "planHash": plan_hash,
        "deploymentRevision": {
            "revisionId": "rev-1",
            "releaseDigest": release_digest,
            "deploymentSpecHash": deployment_spec_hash,
            "physicalPlanHash": plan_hash,
            "resolvedBindingHash": resolved_binding_hash,
            "targetCapabilityHash": target_capability_hash,
            "contentDigest": revision_content_digest,
        },
        "plan": plan,
    }


def test_validate_cli_accepts_valid_graph(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-valid"},
        "spec": {
            "interface": {"outputs": {"result": "graphblocks.ai/ModelResponse@1"}},
            "nodes": {"value": {"block": "model.generate@1"}},
            "edges": [{"from": "value.response", "to": "$output.result"}],
        },
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["validate", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "OK"


def test_plan_cli_prints_hash(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-plan"},
        "spec": {"nodes": {"value": {"block": "model.generate@1"}}},
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["plan", str(path)]) == 0
    assert '"hash": "sha256:' in capsys.readouterr().out


def test_validate_cli_rejects_unknown_blocks_without_explicit_discovery_mode(
    tmp_path, capsys
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-closed-world"},
        "spec": {"nodes": {"custom": {"block": "vendor.custom@1"}}},
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["validate", str(path), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(diagnostic["code"] == "GB1022" for diagnostic in payload["diagnostics"])


def test_validate_cli_allows_unknown_blocks_only_in_explicit_discovery_mode(
    tmp_path, capsys
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-open-discovery"},
        "spec": {"nodes": {"custom": {"block": "vendor.custom@1"}}},
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["validate", str(path), "--allow-unknown-blocks"]) == 0
    assert capsys.readouterr().out.strip() == "OK"


def test_validate_cli_applies_versioned_resource_schemas_to_non_graph_documents(
    tmp_path, capsys
) -> None:
    application = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "invalid-application"},
        "spec": {},
    }
    path = tmp_path / "application.yaml"
    path.write_text(yaml.safe_dump(application), encoding="utf-8")

    assert main(["validate", str(path), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(
        diagnostic["code"] == "GB0014"
        for diagnostic in payload["diagnostics"]
    )


def test_validate_cli_rejects_unknown_versions_of_schema_backed_resource_kinds(
    tmp_path, capsys
) -> None:
    application = {
        "apiVersion": "graphblocks.ai/v9",
        "kind": "Application",
        "metadata": {"name": "unknown-version"},
        "spec": {"surface": {"kind": "http", "protocol": "graphblocks.app.v1"}},
    }
    path = tmp_path / "application.yaml"
    path.write_text(yaml.safe_dump(application), encoding="utf-8")

    assert main(["validate", str(path), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert [diagnostic["code"] for diagnostic in payload["diagnostics"]] == [
        "GB0013"
    ]


def test_plan_cli_requires_explicit_discovery_mode_for_unknown_blocks(
    tmp_path, capsys
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-plan-closed-world"},
        "spec": {"nodes": {"custom": {"block": "vendor.custom@1"}}},
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["plan", str(path)]) == 1
    closed_payload = json.loads(capsys.readouterr().out)
    assert any(diagnostic["code"] == "GB1022" for diagnostic in closed_payload["diagnostics"])

    assert main(["plan", str(path), "--allow-unknown-blocks"]) == 0
    open_payload = json.loads(capsys.readouterr().out)
    assert open_payload["ok"] is True
    assert open_payload["diagnostics"] == []


def test_validate_and_plan_ignore_installed_plugins_without_explicit_opt_in(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "PluginManifest",
        "metadata": {"name": "com.example.ambient", "version": "1.0.0"},
        "spec": {
            "pluginId": "com.example.ambient",
            "version": "1.0.0",
            "blocks": [
                {
                    "typeId": "ambient.custom",
                    "version": 1,
                    "capabilities": [],
                    "configSchema": {"type": "object"},
                }
            ],
        },
    }
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "ambient-plugin-closure"},
        "spec": {"nodes": {"custom": {"block": "ambient.custom@1"}}},
    }
    manifest_path = tmp_path / "graphblocks-plugin.yaml"
    graph_path = tmp_path / "graph.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    graph_path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    distribution = SimpleNamespace(
        files=(Path("graphblocks-plugin.yaml"),),
        locate_file=lambda _file: manifest_path,
    )
    monkeypatch.setattr(
        plugins_module.importlib.metadata,
        "distributions",
        lambda: (distribution,),
    )
    monkeypatch.setattr(
        plugins_module.importlib.metadata,
        "entry_points",
        lambda: SimpleNamespace(select=lambda **_kwargs: ()),
    )

    for command in ("validate", "plan"):
        default_argv = [command, str(graph_path)]
        if command == "validate":
            default_argv.append("--json")
        assert main(default_argv) == 1
        default_payload = json.loads(capsys.readouterr().out)
        assert default_payload["ok"] is False
        assert [
            diagnostic["code"] for diagnostic in default_payload["diagnostics"]
        ] == ["GB1022"]

        assert main([*default_argv, "--discover-installed-plugins"]) == 0
        discovered_payload = json.loads(capsys.readouterr().out)
        assert discovered_payload["ok"] is True
        assert discovered_payload["diagnostics"] == []

    assert main(["plugins", "list", "--json"]) == 0
    plugins_payload = json.loads(capsys.readouterr().out)
    assert "com.example.ambient" in {
        plugin["pluginId"] for plugin in plugins_payload["plugins"]
    }


def test_packages_cli_lists_catalog(capsys) -> None:
    assert main(["packages", "list"]) == 0
    assert "graphblocks-core" in capsys.readouterr().out


def test_packages_cli_doctor_accepts_catalog(capsys) -> None:
    assert main(["packages", "doctor"]) == 0
    assert capsys.readouterr().out.strip() == "OK"


def test_packages_cli_doctor_cross_checks_repo_manifests(capsys) -> None:
    assert main(["packages", "doctor", "--root", "."]) == 0
    assert capsys.readouterr().out.strip() == "OK"


def test_packages_audit_cli_accepts_repo_manifests(capsys) -> None:
    assert main(["packages", "audit", "--root", "."]) == 0

    assert capsys.readouterr().out.strip() == "OK"


def test_packages_audit_cli_reports_blocked_dependency(tmp_path, capsys) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "unsafe-python"
version = "0.1.0"
license = "Apache-2.0"
dependencies = ["vulnerable-sdk>=0"]
""".strip(),
        encoding="utf-8",
    )

    assert main(["packages", "audit", "--root", str(tmp_path), "--blocked-dependency", "vulnerable-sdk", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["diagnostics"][0]["code"] == "PackageBlockedDependency"
    assert payload["diagnostics"][0]["path"] == "$.pyproject.toml.project.dependencies[0]"


def test_packages_wheel_matrix_cli_emits_release_gate_payload(capsys) -> None:
    assert main(["packages", "wheel-matrix", "--root", ".", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    runtime = next(target for target in payload["targets"] if target["distribution"] == "graphblocks-runtime")

    assert payload["ok"] is True
    assert payload["contentDigest"].startswith("sha256:")
    assert payload["targetCount"] == len(payload["targets"])
    assert runtime["kind"] == "native_extension"
    assert runtime["python_versions"] == ["3.11", "3.12"]


def test_schemas_manifest_cli_emits_deterministic_manifest(capsys) -> None:
    assert main(["schemas", "manifest", "schemas"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["manifestVersion"] == 1
    assert payload["contentDigest"] == "sha256:5cd2e5fe720b79e3f0585c0124025bb4cc0ae8a4521f4484e557590547f694c9"
    assert [entry["schemaId"] for entry in payload["schemas"]] == [
        "graphblocks.ai/composition/v1alpha1/graph-fragment.schema.json",
        "graphblocks.ai/v1/graph.schema.json",
        "graphblocks.ai/v1/plugin-manifest.schema.json",
        "graphblocks.ai/v1alpha1/application.schema.json",
        "graphblocks.ai/v1alpha1/binding.schema.json",
        "graphblocks.ai/v1alpha1/plugin-manifest.schema.json",
        "graphblocks.ai/v1alpha3/graph.schema.json",
    ]


def test_schemas_manifest_cli_defaults_to_packaged_schemas(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    package_root = tmp_path / "installed-graphblocks"
    schema_root = package_root / "schemas" / "graphblocks.ai" / "v1alpha1"
    schema_root.mkdir(parents=True)
    (schema_root / "example.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "graphblocks.ai/v1alpha1/example.schema.json",
                "type": "object",
            }
        ),
        encoding="utf-8",
    )
    working_directory = tmp_path / "outside-repository"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)
    monkeypatch.setattr(cli_module.resources, "files", lambda package: package_root)

    assert main(["schemas", "manifest"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [entry["schemaId"] for entry in payload["schemas"]] == [
        "graphblocks.ai/v1alpha1/example.schema.json"
    ]


def test_lock_cli_emits_graph_hash_and_default_package_closure(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-lock"},
        "spec": {"nodes": {"value": {"block": "model.generate@1"}}},
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["lock", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["lockVersion"] == 1
    assert payload["graph"]["id"] == "cli-lock"
    assert payload["graph"]["graphHash"].startswith("sha256:")
    assert payload["graph"]["schemaVersion"] == "graphblocks.ai/v1"
    assert payload["packageLockHash"].startswith("sha256:")
    assert payload["packageCatalogVersion"] == 5
    assert payload["artifacts"] == ["graphblocks"]
    assert "graphblocks-core" in {package["name"] for package in payload["packages"]}


def test_lock_cli_writes_output_file(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-lock-file"},
        "spec": {"nodes": {"value": {"block": "model.generate@1"}}},
    }
    path = tmp_path / "graph.yaml"
    output = tmp_path / "graphblocks.lock.json"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["lock", str(path), "--output", str(output)]) == 0

    assert capsys.readouterr().out == ""
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["graph"]["id"] == "cli-lock-file"
    assert payload["artifacts"] == ["graphblocks"]
    assert "graphblocks-core" in {package["name"] for package in payload["packages"]}


def test_cli_materialized_text_artifacts_use_exact_utf8_bytes(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "exact-output-bytes"},
        "spec": {"nodes": {}},
    }
    source = tmp_path / "graph.yaml"
    source.write_bytes(yaml.safe_dump(graph).encode("utf-8"))
    composition_output = tmp_path / "composition.yaml"
    report_output = tmp_path / "composition-report.json"
    lock_output = tmp_path / "graphblocks.lock.json"

    def reject_platform_text_translation(*args, **kwargs):
        raise AssertionError("deterministic CLI artifacts must not use Path.write_text")

    monkeypatch.setattr(Path, "write_text", reject_platform_text_translation)

    assert main(
        [
            "compose",
            str(source),
            "--output",
            str(composition_output),
            "--report",
            str(report_output),
        ]
    ) == 0
    assert main(["lock", str(source), "--output", str(lock_output)]) == 0
    assert capsys.readouterr().out == ""
    for output in (composition_output, report_output, lock_output):
        assert b"\r\n" not in output.read_bytes()


def test_run_cli_executes_in_process_runtime(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Echo {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["run", str(path), "--input-json", '{"message":{"text":"hi"}}']) == 0
    assert '"prompt": "Echo hi"' in capsys.readouterr().out


def test_run_cli_reports_compile_errors_and_closes_sqlite_resources(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-run"},
        "spec": {"nodes": {"missing": {"block": "missing.block@1"}}},
    }
    source = tmp_path / "graph.yaml"
    source.write_text(yaml.safe_dump(graph), encoding="utf-8")
    run_store = tmp_path / "runs.sqlite3"
    journal_store = tmp_path / "journal.sqlite3"

    assert main(
        [
            "run",
            str(source),
            "--run-store",
            str(run_store),
            "--journal-store",
            str(journal_store),
        ]
    ) == 1

    output = capsys.readouterr().out
    assert output.startswith("runtime execution failed:")
    assert "Traceback" not in output
    for database in (run_store, journal_store):
        if database.exists():
            database.unlink()


def test_plan_cli_does_not_fail_for_plugin_warning(tmp_path, capsys, monkeypatch) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "plugin-warning"},
        "spec": {"nodes": {}},
    }
    source = tmp_path / "graph.yaml"
    source.write_text(yaml.safe_dump(graph), encoding="utf-8")
    registry = PluginRegistry(
        (),
        DiagnosticSet(
            (
                Diagnostic(
                    "GB2999",
                    "optional plugin metadata was ignored",
                    "$.plugins",
                    "warning",
                ),
            )
        ),
    )
    monkeypatch.setattr(cli_module, "discover_plugins", lambda *args, **kwargs: registry)

    assert main(["plan", str(source)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["diagnostics"][0]["severity"] == "warning"


def test_run_cli_rejects_non_standard_input_json_constants(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-run"},
        "spec": {"nodes": {}},
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["run", str(path), "--input-json", '{"score": NaN}']) == 1
    assert "--input-json must be valid strict JSON" in capsys.readouterr().out


def test_run_cli_persists_sqlite_run_and_journal_stores(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-run-persisted"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Persisted {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    graph_path = tmp_path / "graph.yaml"
    run_store_path = tmp_path / "runs.sqlite3"
    journal_store_path = tmp_path / "journal.sqlite3"
    graph_path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert (
        main(
            [
                "run",
                str(graph_path),
                "--input-json",
                '{"message":{"text":"hello"}}',
                "--run-id",
                "run-cli-persisted-1",
                "--run-store",
                str(run_store_path),
                "--journal-store",
                str(journal_store_path),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["runId"] == "run-cli-persisted-1"
    assert payload["status"] == "succeeded"
    assert payload["outputs"] == {"prompt": "Persisted hello"}
    stored_runs = SQLiteRunStore(run_store_path)
    stored_run = stored_runs.get_run(payload["runId"])
    stored_runs.close()
    stored_journal = SQLiteExecutionJournal(journal_store_path, payload["runId"])

    assert stored_run.status == "succeeded"
    assert stored_run.inputs == {"message": {"text": "hello"}}
    assert stored_journal.terminal_kind == "run_succeeded"
    assert [record.kind for record in stored_journal.records] == [
        "run_started",
        "node_started",
        "node_succeeded",
        "run_succeeded",
    ]
    stored_journal.close()


def test_run_cli_persists_signed_deployment_plan_provenance(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-production-provenance"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Production {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    graph_path = tmp_path / "graph.yaml"
    deployment_plan_path = tmp_path / "deployment-plan.json"
    run_store_path = tmp_path / "runs.sqlite3"
    graph_path.write_text(yaml.safe_dump(graph), encoding="utf-8")
    deployment_plan_path.write_text(
        json.dumps(_deployment_plan_payload(graph)),
        encoding="utf-8",
    )

    assert main(
        [
            "run",
            str(graph_path),
            "--input-json",
            '{"message":{"text":"hello"}}',
            "--run-id",
            "run-cli-production-1",
            "--run-store",
            str(run_store_path),
            "--deployment-plan",
            str(deployment_plan_path),
            "--release-signature-digest",
            SIGNATURE_DIGEST,
        ]
    ) == 0
    capsys.readouterr()
    store = SQLiteRunStore(run_store_path)
    persisted = store.get_run("run-cli-production-1")
    store.close()

    assert persisted.deployment_provenance.canonical_value() == {
        "release_digest": RELEASE_DIGEST,
        "deployment_revision_id": "revision-1",
        "physical_plan_hash": _deployment_plan_payload(graph)["planHash"],
        "release_signature_digest": SIGNATURE_DIGEST,
    }


def test_run_cli_rejects_incomplete_production_provenance(tmp_path, capsys) -> None:
    graph_path = tmp_path / "graph.yaml"
    deployment_plan_path = tmp_path / "deployment-plan.json"
    graph_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "incomplete-production-provenance"},
                "spec": {"nodes": {}},
            }
        ),
        encoding="utf-8",
    )
    deployment_plan_path.write_text(
        json.dumps(
            {
                "ok": True,
                "releaseDigest": "sha256:release",
                "deploymentRevisionId": "revision-1",
            }
        ),
        encoding="utf-8",
    )

    assert main(
        [
            "run",
            str(graph_path),
            "--deployment-plan",
            str(deployment_plan_path),
            "--release-signature-digest",
            "sha256:signature",
        ]
    ) == 1
    assert "deploy plan payload planHash must be a non-empty string" in capsys.readouterr().out


def test_run_cli_rejects_noncanonical_production_digest(tmp_path, capsys) -> None:
    graph_path = tmp_path / "graph.yaml"
    deployment_plan_path = tmp_path / "deployment-plan.json"
    graph_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "noncanonical-production-digest"},
                "spec": {"nodes": {}},
            }
        ),
        encoding="utf-8",
    )
    deployment_plan_path.write_text(
        json.dumps(
            {
                "ok": True,
                "releaseDigest": "not-a-digest",
                "deploymentRevisionId": "revision-1",
                "planHash": "sha256:" + ("2" * 64),
                "deploymentRevision": {
                    "revisionId": "revision-1",
                    "releaseDigest": "not-a-digest",
                    "physicalPlanHash": "sha256:" + ("2" * 64),
                },
                "plan": {"graphHash": "sha256:" + ("3" * 64)},
            }
        ),
        encoding="utf-8",
    )

    assert main(
        [
            "run",
            str(graph_path),
            "--deployment-plan",
            str(deployment_plan_path),
            "--release-signature-digest",
            "sha256:" + ("4" * 64),
        ]
    ) == 1
    assert (
        "production deployment provenance release_digest must be a canonical sha256 digest"
        in capsys.readouterr().out
    )


def test_run_cli_rejects_tampered_physical_plan_hash(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "tampered-physical-plan"},
        "spec": {"nodes": {}},
    }
    graph_path = tmp_path / "graph.yaml"
    deployment_plan_path = tmp_path / "deployment-plan.json"
    graph_path.write_text(yaml.safe_dump(graph), encoding="utf-8")
    release_digest = "sha256:" + ("1" * 64)
    graph_hash = compile_graph(graph).graph_hash
    physical_plan = {
        "graphHash": graph_hash,
        "packageLockHash": None,
        "defaultTarget": None,
        "targets": {},
        "placements": [],
    }
    untampered_plan_hash = canonical_hash(
        {
            "release_digest": release_digest,
            "deployment_revision_id": "revision-1",
            "graph_hash": graph_hash,
            "package_lock_hash": None,
            "targets": [],
            "placements": [],
            "default_target": None,
        }
    )
    assert untampered_plan_hash != "sha256:" + ("2" * 64)
    deployment_plan_path.write_text(
        json.dumps(
            {
                "ok": True,
                "releaseDigest": release_digest,
                "deploymentRevisionId": "revision-1",
                "planHash": "sha256:" + ("2" * 64),
                "deploymentRevision": {
                    "revisionId": "revision-1",
                    "releaseDigest": release_digest,
                    "physicalPlanHash": "sha256:" + ("2" * 64),
                },
                "plan": physical_plan,
            }
        ),
        encoding="utf-8",
    )

    assert main(
        [
            "run",
            str(graph_path),
            "--deployment-plan",
            str(deployment_plan_path),
            "--release-signature-digest",
            "sha256:" + ("3" * 64),
        ]
    ) == 1
    assert "deploy plan payload planHash does not match plan content" in capsys.readouterr().out


def test_run_cli_rejects_tampered_deployment_revision_digest(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "tampered-deployment-revision"},
        "spec": {"nodes": {}},
    }
    graph_path = tmp_path / "graph.yaml"
    deployment_plan_path = tmp_path / "deployment-plan.json"
    graph_path.write_text(yaml.safe_dump(graph), encoding="utf-8")
    deployment_plan = _deployment_plan_payload(graph)
    deployment_revision = deployment_plan["deploymentRevision"]
    assert isinstance(deployment_revision, dict)
    deployment_revision["contentDigest"] = "sha256:" + ("8" * 64)
    deployment_plan_path.write_text(json.dumps(deployment_plan), encoding="utf-8")

    assert main(
        [
            "run",
            str(graph_path),
            "--deployment-plan",
            str(deployment_plan_path),
            "--release-signature-digest",
            SIGNATURE_DIGEST,
        ]
    ) == 1
    assert (
        "deploy plan payload deploymentRevision contentDigest does not match revision content"
        in capsys.readouterr().out
    )


def test_run_cli_rejects_deployment_plan_for_different_graph(tmp_path, capsys) -> None:
    graph_path = tmp_path / "graph.yaml"
    deployment_plan_path = tmp_path / "deployment-plan.json"
    graph_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "production-graph-mismatch"},
                "spec": {"nodes": {}},
            }
        ),
        encoding="utf-8",
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "production-graph-mismatch"},
        "spec": {"nodes": {}},
    }
    deployment_plan_path.write_text(
        json.dumps(
            _deployment_plan_payload(
                graph,
                graph_hash="sha256:" + ("4" * 64),
            )
        ),
        encoding="utf-8",
    )

    assert main(
        [
            "run",
            str(graph_path),
            "--deployment-plan",
            str(deployment_plan_path),
            "--release-signature-digest",
            SIGNATURE_DIGEST,
        ]
    ) == 1
    assert "deploy plan graphHash does not match the runtime graph" in capsys.readouterr().out


def test_run_cli_can_delegate_to_native_runtime_bridge(tmp_path, capsys, monkeypatch) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-native-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Native {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def run_stdlib_graph_json(graph_json: str, inputs_json: str) -> str:
        calls.append((json.loads(graph_json), json.loads(inputs_json)))
        return json.dumps(
            {
                "runId": "native-run-1",
                "status": "succeeded",
                "outputs": {"prompt": "Native ok"},
                "journal": [{"kind": "run_succeeded"}],
            }
        )

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            run_stdlib_graph_json=run_stdlib_graph_json,
        ),
    )
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["run", str(path), "--runtime", "native", "--input-json", '{"message":{"text":"ok"}}']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["runId"] == "native-run-1"
    assert payload["outputs"] == {"prompt": "Native ok"}
    assert calls == [(normalize_graph(graph), {"message": {"text": "ok"}})]


def test_run_cli_preserves_arbitrary_precision_input_for_native_runtime(
    tmp_path, capsys, monkeypatch
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-native-arbitrary-precision"},
        "spec": {"nodes": {}},
    }
    inputs: list[str] = []

    def run_stdlib_graph_json(graph_json: str, inputs_json: str) -> str:
        inputs.append(inputs_json)
        return (
            '{"runId":"native-run-arbitrary-precision","status":"succeeded",'
            '"outputs":{"huge":1e400},"journal":[{"kind":"run_succeeded"}]}'
        )

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            run_stdlib_graph_json=run_stdlib_graph_json,
        ),
    )
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert (
        main(
            [
                "run",
                str(path),
                "--runtime",
                "native",
                "--input-json",
                '{"huge":1e400}',
            ]
        )
        == 0
    )

    output = canonical_loads(capsys.readouterr().out)
    assert output["status"] == "succeeded"
    assert canonical_dumps(output["outputs"]) == '{"huge":1e+400}'
    assert inputs == ['{"huge":1e+400}']


def test_run_cli_passes_requested_run_id_to_native_runtime_bridge(tmp_path, capsys, monkeypatch) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-native-run-id"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Native {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    calls: list[tuple[dict[str, object], dict[str, object], dict[str, object]]] = []

    def run_stdlib_graph_with_options_json(
        graph_json: str,
        inputs_json: str,
        options_json: str,
    ) -> str:
        graph_payload = json.loads(graph_json)
        inputs_payload = json.loads(inputs_json)
        options_payload = json.loads(options_json)
        calls.append((graph_payload, inputs_payload, options_payload))
        return json.dumps(
            {
                "runId": options_payload["runId"],
                "status": "succeeded",
                "outputs": {"prompt": "Native requested"},
                "journal": [{"kind": "run_succeeded"}],
            }
        )

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            run_stdlib_graph_with_options_json=run_stdlib_graph_with_options_json,
        ),
    )
    path = tmp_path / "graph.yaml"
    deployment_plan_path = tmp_path / "deployment-plan.json"
    run_store_path = tmp_path / "native-runs.sqlite3"
    journal_store_path = tmp_path / "native-journal.sqlite3"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")
    deployment_plan_path.write_text(
        json.dumps(_deployment_plan_payload(graph)),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "run",
                str(path),
                "--runtime",
                "native",
                "--run-id",
                "run-native-requested-1",
                "--run-store",
                str(run_store_path),
                "--journal-store",
                str(journal_store_path),
                "--deployment-plan",
                str(deployment_plan_path),
                "--release-signature-digest",
                SIGNATURE_DIGEST,
                "--input-json",
                '{"message":{"text":"ok"}}',
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["runId"] == "run-native-requested-1"
    assert payload["outputs"] == {"prompt": "Native requested"}
    assert calls == [
        (
            normalize_graph(graph),
            {"message": {"text": "ok"}},
            {
                "deploymentProvenance": {
                    "deployment_revision_id": "revision-1",
                    "physical_plan_hash": _deployment_plan_payload(graph)["planHash"],
                    "release_digest": RELEASE_DIGEST,
                    "release_signature_digest": SIGNATURE_DIGEST,
                },
                "journalStorePath": str(journal_store_path),
                "runId": "run-native-requested-1",
                "runStorePath": str(run_store_path),
            },
        )
    ]


def test_run_cli_reports_unavailable_native_runtime_bridge(tmp_path, capsys, monkeypatch) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-native-missing"},
        "spec": {"nodes": {"value": {"block": "text.literal@1"}}},
    }
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: False,
            native_extension_status=lambda: {"error": "missing native extension"},
        ),
    )
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["run", str(path), "--runtime", "native"]) == 1

    assert "graphblocks-runtime native extension is not available: missing native extension" in capsys.readouterr().out


def test_validate_cli_uses_plugin_path_for_port_validation(tmp_path, capsys) -> None:
    manifest = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PluginManifest",
        "metadata": {"name": "com.example.ports", "version": "1.0.0"},
        "spec": {
            "pluginId": "com.example.ports",
            "version": "1.0.0",
            "blocks": [
                {
                    "typeId": "text.source",
                    "version": 1,
                    "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
                },
                {
                    "typeId": "text.sink",
                    "version": 1,
                    "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1"}],
                },
            ],
        },
    }
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "bad-port"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.missing"}],
        },
    }
    manifest_path = tmp_path / "plugin.yaml"
    graph_path = tmp_path / "graph.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    graph_path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["validate", str(graph_path), "--plugin-path", str(manifest_path), "--json"]) == 1
    assert '"code": "GB1013"' in capsys.readouterr().out


def test_policy_test_cli_runs_static_policy_cases(tmp_path, capsys) -> None:
    policy = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PolicyBundle",
        "metadata": {"name": "support-policy", "version": "1.0.0"},
        "spec": {
            "ruleLanguage": "static",
            "rules": [
                {
                    "ruleId": "allow-model",
                    "effect": "allow",
                    "actions": ["model.generate"],
                    "resourceSelectors": ["model"],
                }
            ],
        },
    }
    case = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PolicyTestCase",
        "metadata": {"name": "allow-support-model"},
        "spec": {
            "request": {
                "requestId": "request-1",
                "enforcementPoint": "before_provider_call",
                "action": "model.generate",
                "resource": {"resourceId": "model:support", "resourceKind": "model"},
                "occurredAt": "2026-06-23T00:00:00Z",
            },
            "expect": {
                "effect": "allow",
                "reasonCodes": ["allow-model"],
                "enforcementStatus": "enforced",
            },
            "evaluatedAt": "2026-06-23T00:00:01Z",
        },
    }
    policy_path = tmp_path / "policy.yaml"
    cases_path = tmp_path / "cases"
    cases_path.mkdir()
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    (cases_path / "allow.yaml").write_text(yaml.safe_dump(case), encoding="utf-8")

    assert main(["policy", "test", str(policy_path), "--cases", str(cases_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["cases"] == [{"caseId": "allow-support-model", "passed": True, "failures": []}]


def test_policy_test_cli_returns_failure_for_mismatched_case(tmp_path, capsys) -> None:
    policy = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PolicyBundle",
        "metadata": {"name": "support-policy", "version": "1.0.0"},
        "spec": {
            "ruleLanguage": "static",
            "rules": [
                {
                    "ruleId": "allow-model",
                    "effect": "allow",
                    "actions": ["model.generate"],
                    "resourceSelectors": ["model"],
                }
            ],
        },
    }
    case = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PolicyTestCase",
        "metadata": {"name": "deny-support-model"},
        "spec": {
            "request": {
                "requestId": "request-1",
                "enforcementPoint": "before_provider_call",
                "action": "model.generate",
                "resource": {"resourceId": "model:support", "resourceKind": "model"},
                "occurredAt": "2026-06-23T00:00:00Z",
            },
            "expect": {"effect": "deny", "enforcementStatus": "blocked"},
            "evaluatedAt": "2026-06-23T00:00:01Z",
        },
    }
    policy_path = tmp_path / "policy.yaml"
    case_path = tmp_path / "case.yaml"
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    case_path.write_text(yaml.safe_dump(case), encoding="utf-8")

    assert main(["policy", "test", str(policy_path), "--cases", str(case_path)]) == 1

    output = capsys.readouterr().out
    assert "FAIL deny-support-model" in output
    assert "expected effect deny but got allow" in output


def test_observe_run_cli_reads_sqlite_run_store_as_json(tmp_path, capsys) -> None:
    store_path = tmp_path / "runs.sqlite3"
    store = SQLiteRunStore(store_path)
    record = store.create_run("sha256:graph", {"message": {"text": "hello"}})
    store.patch_state(record.run_id, {"node": {"done": True}}, expected_revision=0)
    store.set_status(record.run_id, "succeeded")
    store.close()

    assert main(["observe", "run", record.run_id, "--store", str(store_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "runId": record.run_id,
        "graphHash": "sha256:graph",
        "status": "succeeded",
        "stateRevision": 1,
        "inputs": {"message": {"text": "hello"}},
        "state": {"node": {"done": True}},
    }


def test_observe_run_cli_reads_rust_sqlite_run_store_as_json(tmp_path, capsys) -> None:
    store_path = tmp_path / "rust-runs.sqlite3"
    connection = sqlite3.connect(store_path)
    connection.execute(
        """
        CREATE TABLE runs (
            sequence INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            graph_hash TEXT NOT NULL,
            invocation_mode TEXT NOT NULL DEFAULT 'sync',
            inputs_json TEXT NOT NULL,
            deployment_provenance_json TEXT NOT NULL,
            model_visible_tools_json TEXT NOT NULL,
            status TEXT NOT NULL,
            state_json TEXT NOT NULL,
            state_revision INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO runs (
            sequence,
            run_id,
            graph_hash,
            invocation_mode,
            inputs_json,
            deployment_provenance_json,
            model_visible_tools_json,
            status,
            state_json,
            state_revision
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "run-native-evidence-1",
            "sha256:native",
            "sync",
            '{"message":{"text":"hello"}}',
            "{}",
            "[]",
            "completed",
            '{"render":{"done":true}}',
            0,
        ),
    )
    connection.commit()
    connection.close()

    assert (
        main(
            [
                "observe",
                "run",
                "run-native-evidence-1",
                "--store",
                str(store_path),
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "runId": "run-native-evidence-1",
        "graphHash": "sha256:native",
        "status": "completed",
        "stateRevision": 0,
        "inputs": {"message": {"text": "hello"}},
        "state": {"render": {"done": True}},
    }


def test_observe_journal_cli_reads_sqlite_execution_journal_as_json(tmp_path, capsys) -> None:
    journal_path = tmp_path / "journal.sqlite3"
    journal = SQLiteExecutionJournal(journal_path, "run-000001")
    journal.append("run_started", {"graphHash": "sha256:graph"})
    journal.append("node_started", {"node": "render", "block": "prompt.render@1", "attempt": 1})
    journal.append("node_succeeded", {"node": "render", "outputs": ["prompt"]})
    journal.append_terminal("run_succeeded", {"outputs": {"prompt": "hello"}})
    journal.close()

    assert main(["observe", "journal", "run-000001", "--store", str(journal_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "runId": "run-000001",
        "terminalKind": "run_succeeded",
        "records": [
            {
                "sequence": 1,
                "kind": "run_started",
                "payload": {"graphHash": "sha256:graph"},
            },
            {
                "sequence": 2,
                "kind": "node_started",
                "payload": {"node": "render", "block": "prompt.render@1", "attempt": 1},
            },
            {
                "sequence": 3,
                "kind": "node_succeeded",
                "payload": {"node": "render", "outputs": ["prompt"]},
            },
            {
                "sequence": 4,
                "kind": "run_succeeded",
                "payload": {"outputs": {"prompt": "hello"}},
            },
        ],
    }


def test_observe_journal_cli_reads_rust_sqlite_execution_journal_as_json(tmp_path, capsys) -> None:
    journal_path = tmp_path / "rust-journal.sqlite3"
    connection = sqlite3.connect(journal_path)
    connection.execute(
        """
        CREATE TABLE journal_records (
            run_id TEXT NOT NULL,
            run_sequence INTEGER NOT NULL,
            record_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            causation_id TEXT,
            node_id TEXT,
            attempt_id TEXT,
            lease_epoch INTEGER,
            payload_json TEXT,
            terminal INTEGER NOT NULL,
            PRIMARY KEY (run_id, run_sequence)
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO journal_records (
            run_id,
            run_sequence,
            record_id,
            kind,
            causation_id,
            node_id,
            attempt_id,
            lease_epoch,
            payload_json,
            terminal
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "run-native-evidence-1",
                1,
                "run-native-evidence-1:1",
                "run_started",
                None,
                None,
                None,
                None,
                '{"graphHash":"sha256:native"}',
                0,
            ),
            (
                "run-native-evidence-1",
                2,
                "run-native-evidence-1:2",
                "node_completed",
                None,
                "render",
                "attempt-1",
                None,
                '{"outputs":["prompt"]}',
                0,
            ),
            (
                "run-native-evidence-1",
                3,
                "run-native-evidence-1:3",
                "run_succeeded",
                None,
                None,
                None,
                None,
                '{"outputs":{"prompt":"Native ok"}}',
                1,
            ),
        ],
    )
    connection.commit()
    connection.close()

    assert (
        main(
            [
                "observe",
                "journal",
                "run-native-evidence-1",
                "--store",
                str(journal_path),
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "runId": "run-native-evidence-1",
        "terminalKind": "run_succeeded",
        "records": [
            {
                "sequence": 1,
                "kind": "run_started",
                "payload": {"graphHash": "sha256:native"},
            },
            {
                "sequence": 2,
                "kind": "node_completed",
                "payload": {"outputs": ["prompt"]},
            },
            {
                "sequence": 3,
                "kind": "run_succeeded",
                "payload": {"outputs": {"prompt": "Native ok"}},
            },
        ],
    }


def test_observe_run_cli_reports_missing_run(tmp_path, capsys) -> None:
    store_path = tmp_path / "runs.sqlite3"
    SQLiteRunStore(store_path).close()

    assert main(["observe", "run", "run-missing", "--store", str(store_path)]) == 1

    assert "run not found: run-missing" in capsys.readouterr().out


def test_release_verify_cli_accepts_immutable_release(tmp_path, capsys) -> None:
    release = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "GraphRelease",
        "metadata": {"name": "support-agent", "version": "2026.06.24.1"},
        "spec": {
            "bundle": {
                "digest": "sha256:bundle",
                "mediaType": "application/vnd.graphblocks.release.v1",
            },
            "application": {"hash": "sha256:application"},
            "graphs": {
                "turn": {
                    "graphHash": "sha256:graph-turn",
                    "normalizedPlanHash": "sha256:plan-turn",
                }
            },
            "images": {
                "control": "registry.example.com/graphblocks/control@sha256:image-control",
            },
            "locks": {
                "python": {
                    "ref": "locks/pylock.toml",
                    "digest": "sha256:pylock",
                    "type": "package",
                },
                "policies": "oci://registry.example.com/graphblocks/policies@sha256:policy-lock",
            },
            "prompts": {
                "answer": {
                    "name": "support.answer",
                    "version": "2026.06.24",
                }
            },
            "knowledge": {
                "support_docs": {
                    "indexRevision": "support-docs-v17",
                }
            },
            "supplyChain": {
                "sbomRef": "oci://registry.example.com/graphblocks/sbom@sha256:sbom",
                "provenanceRef": "oci://registry.example.com/graphblocks/provenance@sha256:provenance",
                "signaturePolicy": "production-publishers",
            },
        },
    }
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(release), encoding="utf-8")

    assert main(["release", "verify", str(path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["name"] == "support-agent"
    assert payload["version"] == "2026.06.24.1"
    assert payload["releaseDigest"].startswith("sha256:")
    assert payload["mutableReferences"] == []


def test_release_verify_cli_rejects_mutable_production_references(tmp_path, capsys) -> None:
    release = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "GraphRelease",
        "metadata": {"name": "support-agent", "version": "2026.06.24.1"},
        "spec": {
            "bundle": {
                "digest": "latest",
                "mediaType": "application/vnd.graphblocks.release.v1",
            },
            "graphs": {
                "turn": {
                    "graphHash": "main",
                    "normalizedPlanHash": "sha256:plan-turn",
                }
            },
            "images": {
                "control": "registry.example.com/graphblocks/control:latest",
            },
            "locks": {
                "python": "locks/pylock.toml",
                "policies": {
                    "ref": "locks/policies.lock",
                    "digest": "latest",
                    "type": "policy",
                },
            },
            "prompts": {
                "answer": {
                    "name": "support.answer",
                    "label": "production",
                }
            },
            "knowledge": {
                "support_docs": {
                    "indexRevision": "current",
                }
            },
            "supplyChain": {
                "sbomRef": "oci://registry.example.com/graphblocks/sbom:latest",
                "provenanceRef": "oci://registry.example.com/graphblocks/provenance:latest",
                "signaturePolicy": "production-publishers",
            },
        },
    }
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(release), encoding="utf-8")

    assert main(["release", "verify", str(path), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["mutableReferences"] == [
        "bundle.digest",
        "graphs.turn.graph_hash",
        "images.control",
        "locks.policies.digest",
        "locks.python.digest",
        "knowledge.support_docs.index_revision",
        "prompts.answer",
        "supply_chain.provenance_ref",
        "supply_chain.sbom_ref",
    ]


def test_deploy_plan_cli_builds_physical_execution_plan(tmp_path, capsys) -> None:
    release = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "GraphRelease",
        "metadata": {"name": "support-agent", "version": "2026.06.24.1"},
        "spec": {
            "bundle": {
                "digest": "sha256:bundle",
                "mediaType": "application/vnd.graphblocks.release.v1",
            },
            "graphs": {
                "turn": {
                    "graphHash": "sha256:graph-turn",
                    "normalizedPlanHash": "sha256:plan-turn",
                }
            },
        },
    }
    deployment = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "GraphDeployment",
        "metadata": {"name": "support-production"},
        "spec": {
            "releaseRef": {"name": "support-agent"},
            "profile": "production",
            "bindingRef": "bindings/support-production.yaml",
            "coordinator": {"target": "control"},
            "targets": {
                "control": {
                    "kind": "service",
                    "executionHost": "rust",
                    "image": "registry.example.com/graphblocks/control@sha256:control",
                    "accepts": {"capabilities": ["graph.coordinator"]},
                },
                "docs": {
                    "kind": "workerPool",
                    "executionHost": "python_worker",
                    "packageLock": "locks/docs.lock",
                    "accepts": {"capabilities": ["document.parse.pdf"]},
                },
            },
            "placements": [
                {
                    "id": "document-parser",
                    "select": {"capabilities": ["document.parse.pdf"]},
                    "target": "docs",
                },
                {
                    "id": "fallback",
                    "select": {"default": True},
                    "target": "control",
                },
            ],
        },
    }
    path = tmp_path / "deployment.yaml"
    path.write_text(yaml.safe_dump_all([release, deployment]), encoding="utf-8")

    assert main(
        [
            "deploy",
            "plan",
            str(path),
            "--revision",
            "rev-1",
            "--created-at",
            "2026-06-24T00:00:00Z",
            "--json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["deploymentId"] == "support-production"
    assert payload["deploymentRevisionId"] == "rev-1"
    assert payload["graphName"] == "turn"
    assert payload["releaseDigest"].startswith("sha256:")
    assert payload["planHash"].startswith("sha256:")
    assert payload["deploymentSpecHash"].startswith("sha256:")
    assert payload["deploymentRevision"]["revisionId"] == "rev-1"
    assert payload["deploymentRevision"]["releaseDigest"] == payload["releaseDigest"]
    assert payload["deploymentRevision"]["deploymentSpecHash"] == payload["deploymentSpecHash"]
    assert payload["deploymentRevision"]["physicalPlanHash"] == payload["planHash"]
    assert payload["deploymentRevision"]["resolvedBindingHash"].startswith("sha256:")
    assert payload["deploymentRevision"]["targetCapabilityHash"].startswith("sha256:")
    assert payload["deploymentRevision"]["createdAt"] == "2026-06-24T00:00:00Z"
    assert payload["deploymentRevision"]["contentDigest"].startswith("sha256:")
    assert payload["plan"]["graphHash"] == "sha256:graph-turn"
    assert payload["plan"]["defaultTarget"] == "control"
    assert payload["plan"]["targets"]["docs"]["kind"] == "worker_pool"
    assert payload["plan"]["targets"]["docs"]["capabilities"] == ["document.parse.pdf"]
    assert payload["plan"]["placements"] == [
        {
            "ruleId": "document-parser",
            "selector": {
                "kind": "capabilities",
                "values": ["document.parse.pdf"],
            },
            "target": "docs",
        }
    ]


def test_deploy_render_cli_renders_kubernetes_manifest_set(tmp_path, capsys) -> None:
    plan = _render_plan_payload()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    assert main(["deploy", "render", str(path), "--target", "kubernetes", "--namespace", "support", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["target"] == "kubernetes"
    assert payload["manifestDigest"].startswith("sha256:")
    assert payload["manifests"][0]["kind"] == "Deployment"
    assert payload["manifests"][0]["metadata"]["name"] == "support-production-control"
    assert payload["manifests"][0]["metadata"]["namespace"] == "support"
    assert payload["manifests"][0]["metadata"]["annotations"]["graphblocks.ai/target-id"] == "control"
    assert payload["manifests"][0]["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "registry.example.com/graphblocks/control@sha256:control"
    )


def test_deploy_render_cli_requires_explicit_successful_plan(tmp_path, capsys) -> None:
    plan = _render_plan_payload()
    del plan["ok"]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    assert main(["deploy", "render", str(path), "--target", "kubernetes", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "error": "deploy plan payload is not successful"}


def test_deploy_render_cli_rejects_noncanonical_release_digest(tmp_path, capsys) -> None:
    plan = _render_plan_payload()
    plan["releaseDigest"] = "not-a-digest"
    deployment_revision = plan["deploymentRevision"]
    assert isinstance(deployment_revision, dict)
    deployment_revision["releaseDigest"] = "not-a-digest"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    assert main(["deploy", "render", str(path), "--target", "kubernetes", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "error": "deploy plan payload releaseDigest must be a canonical sha256 digest",
    }


def test_deploy_render_cli_rejects_tampered_plan_and_revision_digests(tmp_path, capsys) -> None:
    path = tmp_path / "plan.json"
    plan = _render_plan_payload()
    plan_payload = plan["plan"]
    assert isinstance(plan_payload, dict)
    targets = plan_payload["targets"]
    assert isinstance(targets, dict)
    control = targets["control"]
    assert isinstance(control, dict)
    control["image"] = "registry.example.com/graphblocks/control@sha256:tampered"
    path.write_text(json.dumps(plan), encoding="utf-8")

    assert main(["deploy", "render", str(path), "--target", "kubernetes", "--json"]) == 1
    assert "planHash does not match plan content" in capsys.readouterr().out

    plan = _render_plan_payload()
    deployment_revision = plan["deploymentRevision"]
    assert isinstance(deployment_revision, dict)
    deployment_revision["contentDigest"] = "sha256:" + ("c" * 64)
    path.write_text(json.dumps(plan), encoding="utf-8")

    assert main(["deploy", "render", str(path), "--target", "kubernetes", "--json"]) == 1
    assert "contentDigest does not match revision content" in capsys.readouterr().out


def test_deploy_render_cli_renders_helm_chart_package(tmp_path, capsys) -> None:
    plan = _render_plan_payload()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    assert main(["deploy", "render", str(path), "--target", "helm", "--namespace", "support", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["target"] == "helm"
    assert payload["chartName"] == "support-production"
    assert payload["chartDigest"].startswith("sha256:")
    assert payload["manifestDigest"].startswith("sha256:")
    assert sorted(payload["files"]) == [
        "Chart.yaml",
        "templates/support-production-control-deployment.yaml",
        "values.yaml",
    ]
    assert yaml.safe_load(payload["files"]["Chart.yaml"])["appVersion"] == "rev-1"
    assert yaml.safe_load(payload["files"]["values.yaml"]) == {
        "deploymentId": "support-production",
        "deploymentRevisionId": "rev-1",
        "manifestDigest": payload["manifestDigest"],
        "planHash": plan["planHash"],
        "releaseDigest": plan["releaseDigest"],
    }
    deployment = yaml.safe_load(payload["files"]["templates/support-production-control-deployment.yaml"])
    assert deployment["metadata"]["namespace"] == "support"
    assert deployment["metadata"]["annotations"]["graphblocks.ai/target-id"] == "control"


def test_deploy_targets_verify_cli_accepts_production_target_manifest(capsys) -> None:
    root = Path(__file__).parents[1]

    assert main(["deploy", "targets-verify", str(root / "deployment" / "production-targets.yaml"), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["targetCount"] == 5
    assert payload["targetIds"] == ["control", "document-cpu", "ocr-gpu", "rag-cpu", "sandbox"]
    assert payload["imageRoles"] == ["control-plane", "rag-cpu", "document-cpu", "ocr-gpu", "sandbox"]
    assert payload["contentDigest"].startswith("sha256:")
    assert payload["issues"] == []


def test_deploy_targets_verify_cli_reports_missing_required_role(tmp_path, capsys) -> None:
    manifest = {
        "apiVersion": "graphblocks.ai/deployment/v1alpha1",
        "kind": "DeploymentTargetProfileSet",
        "spec": {
            "targets": [
                {
                    "id": "control",
                    "imageRole": "control-plane",
                    "kind": "service",
                    "executionHost": "rust",
                }
            ]
        },
    }
    path = tmp_path / "targets.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    assert main(["deploy", "targets-verify", str(path), "--required-role", "rag-cpu", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["issues"] == [
        {
            "code": "DeploymentTargetRoleMissing",
            "image_role": "rag-cpu",
            "target_id": "",
            "path": "$.spec.targets",
            "message": "required production image role has no deployment target profile",
        }
    ]


def test_deploy_plan_cli_rejects_mismatched_release_reference(tmp_path, capsys) -> None:
    release = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "GraphRelease",
        "metadata": {"name": "support-agent", "version": "2026.06.24.1"},
        "spec": {
            "bundle": {"digest": "sha256:bundle"},
            "graphs": {
                "turn": {
                    "graphHash": "sha256:graph-turn",
                    "normalizedPlanHash": "sha256:plan-turn",
                }
            },
        },
    }
    deployment = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "GraphDeployment",
        "metadata": {"name": "support-production"},
        "spec": {
            "releaseRef": {"name": "another-release"},
            "targets": {},
        },
    }
    path = tmp_path / "deployment.yaml"
    path.write_text(yaml.safe_dump_all([release, deployment]), encoding="utf-8")

    assert main(["deploy", "plan", str(path), "--revision", "rev-1", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "GraphDeployment releaseRef.name 'another-release' does not match 'support-agent'"


def test_release_build_cli_creates_deterministic_verifiable_bundle(tmp_path, capsys) -> None:
    release = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "GraphRelease",
        "metadata": {"name": "support-agent", "version": "2026.06.24.1"},
        "spec": {
            "bundle": {
                "digest": "sha256:source-bundle",
                "mediaType": "application/vnd.graphblocks.release.v1",
            },
            "graphs": {
                "turn": {
                    "graphHash": "sha256:graph-turn",
                    "normalizedPlanHash": "sha256:plan-turn",
                }
            },
            "images": {
                "control": "registry.example.com/graphblocks/control@sha256:control",
            },
        },
    }
    path = tmp_path / "release.yaml"
    first = tmp_path / "first.gbr"
    second = tmp_path / "second.gbr"
    path.write_text(yaml.safe_dump(release), encoding="utf-8")

    assert main(["release", "build", str(path), "--out", str(first), "--json"]) == 0
    first_payload = json.loads(capsys.readouterr().out)
    assert main(["release", "build", str(path), "--out", str(second), "--json"]) == 0
    second_payload = json.loads(capsys.readouterr().out)

    assert first.read_bytes() == second.read_bytes()
    assert first_payload["bundleDigest"] == second_payload["bundleDigest"]
    assert first_payload["releaseDigest"] == second_payload["releaseDigest"]
    with tarfile.open(first, "r:") as archive:
        assert archive.getnames() == ["manifest.json", "release.json"]

    assert main(["release", "verify", str(first), "--json"]) == 0
    verify_payload = json.loads(capsys.readouterr().out)
    assert verify_payload["ok"] is True
    assert verify_payload["bundleDigest"] == first_payload["bundleDigest"]
    assert verify_payload["releaseDigest"] == first_payload["releaseDigest"]

    reordered = tmp_path / "reordered.gbr"
    with tarfile.open(first, "r:") as source_archive:
        members = {}
        for name in ("manifest.json", "release.json"):
            extracted = source_archive.extractfile(name)
            assert extracted is not None
            members[name] = (source_archive.getmember(name), extracted.read())
    with tarfile.open(reordered, "w:") as reordered_archive:
        for name in ("release.json", "manifest.json"):
            member, contents = members[name]
            reordered_archive.addfile(member, io.BytesIO(contents))

    assert main(["release", "verify", str(reordered), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_release_build_cli_rejects_non_finite_release_numbers(tmp_path, capsys) -> None:
    release = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "GraphRelease",
        "metadata": {"name": "non-finite-release", "version": "2026.07.10.1"},
        "spec": {
            "bundle": {
                "digest": "sha256:bundle",
                "mediaType": "application/vnd.graphblocks.release.v1",
            },
            "graphs": {
                "main": {
                    "graphHash": "sha256:graph",
                    "normalizedPlanHash": float("nan"),
                }
            },
        },
    }
    source = tmp_path / "release.yaml"
    bundle = tmp_path / "release.gbr"
    source.write_text(yaml.safe_dump(release), encoding="utf-8")

    assert main(["release", "build", str(source), "--out", str(bundle), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": "GraphRelease document must contain only finite JSON numbers",
        "ok": False,
    }
    assert not bundle.exists()
