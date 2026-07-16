from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graphblocks.cli import main
from graphblocks.runtime import InProcessRuntime, stdlib_registry


def _mismatched_stdlib_interface_graph() -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "mismatched-stdlib-interface"},
        "spec": {
            "interface": {
                "inputs": {"message": "graphblocks.ai/Text@1"},
                "outputs": {"context": "graphblocks.ai/Text@1"},
            },
            "nodes": {
                "context": {
                    "block": "context.build@1",
                    "inputs": {"currentMessage": "$input.message"},
                    "outputs": {"pack": "$output.context"},
                }
            },
        },
    }


def test_default_validate_uses_builtin_catalog_for_interface_types(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    graph_path = tmp_path / "graph.yaml"
    graph_path.write_text(
        yaml.safe_dump(_mismatched_stdlib_interface_graph()),
        encoding="utf-8",
    )

    assert main(["validate", str(graph_path)]) == 1
    assert "GB1018" in capsys.readouterr().out


def test_stdlib_runtime_rejects_interface_type_mismatch_before_execution() -> None:
    with pytest.raises(ValueError, match="GB1018"):
        InProcessRuntime(stdlib_registry()).run(
            _mismatched_stdlib_interface_graph(),
            {"message": {"text": "wrong nominal schema"}},
        )


@pytest.mark.parametrize("command", ["validate", "plan"])
def test_cli_reports_cross_plugin_block_catalog_conflicts_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    graph_path = tmp_path / "graph.yaml"
    graph_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "catalog-conflict"},
                "spec": {"nodes": {}},
            }
        ),
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    for index in (1, 2):
        (plugin_dir / f"plugin-{index}.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "graphblocks.ai/v1alpha1",
                    "kind": "PluginManifest",
                    "metadata": {"name": f"com.example.plugin{index}"},
                    "spec": {
                        "pluginId": f"com.example.plugin{index}",
                        "version": "1.0.0",
                        "blocks": [{"typeId": "test.echo", "version": 1}],
                    },
                }
            ),
            encoding="utf-8",
        )

    assert main([command, str(graph_path), "--plugin-path", str(plugin_dir)]) == 1
    assert "GB2017" in capsys.readouterr().out
