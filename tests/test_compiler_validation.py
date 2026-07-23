from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog


def _error_diagnostics(
    graph: dict[str, object],
    *,
    block_catalog: BlockCatalog | None = None,
) -> list[tuple[str, str, str]]:
    plan = compile_graph(
        graph,
        block_catalog=block_catalog,
        allow_unknown_blocks=block_catalog is None,
    )
    return [
        (diagnostic.code, diagnostic.message, diagnostic.path)
        for diagnostic in plan.diagnostics.diagnostics
        if diagnostic.severity == "error"
    ]


def _unknown_block_graph() -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unknown-block"},
        "spec": {
            "nodes": {
                "unknown": {"block": "test.unknown@1"},
            },
        },
    }


def test_compile_uses_closed_builtin_catalog_by_default() -> None:
    plan = compile_graph(_unknown_block_graph())

    assert [
        diagnostic.code
        for diagnostic in plan.diagnostics.diagnostics
        if diagnostic.severity == "error"
    ] == ["GB1022"]


def test_compile_allows_unknown_blocks_only_with_explicit_discovery_opt_in() -> None:
    assert compile_graph(_unknown_block_graph(), allow_unknown_blocks=True).ok


def test_compile_normalizes_hostile_mapping_traversal_errors() -> None:
    class ExplodingMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError("hostile lookup")

        def __iter__(self) -> Iterator[str]:
            return iter(("apiVersion",))

        def __len__(self) -> int:
            return 1

    with pytest.raises(
        ValueError,
        match="graph document must contain stable canonical JSON values",
    ) as captured:
        compile_graph(ExplodingMapping())  # type: ignore[arg-type]

    assert isinstance(captured.value.__cause__, RuntimeError)


@pytest.mark.parametrize(
    ("camel_case", "snake_case"),
    (
        ("eventStream", "event_stream"),
        ("asyncOperations", "async_operations"),
        ("callbackSubscriptions", "callback_subscriptions"),
        ("outputPolicy", "output_policy"),
        ("toolExecution", "tool_execution"),
    ),
)
def test_compile_rejects_conflicting_graph_spec_aliases(
    camel_case: str,
    snake_case: str,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "conflicting-aliases"},
        "spec": {
            "nodes": {},
            camel_case: {},
            snake_case: {"conflicting": True},
        },
    }

    assert (
        "GB0014",
        f"configuration must not contain both {camel_case!r} and {snake_case!r} aliases",
        f"$.spec.{snake_case}",
    ) in _error_diagnostics(graph)


def test_compile_rejects_conflicting_nested_control_aliases() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "nested-conflicting-aliases"},
        "spec": {
            "nodes": {},
            "asyncOperations": {
                "upload": {
                    "expiresAtUnixMs": 1,
                    "expires_at_unix_ms": 2,
                }
            },
            "callbackSubscriptions": {
                "events": {
                    "failurePolicy": "ignore",
                    "failure_policy": "retry_then_dead_letter",
                    "delivery": {"kind": "local_callback"},
                }
            },
        },
    }

    diagnostics = _error_diagnostics(graph)

    assert (
        "GB0014",
        "configuration must not contain both 'expiresAtUnixMs' "
        "and 'expires_at_unix_ms' aliases",
        "$.spec.asyncOperations.upload.expires_at_unix_ms",
    ) in diagnostics
    assert (
        "GB0014",
        "configuration must not contain both 'failurePolicy' "
        "and 'failure_policy' aliases",
        "$.spec.callbackSubscriptions.events.failure_policy",
    ) in diagnostics


def test_plan_dictionary_projection_does_not_mutate_the_compiled_plan() -> None:
    plan = compile_graph(
        {
            "apiVersion": "graphblocks.ai/v1",
            "kind": "Graph",
            "metadata": {"name": "detached-plan"},
            "spec": {"nodes": {}},
        }
    )
    projection = plan.to_dict()
    projected_graph = projection["graph"]
    assert isinstance(projected_graph, dict)
    projected_metadata = projected_graph["metadata"]
    assert isinstance(projected_metadata, dict)

    projected_metadata["name"] = "mutated"

    assert plan.normalized["metadata"]["name"] == "detached-plan"
    assert plan.to_dict()["graph"]["metadata"]["name"] == "detached-plan"


def test_compile_reports_oversized_schema_version_as_diagnostic() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "oversized-schema-version"},
        "spec": {
            "interface": {
                "inputs": {"request": f"schemas/Request@{'9' * 10_000}"},
            },
            "nodes": {},
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB0015",
            "graph interface input schema id is invalid: "
            "schema id major version must be a positive integer",
            "$.spec.interface.inputs.request",
        )
    ]


