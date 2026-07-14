from __future__ import annotations

import sys
from decimal import Decimal
from types import SimpleNamespace

import graphblocks
import pytest
from graphblocks import (
    canonical_dumps,
    canonical_hash,
    canonical_loads,
    compile_graph,
    compile_graph_native,
    normalize_graph,
)
from graphblocks import compiler as compiler_module
from graphblocks.output_policy import (
    VALID_DELIVERY_MODES,
    VALID_DRAFT_DISPOSITIONS,
    VALID_FLUSH_BOUNDARIES,
    VALID_OUTPUT_DISPOSITIONS,
    VALID_OUTPUT_DURABLE_RESULTS,
    VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
    VALID_PROVIDER_CANCELLATIONS,
    VALID_VIOLATION_ACTIONS,
)
from graphblocks.policy import VALID_ENFORCEMENT_POINTS
from graphblocks.plugins import BlockCatalog
from graphblocks.tools import (
    VALID_TOOL_APPROVALS,
    VALID_TOOL_CANCELLATIONS,
    VALID_TOOL_EFFECTS,
    VALID_TOOL_IDEMPOTENCIES,
    VALID_TOOL_RESULT_MODES,
)


DISCOVERY_CATALOG = BlockCatalog({}, allow_unknown_blocks=True)


def _error_codes(graph: dict) -> list[str]:
    return [
        item.code
        for item in compile_graph(
            graph,
            block_catalog=DISCOVERY_CATALOG,
        ).diagnostics.diagnostics
        if item.severity == "error"
    ]


def test_python_compiler_uses_canonical_literal_sets() -> None:
    assert compiler_module.VALID_TOOL_EFFECTS is VALID_TOOL_EFFECTS
    assert compiler_module.VALID_TOOL_APPROVALS is VALID_TOOL_APPROVALS
    assert compiler_module.VALID_TOOL_IDEMPOTENCIES is VALID_TOOL_IDEMPOTENCIES
    assert compiler_module.VALID_TOOL_CANCELLATIONS is VALID_TOOL_CANCELLATIONS
    assert compiler_module.VALID_TOOL_RESULT_MODES is VALID_TOOL_RESULT_MODES
    assert compiler_module.VALID_OUTPUT_DELIVERY_MODES is VALID_DELIVERY_MODES
    assert compiler_module.VALID_VIOLATION_ACTIONS is VALID_VIOLATION_ACTIONS
    assert compiler_module.VALID_DRAFT_DISPOSITIONS is VALID_DRAFT_DISPOSITIONS
    assert compiler_module.VALID_FLUSH_BOUNDARIES is VALID_FLUSH_BOUNDARIES
    assert compiler_module.VALID_OUTPUT_DISPOSITIONS is VALID_OUTPUT_DISPOSITIONS
    assert compiler_module.VALID_PROVIDER_CANCELLATIONS is VALID_PROVIDER_CANCELLATIONS
    assert compiler_module.VALID_PENDING_TOOL_CALLS_DISPOSITIONS is VALID_PENDING_TOOL_CALLS_DISPOSITIONS
    assert compiler_module.VALID_OUTPUT_DURABLE_RESULTS is VALID_OUTPUT_DURABLE_RESULTS
    assert compiler_module.VALID_POLICY_ENFORCEMENT_POINTS is VALID_ENFORCEMENT_POINTS


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


def test_canonical_json_rejects_non_string_object_keys() -> None:
    with pytest.raises(TypeError, match="canonical JSON object keys must be strings"):
        canonical_dumps({1: "coerced"})
    with pytest.raises(TypeError, match="canonical JSON object keys must be strings"):
        canonical_dumps({"nested": {2: "coerced"}})
    with pytest.raises(TypeError, match="canonical JSON object keys must be strings"):
        canonical_hash({"nested": {3: "coerced"}})


def test_canonical_json_rejects_duplicate_object_keys() -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key 'policy'"):
        canonical_loads('{"policy":"deny","policy":"allow"}')


@pytest.mark.parametrize("container_kind", ["mapping", "array"])
def test_canonical_json_rejects_recursive_values(container_kind: str) -> None:
    if container_kind == "mapping":
        value: object = {}
        value["self"] = value  # type: ignore[index]
    else:
        value = []
        value.append(value)  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="must not be recursive"):
        canonical_dumps(value)


def test_canonical_json_allows_shared_non_recursive_values() -> None:
    shared = {"value": Decimal("1.25")}

    assert canonical_dumps({"left": shared, "right": shared}) == (
        '{"left":{"value":1.25},"right":{"value":1.25}}'
    )


def test_canonical_json_normalizes_decimal_numbers_beyond_binary64_range() -> None:
    value = {
        "equivalent": Decimal("10e399"),
        "huge": Decimal("1e400"),
        "negative": Decimal("-0.01e402"),
    }

    assert canonical_dumps(value) == (
        '{"equivalent":1e+400,"huge":1e+400,"negative":-1e+400}'
    )
    assert canonical_dumps(canonical_loads("[10e999999,1e1000000]")) == (
        "[1e+1000000,1e+1000000]"
    )
    assert canonical_dumps([Decimal("1e-7"), Decimal("1e16"), Decimal("1.0")]) == (
        "[1e-07,1e+16,1.0]"
    )


