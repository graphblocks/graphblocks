from __future__ import annotations

import json
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