def test_compile_rejects_duplicate_edge_identity() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "duplicate-edge"},
        "spec": {
            "interface": {
                "inputs": {"value": "schemas/Value@1"},
                "outputs": {"value": "schemas/Value@1"},
            },
            "nodes": {},
            "edges": [
                {"from": "$input.value", "to": "$output.value"},
                {"from": "$input.value", "to": "$output.value"},
            ],
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1005",
            "duplicate edge identity '$input.value' -> '$output.value'",
            "$.spec.edges[1]",
        )
    ]


@pytest.mark.parametrize("target", ["sink.value", "$output.value"])
def test_compile_rejects_distinct_sources_writing_the_same_target(
    target: str,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "competing-edge-sources"},
        "spec": {
            "interface": {
                "inputs": {
                    "left": "schemas/Value@1",
                    "right": "schemas/Value@1",
                },
                "outputs": {"value": "schemas/Value@1"},
            },
            "nodes": {"sink": {"block": "test.sink@1"}},
            "edges": [
                {"from": "$input.left", "to": target},
                {"from": "$input.right", "to": target},
            ],
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1007",
            f"multiple distinct edge sources write target {target!r}: "
            "'$input.left' and '$input.right'",
            "$.spec.edges[1]",
        )
    ]


def test_compile_allows_one_source_to_fan_out_to_multiple_targets() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "edge-fan-out"},
        "spec": {
            "interface": {
                "inputs": {"value": "schemas/Value@1"},
                "outputs": {
                    "left": "schemas/Value@1",
                    "right": "schemas/Value@1",
                },
            },
            "nodes": {},
            "edges": [
                {"from": "$input.value", "to": "$output.left"},
                {"from": "$input.value", "to": "$output.right"},
            ],
        },
    }

    assert _error_diagnostics(graph) == []


@pytest.mark.parametrize(
    ("first_target", "second_target"),
    (("sink.value", "sink.value.deep"), ("sink.value.deep", "sink.value")),
)
def test_compile_rejects_overlapping_nested_edge_targets(
    first_target: str,
    second_target: str,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "overlapping-edge-targets"},
        "spec": {
            "interface": {
                "inputs": {
                    "left": "schemas/Value@1",
                    "right": "schemas/Value@1",
                },
            },
            "nodes": {"sink": {"block": "test.sink@1"}},
            "edges": [
                {"from": "$input.left", "to": first_target},
                {"from": "$input.right", "to": second_target},
            ],
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1007",
            f"overlapping edge targets {first_target!r} and {second_target!r} "
            "cannot have independent writers",
            "$.spec.edges[1]",
        )
    ]


@pytest.mark.parametrize("pseudo_source", ["$state", "$context", "$execution"])
def test_compile_rejects_local_runtime_unsupported_pseudo_sources(
    pseudo_source: str,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "unsupported-pseudo-source"},
        "spec": {
            "interface": {"outputs": {"value": "schemas/Value@1"}},
            "nodes": {},
            "edges": [
                {"from": f"{pseudo_source}.value", "to": "$output.value"},
            ],
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1020",
            f"{pseudo_source} is not supported as an edge source by the local runtime",
            "$.spec.edges[0].from",
        )
    ]


@pytest.mark.parametrize("pseudo_target", ["$state", "$context", "$execution"])
def test_compile_rejects_local_runtime_unsupported_pseudo_targets(
    pseudo_target: str,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "unsupported-pseudo-target"},
        "spec": {
            "interface": {"inputs": {"value": "schemas/Value@1"}},
            "nodes": {},
            "edges": [
                {"from": "$input.value", "to": f"{pseudo_target}.value"},
            ],
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1020",
            f"{pseudo_target} is not supported as an edge target by the local runtime",
            "$.spec.edges[0].to",
        )
    ]


def test_compile_preserves_supported_input_to_output_pseudo_edge() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "supported-pseudo-edge"},
        "spec": {
            "interface": {
                "inputs": {"value": "schemas/Value@1"},
                "outputs": {"value": "schemas/Value@1"},
            },
            "nodes": {},
            "edges": [
                {"from": "$input.value", "to": "$output.value"},
            ],
        },
    }

    assert _error_diagnostics(graph) == []


def test_compile_rejects_unenforced_holdback_duration_bound() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "duration-only-holdback"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxDuration": "250ms",
                    "onViolation": "abort_response",
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit",
                    ],
                },
            },
            "nodes": {},
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1051",
            "holdback duration bounds are not supported by the local runtime",
            "$.spec.outputPolicy.delivery",
        )
    ]


def test_compile_rejects_explicit_numeric_nested_source_path() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "numeric-source-path"},
        "spec": {
            "interface": {
                "inputs": {"items": "schemas/Items@1"},
                "outputs": {"result": "schemas/Item@1"},
            },
            "nodes": {},
            "edges": [
                {"from": "$input.items.0", "to": "$output.result"},
            ],
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1020",
            "edge from endpoint must not contain numeric nested path segments",
            "$.spec.edges[0].from",
        )
    ]