def test_canonical_json_preserves_raw_numbers_beyond_binary64_range() -> None:
    value = canonical_loads(
        '{"ordinary":1.5,"equivalent":10e399,"huge":1e400,"negative":-0.01e402}'
    )

    assert type(value["ordinary"]) is float
    assert value["huge"] == Decimal("1e400")
    assert canonical_dumps(value) == (
        '{"equivalent":1e+400,"huge":1e+400,"negative":-1e+400,"ordinary":1.5}'
    )


def test_canonical_json_keeps_distinct_decimals_above_binary64_integer_precision() -> None:
    left = canonical_loads("9007199254740992.0")
    right = canonical_loads("9007199254740993.0")
    right_equivalent = canonical_loads("90071992547409930e-1")

    assert canonical_dumps(left) == "9007199254740992.0"
    assert canonical_dumps(right) == "9007199254740993.0"
    assert canonical_dumps(right_equivalent) == "9007199254740993.0"
    assert canonical_hash(left) != canonical_hash(right)
    assert canonical_hash(right) == canonical_hash(right_equivalent)


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")))
def test_canonical_json_rejects_non_finite_decimals(value: Decimal) -> None:
    with pytest.raises(ValueError, match="Out of range decimal values"):
        canonical_dumps(value)


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


def test_normalize_graph_rejects_unknown_graph_versions() -> None:
    with pytest.raises(ValueError, match="GB0002"):
        normalize_graph(
            {
                "apiVersion": "graphblocks.ai/v2",
                "kind": "Graph",
                "metadata": {"name": "future-graph"},
                "spec": {"nodes": {}},
            }
        )


def test_compile_reports_invalid_interface_schema_ids() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-interface-schema"},
        "spec": {
            "interface": {
                "inputs": {"request": "schemas/Request"},
                "outputs": {"result": "schemas/Result"},
            },
            "nodes": {},
        },
    }

    assert _error_codes(graph) == ["GB0015", "GB0015"]


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

    plan = compile_graph(graph, block_catalog=DISCOVERY_CATALOG)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics] == ["GB1002"]


def test_compile_rejects_effect_retry_without_idempotency_key() -> None:
    for effect in ("external_write", "filesystem_write"):
        graph = {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": f"unsafe-retry-{effect}"},
            "spec": {
                "nodes": {
                    "write": {
                        "block": "storage.write@1",
                        "effects": [effect],
                        "flow": {"retry": {"maxAttempts": 2}},
                    }
                }
            },
        }

        plan = compile_graph(graph, block_catalog=DISCOVERY_CATALOG)

        assert not plan.ok
        assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1011"]


def test_compile_does_not_coerce_non_numeric_effect_retry_attempts() -> None:
    for max_attempts in ("2", "two", True):
        graph = {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "non-numeric-retry"},
            "spec": {
                "nodes": {
                    "write": {
                        "block": "storage.write@1",
                        "effects": ["external_write"],
                        "flow": {"retry": {"maxAttempts": max_attempts}},
                    }
                }
            },
        }

        plan = compile_graph(graph, block_catalog=DISCOVERY_CATALOG)

        assert "GB1011" not in [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"]


def test_compile_allows_effect_retry_with_idempotency_key() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "safe-retry"},
        "spec": {
            "nodes": {
                "write": {
                    "block": "storage.write@1",
                    "effects": ["external_write"],
                    "flow": {"retry": {"maxAttempts": 2, "idempotencyKey": "$input.request_id"}},
                }
            }
        },
    }

    plan = compile_graph(graph, block_catalog=DISCOVERY_CATALOG)

    assert "GB1011" not in [item.code for item in plan.diagnostics.diagnostics]


@pytest.mark.parametrize("idempotency_key", ("", " ", " idem-1", {"path": "$input.request_id"}))
def test_compile_rejects_effect_retry_with_invalid_idempotency_key(idempotency_key: object) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unsafe-invalid-idempotency-key"},
        "spec": {
            "nodes": {
                "write": {
                    "block": "storage.write@1",
                    "effects": ["external_write"],
                    "flow": {"retry": {"maxAttempts": 2, "idempotencyKey": idempotency_key}},
                }
            }
        },
    }

    plan = compile_graph(graph, block_catalog=DISCOVERY_CATALOG)

    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1011"]


def test_native_compile_helper_delegates_to_runtime(monkeypatch) -> None:
    calls: list[tuple[dict[str, object], object | None, bool]] = []

    def native_compile_graph(
        document: dict[str, object],
        block_catalog: object | None = None,
        *,
        allow_unknown_blocks: bool = False,
    ) -> dict[str, object]:
        calls.append((document, block_catalog, allow_unknown_blocks))
        return {
            "ok": True,
            "graph": document,
            "blockCatalog": block_catalog,
            "allowUnknownBlocks": allow_unknown_blocks,
            "diagnostics": [],
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(compile_graph=native_compile_graph),
    )

    result = compile_graph_native(
        {"kind": "Graph", "metadata": {"name": "native"}},
        block_catalog=[{"typeId": "prompt.render@1"}],
        allow_unknown_blocks=True,
    )

    assert result == {
        "ok": True,
        "graph": {"kind": "Graph", "metadata": {"name": "native"}},
        "blockCatalog": [{"typeId": "prompt.render@1"}],
        "allowUnknownBlocks": True,
        "diagnostics": [],
    }
    assert calls == [
        (
            {"kind": "Graph", "metadata": {"name": "native"}},
            [{"typeId": "prompt.render@1"}],
            True,
        )
    ]
    assert "compile_graph_native" in graphblocks.__all__


