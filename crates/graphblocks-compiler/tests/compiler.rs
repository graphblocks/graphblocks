use graphblocks_compiler::compiler::{BlockCatalog, compile_graph, compile_graph_with_catalog};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_compiler::graph::GRAPH_API_VERSION;
use serde_json::json;

#[test]
fn compile_graph_returns_normalized_plan_hash() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "ordered"},
        "spec": {
            "nodes": {
                "b": {"block": "text.join@1", "config": {"second": 2, "first": 1}},
                "a": {"block": "text.literal@1"}
            },
            "edges": [
                {"to": "b.value", "from": "a.value"},
                {"to": "$output.result", "from": "b.value"}
            ]
        }
    });

    let plan = compile_graph(&graph);

    assert!(plan.ok());
    assert_eq!(
        plan.graph_hash,
        "sha256:0b2636678ee1af1446500624da2f5db0dab238aceb858058a6f3b60f9e06f3a8"
    );
}

#[test]
fn compile_graph_reports_non_graph_documents() {
    let document = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Application",
        "metadata": {"name": "app"}
    });

    let plan = compile_graph(&document);

    assert!(!plan.ok());
    assert_eq!(plan.diagnostics[0].code, "GB0001");
}

#[test]
fn compile_graph_requires_metadata_name() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {},
        "spec": {"nodes": {}}
    });

    let plan = compile_graph(&graph);

    assert!(!plan.ok());
    assert_eq!(plan.diagnostics[0].code, "GB0003");
}

#[test]
fn block_catalog_rejects_invalid_descriptor_schema_ids() {
    assert_eq!(
        BlockCatalog::from_blocks(&json!([
            {
                "typeId": "text.source",
                "version": 1,
                "outputs": [{"name": "value", "type": "schemas/Text"}]
            }
        ])),
        Err(
            "block catalog entry 0 output value has invalid type schemas/Text: schema id must include a major version suffix"
                .to_owned()
        ),
    );
}

#[test]
fn block_catalog_allows_descriptor_type_expressions() {
    assert!(
        BlockCatalog::from_blocks(&json!([
            {
                "typeId": "control.map",
                "version": 1,
                "inputs": [{"name": "items", "type": "List<Any>"}],
                "outputs": [{"name": "values", "type": "List<Any>"}]
            }
        ]))
        .is_ok()
    );
}

#[test]
fn compile_graph_migrates_legacy_graph_api_versions() {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha2",
        "kind": "Graph",
        "metadata": {"name": "legacy"},
        "spec": {"nodes": {}}
    });

    let plan = compile_graph(&graph);

    assert!(plan.ok());
    assert_eq!(
        plan.graph_hash,
        "sha256:938ea0b58b94b431fef6780b98eb8434575a699a74a417688072dbefff3ae324"
    );
    assert_eq!(
        plan.normalized
            .pointer("/metadata/annotations/graphblocks.ai~1migratedFrom")
            .and_then(serde_json::Value::as_str),
        Some("graphblocks.ai/v1alpha2")
    );
}

#[test]
fn compile_graph_reports_unknown_edge_endpoint() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "bad-edge"},
        "spec": {
            "nodes": {"consumer": {"block": "text.join@1"}},
            "edges": [{"from": "missing.value", "to": "consumer.value"}]
        }
    });

    let plan = compile_graph(&graph);

    assert!(!plan.ok());
    assert_eq!(
        plan.diagnostics
            .iter()
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1002"]
    );
}

#[test]
fn compile_graph_warns_for_dead_nodes_when_outputs_are_declared() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "dead-node"},
        "spec": {
            "nodes": {
                "produce": {"block": "text.literal@1"},
                "orphan": {"block": "text.literal@1"}
            },
            "edges": [{"from": "produce.value", "to": "$output.result"}]
        }
    });

    let plan = compile_graph(&graph);

    assert!(plan.ok());
    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Warning)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1001"]
    );
}

