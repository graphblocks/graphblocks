from __future__ import annotations

from graphblocks.runtime import InProcessRuntime, stdlib_registry


def test_control_map_runs_block_for_each_item_in_order() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "map-prompts"},
        "spec": {
            "nodes": {
                "map": {
                    "block": "control.map@2",
                    "inputs": {"items": "$input.items"},
                    "outputs": {"values": "$output.values"},
                    "config": {
                        "block": "prompt.render@1",
                        "inputName": "message",
                        "outputName": "prompt",
                        "config": {"template": "Item {message.index}: {message.text}"},
                    },
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(
        graph,
        {"items": [{"index": 1, "text": "alpha"}, {"index": 2, "text": "beta"}]},
    )

    assert result.status == "succeeded"
    assert result.outputs == {"values": ["Item 1: alpha", "Item 2: beta"]}


def test_control_map_collects_item_errors_when_configured() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "map-collect-errors"},
        "spec": {
            "nodes": {
                "map": {
                    "block": "control.map@2",
                    "inputs": {"items": "$input.items"},
                    "outputs": {"outcomes": "$output.outcomes"},
                    "config": {
                        "block": "prompt.render@1",
                        "inputName": "message",
                        "outputName": "prompt",
                        "onError": "collect",
                        "config": {"template": "Value {message.text}"},
                    },
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {"items": [{"text": "ok"}, {}]})

    assert result.status == "succeeded"
    assert result.outputs["outcomes"][0] == {"status": "succeeded", "value": "Value ok"}
    assert result.outputs["outcomes"][1]["status"] == "failed"
    assert "text" in result.outputs["outcomes"][1]["error"]


def test_control_map_fails_fast_by_default() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "map-fail-fast"},
        "spec": {
            "nodes": {
                "map": {
                    "block": "control.map@2",
                    "inputs": {"items": "$input.items"},
                    "outputs": {"values": "$output.values"},
                    "config": {
                        "block": "prompt.render@1",
                        "inputName": "message",
                        "outputName": "prompt",
                        "config": {"template": "Value {message.text}"},
                    },
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {"items": [{"text": "ok"}, {}]})

    assert result.status == "failed"
    assert result.outputs == {}


def test_control_select_returns_first_present_case() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "select-first"},
        "spec": {
            "nodes": {
                "select": {
                    "block": "control.select@1",
                    "inputs": {"cases": "$input.cases"},
                    "outputs": {"value": "$output.value"},
                    "config": {"order": ["ocr", "parsed"]},
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(
        graph,
        {"cases": {"parsed": {"document": "parsed"}, "ocr": {"document": "ocr"}}},
    )

    assert result.status == "succeeded"
    assert result.outputs == {"value": {"document": "ocr"}}


def test_control_select_treats_null_as_present_value() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "select-null"},
        "spec": {
            "nodes": {
                "select": {
                    "block": "control.select@1",
                    "inputs": {"cases": "$input.cases"},
                    "outputs": {"value": "$output.value"},
                    "config": {"order": ["value"], "default": "fallback"},
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {"cases": {"value": None}})

    assert result.status == "succeeded"
    assert result.outputs == {"value": None}


def test_control_select_fails_without_present_case_or_default() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "select-missing"},
        "spec": {
            "nodes": {
                "select": {
                    "block": "control.select@1",
                    "inputs": {"cases": "$input.cases"},
                    "outputs": {"value": "$output.value"},
                    "config": {"order": ["missing"]},
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {"cases": {}})

    assert result.status == "failed"
