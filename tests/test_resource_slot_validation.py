from __future__ import annotations

import pytest

from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog


def test_block_catalog_rejects_invalid_resource_slot_schema_ids() -> None:
    with pytest.raises(
        ValueError,
        match="block catalog entry 0 resource slot model has invalid type resources/Model",
    ):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "model.generate",
                    "version": 1,
                    "resourceSlots": {"model": {"type": "resources/Model"}},
                }
            ]
        )


def test_compile_rejects_missing_required_resource_slot_binding() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "model.generate",
                "version": 1,
                "resourceSlots": [{"name": "model", "type": "graphblocks.ai/ChatModel@1"}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "missing-resource"},
        "spec": {"nodes": {"generate": {"block": "model.generate@1"}}},
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1016"]


def test_compile_rejects_unknown_resource_slot_binding() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "model.generate",
                "version": 1,
                "resourceSlots": [{"name": "model", "type": "graphblocks.ai/ChatModel@1"}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unknown-resource-slot"},
        "spec": {
            "nodes": {
                "generate": {
                    "block": "model.generate@1",
                    "bindings": {"unknown": "answer-model"},
                }
            }
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1017"]


def test_compile_accepts_required_resource_slot_binding() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "model.generate",
                "version": 1,
                "resourceSlots": [{"name": "model", "type": "graphblocks.ai/ChatModel@1"}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "bound-resource"},
        "spec": {
            "nodes": {
                "generate": {
                    "block": "model.generate@1",
                    "bindings": {"model": "answer-model"},
                }
            }
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert "GB1016" not in [item.code for item in plan.diagnostics.diagnostics]
    assert "GB1017" not in [item.code for item in plan.diagnostics.diagnostics]


def test_compile_allows_optional_resource_slot_to_be_unbound() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "rank.documents",
                "version": 1,
                "resourceSlots": [
                    {"name": "reranker", "type": "graphblocks.ai/Reranker@1", "optional": True}
                ],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "optional-resource"},
        "spec": {"nodes": {"rank": {"block": "rank.documents@1"}}},
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert "GB1016" not in [item.code for item in plan.diagnostics.diagnostics]