#[test]
fn compile_graph_rejects_required_input_never_produced() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "text.sink",
            "version": 1,
            "inputs": [
                {"name": "text", "type": "graphblocks.ai/Text@1", "required": true}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "required-input"},
        "spec": {
            "nodes": {"sink": {"block": "text.sink@1"}}
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1003"]
    );
    Ok(())
}

#[test]
fn compile_graph_rejects_unknown_input_port() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "text.source",
            "version": 1,
            "outputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ]
        },
        {
            "typeId": "text.sink",
            "version": 1,
            "inputs": [
                {"name": "text", "type": "graphblocks.ai/Text@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-input-port"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"}
            },
            "edges": [{"from": "source.value", "to": "sink.missing"}]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1013"]
    );
    Ok(())
}

#[test]
fn compile_graph_rejects_unknown_output_port() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "text.source",
            "version": 1,
            "outputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ]
        },
        {
            "typeId": "text.sink",
            "version": 1,
            "inputs": [
                {"name": "text", "type": "graphblocks.ai/Text@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-output-port"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"}
            },
            "edges": [{"from": "source.missing", "to": "sink.text"}]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1014"]
    );
    Ok(())
}

#[test]
fn compile_graph_reports_async_callback_amendment_diagnostics() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "async-callback-diagnostics"},
        "spec": {
            "nodes": {
                "start": {
                    "block": "async.start_operation@1",
                    "config": {"callback": {"required": true}}
                },
                "agent": {"block": "agent.run@1"}
            },
            "execution": {
                "lifetime": "background",
                "clientConnectionRequired": true
            },
            "eventStream": {
                "retention": "1h",
                "reconnectReplayGuarantee": "24h"
            },
            "asyncOperations": {
                "ci": {
                    "kind": "ci_job",
                    "timeout": "30m",
                    "idempotencyKey": "$input.request_id",
                    "callback": {
                        "required": true,
                        "schema": "schemas/CICallback@1",
                        "expectedPayloadBytes": 524288,
                        "maxPayloadBytes": 262144
                    }
                }
            },
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-unsafe",
                    "scope": "run",
                    "scopeId": "run-1",
                    "authoritativeFor": ["billing"],
                    "delivery": {
                        "kind": "webhook",
                        "url": "http://127.0.0.1/events"
                    }
                },
                {
                    "subscriptionId": "sub-mandatory",
                    "scope": "run",
                    "scopeId": "run-1",
                    "mandatory": true,
                    "delivery": {
                        "kind": "local_callback",
                        "callbackName": "ide",
                        "ordering": {"mode": "ordered", "scope": "run"}
                    }
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
                            "secretRef": "secret://relay"
                        }
                    }
                }
            ]
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec![
            "GB6001", "GB6003", "GB6007", "GB6008", "GB6015", "GB6016", "GB6005", "GB6009",
            "GB6013", "GB6010", "GB6008", "GB6015", "GB6016", "GB6002", "GB6011", "GB6004",
            "GB6006", "GB6012", "GB6014"
        ]
    );
}

#[test]
fn compile_graph_allows_mandatory_callback_fallback_policy() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
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
                            "secretRef": "secret://relay"
                        }
                    }
                }
            ]
        }
    });

    let plan = compile_graph(&graph);
    let error_codes = plan
        .diagnostics
        .iter()
        .filter(|diagnostic| diagnostic.severity == Severity::Error)
        .map(|diagnostic| diagnostic.code.as_str())
        .collect::<Vec<_>>();

    assert!(!error_codes.contains(&"GB6014"), "{error_codes:?}");
}

#[test]
fn compile_graph_reports_async_poll_operation_without_timeout() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "async-poll-timeout-diagnostics"},
        "spec": {
            "nodes": {
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "intervalMs": 30_000,
                        "maxIntervalMs": 300_000,
                        "idempotencyKey": "$input.request_id",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    }
                }
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB6001"]
    );
}

