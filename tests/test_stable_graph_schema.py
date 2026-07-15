from __future__ import annotations

from copy import deepcopy

import pytest

from graphblocks import compile_graph, resource_schema_errors, validate_resource


def _stable_graph() -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "stable-core"},
        "spec": {
            "interface": {
                "inputs": {"request": "schemas/Request@1"},
                "outputs": {"response": "schemas/Response@1"},
            },
            "nodes": {
                "worker": {
                    "block": "example.worker@1",
                    "inputs": {"request": "$input.request"},
                    "outputs": {"response": "$output.response"},
                    "config": {"limit": 3, "nested": {"enabled": True}},
                    "bindings": {"model": "local-model"},
                    "when": "$input.enabled",
                    "flow": {
                        "timeout": "5s",
                        "retry": {"maxAttempts": 2, "idempotencyKey": "$input.request_id"},
                    },
                    "effects": ["external_read", "network"],
                }
            },
            "edges": [],
        },
    }


def test_stable_graph_schema_accepts_closed_c0_c1_graph() -> None:
    validate_resource(_stable_graph())


@pytest.mark.parametrize("retry", (100, {"maxAttempts": 100}))
def test_stable_graph_schema_accepts_retry_attempt_limit(retry: object) -> None:
    graph = _stable_graph()
    graph["spec"]["nodes"]["worker"]["flow"]["retry"] = retry  # type: ignore[index]

    validate_resource(graph)


@pytest.mark.parametrize(
    "retry",
    (
        101,
        10**100,
        {"maxAttempts": 101},
        {"maxAttempts": 10**100},
    ),
)
def test_stable_graph_schema_rejects_retry_attempts_above_limit(
    retry: object,
) -> None:
    graph = _stable_graph()
    graph["spec"]["nodes"]["worker"]["flow"]["retry"] = retry  # type: ignore[index]

    violations = resource_schema_errors(graph)

    assert [(violation.path, violation.keyword) for violation in violations] == [
        ("$.spec.nodes.worker.flow.retry", "oneOf")
    ]


@pytest.mark.parametrize(
    "retry",
    (True, "100", {"maxAttempts": True}, {"maxAttempts": "100"}),
)
def test_stable_graph_schema_preserves_retry_type_rejection(retry: object) -> None:
    graph = _stable_graph()
    graph["spec"]["nodes"]["worker"]["flow"]["retry"] = retry  # type: ignore[index]

    assert resource_schema_errors(graph)


@pytest.mark.parametrize("retry", ({}, {"idempotencyKey": "$input.request_id"}))
def test_stable_graph_schema_rejects_retry_object_without_attempts(
    retry: object,
) -> None:
    graph = _stable_graph()
    graph["spec"]["nodes"]["worker"]["flow"]["retry"] = retry  # type: ignore[index]

    violations = resource_schema_errors(graph)

    assert [(violation.path, violation.keyword) for violation in violations] == [
        ("$.spec.nodes.worker.flow.retry", "oneOf")
    ]


@pytest.mark.parametrize(
    "field,value",
    (
        ("composition", {"apiVersion": "graphblocks.ai/composition/v1alpha1"}),
        ("execution", {"mode": "background"}),
        ("eventStream", {"retention": "durable"}),
        ("event_stream", {"retention": "durable"}),
        ("asyncOperations", {}),
        ("async_operations", {}),
        ("callbackSubscriptions", {}),
        ("callback_subscriptions", {}),
        ("extensions", ["graphblocks.voice/v1alpha1"]),
        ("voice", {"pipeline": {"kind": "realtime"}}),
        ("state", {"scope": "conversation"}),
        ("policy", {"mode": "governed"}),
    ),
)
def test_stable_graph_schema_rejects_preview_spec_fields(field: str, value: object) -> None:
    graph = _stable_graph()
    graph["spec"][field] = value  # type: ignore[index]

    violations = resource_schema_errors(graph)

    assert violations
    assert violations[0].code == "GB0014"
    assert violations[0].path == "$.spec"
    assert violations[0].keyword == "additionalProperties"