def test_compile_rejects_list_shorthand_numeric_target_paths() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "numeric-shorthand-targets"},
        "spec": {
            "interface": {
                "inputs": {
                    "first": "schemas/Item@1",
                    "second": "schemas/Item@1",
                }
            },
            "nodes": {
                "sink": {
                    "block": "test.sink@1",
                    "inputs": {"items": ["$input.first", "$input.second"]},
                }
            },
        },
    }

    assert _error_diagnostics(graph) == [
        (
            "GB1020",
            "edge to endpoint must not contain numeric nested path segments",
            "$.spec.edges[0].to",
        ),
        (
            "GB1020",
            "edge to endpoint must not contain numeric nested path segments",
            "$.spec.edges[1].to",
        ),
    ]


def test_compile_preserves_nested_object_endpoint_paths() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "nested-object-paths"},
        "spec": {
            "interface": {
                "inputs": {"payload": "schemas/Payload@1"},
                "outputs": {"result": "schemas/Result@1"},
            },
            "nodes": {},
            "edges": [
                {
                    "from": "$input.payload.field",
                    "to": "$output.result.field",
                },
            ],
        },
    }

    assert _error_diagnostics(graph) == []


def test_compile_discovery_mode_still_validates_known_builtin_blocks() -> None:
    graph = _unknown_block_graph()
    spec = graph["spec"]
    assert isinstance(spec, dict)
    nodes = spec["nodes"]
    assert isinstance(nodes, dict)
    nodes["known"] = {"block": "prompt.render@1"}

    plan = compile_graph(graph, allow_unknown_blocks=True)

    assert [
        diagnostic.code
        for diagnostic in plan.diagnostics.diagnostics
        if diagnostic.severity == "error"
    ] == ["GB1003"]


def test_compile_supports_local_config_schema_references() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.local_ref",
                "version": 1,
                "configSchema": {
                    "$defs": {"count": {"type": "integer", "minimum": 1}},
                    "type": "object",
                    "properties": {"count": {"$ref": "#/$defs/count"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "local-config-ref"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.local_ref@1",
                    "config": {"count": 0},
                }
            }
        },
    }

    assert _error_diagnostics(graph, block_catalog=catalog) == [
        (
            "GB2019",
            "node config does not satisfy test.local_ref@1 configSchema: 0 is less than the minimum of 1",
            "$.spec.nodes.configured.config.count",
        )
    ]


def test_compile_reports_arbitrary_size_numeric_constraint_values_safely() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.maximum",
                "version": 1,
                "configSchema": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "maximum": 1},
                    },
                    "required": ["count"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "arbitrary-size-config-value"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.maximum@1",
                    "config": {"count": 10**5_000},
                }
            }
        },
    }

    assert _error_diagnostics(graph, block_catalog=catalog) == [
        (
            "GB2019",
            "node config does not satisfy test.maximum@1 configSchema: "
            "<arbitrary-size integer> is greater than the maximum of 1",
            "$.spec.nodes.configured.config.count",
        )
    ]


def test_compile_bounds_config_validation_message_size() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.pattern",
                "version": 1,
                "configSchema": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "pattern": "^allowed$"},
                    },
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "bounded-config-message"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.pattern@1",
                    "config": {"value": "x" * 100_000},
                }
            }
        },
    }

    diagnostics = _error_diagnostics(graph, block_catalog=catalog)

    assert len(diagnostics) == 1
    assert len(diagnostics[0][1]) < 1_200
    assert diagnostics[0][1].endswith("...")


def test_compile_bounds_retained_config_validation_errors() -> None:
    property_count = 105
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.many_errors",
                "version": 1,
                "configSchema": {
                    "type": "object",
                    "properties": {
                        f"field_{index}": {"type": "integer"}
                        for index in range(property_count)
                    },
                    "additionalProperties": False,
                },
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "bounded-config-errors"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.many_errors@1",
                    "config": {
                        f"field_{index}": "invalid"
                        for index in range(property_count)
                    },
                }
            }
        },
    }

    diagnostics = _error_diagnostics(graph, block_catalog=catalog)

    assert len(diagnostics) == 101
    assert diagnostics[-1] == (
        "GB2019",
        "node config does not satisfy test.many_errors@1 configSchema: "
        "5 additional violations were omitted after the first 100",
        "$.spec.nodes.configured.config",
    )


