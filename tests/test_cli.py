from __future__ import annotations

import yaml

from graphblocks.cli import main


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