def test_compile_rejects_unbounded_output_holdback_and_unsafe_immediate_draft() -> None:
    base = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "output-policy"},
        "spec": {"nodes": {"model": {"block": "model.generate@1"}}},
    }

    unbounded = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                "delivery": {"mode": "bounded_holdback", "onViolation": "abort_response"}
            },
        },
    }
    immediate_draft = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                "delivery": {
                    "mode": "immediate_draft",
                    "onViolation": "abort_response",
                    "deliveredDraftDisposition": "keep",
                }
            },
        },
    }
    boolean_bound = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": True,
                    "onViolation": "abort_response",
                }
            },
        },
    }
    invalid_duration_bound = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxDuration": "soon",
                    "onViolation": "abort_response",
                }
            },
        },
    }

    assert _error_codes(unbounded) == ["GB1051", "GB1046"]
    assert _error_codes(boolean_bound) == ["GB1051", "GB1046"]
    assert _error_codes(invalid_duration_bound) == ["GB1051", "GB1046"]
    assert _error_codes(immediate_draft) == [
        "GB1025",
        "GB1046",
    ]


def test_compile_rejects_output_policy_bypass_and_gate_after_delivery() -> None:
    base = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "output-policy"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response",
                },
                "evaluation": {"enforcementPoints": ["on_generation_chunk", "before_output_commit"]},
            },
        },
    }
    missing_enforcement_points = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                "delivery": base["spec"]["outputPolicy"]["delivery"],
            },
        },
    }
    missing_generation_gate = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                **base["spec"]["outputPolicy"],
                "evaluation": {
                    "enforcementPoints": ["before_client_delivery"],
                },
            },
        },
    }
    missing_commit_gate = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                **base["spec"]["outputPolicy"],
                "evaluation": {
                    "enforcementPoints": ["on_generation_chunk", "before_client_delivery"],
                },
            },
        },
    }
    late_gate = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                **base["spec"]["outputPolicy"],
                "evaluation": {
                    "enforcementPoints": [
                        "before_client_delivery",
                        "on_generation_chunk",
                        "before_output_commit",
                    ]
                },
            },
        },
    }

    assert _error_codes(base) == ["GB1046"]
    assert _error_codes(missing_enforcement_points) == ["GB1046"]
    assert _error_codes(missing_generation_gate) == ["GB1046"]
    assert _error_codes(missing_commit_gate) == ["GB1046"]
    assert _error_codes(late_gate) == ["GB1048"]


def test_compile_reports_malformed_output_policy_shapes() -> None:
    base = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "malformed-output-policy"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response",
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit",
                    ]
                },
                "onViolation": {
                    "disposition": "abort_response",
                    "pendingToolCalls": {"disposition": "deny"},
                    "durableResult": {"disposition": "none"},
                },
            },
        },
    }
    non_mapping_policy = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": "standard",
        },
    }
    non_mapping_delivery = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                **base["spec"]["outputPolicy"],
                "delivery": "bounded_holdback",
            },
        },
    }
    non_mapping_evaluation = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                **base["spec"]["outputPolicy"],
                "evaluation": "runtime",
            },
        },
    }
    non_list_enforcement_points = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                **base["spec"]["outputPolicy"],
                "evaluation": {"enforcementPoints": "before_client_delivery"},
            },
        },
    }
    non_mapping_on_violation = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                **base["spec"]["outputPolicy"],
                "onViolation": "abort_response",
            },
        },
    }

    assert _error_codes(base) == []
    assert _error_codes(non_mapping_policy) == ["GB1034"]
    assert _error_codes(non_mapping_delivery) == ["GB1034"]
    assert _error_codes(non_mapping_evaluation) == ["GB1034", "GB1046"]
    assert _error_codes(non_list_enforcement_points) == ["GB1033", "GB1046"]
    assert _error_codes(non_mapping_on_violation) == ["GB1034"]


def test_compile_rejects_policy_abort_that_keeps_tools_or_commits_result() -> None:
    base = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "policy-abort-cleanup"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response",
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit",
                    ]
                },
                "onViolation": {
                    "disposition": "abort_response",
                    "pendingToolCalls": {"disposition": "keep"},
                    "durableResult": {"disposition": "none"},
                },
            },
        },
    }
    commits_result = {
        **base,
        "spec": {
            **base["spec"],
            "outputPolicy": {
                **base["spec"]["outputPolicy"],
                "onViolation": {
                    "disposition": "abort_response",
                    "pendingToolCalls": {"disposition": "deny"},
                    "durableResult": {"disposition": "partial"},
                },
            },
        },
    }

    assert _error_codes(base) == ["GB1047"]
    assert _error_codes(commits_result) == ["GB1024"]


