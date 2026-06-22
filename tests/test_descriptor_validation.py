from __future__ import annotations

from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog


def test_compile_rejects_edge_to_unknown_input_port() -> None:
    catalog = BlockCatalog.from_blocks(
        [
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
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "bad-target-port"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.missing"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1013"]


def test_compile_rejects_edge_from_unknown_output_port() -> None:
    catalog = BlockCatalog.from_blocks(
        [
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
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "bad-source-port"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "source.missing", "to": "sink.text"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1014"]


def test_compile_rejects_required_input_never_produced() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1", "required": True}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "missing-required-input"},
        "spec": {"nodes": {"sink": {"block": "text.sink@1"}}},
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1003"]


def test_compile_allows_optional_input_without_edge() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.optional_sink",
                "version": 1,
                "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1", "required": False}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "optional-input"},
        "spec": {"nodes": {"sink": {"block": "text.optional_sink@1"}}},
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert "GB1003" not in [item.code for item in plan.diagnostics.diagnostics]