@pytest.mark.parametrize("field", ("slot", "policies", "execution", "projection"))
def test_stable_graph_schema_rejects_preview_node_fields(field: str) -> None:
    graph = _stable_graph()
    graph["spec"]["nodes"]["worker"][field] = (  # type: ignore[index]
        "replacement" if field == "slot" else {"future": True}
    )

    violations = resource_schema_errors(graph)

    assert violations
    assert any(
        violation.path == "$.spec.nodes.worker" and violation.keyword == "additionalProperties"
        for violation in violations
    )


@pytest.mark.parametrize(
    "field,value",
    (
        ("bindings", {"attackerDefinedFutureField": {"nested": True}}),
        ("toolExecution", {"attackerDefinedFutureField": {"nested": True}}),
        ("outputPolicy", {"attackerDefinedFutureField": {"nested": True}}),
    ),
)
def test_stable_graph_schema_rejects_unknown_c1_contract_fields(field: str, value: object) -> None:
    graph = _stable_graph()
    graph["spec"][field] = value  # type: ignore[index]

    violations = resource_schema_errors(graph)

    assert violations
    assert all(violation.code == "GB0014" for violation in violations)


def test_stable_compiler_rejects_preview_field_without_reinterpreting_it() -> None:
    graph = _stable_graph()
    graph["spec"]["state"] = {"attackerDefinedFutureField": True}  # type: ignore[index]

    plan = compile_graph(graph, allow_unknown_blocks=True)

    assert not plan.ok
    assert any(
        diagnostic.code == "GB0014" and diagnostic.path == "$.spec"
        for diagnostic in plan.diagnostics.diagnostics
    )


def test_stable_graph_schema_accepts_closed_tool_and_output_policy_contracts() -> None:
    graph = _stable_graph()
    graph["spec"].update(  # type: ignore[union-attr]
        {
            "bindings": {
                "tools": {
                    "lookup": {
                        "definition": {
                            "name": "knowledge.lookup",
                            "description": "Look up deterministic local knowledge.",
                            "inputSchema": "schemas/LookupRequest@1",
                            "outputSchema": "schemas/LookupResponse@1",
                            "tags": ["local"],
                        },
                        "implementation": {"kind": "block", "block": "knowledge.lookup@1"},
                        "effects": "external_read",
                        "approval": {"mode": "never"},
                        "idempotency": "not_applicable",
                        "cancellation": "cooperative",
                        "resultMode": "value",
                    }
                }
            },
            "toolExecution": {"maximumParallelism": 1, "parallelToolCalls": False},
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 256,
                    "onViolation": "abort_response",
                    "flushBoundaries": ["response"],
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
                    "providerCancellation": {"mode": "request"},
                    "pendingToolCalls": {"disposition": "deny"},
                    "deliveredDraft": {"disposition": "retract"},
                    "durableResult": {"disposition": "none"},
                },
            },
        }
    )

    validate_resource(graph)


@pytest.mark.parametrize(
    "approval",
    (
        {},
        {"bindArgumentsDigest": True},
        {
            "mode": "always",
            "argumentsDigest": "sha256:resolved",
            "argumentsDigestRef": "$input.arguments_digest",
        },
    ),
)
def test_stable_graph_schema_rejects_ambiguous_tool_approval_objects(
    approval: object,
) -> None:
    graph = _stable_graph()
    graph["spec"]["bindings"] = {  # type: ignore[index]
        "tools": {
            "lookup": {
                "definition": {
                    "name": "knowledge.lookup",
                    "description": "Look up deterministic local knowledge.",
                    "inputSchema": "schemas/LookupRequest@1",
                },
                "implementation": {
                    "kind": "block",
                    "block": "knowledge.lookup@1",
                },
                "approval": approval,
            }
        }
    }

    violations = resource_schema_errors(graph)

    assert [(violation.path, violation.keyword) for violation in violations] == [
        ("$.spec.bindings.tools.lookup.approval", "oneOf")
    ]


def test_preview_alpha_graph_retains_preview_fields_without_widening_v1() -> None:
    graph = deepcopy(_stable_graph())
    graph["apiVersion"] = "graphblocks.ai/v1alpha3"
    graph["spec"].update(  # type: ignore[union-attr]
        {
            "execution": {"mode": "background"},
            "eventStream": {"retention": "durable"},
            "bindings": {"tools": {}},
            "toolExecution": {"maximumParallelism": 2},
            "voice": {"pipeline": {"kind": "realtime"}},
        }
    )

    validate_resource(graph)