def test_compile_reports_invalid_output_policy_literals() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-output-policy-literals"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "outputPolicy": {
                "delivery": {
                    "mode": "stream",
                    "holdbackMaxTokens": 48,
                    "onViolation": "pause",
                    "flushBoundaries": ["sentence", "clause"],
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit",
                        "after_client_delivery",
                    ]
                },
                "onViolation": {
                    "disposition": "halt",
                    "providerCancellation": {"mode": "force"},
                    "pendingToolCalls": {"disposition": "pause"},
                    "deliveredDraft": {"disposition": "erase"},
                    "durableResult": {"disposition": "committed"},
                },
            },
        },
    }

    assert _error_codes(graph) == [
        "GB1030",
        "GB1044",
        "GB1029",
        "GB1033",
        "GB1031",
        "GB1036",
        "GB1035",
        "GB1028",
        "GB1032",
    ]


def test_compile_allows_safe_output_policy_settings() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "safe-output-policy"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response",
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit",
                    ]
                },
                "onViolation": {
                    "disposition": "abort_response",
                    "pendingToolCalls": {"disposition": "deny"},
                    "durableResult": {"disposition": "none"},
                },
            },
        },
    }

    assert not {
        "GB1051",
        "GB1025",
        "GB1046",
        "GB1048",
        "GB1047",
        "GB1024",
        "GB1030",
        "GB1044",
        "GB1029",
        "GB1033",
        "GB1031",
        "GB1036",
        "GB1035",
        "GB1028",
        "GB1032",
    } & set(_error_codes(graph))


def test_compile_reports_tool_definition_without_binding_or_input_schema() -> None:
    missing_binding = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "missing-tool-binding"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                        }
                    }
                }
            },
        },
    }
    missing_schema = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "missing-tool-schema"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                        },
                        "implementation": {"kind": "block", "block": "blocks.search"},
                    }
                }
            },
        },
    }
    missing_definition = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "missing-tool-definition"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "implementation": {"kind": "block", "block": "blocks.search"},
                    }
                }
            },
        },
    }
    invalid_schema = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-tool-schema"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search",
                        },
                        "implementation": {"kind": "block", "block": "blocks.search"},
                    }
                }
            },
        },
    }

    assert _error_codes(missing_binding) == ["GB1049"]
    assert _error_codes(missing_schema) == ["GB1050"]
    assert _error_codes(missing_definition) == ["GB1050"]
    assert _error_codes(invalid_schema) == ["GB0015"]


def test_compile_reports_malformed_tool_implementation_bindings() -> None:
    base = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "malformed-tool-implementation"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                        },
                        "implementation": {"kind": "block"},
                    }
                }
            },
        },
    }
    unknown_kind = {
        **base,
        "spec": {
            **base["spec"],
            "bindings": {
                "tools": {
                    "search": {
                        **base["spec"]["bindings"]["tools"]["search"],
                        "implementation": {"kind": "lambda", "function": "search"},
                    }
                }
            },
        },
    }
    missing_openapi_operation = {
        **base,
        "spec": {
            **base["spec"],
            "bindings": {
                "tools": {
                    "search": {
                        **base["spec"]["bindings"]["tools"]["search"],
                        "implementation": {"kind": "openapi", "connection": "ticket-system"},
                    }
                }
            },
        },
    }

    assert _error_codes(base) == ["GB1049"]
    assert _error_codes(unknown_kind) == ["GB1049"]
    assert _error_codes(missing_openapi_operation) == ["GB1049"]


def test_compile_reports_malformed_tool_definition_identity_fields() -> None:
    blank_name = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "blank-tool-definition-name"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": " ",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                        },
                        "implementation": {"kind": "block", "block": "blocks.search"},
                    }
                }
            },
        },
    }
    non_string_description = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "non-string-tool-definition-description"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": {"text": "Search documentation."},
                            "inputSchema": "schemas/Search@1",
                        },
                        "implementation": {"kind": "block", "block": "blocks.search"},
                    }
                }
            },
        },
    }
    blank_version = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "blank-tool-definition-version"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                            "version": " ",
                        },
                        "implementation": {"kind": "block", "block": "blocks.search"},
                    }
                }
            },
        },
    }
    non_string_tag = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "non-string-tool-definition-tag"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                            "tags": ["knowledge", 7],
                        },
                        "implementation": {"kind": "block", "block": "blocks.search"},
                    }
                }
            },
        },
    }

    assert _error_codes(blank_name) == ["GB1039"]
    assert _error_codes(non_string_description) == ["GB1039"]
    assert _error_codes(blank_version) == ["GB1039"]
    assert _error_codes(non_string_tag) == ["GB1039"]


def test_compile_rejects_forbidden_tool_definition_execution_details() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "tool-definition-leaks-execution-details"},
        "spec": {
            "nodes": {"model": {"block": "model.generate@1"}},
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                            "credentials": {"secretRef": "support-search-token"},
                            "connection": "support-api",
                            "implementation": {"kind": "remote"},
                        },
                        "implementation": {"kind": "block", "block": "blocks.search"},
                    }
                }
            },
        },
    }

    assert _error_codes(graph) == [
        "GB1039",
        "GB1039",
        "GB1039",
    ]