#[test]
fn compile_graph_reports_async_poll_operation_with_zero_timeout() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "async-poll-zero-timeout-diagnostics"},
        "spec": {
            "nodes": {
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "timeoutMs": 0,
                        "idempotencyKey": "$input.request_id",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    }
                }
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB6001"]
    );
}

#[test]
fn compile_graph_reports_async_poll_operation_with_invalid_string_timeout() {
    for timeout in ["0ms", "soon"] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": format!("async-poll-invalid-timeout-{timeout}")},
            "spec": {
                "nodes": {
                    "poll": {
                        "block": "async.poll_operation@1",
                        "config": {
                            "timeout": timeout,
                            "idempotencyKey": "$input.request_id",
                            "callback": {"schema": "schemas/PollResult@1"},
                            "resume": {
                                "requirePolicyReevaluation": true,
                                "requireBudgetReservation": true,
                                "requireReleaseCompatibility": true,
                                "requireOwnershipFence": true
                            },
                            "attemptFencing": true
                        }
                    }
                }
            }
        });

        let plan = compile_graph(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB6001"],
            "timeout {timeout:?} should not satisfy async wait timeout"
        );
    }
}

#[test]
fn compile_graph_reports_async_poll_operation_with_invalid_interval_durations() {
    for (field, value) in [("interval", "0s"), ("maxInterval", "soon")] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": format!("async-poll-invalid-{field}")},
            "spec": {
                "nodes": {
                    "poll": {
                        "block": "async.poll_operation@1",
                        "config": {
                            "timeout": "30m",
                            field: value,
                            "idempotencyKey": "$input.request_id",
                            "callback": {"schema": "schemas/PollResult@1"},
                            "resume": {
                                "requirePolicyReevaluation": true,
                                "requireBudgetReservation": true,
                                "requireReleaseCompatibility": true,
                                "requireOwnershipFence": true
                            },
                            "attemptFencing": true
                        }
                    }
                }
            }
        });

        let plan = compile_graph(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["InvalidAsyncOperation"],
            "{field}={value:?} should be rejected before runtime execution"
        );
    }
}

#[test]
fn compile_graph_rejects_async_operation_with_callback_and_polling_refs() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
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
                        "required": true,
                        "schema": "schemas/ExternalCallback@1"
                    },
                    "polling": {
                        "endpoint": "providers/batch/status"
                    },
                    "resume": {
                        "requirePolicyReevaluation": true,
                        "requireBudgetReservation": true,
                        "requireReleaseCompatibility": true,
                        "requireOwnershipFence": true
                    },
                    "attemptFencing": true
                }
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["InvalidAsyncOperation"]
    );
}

#[test]
fn compile_graph_reports_async_await_callback_with_unknown_on_timeout_policy() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "async-await-invalid-on-timeout-diagnostics"},
        "spec": {
            "nodes": {
                "wait": {
                    "block": "async.await_callback@1",
                    "config": {
                        "timeout": "30m",
                        "onTimeout": "continue_anyway",
                        "idempotencyKey": "$input.request_id",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    }
                }
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["InvalidAsyncOperation"]
    );
}

#[test]
fn compile_graph_rejects_catalog_port_type_mismatch() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "text.source",
            "version": 1,
            "outputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ]
        },
        {
            "typeId": "number.sink",
            "version": 1,
            "inputs": [
                {"name": "value", "type": "graphblocks.ai/Number@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "port-type-mismatch"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "number.sink@1"}
            },
            "edges": [{"from": "source.value", "to": "sink.value"}]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1018"]
    );
    Ok(())
}