@pytest.mark.parametrize(
    ("property_schema", "expected_message"),
    [
        (
            {"type": "string"},
            'value must have JSON type "string"',
        ),
        (
            {"enum": [0]},
            "value must be one of [0]",
        ),
    ],
    ids=["type", "enum"],
)
def test_compile_reports_arbitrary_size_values_safely_for_every_keyword(
    property_schema: dict[str, object],
    expected_message: str,
) -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.arbitrary_size",
                "version": 1,
                "configSchema": {
                    "type": "object",
                    "properties": {"value": property_schema},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "arbitrary-size-config-value"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.arbitrary_size@1",
                    "config": {"value": 10**5_000},
                }
            }
        },
    }

    assert _error_diagnostics(graph, block_catalog=catalog) == [
        (
            "GB2019",
            "node config does not satisfy test.arbitrary_size@1 configSchema: "
            f"{expected_message}",
            "$.spec.nodes.configured.config.value",
        )
    ]


def test_compile_reports_arbitrary_size_values_safely_across_schema_dialects() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.dialect",
                "version": 1,
                "configSchema": {
                    "$defs": {
                        "bounded": {
                            "$schema": "https://json-schema.org/draft/2020-12/schema",
                            "type": "integer",
                            "maximum": 1,
                        }
                    },
                    "type": "object",
                    "properties": {"count": {"$ref": "#/$defs/bounded"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "arbitrary-size-dialect-config"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.dialect@1",
                    "config": {"count": 10**5_000},
                }
            }
        },
    }

    assert _error_diagnostics(graph, block_catalog=catalog) == [
        (
            "GB2019",
            "node config does not satisfy test.dialect@1 configSchema: "
            "<arbitrary-size integer> is greater than the maximum of 1",
            "$.spec.nodes.configured.config.count",
        )
    ]


def test_compile_reports_arbitrary_size_numeric_constraints_safely() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.minimum",
                "version": 1,
                "configSchema": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "minimum": 10**5_000},
                    },
                    "required": ["count"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "arbitrary-size-config-constraint"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.minimum@1",
                    "config": {"count": 1},
                }
            }
        },
    }

    assert _error_diagnostics(graph, block_catalog=catalog) == [
        (
            "GB2019",
            "node config does not satisfy test.minimum@1 configSchema: "
            "1 is less than the minimum of <arbitrary-size integer>",
            "$.spec.nodes.configured.config.count",
        )
    ]


@pytest.mark.parametrize(
    "config_schema",
    [
        {
            "type": "object",
            "properties": {"count": {"$ref": "#/$defs/missing"}},
        },
        {"$ref": "#"},
    ],
    ids=["unresolved", "nonterminating"],
)
def test_compile_reports_invalid_local_config_schema_reference(
    config_schema: dict[str, object],
) -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.missing_ref",
                "version": 1,
                "configSchema": config_schema,
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "missing-config-ref"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.missing_ref@1",
                    "config": {"count": 1},
                }
            }
        },
    }

    assert _error_diagnostics(graph, block_catalog=catalog) == [
        (
            "GB2019",
            "node config cannot be validated against test.missing_ref@1 because its configSchema contains an unresolved or nonterminating local reference",
            "$.spec.nodes.configured.config",
        )
    ]


def test_compile_rejects_unsupported_control_map_modes() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "unsupported-control-map-mode"},
        "spec": {
            "nodes": {
                "map": {
                    "block": "control.map@2",
                    "config": {"graph": "nested-graph"},
                }
            }
        },
    }

    diagnostics = [
        diagnostic
        for diagnostic in _error_diagnostics(graph)
        if diagnostic[0] == "GB2019"
    ]

    assert [code for code, _message, _path in diagnostics] == ["GB2019", "GB2019"]
    assert {path for _code, _message, path in diagnostics} == {
        "$.spec.nodes.map.config"
    }


@pytest.mark.parametrize(
    ("edges", "expected_warnings"),
    [
        ([], ["GB1004"]),
        ([{"from": "$input.value", "to": "$output.value"}], []),
    ],
    ids=["missing-writer", "written"],
)
def test_compile_warns_when_declared_graph_outputs_have_no_writer(
    edges: list[dict[str, str]],
    expected_warnings: list[str],
) -> None:
    graph: dict[str, object] = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "declared-output"},
        "spec": {
            "interface": {
                "inputs": {"value": "graphblocks.ai/Text@1"},
                "outputs": {"value": "graphblocks.ai/Text@1"},
            },
            "nodes": {},
            "edges": edges,
        },
    }

    plan = compile_graph(graph, allow_unknown_blocks=True)

    assert plan.ok
    assert [
        diagnostic.code
        for diagnostic in plan.diagnostics.diagnostics
        if diagnostic.severity == "warning"
    ] == expected_warnings


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
        (
            "$context.enabled",
            "$context is not supported as a when source by the local runtime",
        ),
        (False, "node when reference must be a string"),
    ],
    ids=["missing-port", "output-source", "unsupported-pseudo", "non-string"],
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