def test_compile_reports_invalid_tool_effect_literals() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-tool-effect"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1",
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket",
                        },
                        "effects": ["external-write"],
                    }
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB1040"]

    conflicting_none = {
        **graph,
        "metadata": {"name": "conflicting-none-effect"},
        "spec": {
            **graph["spec"],
            "bindings": {
                "tools": {
                    "createTicket": {
                        **graph["spec"]["bindings"]["tools"]["createTicket"],
                        "effects": ["none", "network"],
                    }
                }
            },
        },
    }

    assert _error_codes(conflicting_none) == ["GB1040"]


def test_compile_reports_invalid_tool_binding_literals() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-tool-binding-literals"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1",
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket",
                        },
                        "effects": ["external_write", "network"],
                        "approval": {"mode": "sometimes"},
                        "idempotency": "maybe",
                        "cancellation": "eventually",
                        "resultMode": "firehose",
                    }
                }
            },
        },
    }

    assert _error_codes(graph) == [
        "GB1037",
        "GB1042",
        "GB1038",
        "GB1043",
    ]


def test_compile_rejects_parallel_state_changing_tools_without_effect_serialization() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unsafe-parallel-tools"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1",
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket",
                        },
                        "effects": ["external_write", "network"],
                    }
                }
            },
            "toolExecution": {"maximumParallelism": 4},
        },
    }

    assert _error_codes(graph) == ["GB1053"]


def test_compile_rejects_malformed_tool_execution_settings() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "malformed-tool-execution"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1",
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket",
                        },
                        "effects": ["external_write", "network"],
                    }
                }
            },
            "toolExecution": {
                "maximumParallelism": 0,
                "parallelToolCalls": "false",
                "effectSerialization": {"keyTemplate": ""},
            },
        },
    }
    non_mapping = {
        **graph,
        "spec": {
            **graph["spec"],
            "toolExecution": "parallel",
        },
    }
    non_mapping_effect_serialization = {
        **graph,
        "spec": {
            **graph["spec"],
            "toolExecution": {
                "maximumParallelism": 1,
                "effectSerialization": "resource",
            },
        },
    }

    assert _error_codes(graph) == [
        "GB1041",
        "GB1041",
        "GB1041",
    ]
    assert _error_codes(non_mapping) == ["GB1041"]
    assert _error_codes(non_mapping_effect_serialization) == ["GB1041"]


def test_compile_rejects_retried_write_tool_without_required_idempotency() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "nonidempotent-tool-retry"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1",
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket",
                        },
                        "effects": ["external_write", "network"],
                        "retryPolicyRef": "retry/default",
                    }
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB1045"]


def test_compile_rejects_tool_approval_without_argument_digest_binding() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "approval-without-argument-digest"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1",
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket",
                        },
                        "effects": ["external_write", "network"],
                        "approval": {"mode": "always"},
                    }
                }
            },
        },
    }

    string_approval = {
        **graph,
        "metadata": {"name": "string-approval-without-argument-digest"},
        "spec": {
            **graph["spec"],
            "bindings": {
                "tools": {
                    "createTicket": {
                        **graph["spec"]["bindings"]["tools"]["createTicket"],
                        "approval": "always",
                    }
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB1023"]
    assert _error_codes(string_approval) == ["GB1023"]


def test_compile_allows_safe_tool_execution_settings() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "safe-tool-settings"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1",
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket",
                        },
                        "effects": ["external_write", "network"],
                        "retryPolicyRef": "retry/default",
                        "idempotency": "required",
                        "approval": {"mode": "always", "bindArgumentsDigest": True},
                    }
                }
            },
            "toolExecution": {
                "maximumParallelism": 4,
                "effectSerialization": {"keyTemplate": "{tool.name}:{arguments.resource_id}"},
            },
        },
    }

    assert not {
        "GB1049",
        "GB1050",
        "GB1053",
        "GB1045",
        "GB1023",
    } & set(_error_codes(graph))