#[test]
fn compile_graph_rejects_optional_output_to_required_input() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "branch.maybe_text",
            "version": 1,
            "outputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1", "required": false}
            ]
        },
        {
            "typeId": "text.sink",
            "version": 1,
            "inputs": [
                {"name": "text", "type": "graphblocks.ai/Text@1", "required": true}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "optional-output-required-input"},
        "spec": {
            "nodes": {
                "maybe": {"block": "branch.maybe_text@1"},
                "sink": {"block": "text.sink@1"}
            },
            "edges": [{"from": "maybe.value", "to": "sink.text"}]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1015"]
    );
    Ok(())
}

#[test]
fn compile_graph_rejects_missing_required_resource_slot_binding() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "model.generate",
            "version": 1,
            "resourceSlots": [
                {"name": "model", "type": "graphblocks.ai/ChatModel@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "missing-resource"},
        "spec": {
            "nodes": {
                "generate": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1016"]
    );
    Ok(())
}

#[test]
fn compile_graph_rejects_unknown_resource_slot_binding() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "model.generate",
            "version": 1,
            "resourceSlots": [
                {"name": "model", "type": "graphblocks.ai/ChatModel@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-resource-slot"},
        "spec": {
            "nodes": {
                "generate": {
                    "block": "model.generate@1",
                    "bindings": {"unknown": "answer-model"}
                }
            }
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1017"]
    );
    Ok(())
}

#[test]
fn compile_graph_allows_optional_resource_slot_to_be_unbound() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "rank.documents",
            "version": 1,
            "resourceSlots": [
                {"name": "reranker", "type": "graphblocks.ai/Reranker@1", "optional": true}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "optional-resource"},
        "spec": {
            "nodes": {
                "rank": {"block": "rank.documents@1"}
            }
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| matches!(diagnostic.code.as_str(), "GB1016" | "GB1017"))
    );
    Ok(())
}

#[test]
fn compile_graph_rejects_effect_retry_without_idempotency_key() {
    for effect in ["external_write", "filesystem_write"] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": format!("unsafe-retry-{effect}")},
            "spec": {
                "nodes": {
                    "write": {
                        "block": "storage.write@1",
                        "effects": [effect],
                        "flow": {"retry": {"maxAttempts": 2}}
                    }
                }
            }
        });

        let plan = compile_graph(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB1011"]
        );
    }
}

#[test]
fn compile_graph_does_not_coerce_non_numeric_effect_retry_attempts() {
    for max_attempts in [json!("2"), json!("two"), json!(true)] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "non-numeric-retry"},
            "spec": {
                "nodes": {
                    "write": {
                        "block": "storage.write@1",
                        "effects": ["external_write"],
                        "flow": {"retry": {"maxAttempts": max_attempts}}
                    }
                }
            }
        });

        let plan = compile_graph(&graph);

        assert!(!plan.diagnostics.iter().any(|diagnostic| {
            diagnostic.severity == Severity::Error && diagnostic.code == "GB1011"
        }));
    }
}

#[test]
fn compile_graph_allows_effect_retry_with_idempotency_key() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "safe-retry"},
        "spec": {
            "nodes": {
                "write": {
                    "block": "storage.write@1",
                    "effects": ["external_write"],
                    "flow": {"retry": {"maxAttempts": 2, "idempotencyKey": "$input.request_id"}}
                }
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "GB1011")
    );
}

