from __future__ import annotations

from graphblocks import canonical_hash, compile_graph, normalize_graph


def test_normalized_hash_is_stable_for_mapping_order() -> None:
    left = {
        "kind": "Graph",
        "apiVersion": "graphblocks.ai/v1alpha3",
        "metadata": {"name": "ordered"},
        "spec": {
            "nodes": {
                "b": {"block": "text.join@1", "config": {"second": 2, "first": 1}},
                "a": {"block": "text.literal@1"},
            },
            "edges": [{"to": "b.value", "from": "a.value"}, {"to": "$output.result", "from": "b.value"}],
            "interface": {"outputs": {"result": "graphblocks.ai/Text@1"}},
        },
    }
    right = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "spec": {
            "interface": {"outputs": {"result": "graphblocks.ai/Text@1"}},
            "edges": [{"from": "b.value", "to": "$output.result"}, {"from": "a.value", "to": "b.value"}],
            "nodes": {
                "a": {"block": "text.literal@1"},
                "b": {"config": {"first": 1, "second": 2}, "block": "text.join@1"},
            },
        },
        "metadata": {"name": "ordered"},
    }

    assert canonical_hash(normalize_graph(left)) == canonical_hash(normalize_graph(right))


def test_node_inputs_are_normalized_to_edges() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "input-shorthand"},
        "spec": {
            "interface": {"inputs": {"message": "graphblocks.ai/Text@1"}},
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "inputs": {"message": "$input.message", "context": {"current": "lookup.value"}},
                },
                "lookup": {"block": "memory.lookup@1"},
            },
        },
    }

    normalized = normalize_graph(graph)

    assert normalized["spec"]["nodes"]["render"] == {"block": "prompt.render@1"}
    assert {"from": "$input.message", "to": "render.message"} in normalized["spec"]["edges"]
    assert {"from": "lookup.value", "to": "render.context.current"} in normalized["spec"]["edges"]


def test_compile_reports_unknown_edge_endpoint() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "bad-edge"},
        "spec": {
            "nodes": {"consumer": {"block": "text.join@1"}},
            "edges": [{"from": "missing.value", "to": "consumer.value"}],
        },
    }

    plan = compile_graph(graph)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics] == ["GB1002"]