def test_compile_reports_async_operation_missing_timeout_idempotency_and_schema() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "async-operation-missing-contracts"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "asyncOperations": {
                "ci": {
                    "kind": "ci_job",
                    "callback": {"required": True},
                    "resume": {
                        "requirePolicyReevaluation": True,
                        "requireBudgetReservation": True,
                        "requireReleaseCompatibility": True,
                        "requireOwnershipFence": True,
                    },
                    "attemptFencing": True,
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB6001", "GB6003", "GB6007"]


def test_compile_reports_async_start_operation_node_missing_callback_contracts() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "async-start-operation-missing-contracts"},
        "spec": {
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "provider": "github-actions",
                        "operation": "workflow_dispatch",
                        "callback": {"required": True},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB6001", "GB6003", "GB6007"]


def test_compile_reports_async_poll_operation_node_missing_timeout() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "async-poll-operation-missing-timeout"},
        "spec": {
            "nodes": {
                "pollCI": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "intervalMs": 30_000,
                        "maxIntervalMs": 300_000,
                        "idempotencyKey": "$input.request_id",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB6001"]


def test_compile_reports_async_poll_operation_node_zero_timeout() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "async-poll-operation-zero-timeout"},
        "spec": {
            "nodes": {
                "pollCI": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "timeoutMs": 0,
                        "idempotencyKey": "$input.request_id",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB6001"]


def test_compile_reports_async_poll_operation_node_invalid_string_timeout() -> None:
    for timeout in ("0ms", "soon"):
        graph = {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": f"async-poll-operation-invalid-timeout-{timeout}"},
            "spec": {
                "nodes": {
                    "pollCI": {
                        "block": "async.poll_operation@1",
                        "config": {
                            "timeout": timeout,
                            "idempotencyKey": "$input.request_id",
                            "callback": {"schema": "schemas/PollResult@1"},
                            "resume": {
                                "requirePolicyReevaluation": True,
                                "requireBudgetReservation": True,
                                "requireReleaseCompatibility": True,
                                "requireOwnershipFence": True,
                            },
                            "attemptFencing": True,
                        },
                    }
                },
            },
        }

        assert _error_codes(graph) == ["GB6001"]


def test_compile_reports_async_poll_operation_node_invalid_interval_durations() -> None:
    for field, value in (("interval", "0s"), ("maxInterval", "soon")):
        graph = {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": f"async-poll-operation-invalid-{field}"},
            "spec": {
                "nodes": {
                    "pollCI": {
                        "block": "async.poll_operation@1",
                        "config": {
                            "timeout": "30m",
                            field: value,
                            "idempotencyKey": "$input.request_id",
                            "callback": {"schema": "schemas/PollResult@1"},
                            "resume": {
                                "requirePolicyReevaluation": True,
                                "requireBudgetReservation": True,
                                "requireReleaseCompatibility": True,
                                "requireOwnershipFence": True,
                            },
                            "attemptFencing": True,
                        },
                    }
                },
            },
        }

        assert _error_codes(graph) == ["GB1026"]


def test_compile_reports_async_await_callback_node_invalid_on_timeout_policy() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "async-await-invalid-on-timeout"},
        "spec": {
            "nodes": {
                "waitCI": {
                    "block": "async.await_callback@1",
                    "config": {
                        "timeout": "30m",
                        "onTimeout": "continue_anyway",
                        "idempotencyKey": "$input.request_id",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB1026"]


def test_compile_reports_async_operation_missing_resume_and_fencing_contracts() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "async-operation-missing-resume-contracts"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "asyncOperations": {
                "ci": {
                    "kind": "ci_job",
                    "timeout": "30m",
                    "idempotencyKey": "$input.request_id",
                    "callback": {
                        "required": True,
                        "schema": "schemas/CICallback@1",
                    },
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB6008", "GB6015", "GB6016"]


def test_compile_rejects_async_operation_with_callback_and_polling_refs() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "async-operation-ambiguous-completion"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "asyncOperations": {
                "external": {
                    "kind": "external_provider_job",
                    "timeout": "30m",
                    "idempotencyKey": "$input.request_id",
                    "callback": {
                        "required": True,
                        "schema": "schemas/ExternalCallback@1",
                    },
                    "polling": {
                        "endpoint": "providers/batch/status",
                    },
                    "resume": {
                        "requirePolicyReevaluation": True,
                        "requireBudgetReservation": True,
                        "requireReleaseCompatibility": True,
                        "requireOwnershipFence": True,
                    },
                    "attemptFencing": True,
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB1026"]


def test_compile_allows_async_operation_with_timeout_idempotency_and_schema() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "async-operation-safe-contracts"},
        "spec": {
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "provider": "github-actions",
                        "operation": "workflow_dispatch",
                        "timeout": "30m",
                        "idempotencyKey": "$input.request_id",
                        "callback": {
                            "required": True,
                            "schema": "schemas/CICallback@1",
                        },
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                }
            },
            "asyncOperations": {
                "ci": {
                    "kind": "ci_job",
                    "timeout": "30m",
                    "idempotencyKey": "$input.request_id",
                    "callback": {
                        "required": True,
                        "schema": "schemas/CICallback@1",
                    },
                    "resume": {
                        "requirePolicyReevaluation": True,
                        "requireBudgetReservation": True,
                        "requireReleaseCompatibility": True,
                        "requireOwnershipFence": True,
                    },
                    "attemptFencing": True,
                }
            },
        },
    }

    assert not {"GB6001", "GB6003", "GB6007", "GB6008", "GB6015", "GB6016"} & set(_error_codes(graph))


def test_compile_reports_callback_subscription_safety_diagnostics() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unsafe-callback-subscription"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-unsafe",
                    "scope": "run",
                    "scopeId": "run-1",
                    "authoritativeFor": ["billing"],
                    "delivery": {
                        "kind": "webhook",
                        "url": "http://127.0.0.1/events",
                    },
                },
                {
                    "subscriptionId": "sub-mandatory",
                    "scope": "run",
                    "scopeId": "run-1",
                    "mandatory": True,
                    "delivery": {
                        "kind": "local_callback",
                        "callbackName": "ide",
                        "ordering": {"mode": "ordered", "scope": "run"},
                    },
                },
                {
                    "subscriptionId": "sub-fail",
                    "scope": "run",
                    "scopeId": "run-1",
                    "failurePolicy": "fail_run_on_failure",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example.com/events",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                    },
                },
            ],
        },
    }

    assert _error_codes(graph) == [
        "GB6002",
        "GB6011",
        "GB6004",
        "GB6006",
        "GB6012",
        "GB6014",
    ]


def test_compile_reports_invalid_callback_subscription_scope() -> None:
    for scope in ("run ", "workspace"):
        graph = {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": f"invalid-callback-subscription-scope-{scope.strip()}"},
            "spec": {
                "nodes": {"agent": {"block": "agent.run@1"}},
                "callbackSubscriptions": [
                    {
                        "subscriptionId": "sub-invalid-scope",
                        "scope": scope,
                        "scopeId": "scope-1",
                        "delivery": {"kind": "local_callback", "callbackName": "ide"},
                    },
                ],
            },
        }

        errors = [
            diagnostic
            for diagnostic in compile_graph(
                graph,
                block_catalog=DISCOVERY_CATALOG,
            ).diagnostics.diagnostics
            if diagnostic.severity == "error"
        ]

        assert [diagnostic.code for diagnostic in errors] == ["GB1027"]
        assert (
            errors[0].message
            == "callback subscription scope must be one of run, conversation, project, tenant, or deployment"
        )


def test_compile_keeps_validating_callback_safety_when_delivery_is_not_a_mapping() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "malformed-authoritative-callback"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-malformed",
                    "scope": "billing",
                    "scopeId": "account-1",
                    "authoritativeFor": ["billing"],
                    "delivery": "https://127.0.0.1/events",
                }
            ],
        },
    }

    errors = [
        diagnostic
        for diagnostic in compile_graph(
            graph,
            block_catalog=DISCOVERY_CATALOG,
        ).diagnostics.diagnostics
        if diagnostic.severity == "error"
    ]

    assert [(item.code, item.path) for item in errors] == [
        ("GB1027", "$.spec.callbackSubscriptions[0].delivery"),
        ("GB1027", "$.spec.callbackSubscriptions[0].scope"),
        ("GB6004", "$.spec.callbackSubscriptions[0]"),
    ]