#[test]
fn compile_graph_reports_malformed_output_policy_structure() {
    let base = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "malformed-output-policy"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 8,
                    "onViolation": "abort_response"
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit"
                    ]
                },
                "onViolation": {
                    "disposition": "abort_response"
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });
    let cases = [
        (
            json!({"spec": {"outputPolicy": "strict"}}),
            vec!["InvalidOutputPolicy"],
        ),
        (
            json!({"spec": {"outputPolicy": {"delivery": "bounded"}}}),
            vec!["InvalidOutputPolicy", "OutputPolicyBypass"],
        ),
        (
            json!({"spec": {"outputPolicy": {"evaluation": "mandatory"}}}),
            vec!["InvalidOutputPolicy", "OutputPolicyBypass"],
        ),
        (
            json!({"spec": {"outputPolicy": {"evaluation": {"enforcementPoints": "before_client_delivery"}}}}),
            vec!["InvalidOutputEnforcementPoint", "OutputPolicyBypass"],
        ),
        (
            json!({"spec": {"outputPolicy": {
                "evaluation": {"enforcementPoints": [
                    "on_generation_chunk",
                    "before_client_delivery",
                    "before_output_commit"
                ]},
                "onViolation": "abort_response"
            }}}),
            vec!["InvalidOutputPolicy"],
        ),
    ];

    for (override_fragment, expected_codes) in cases {
        let mut graph = base.clone();
        let graph_object = graph.as_object_mut().expect("graph must be an object");
        let override_object = override_fragment
            .as_object()
            .expect("override must be an object");
        for (key, value) in override_object {
            graph_object.insert(key.clone(), value.clone());
        }

        let plan = compile_graph(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            expected_codes
        );
    }
}

#[test]
fn compile_graph_rejects_unbounded_output_holdback() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unbounded-output-policy"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "onViolation": "abort_response"
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["UnboundedPolicyHoldback", "OutputPolicyBypass"]
    );
}

#[test]
fn compile_graph_rejects_boolean_output_holdback_bound() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "boolean-output-policy-bound"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": true,
                    "onViolation": "abort_response"
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["UnboundedPolicyHoldback", "OutputPolicyBypass"]
    );
}

#[test]
fn compile_graph_rejects_invalid_output_holdback_duration() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "invalid-duration-output-policy-bound"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxDuration": "soon",
                    "onViolation": "abort_response"
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["UnboundedPolicyHoldback", "OutputPolicyBypass"]
    );
}

#[test]
fn compile_graph_rejects_immediate_draft_without_retraction_support() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unsafe-draft-policy"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "immediate_draft",
                    "onViolation": "abort_response",
                    "deliveredDraftDisposition": "keep"
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec![
            "ImmediateDraftWithoutRetractionSupport",
            "OutputPolicyBypass"
        ]
    );
}

#[test]
fn compile_graph_allows_bounded_holdback_with_time_or_size_bound() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "bounded-output-policy"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxDuration": "250ms",
                    "onViolation": "abort_response"
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(!plan.diagnostics.iter().any(|diagnostic| matches!(
        diagnostic.code.as_str(),
        "UnboundedPolicyHoldback" | "ImmediateDraftWithoutRetractionSupport"
    )));
}

#[test]
fn compile_graph_rejects_output_policy_without_client_delivery_gate() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "output-policy-bypass"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response"
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_output_commit"
                    ]
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["OutputPolicyBypass"]
    );
}

#[test]
fn compile_graph_rejects_output_policy_without_commit_gate() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "output-policy-missing-commit-gate"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response"
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery"
                    ]
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["OutputPolicyBypass"]
    );
}

#[test]
fn compile_graph_rejects_output_policy_gate_after_delivery() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "late-output-policy-gate"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response"
                },
                "evaluation": {
                    "enforcementPoints": [
                        "before_client_delivery",
                        "on_generation_chunk",
                        "before_output_commit"
                    ]
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["PolicyGateAfterDelivery"]
    );
}

#[test]
fn compile_graph_allows_output_policy_gate_before_delivery() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "ordered-output-policy-gate"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response"
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit"
                    ]
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(!plan.diagnostics.iter().any(|diagnostic| matches!(
        diagnostic.code.as_str(),
        "OutputPolicyBypass" | "PolicyGateAfterDelivery"
    )));
}

#[test]
fn compile_graph_rejects_pending_tool_calls_after_policy_abort() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "pending-tools-after-policy-abort"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response"
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit"
                    ]
                },
                "onViolation": {
                    "disposition": "abort_response",
                    "pendingToolCalls": {
                        "disposition": "keep"
                    },
                    "durableResult": {
                        "disposition": "none"
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["PendingToolCallAfterAbort"]
    );
}

