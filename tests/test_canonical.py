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

    assert _error_codes(unbounded) == ["UnboundedPolicyHoldback"]
    assert _error_codes(immediate_draft) == ["ImmediateDraftWithoutRetractionSupport"]


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
    } & set(_error_codes(graph))
