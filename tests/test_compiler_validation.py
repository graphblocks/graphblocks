from __future__ import annotations

import pytest

from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog


def _error_diagnostics(
    graph: dict[str, object],
    *,
    block_catalog: BlockCatalog | None = None,
) -> list[tuple[str, str, str]]:
    plan = compile_graph(graph, block_catalog=block_catalog)
    return [
        (diagnostic.code, diagnostic.message, diagnostic.path)
        for diagnostic in plan.diagnostics.diagnostics
        if diagnostic.severity == "error"
    ]


def _voice_feedback_graph() -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "duplex-voice-feedback"},
        "spec": {
            "extensions": ["graphblocks.voice/v1alpha1"],
            "execution": {
                "lifetime": "session",
                "interaction": "duplex",
                "durability": "checkpointed",
            },
            "voice": {"pipeline": {"kind": "realtime"}},
            "nodes": {
                "session": {"block": "realtime.session@1"},
                "tools": {"block": "tools.dispatch@1"},
            },
            "edges": [
                {"from": "session.toolCalls", "to": "tools.calls"},
                {"from": "tools.results", "to": "session.toolResults"},
            ],
        },
    }


@pytest.mark.parametrize(
    ("edge", "expected_message", "expected_path"),
    [
        (
            {"from": "source", "to": "sink.value"},
            "edge from endpoint must include a port path",
            "$.spec.edges[0].from",
        ),
        (
            {"from": "source.value", "to": "sink"},
            "edge to endpoint must include a port path",
            "$.spec.edges[0].to",
        ),
        (
            {"from": "$input", "to": "sink.value"},
            "edge from endpoint must include a port path",
            "$.spec.edges[0].from",
        ),
        (
            {"from": "source.value", "to": "$output"},
            "edge to endpoint must include a port path",
            "$.spec.edges[0].to",
        ),
        (
            {"from": "$output.value", "to": "sink.value"},
            "$output cannot be used as an edge source",
            "$.spec.edges[0].from",
        ),
        (
            {"from": "source.value", "to": "$input.value"},
            "$input cannot be used as an edge target",
            "$.spec.edges[0].to",
        ),
    ],
    ids=[
        "node-source-port",
        "node-target-port",
        "input-port",
        "output-port",
        "output-direction",
        "input-direction",
    ],
)
def test_compile_rejects_malformed_or_direction_invalid_endpoints(
    edge: dict[str, str],
    expected_message: str,
    expected_path: str,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-endpoint"},
        "spec": {
            "nodes": {
                "source": {"block": "test.source@1"},
                "sink": {"block": "test.sink@1"},
            },
            "edges": [edge],
        },
    }

    assert _error_diagnostics(graph) == [
        ("GB1020", expected_message, expected_path)
    ]


@pytest.mark.parametrize(
    "graph",
    [
        {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "edge-cycle"},
            "spec": {
                "nodes": {
                    "a": {"block": "test.node@1"},
                    "b": {"block": "test.node@1"},
                },
                "edges": [
                    {"from": "a.value", "to": "b.value"},
                    {"from": "b.value", "to": "a.value"},
                ],
            },
        },
        {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "guard-cycle"},
            "spec": {
                "nodes": {
                    "a": {"block": "test.node@1", "when": "b.enabled"},
                    "b": {"block": "test.node@1", "when": "a.enabled"},
                }
            },
        },
    ],
    ids=["edges", "when-guards"],
)
def test_compile_rejects_dependency_cycles(graph: dict[str, object]) -> None:
    assert _error_diagnostics(graph) == [
        ("GB1021", "graph dependency cycle detected: a -> b -> a", "$.spec")
    ]


def test_compile_allows_exact_checkpointed_duplex_voice_feedback_cycle() -> None:
    assert not any(
        code == "GB1021" for code, _message, _path in _error_diagnostics(_voice_feedback_graph())
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("extensions",), []),
        (("execution", "lifetime"), "job"),
        (("execution", "interaction"), "incremental"),
        (("execution", "durability"), "ephemeral"),
        (("voice", "pipeline", "kind"), "batch"),
        (("nodes", "session", "block"), "test.session@1"),
    ],
    ids=["extension", "lifetime", "interaction", "durability", "pipeline", "session-block"],
)
def test_compile_rejects_voice_feedback_without_the_exact_runtime_profile(
    path: tuple[str, ...],
    value: object,
) -> None:
    graph = _voice_feedback_graph()
    target = graph["spec"]
    assert isinstance(target, dict)
    for key in path[:-1]:
        target = target[key]
        assert isinstance(target, dict)
    target[path[-1]] = value

    assert any(code == "GB1021" for code, _message, _path in _error_diagnostics(graph))


def test_compile_rejects_other_cycles_in_a_duplex_voice_graph() -> None:
    graph = _voice_feedback_graph()
    spec = graph["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    edges = spec["edges"]
    assert isinstance(nodes, dict)
    assert isinstance(edges, list)
    nodes.update(
        {
            "a": {"block": "test.node@1"},
            "b": {"block": "test.node@1"},
        }
    )
    edges.extend(
        [
            {"from": "a.value", "to": "b.value"},
            {"from": "b.value", "to": "a.value"},
        ]
    )

    assert any(code == "GB1021" for code, _message, _path in _error_diagnostics(graph))


def test_compile_rejects_when_dependencies_inside_voice_feedback_cycle() -> None:
    graph = _voice_feedback_graph()
    spec = graph["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, dict)
    session = nodes["session"]
    assert isinstance(session, dict)
    session["when"] = "tools.enabled"

    assert any(code == "GB1021" for code, _message, _path in _error_diagnostics(graph))


@pytest.mark.parametrize(
    ("when", "expected_message"),
    [
        ("$input", "node when reference must include a port path"),
        ("$output.enabled", "$output cannot be used as a when source"),
        (False, "node when reference must be a string"),
    ],
    ids=["missing-port", "output-source", "non-string"],
)
def test_compile_rejects_malformed_or_direction_invalid_when_references(
    when: object,
    expected_message: str,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-when-reference"},
        "spec": {
            "nodes": {
                "branch": {"block": "test.branch@1", "when": when},
            },
        },
    }

    assert _error_diagnostics(graph) == [
        ("GB1020", expected_message, "$.spec.nodes.branch.when")
    ]


def test_compile_rejects_unknown_interface_input_used_by_when() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unknown-interface-when-port"},
        "spec": {
            "interface": {"inputs": {"enabled": "graphblocks.ai/Flag@1"}},
            "nodes": {
                "branch": {
                    "block": "test.branch@1",
                    "when": "$input.missing",
                },
            },
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1014",
            "graph interface has no input port 'missing'",
            "$.spec.nodes.branch.when",
        )
    ]


def test_compile_rejects_unknown_block_output_used_by_when() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.source",
                "version": 1,
                "outputs": [
                    {
                        "name": "enabled",
                        "type": "graphblocks.ai/Flag@1",
                    }
                ],
            },
            {"typeId": "test.branch", "version": 1},
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unknown-block-when-port"},
        "spec": {
            "nodes": {
                "source": {"block": "test.source@1"},
                "branch": {
                    "block": "test.branch@1",
                    "when": "source.missing",
                },
            },
        },
    }

    assert _error_diagnostics(graph, block_catalog=catalog) == [
        (
            "GB1014",
            "block test.source@1 has no output port 'missing'",
            "$.spec.nodes.branch.when",
        )
    ]