#[test]
fn compile_graph_rejects_durable_commit_after_policy_stop() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "commit-after-policy-stop"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response"
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit"
                    ]
                },
                "onViolation": {
                    "disposition": "abort_response",
                    "pendingToolCalls": {
                        "disposition": "deny"
                    },
                    "durableResult": {
                        "disposition": "partial"
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["CommitAfterPolicyStop"]
    );
}

#[test]
fn compile_graph_reports_invalid_output_policy_literals() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "invalid-output-policy-literals"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "stream",
                    "holdbackMaxTokens": 48,
                    "onViolation": "pause",
                    "flushBoundaries": ["sentence", "clause"]
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit",
                        "after_client_delivery"
                    ]
                },
                "onViolation": {
                    "disposition": "halt",
                    "providerCancellation": {
                        "mode": "force"
                    },
                    "pendingToolCalls": {
                        "disposition": "pause"
                    },
                    "deliveredDraft": {
                        "disposition": "erase"
                    },
                    "durableResult": {
                        "disposition": "committed"
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec![
            "InvalidOutputDeliveryMode",
            "InvalidViolationAction",
            "InvalidFlushBoundary",
            "InvalidOutputEnforcementPoint",
            "InvalidOutputDisposition",
            "InvalidProviderCancellation",
            "InvalidPendingToolCallsDisposition",
            "InvalidDraftDisposition",
            "InvalidOutputDurableResult"
        ]
    );
}

#[test]
fn compile_graph_allows_safe_policy_abort_cleanup_settings() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "safe-policy-abort-cleanup"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response"
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit"
                    ]
                },
                "onViolation": {
                    "disposition": "abort_response",
                    "pendingToolCalls": {
                        "disposition": "deny"
                    },
                    "durableResult": {
                        "disposition": "none"
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(!plan.diagnostics.iter().any(|diagnostic| matches!(
        diagnostic.code.as_str(),
        "PendingToolCallAfterAbort" | "CommitAfterPolicyStop"
    )));
}

#[test]
fn compile_graph_reports_model_visible_tool_without_binding() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "missing-tool-binding"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["ToolBindingMissing"]
    );
}

#[test]
fn compile_graph_reports_tool_definition_without_input_schema() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "missing-tool-schema"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation."
                        },
                        "implementation": {
                            "kind": "block",
                            "block": "blocks.search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["ToolSchemaMissing"]
    );
}

#[test]
fn compile_graph_reports_malformed_tool_implementation_bindings() {
    let block_missing_target = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "malformed-block-tool"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1"
                        },
                        "implementation": {
                            "kind": "block"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });
    let unknown_kind = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-tool-kind"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1"
                        },
                        "implementation": {
                            "kind": "lambda",
                            "function": "search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });
    let missing_openapi_operation = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "openapi-tool-missing-operation"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    for graph in [
        block_missing_target,
        unknown_kind,
        missing_openapi_operation,
    ] {
        let plan = compile_graph(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["ToolBindingMissing"]
        );
    }
}

#[test]
fn compile_graph_reports_malformed_tool_definition_identity_fields() {
    let blank_name = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "blank-tool-definition-name"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": " ",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1"
                        },
                        "implementation": {
                            "kind": "block",
                            "block": "blocks.search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });
    let non_string_description = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "non-string-tool-definition-description"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": {"text": "Search documentation."},
                            "inputSchema": "schemas/Search@1"
                        },
                        "implementation": {
                            "kind": "block",
                            "block": "blocks.search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });
    let blank_version = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "blank-tool-definition-version"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                            "version": " "
                        },
                        "implementation": {
                            "kind": "block",
                            "block": "blocks.search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });
    let non_string_tag = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "non-string-tool-definition-tag"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                            "tags": ["knowledge", 7]
                        },
                        "implementation": {
                            "kind": "block",
                            "block": "blocks.search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    for graph in [
        blank_name,
        non_string_description,
        blank_version,
        non_string_tag,
    ] {
        let plan = compile_graph(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["InvalidToolDefinition"]
        );
    }
}