def test_compile_reports_userinfo_callback_webhook_url_as_unsafe() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "userinfo-callback-subscription"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-userinfo",
                    "scope": "run",
                    "scopeId": "run-1",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://callback-token@relay.example.com/events",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                    },
                },
            ],
        },
    }

    assert "GB6011" in _error_codes(graph)


def test_compile_reports_whitespace_wrapped_callback_webhook_url_as_unsafe() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "whitespace-callback-subscription-url"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-whitespace-url",
                    "scope": "run",
                    "scopeId": "run-1",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example.com/events ",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                    },
                },
            ],
        },
    }

    assert _error_codes(graph) == ["GB6011"]


def test_compile_reports_invalid_callback_delivery_kind() -> None:
    for delivery_kind in ("webhook ", "http_callback"):
        graph = {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": f"invalid-callback-delivery-kind-{delivery_kind.strip()}"},
            "spec": {
                "nodes": {"agent": {"block": "agent.run@1"}},
                "callbackSubscriptions": [
                    {
                        "subscriptionId": "sub-invalid-kind",
                        "scope": "run",
                        "scopeId": "run-1",
                        "delivery": {
                            "kind": delivery_kind,
                            "url": "https://relay.example.com/events",
                            "signing": {
                                "algorithm": "hmac-sha256",
                                "secretRef": "secret://relay",
                            },
                        },
                    },
                ],
            },
        }

        errors = [
            diagnostic
            for diagnostic in compile_graph(
                graph,
                block_catalog=DISCOVERY_CATALOG,
            ).diagnostics.diagnostics
            if diagnostic.severity == "error"
        ]

        assert [diagnostic.code for diagnostic in errors] == ["GB1027"]
        assert (
            errors[0].message
            == "callback delivery kind must be one of webhook, websocket, sse, push_notification, email, or local_callback"
        )


def test_compile_reports_non_post_callback_webhook_method() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "non-post-callback-webhook"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-webhook-get",
                    "scope": "run",
                    "scopeId": "run-1",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example.com/events",
                        "method": "GET",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                    },
                },
            ],
        },
    }

    errors = [
        diagnostic
        for diagnostic in compile_graph(
            graph,
            block_catalog=DISCOVERY_CATALOG,
        ).diagnostics.diagnostics
        if diagnostic.severity == "error"
    ]

    assert [diagnostic.code for diagnostic in errors] == ["GB1027"]
    assert errors[0].message == "webhook callback delivery method must be POST"


def test_compile_allows_safe_callback_subscription_contract() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "safe-callback-subscription"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": {
                "ide": {
                    "scope": "run",
                    "scopeId": "run-1",
                    "failurePolicy": "retry_then_dead_letter",
                    "deadLetterPolicy": "standard",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example.com/events",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                        "ordering": {"mode": "ordered", "scope": "run"},
                    },
                }
            },
        },
    }

    assert not {"GB6002", "GB6004", "GB6006", "GB6011", "GB6012", "GB6014"} & set(_error_codes(graph))


