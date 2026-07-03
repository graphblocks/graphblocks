from __future__ import annotations

import sys
from types import SimpleNamespace

import graphblocks
from graphblocks import canonical_hash, compile_graph, compile_graph_native, normalize_graph
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
from graphblocks.tools import (
    VALID_TOOL_APPROVALS,
    VALID_TOOL_CANCELLATIONS,
    VALID_TOOL_EFFECTS,
    VALID_TOOL_IDEMPOTENCIES,
    VALID_TOOL_RESULT_MODES,
)


def _error_codes(graph: dict) -> list[str]:
    return [item.code for item in compile_graph(graph).diagnostics.diagnostics if item.severity == "error"]


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

    assert _error_codes(graph) == ["InvalidSchemaId", "InvalidSchemaId"]


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

        plan = compile_graph(graph)

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

        plan = compile_graph(graph)

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

    plan = compile_graph(graph)

    assert "GB1011" not in [item.code for item in plan.diagnostics.diagnostics]


def test_native_compile_helper_delegates_to_runtime(monkeypatch) -> None:
    calls: list[tuple[dict[str, object], object | None]] = []

    def native_compile_graph(document: dict[str, object], block_catalog: object | None = None) -> dict[str, object]:
        calls.append((document, block_catalog))
        return {"ok": True, "graph": document, "blockCatalog": block_catalog, "diagnostics": []}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(compile_graph=native_compile_graph),
    )

    result = compile_graph_native(
        {"kind": "Graph", "metadata": {"name": "native"}},
        block_catalog=[{"typeId": "prompt.render@1"}],
    )

    assert result == {
        "ok": True,
        "graph": {"kind": "Graph", "metadata": {"name": "native"}},
        "blockCatalog": [{"typeId": "prompt.render@1"}],
        "diagnostics": [],
    }
    assert calls == [
        (
            {"kind": "Graph", "metadata": {"name": "native"}},
            [{"typeId": "prompt.render@1"}],
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

    assert _error_codes(unbounded) == ["UnboundedPolicyHoldback", "OutputPolicyBypass"]
    assert _error_codes(boolean_bound) == ["UnboundedPolicyHoldback", "OutputPolicyBypass"]
    assert _error_codes(invalid_duration_bound) == ["UnboundedPolicyHoldback", "OutputPolicyBypass"]
    assert _error_codes(immediate_draft) == [
        "ImmediateDraftWithoutRetractionSupport",
        "OutputPolicyBypass",
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

    assert _error_codes(base) == ["OutputPolicyBypass"]
    assert _error_codes(missing_enforcement_points) == ["OutputPolicyBypass"]
    assert _error_codes(missing_generation_gate) == ["OutputPolicyBypass"]
    assert _error_codes(missing_commit_gate) == ["OutputPolicyBypass"]
    assert _error_codes(late_gate) == ["PolicyGateAfterDelivery"]


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
    assert _error_codes(non_mapping_policy) == ["InvalidOutputPolicy"]
    assert _error_codes(non_mapping_delivery) == ["InvalidOutputPolicy"]
    assert _error_codes(non_mapping_evaluation) == ["InvalidOutputPolicy", "OutputPolicyBypass"]
    assert _error_codes(non_list_enforcement_points) == ["InvalidOutputEnforcementPoint", "OutputPolicyBypass"]
    assert _error_codes(non_mapping_on_violation) == ["InvalidOutputPolicy"]


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

    assert _error_codes(base) == ["PendingToolCallAfterAbort"]
    assert _error_codes(commits_result) == ["CommitAfterPolicyStop"]


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
        "InvalidOutputDeliveryMode",
        "InvalidViolationAction",
        "InvalidFlushBoundary",
        "InvalidOutputEnforcementPoint",
        "InvalidOutputDisposition",
        "InvalidProviderCancellation",
        "InvalidPendingToolCallsDisposition",
        "InvalidDraftDisposition",
        "InvalidOutputDurableResult",
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
        "UnboundedPolicyHoldback",
        "ImmediateDraftWithoutRetractionSupport",
        "OutputPolicyBypass",
        "PolicyGateAfterDelivery",
        "PendingToolCallAfterAbort",
        "CommitAfterPolicyStop",
        "InvalidOutputDeliveryMode",
        "InvalidViolationAction",
        "InvalidFlushBoundary",
        "InvalidOutputEnforcementPoint",
        "InvalidOutputDisposition",
        "InvalidProviderCancellation",
        "InvalidPendingToolCallsDisposition",
        "InvalidDraftDisposition",
        "InvalidOutputDurableResult",
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

    assert _error_codes(missing_binding) == ["ToolBindingMissing"]
    assert _error_codes(missing_schema) == ["ToolSchemaMissing"]
    assert _error_codes(missing_definition) == ["ToolSchemaMissing"]
    assert _error_codes(invalid_schema) == ["InvalidSchemaId"]


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

    assert _error_codes(base) == ["ToolBindingMissing"]
    assert _error_codes(unknown_kind) == ["ToolBindingMissing"]
    assert _error_codes(missing_openapi_operation) == ["ToolBindingMissing"]


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

    assert _error_codes(blank_name) == ["InvalidToolDefinition"]
    assert _error_codes(non_string_description) == ["InvalidToolDefinition"]
    assert _error_codes(blank_version) == ["InvalidToolDefinition"]
    assert _error_codes(non_string_tag) == ["InvalidToolDefinition"]


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
        "InvalidToolDefinition",
        "InvalidToolDefinition",
        "InvalidToolDefinition",
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

    assert _error_codes(graph) == ["InvalidToolEffect"]

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

    assert _error_codes(conflicting_none) == ["InvalidToolEffect"]


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
        "InvalidToolApproval",
        "InvalidToolIdempotency",
        "InvalidToolCancellation",
        "InvalidToolResultMode",
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

    assert _error_codes(graph) == ["UnsafeParallelEffects"]


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
        "InvalidToolExecution",
        "InvalidToolExecution",
        "InvalidToolExecution",
    ]
    assert _error_codes(non_mapping) == ["InvalidToolExecution"]
    assert _error_codes(non_mapping_effect_serialization) == ["InvalidToolExecution"]


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

    assert _error_codes(graph) == ["NonIdempotentRetry"]


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

    assert _error_codes(graph) == ["ApprovalWithoutArgumentDigest"]
    assert _error_codes(string_approval) == ["ApprovalWithoutArgumentDigest"]


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
        "ToolBindingMissing",
        "ToolSchemaMissing",
        "UnsafeParallelEffects",
        "NonIdempotentRetry",
        "ApprovalWithoutArgumentDigest",
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