#[test]
fn compile_graph_rejects_forbidden_tool_definition_execution_details() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "tool-definition-leaks-execution-details"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1",
                            "credentials": {"secretRef": "support-search-token"},
                            "connection": "support-api",
                            "implementation": {"kind": "remote"}
                        },
                        "implementation": {
                            "kind": "block",
                            "block": "blocks.search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });
    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec![
            "InvalidToolDefinition",
            "InvalidToolDefinition",
            "InvalidToolDefinition"
        ]
    );
}

#[test]
fn compile_graph_reports_tool_definition_with_invalid_input_schema() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "invalid-tool-schema"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search"
                        },
                        "implementation": {
                            "kind": "block",
                            "block": "blocks.search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["InvalidSchemaId"]
    );
}

#[test]
fn compile_graph_reports_invalid_interface_schema_ids() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "invalid-interface-schema"},
        "spec": {
            "interface": {
                "inputs": {"request": "schemas/Request"},
                "outputs": {"result": "schemas/Result"}
            },
            "nodes": {}
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["InvalidSchemaId", "InvalidSchemaId"]
    );
}

#[test]
fn compile_graph_accepts_tool_definition_with_schema_and_binding() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "valid-tool-binding"},
        "spec": {
            "bindings": {
                "tools": {
                    "search": {
                        "definition": {
                            "name": "knowledge.search",
                            "description": "Search documentation.",
                            "inputSchema": "schemas/Search@1"
                        },
                        "implementation": {
                            "kind": "block",
                            "block": "blocks.search"
                        }
                    }
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(!plan.diagnostics.iter().any(|diagnostic| matches!(
        diagnostic.code.as_str(),
        "ToolBindingMissing" | "ToolSchemaMissing"
    )));
}

#[test]
fn compile_graph_reports_invalid_tool_effect_literals() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "invalid-tool-effect"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external-write"]
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["InvalidToolEffect"]
    );

    let conflicting_none = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "conflicting-none-effect"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["none", "network"]
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let conflicting_plan = compile_graph(&conflicting_none);

    assert_eq!(
        conflicting_plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["InvalidToolEffect"]
    );
}

#[test]
fn compile_graph_reports_invalid_tool_binding_literals() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "invalid-tool-binding-literals"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external_write", "network"],
                        "approval": {"mode": "sometimes"},
                        "idempotency": "maybe",
                        "cancellation": "eventually",
                        "resultMode": "firehose"
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec![
            "InvalidToolApproval",
            "InvalidToolIdempotency",
            "InvalidToolCancellation",
            "InvalidToolResultMode"
        ]
    );
}

#[test]
fn compile_graph_reports_invalid_tool_execution_settings() {
    let cases = [
        json!("parallel"),
        json!({"maximumParallelism": 0}),
        json!({"maximumParallelism": "4"}),
        json!({"parallelToolCalls": "true"}),
        json!({"effectSerialization": "resource"}),
        json!({"effectSerialization": {"keyTemplate": " "}}),
    ];

    for tool_execution in cases {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "invalid-tool-execution-settings"},
            "spec": {
                "bindings": {
                    "tools": {
                        "knowledgeSearch": {
                            "definition": {
                                "name": "knowledge.search",
                                "description": "Search support documentation.",
                                "inputSchema": "schemas/SearchRequest@1"
                            },
                            "implementation": {
                                "kind": "block",
                                "block": "tools.search"
                            },
                            "effects": ["external_read"]
                        }
                    }
                },
                "toolExecution": tool_execution,
                "nodes": {
                    "agent": {"block": "agent.run@1"}
                }
            }
        });

        let plan = compile_graph(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["InvalidToolExecution"]
        );
    }
}