def test_compile_allows_mandatory_callback_failure_policy_with_fallback_behavior() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "fallback-callback-subscription"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-fallback",
                    "scope": "run",
                    "scopeId": "run-1",
                    "failurePolicy": "fail_run_on_failure",
                    "fallbackPolicy": "operator_review",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example.com/events",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                    },
                }
            ],
        },
    }

    assert "GB6014" not in _error_codes(graph)


def test_compile_allows_mandatory_callback_delivery_with_dead_letter_policy() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "mandatory-callback-dead-letter"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-mandatory-dead-letter",
                    "scope": "run",
                    "scopeId": "run-1",
                    "mandatory": True,
                    "deadLetterPolicy": "standard",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example.com/events",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                    },
                }
            ],
        },
    }

    assert "GB6006" not in _error_codes(graph)


def test_compile_allows_mandatory_callback_delivery_with_retry_policy_ref() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "mandatory-callback-retry-policy"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-mandatory-retry-policy",
                    "scope": "run",
                    "scopeId": "run-1",
                    "mandatory": True,
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example.com/events",
                        "retryPolicyRef": "webhook-standard",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                    },
                }
            ],
        },
    }

    assert "GB6006" not in _error_codes(graph)


def test_compile_reports_retrying_callback_subscription_without_dead_letter_behavior() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "retrying-callback-without-dead-letter"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-retry-without-dead-letter",
                    "scope": "run",
                    "scopeId": "run-1",
                    "failurePolicy": "retry_then_dead_letter",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example.com/events",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay",
                        },
                    },
                }
            ],
        },
    }

    assert _error_codes(graph) == ["GB6014"]


def test_compile_reports_background_run_replay_and_retention_diagnostics() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "background-run-missing-replay"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "execution": {
                "lifetime": "background",
                "clientConnectionRequired": True,
            },
            "eventStream": {
                "retention": "1h",
                "reconnectReplayGuarantee": "24h",
            },
        },
    }

    assert _error_codes(graph) == ["GB6005", "GB6009", "GB6013"]


def test_compile_reports_oversized_async_callback_payload_contract() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "oversized-async-callback"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "asyncOperations": {
                "ci": {
                    "kind": "ci_job",
                    "timeout": "30m",
                    "idempotencyKey": "$input.request_id",
                    "attemptFencing": True,
                    "callback": {
                        "required": True,
                        "schema": "schemas/CICallback@1",
                        "expectedPayloadBytes": 524288,
                        "maxPayloadBytes": 262144,
                    },
                    "resume": {
                        "requirePolicyReevaluation": True,
                        "requireBudgetReservation": True,
                        "requireReleaseCompatibility": True,
                        "requireOwnershipFence": True,
                    },
                }
            },
        },
    }

    assert _error_codes(graph) == ["GB6010"]


def test_compile_reports_non_positive_async_callback_payload_limit() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-async-callback-payload-limit"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "asyncOperations": {
                "ci": {
                    "kind": "ci_job",
                    "timeout": "30m",
                    "idempotencyKey": "$input.request_id",
                    "attemptFencing": True,
                    "callback": {
                        "required": True,
                        "schema": "schemas/CICallback@1",
                        "expectedPayloadBytes": 1,
                        "maxPayloadBytes": 0,
                    },
                    "resume": {
                        "requirePolicyReevaluation": True,
                        "requireBudgetReservation": True,
                        "requireReleaseCompatibility": True,
                        "requireOwnershipFence": True,
                    },
                }
            },
        },
    }

    errors = [
        diagnostic
        for diagnostic in compile_graph(
            graph,
            block_catalog=DISCOVERY_CATALOG,
        ).diagnostics.diagnostics
        if diagnostic.severity == "error"
    ]

    assert [diagnostic.code for diagnostic in errors] == ["GB1026"]
    assert errors[0].message == "async callback maxPayloadBytes must be a positive integer"


def test_compile_allows_background_run_with_replay_and_safe_payload_contracts() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "background-run-safe"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "execution": {"lifetime": "background"},
            "eventStream": {
                "replayable": True,
                "retention": "14d",
                "reconnectReplayGuarantee": "24h",
            },
            "asyncOperations": {
                "ci": {
                    "kind": "ci_job",
                    "timeout": "30m",
                    "idempotencyKey": "$input.request_id",
                    "attemptFencing": True,
                    "callback": {
                        "required": True,
                        "schema": "schemas/CICallback@1",
                        "expectedPayloadBytes": 65536,
                        "maxPayloadBytes": 262144,
                    },
                    "resume": {
                        "requirePolicyReevaluation": True,
                        "requireBudgetReservation": True,
                        "requireReleaseCompatibility": True,
                        "requireOwnershipFence": True,
                    },
                }
            },
        },
    }

    assert not {"GB6005", "GB6009", "GB6010", "GB6013"} & set(_error_codes(graph))
