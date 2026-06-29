from __future__ import annotations

from graphblocks import canonical_hash, compile_graph, normalize_graph


def _error_codes(graph: dict) -> list[str]:
    return [item.code for item in compile_graph(graph).diagnostics.diagnostics if item.severity == "error"]


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
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unsafe-retry"},
        "spec": {
            "nodes": {
                "write": {
                    "block": "storage.write@1",
                    "effects": ["external_write"],
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