#[test]
fn compile_graph_rejects_parallel_state_changing_tools_without_effect_serialization() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unsafe-parallel-tools"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external_write", "network"]
                    }
                }
            },
            "toolExecution": {
                "maximumParallelism": 4,
                "failurePolicy": "return_failures_to_model"
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["UnsafeParallelEffects"]
    );
}

#[test]
fn compile_graph_allows_parallel_state_changing_tools_with_effect_serialization() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "safe-parallel-tools"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external_write", "network"]
                    }
                }
            },
            "toolExecution": {
                "maximumParallelism": 4,
                "failurePolicy": "return_failures_to_model",
                "effectSerialization": {
                    "keyTemplate": "{tool.name}:{arguments.resource_id}"
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "UnsafeParallelEffects")
    );
}

#[test]
fn compile_graph_rejects_retried_write_tool_without_required_idempotency() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "nonidempotent-retry-tool"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external_write", "network"],
                        "retryPolicyRef": "retry/default"
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["NonIdempotentRetry"]
    );
}

#[test]
fn compile_graph_allows_retried_write_tool_with_required_idempotency() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "idempotent-retry-tool"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external_write", "network"],
                        "retryPolicyRef": "retry/default",
                        "idempotency": "required"
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "NonIdempotentRetry")
    );
}

#[test]
fn compile_graph_rejects_explicit_tool_approval_without_argument_digest_binding() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "approval-without-argument-digest"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external_write", "network"],
                        "approval": {
                            "mode": "always",
                            "summary": "Operator must approve ticket creation."
                        }
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["ApprovalWithoutArgumentDigest"]
    );
}

#[test]
fn compile_graph_rejects_string_tool_approval_without_argument_digest_binding() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "string-approval-without-argument-digest"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external_write", "network"],
                        "approval": "always"
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["ApprovalWithoutArgumentDigest"]
    );
}

#[test]
fn compile_graph_allows_explicit_tool_approval_bound_to_argument_digest() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "approval-with-argument-digest"},
        "spec": {
            "bindings": {
                "tools": {
                    "createTicket": {
                        "definition": {
                            "name": "ticket.create",
                            "description": "Create a support ticket.",
                            "inputSchema": "schemas/TicketCreateRequest@1"
                        },
                        "implementation": {
                            "kind": "openapi",
                            "connection": "ticket-system",
                            "operationId": "createTicket"
                        },
                        "effects": ["external_write", "network"],
                        "approval": {
                            "mode": "always",
                            "bindArgumentsDigest": true,
                            "summary": "Operator must approve ticket creation."
                        }
                    }
                }
            },
            "nodes": {
                "agent": {"block": "agent.run@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "ApprovalWithoutArgumentDigest")
    );
}

#[test]
fn compile_graph_rejects_oversized_remote_inline_payload() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "oversized-remote-inline-payload"},
        "spec": {
            "remotePayloadLimits": {
                "maxInlineBytes": 8
            },
            "remotePayloads": [
                {
                    "mode": "inline",
                    "schema": "graphblocks.ai/Message@1",
                    "value": {"body": "this payload is too large"}
                }
            ],
            "nodes": {
                "remote": {"block": "remote.invoke@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["RemoteInlinePayloadTooLarge"]
    );
}

#[test]
fn compile_graph_allows_large_remote_payload_by_artifact_reference() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "remote-artifact-payload"},
        "spec": {
            "remotePayloadLimits": {
                "maxInlineBytes": 8
            },
            "remotePayloads": [
                {
                    "mode": "artifact_ref",
                    "schema": "graphblocks.ai/ArtifactRef@1",
                    "artifact": {
                        "artifactId": "artifact-1",
                        "uri": "s3://bucket/large.json"
                    }
                }
            ],
            "nodes": {
                "remote": {"block": "remote.invoke@1"}
            }
        }
    });

    let plan = compile_graph(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "RemoteInlinePayloadTooLarge")
    );
}
