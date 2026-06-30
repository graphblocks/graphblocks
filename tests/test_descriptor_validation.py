from __future__ import annotations

import pytest

from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog


def test_block_catalog_rejects_invalid_port_schema_ids() -> None:
    with pytest.raises(
        ValueError,
        match="block catalog entry 0 output value has invalid type schemas/Text",
    ):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "text.source",
                    "version": 1,
                    "outputs": [{"name": "value", "type": "schemas/Text"}],
                }
            ]
        )


def test_block_catalog_allows_port_type_expressions() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "control.map",
                "version": 1,
                "inputs": [{"name": "items", "type": "List<Any>"}],
                "outputs": [{"name": "values", "type": "List<Any>"}],
            }
        ]
    )

    assert catalog.get("control.map@1") is not None


@pytest.mark.parametrize("version", [True, 0, 1.0, "", "+1", "01", "1.0", "one"])
def test_block_catalog_rejects_non_canonical_block_versions(version: object) -> None:
    with pytest.raises(
        ValueError,
        match="block catalog entry 0 version is invalid",
    ):
        BlockCatalog.from_blocks([{"typeId": "bad.version", "version": version}])


def test_block_catalog_rejects_non_canonical_inline_block_version() -> None:
    with pytest.raises(
        ValueError,
        match="block catalog entry 0 version is invalid",
    ):
        BlockCatalog.from_blocks([{"typeId": "bad.version@01"}])


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


def test_compile_rejects_optional_output_to_required_input() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "branch.maybe_text",
                "version": 1,
                "outputs": [
                    {"name": "value", "type": "graphblocks.ai/Text@1", "required": False}
                ],
            },
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1", "required": True}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "optional-output-required-input"},
        "spec": {
            "nodes": {
                "maybe": {"block": "branch.maybe_text@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "maybe.value", "to": "sink.text"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1015"]


def test_compile_rejects_port_type_mismatch() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.source",
                "version": 1,
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
            {
                "typeId": "number.sink",
                "version": 1,
                "inputs": [{"name": "value", "type": "graphblocks.ai/Number@1"}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "type-mismatch"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "number.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.value"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1018"]


def test_compile_accepts_matching_port_types() -> None:
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
                "inputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "type-match"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.value"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert "GB1018" not in [item.code for item in plan.diagnostics.diagnostics]
