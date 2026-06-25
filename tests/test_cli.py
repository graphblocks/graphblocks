from __future__ import annotations

import json
from pathlib import Path
import tarfile
import yaml

from graphblocks.cli import main
from graphblocks.run_store import SQLiteRunStore


def test_validate_cli_accepts_valid_graph(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-valid"},
        "spec": {
            "interface": {"outputs": {"result": "graphblocks.ai/Text@1"}},
            "nodes": {"value": {"block": "text.literal@1"}},
            "edges": [{"from": "value.value", "to": "$output.result"}],
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
        "spec": {"nodes": {"value": {"block": "text.literal@1"}}},
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["plan", str(path)]) == 0
    assert '"hash": "sha256:' in capsys.readouterr().out


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
    assert payload["contentDigest"] == "sha256:3bcd67f34d6c22940158b7c3d3290fb33620fa32de72c177533d7f20188a013e"
    assert [entry["schemaId"] for entry in payload["schemas"]] == [
        "graphblocks.ai/v1alpha1/application.schema.json",
        "graphblocks.ai/v1alpha1/binding.schema.json",
        "graphblocks.ai/v1alpha1/plugin-manifest.schema.json",
        "graphblocks.ai/v1alpha3/graph.schema.json",
    ]


def test_lock_cli_emits_graph_hash_and_default_package_closure(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-lock"},
        "spec": {"nodes": {"value": {"block": "text.literal@1"}}},
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["lock", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["lockVersion"] == 1
    assert payload["graph"]["id"] == "cli-lock"
    assert payload["graph"]["graphHash"].startswith("sha256:")
    assert payload["graph"]["schemaVersion"] == "graphblocks.ai/v1alpha3"
    assert payload["packageLockHash"].startswith("sha256:")
    assert payload["packageCatalogVersion"] == 4
    assert "graphblocks-core" in {package["name"] for package in payload["packages"]}


def test_lock_cli_writes_output_file(tmp_path, capsys) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cli-lock-file"},
        "spec": {"nodes": {"value": {"block": "text.literal@1"}}},
    }
    path = tmp_path / "graph.yaml"
    output = tmp_path / "graphblocks.lock.json"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    assert main(["lock", str(path), "--output", str(output)]) == 0

    assert capsys.readouterr().out == ""
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["graph"]["id"] == "cli-lock-file"
    assert payload["packages"][0]["name"] == "graphblocks"


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


def test_deploy_render_cli_renders_kubernetes_manifest_set(tmp_path, capsys, monkeypatch) -> None:
    root = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(root / "packages" / "graphblocks-deployment" / "src"))
    monkeypatch.syspath_prepend(str(root / "packages" / "graphblocks-kubernetes" / "src"))
    plan = {
        "ok": True,
        "deploymentId": "support-production",
        "deploymentRevisionId": "rev-1",
        "releaseDigest": "sha256:release",
        "planHash": "sha256:plan",
        "plan": {
            "targets": {
                "control": {
                    "kind": "service",
                    "executionHost": "rust",
                    "capabilities": ["graph.coordinator"],
                    "effects": [],
                    "packageLock": None,
                    "image": "registry.example.com/graphblocks/control@sha256:control",
                }
            }
        },
    }
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
