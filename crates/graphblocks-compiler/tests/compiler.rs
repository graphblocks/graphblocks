use graphblocks_compiler::compiler::{
    BlockCatalog, ExecutionPhase, compile_graph, compile_graph_for_discovery,
    compile_graph_with_catalog,
};
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(plan.ok());
    assert_eq!(
        plan.graph_hash,
        "sha256:41e285b218c0dccac9a67b701dfc08a4fb74c064a96247088e599a76e5dcb516"
    );
}

#[test]
fn compile_graph_rejects_undeclared_blocks_by_default() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-block"},
        "spec": {
            "nodes": {
                "unknown": {"block": "test.unknown@1"}
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
        vec!["GB1022"]
    );
}

#[test]
fn compile_graph_for_discovery_explicitly_allows_undeclared_blocks() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-block"},
        "spec": {
            "nodes": {
                "unknown": {"block": "test.unknown@1"}
            }
        }
    });

    assert!(compile_graph_for_discovery(&graph).ok());
}

#[test]
fn explicitly_open_catalog_still_validates_declared_blocks() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "text.sink",
            "version": 1,
            "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1"}]
        }
    ]))?
    .with_unknown_blocks_allowed();
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "open-catalog"},
        "spec": {
            "nodes": {
                "known": {"block": "text.sink@1"},
                "unknown": {"block": "test.unknown@1"}
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
        vec!["GB1003"]
    );
    Ok(())
}

#[test]
fn compile_graph_reports_non_graph_documents() {
    let document = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Application",
        "metadata": {"name": "app"}
    });

    let plan = compile_graph_for_discovery(&document);

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

    let plan = compile_graph_for_discovery(&graph);

    assert!(!plan.ok());
    assert_eq!(plan.diagnostics[0].code, "GB0003");
}

#[test]
fn compile_graph_rejects_unexpanded_composition_and_slot() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unexpanded"},
        "spec": {
            "composition": {
                "apiVersion": "graphblocks.ai/composition/v1alpha1",
                "imports": {},
                "slots": {}
            },
            "nodes": {"placeholder": {"slot": "missing"}}
        }
    });

    let plan = compile_graph_for_discovery(&graph);
    let error_codes = plan
        .diagnostics
        .iter()
        .filter(|diagnostic| diagnostic.severity == Severity::Error)
        .map(|diagnostic| diagnostic.code.as_str())
        .collect::<Vec<_>>();

    assert_eq!(error_codes, vec!["GB1052", "GB1052"]);
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
fn block_catalog_retains_and_enforces_config_schema() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([{
        "typeId": "test.configured",
        "version": 1,
        "configSchema": {
            "type": "object",
            "properties": {"allowed": {"type": "string"}},
            "additionalProperties": false
        }
    }]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "config-schema"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.configured@1",
                    "config": {"typo": "ignored"}
                }
            }
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert!(plan.diagnostics.iter().any(|diagnostic| {
        diagnostic.code == "GB2019"
            && diagnostic.path == "$.spec.nodes.configured.config"
            && diagnostic
                .message
                .contains("test.configured@1 configSchema")
    }));
    Ok(())
}

#[test]
fn block_catalog_rejects_invalid_or_external_config_schemas() {
    for schema in [
        json!({"type": "not-a-json-type"}),
        json!({"$ref": ""}),
        json!({"$ref": "file:///tmp/graphblocks-config.schema.json"}),
        json!({"$dynamicRef": "https://example.invalid/config.schema.json"}),
    ] {
        let error = BlockCatalog::from_blocks(&json!([{
            "typeId": "test.configured",
            "version": 1,
            "configSchema": schema
        }]))
        .expect_err("invalid config schema must be rejected");
        assert!(error.contains("configSchema"), "unexpected error: {error}");
    }
}

#[test]
fn required_when_pointer_limit_counts_unicode_characters() -> Result<(), String> {
    let pointer = format!("/{}", "😀".repeat(128));
    let catalog = BlockCatalog::from_blocks(&json!([{
        "typeId": "test.unicode-pointer",
        "version": 1,
        "outputs": [{
            "name": "value",
            "required": false,
            "requiredWhen": {
                "configEquals": {"pointer": pointer, "value": true}
            }
        }]
    }]))?;

    assert!(catalog.get("test.unicode-pointer@1").is_some());
    Ok(())
}

#[test]
fn block_catalog_allows_descriptor_type_expressions() {
    assert!(
        BlockCatalog::from_blocks(&json!([
            {
                "typeId": "control.map",
                "version": 1,
                "inputs": [
                    {
                        "name": "items",
                        "type": "Optional<Map<String,List<graphblocks.ai/Text@1>>>"
                    }
                ],
                "outputs": [
                    {
                        "name": "values",
                        "type": "Map<graphblocks.ai/Key@1,Optional<Number>>"
                    }
                ],
                "resourceSlots": {
                    "component": {"type": "haystack.component"},
                    "model": {"type": "graphblocks.ai/ChatModel@1", "optional": true}
                }
            }
        ]))
        .is_ok()
    );
}

#[test]
fn block_catalog_rejects_malformed_descriptor_shapes() {
    let cases = [
        (
            json!([{"typeId": "test.block", "version": 1, "inputs": {}}]),
            "inputs must be an array",
        ),
        (
            json!([{"typeId": "test.block", "version": 1, "outputs": [null]}]),
            "output 0 must be an object",
        ),
        (
            json!([{"typeId": "test.block", "version": 1, "resourceSlots": "model"}]),
            "resourceSlots must be an array or object",
        ),
        (
            json!([{"typeId": "test.block", "version": 1, "resourceSlots": [null]}]),
            "resource slot 0 must be an object",
        ),
        (
            json!([{
                "typeId": "test.block",
                "version": 1,
                "resourceSlots": {"model": "not-an-object"}
            }]),
            "resource slot \"model\" must be an object",
        ),
    ];

    for (blocks, expected) in cases {
        let error = BlockCatalog::from_blocks(&blocks).expect_err("catalog must be rejected");
        assert!(error.contains(expected), "unexpected error: {error}");
    }
}

#[test]
fn block_catalog_rejects_missing_or_empty_descriptor_names() {
    let cases = [
        json!([{"typeId": "test.block", "version": 1, "inputs": [{}]}]),
        json!([{"typeId": "test.block", "version": 1, "outputs": [{"name": ""}]}]),
        json!([{
            "typeId": "test.block",
            "version": 1,
            "resourceSlots": [{"name": "   "}]
        }]),
        json!([{"typeId": "test.block", "version": 1, "resourceSlots": {"": {}}}]),
    ];

    for blocks in cases {
        let error = BlockCatalog::from_blocks(&blocks).expect_err("catalog must be rejected");
        assert!(
            error.contains("non-empty name"),
            "unexpected error: {error}"
        );
    }
}

#[test]
fn block_catalog_rejects_ambiguous_endpoint_names() {
    for (direction, name) in [("inputs", "payload.value"), ("outputs", "value.part")] {
        let mut descriptor = json!({
            "typeId": "test.block",
            "version": 1
        });
        descriptor[direction] = json!([{"name": name}]);
        let blocks = json!([descriptor]);
        let error = BlockCatalog::from_blocks(&blocks).expect_err("catalog must be rejected");
        assert!(
            error.contains("canonical endpoint name"),
            "unexpected error: {error}"
        );
    }
}

#[test]
fn block_catalog_requires_boolean_required_and_optional_flags() {
    let cases = [
        (
            json!([{
                "typeId": "test.block",
                "version": 1,
                "inputs": [{"name": "value", "required": "false"}]
            }]),
            "required must be a boolean",
        ),
        (
            json!([{
                "typeId": "test.block",
                "version": 1,
                "outputs": [{"name": "value", "required": 0}]
            }]),
            "required must be a boolean",
        ),
        (
            json!([{
                "typeId": "test.block",
                "version": 1,
                "resourceSlots": [{"name": "model", "optional": "true"}]
            }]),
            "optional must be a boolean",
        ),
    ];

    for (blocks, expected) in cases {
        let error = BlockCatalog::from_blocks(&blocks).expect_err("catalog must be rejected");
        assert!(error.contains(expected), "unexpected error: {error}");
    }
}

#[test]
fn block_catalog_rejects_duplicate_descriptor_and_port_names() {
    let cases = [
        (
            json!([
                {"typeId": "test.block", "version": 1},
                {"typeId": "test.block", "version": 1}
            ]),
            "duplicate block catalog descriptor test.block@1",
        ),
        (
            json!([{
                "typeId": "test.block",
                "version": 1,
                "inputs": [{"name": "value"}, {"name": "value"}]
            }]),
            "duplicate input \"value\"",
        ),
        (
            json!([{
                "typeId": "test.block",
                "version": 1,
                "outputs": [{"name": "value"}, {"name": "value"}]
            }]),
            "duplicate output \"value\"",
        ),
        (
            json!([{
                "typeId": "test.block",
                "version": 1,
                "resourceSlots": [{"name": "model"}, {"name": "model"}]
            }]),
            "duplicate resource slot \"model\"",
        ),
    ];

    for (blocks, expected) in cases {
        let error = BlockCatalog::from_blocks(&blocks).expect_err("catalog must be rejected");
        assert!(error.contains(expected), "unexpected error: {error}");
    }
}

#[test]
fn block_catalog_rejects_malformed_port_type_expressions() {
    let invalid_types = [
        json!(""),
        json!("List<>"),
        json!("Map<String>"),
        json!("Optional<String,String>"),
        json!("Set<String>"),
        json!("List<Map<String,Any>"),
        json!("List< String>"),
        json!(42),
    ];

    for invalid_type in invalid_types {
        let blocks = json!([{
            "typeId": "test.block",
            "version": 1,
            "inputs": [{"name": "value", "type": invalid_type}]
        }]);
        let error = BlockCatalog::from_blocks(&blocks).expect_err("catalog must be rejected");
        assert!(
            error.contains("input value has invalid type"),
            "unexpected error: {error}"
        );
    }
}

#[test]
fn block_catalog_rejects_invalid_resource_type_references() {
    for invalid_type in [
        json!("???"),
        json!("<>"),
        json!("model,provider"),
        json!("."),
        json!(7),
    ] {
        let blocks = json!([{
            "typeId": "test.block",
            "version": 1,
            "resourceSlots": [{"name": "model", "type": invalid_type}]
        }]);
        let error = BlockCatalog::from_blocks(&blocks).expect_err("catalog must be rejected");
        assert!(
            error.contains("resource slot model has invalid type"),
            "unexpected error: {error}"
        );
    }
}

#[test]
fn compile_graph_migrates_legacy_graph_api_versions() {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha2",
        "kind": "Graph",
        "metadata": {"name": "legacy"},
        "spec": {"nodes": {}}
    });

    let plan = compile_graph_for_discovery(&graph);

    assert!(plan.ok());
    assert_eq!(
        plan.graph_hash,
        "sha256:d904303e2f3c52b6eaa405336f8f78670b9e8c1d6ab8fafc42ea1b537d409041"
    );
    assert_eq!(
        plan.normalized
            .pointer("/metadata/annotations/graphblocks.ai~1migratedFrom")
            .and_then(serde_json::Value::as_str),
        Some("graphblocks.ai/v1alpha2")
    );
}

#[test]
fn compile_graph_migrates_every_alpha_and_overwrites_untrusted_provenance() {
    for api_version in [
        "graphblocks.ai/v1alpha1",
        "graphblocks.ai/v1alpha2",
        "graphblocks.ai/v1alpha3",
    ] {
        let graph = json!({
            "apiVersion": api_version,
            "kind": "Graph",
            "metadata": {
                "name": "legacy",
                "annotations": {
                    "graphblocks.ai/migratedFrom": "graphblocks.ai/v0"
                }
            },
            "spec": {"nodes": {}}
        });

        let plan = compile_graph_for_discovery(&graph);

        assert!(plan.ok(), "{api_version}: {:?}", plan.diagnostics);
        assert_eq!(
            plan.normalized
                .get("apiVersion")
                .and_then(serde_json::Value::as_str),
            Some(GRAPH_API_VERSION),
        );
        assert_eq!(
            plan.normalized
                .pointer("/metadata/annotations/graphblocks.ai~1migratedFrom")
                .and_then(serde_json::Value::as_str),
            Some(api_version),
        );
    }
}

#[test]
fn compile_graph_does_not_relabel_unknown_stable_versions() {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v2",
        "kind": "Graph",
        "metadata": {"name": "future"},
        "spec": {"nodes": {}}
    });

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.normalized
            .get("apiVersion")
            .and_then(serde_json::Value::as_str),
        Some("graphblocks.ai/v2"),
    );
    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB0002"],
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

    let plan = compile_graph_for_discovery(&graph);

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

    let plan = compile_graph_for_discovery(&graph);

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
fn compile_graph_warns_when_declared_outputs_have_no_writer() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "declared-output"},
        "spec": {
            "interface": {
                "inputs": {"value": "graphblocks.ai/Text@1"},
                "outputs": {"value": "graphblocks.ai/Text@1"}
            },
            "nodes": {},
            "edges": []
        }
    });

    let plan = compile_graph_for_discovery(&graph);

    assert!(plan.ok());
    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Warning)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1004"]
    );
}

#[test]
fn schema_errors_are_not_suppressed_by_overlapping_warnings() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "schema-error-with-warning"},
        "spec": {
            "interface": {"outputs": {"value": "graphblocks.ai/Text@1"}},
            "nodes": {},
            "callbackSubscriptions": {}
        }
    });

    let plan = compile_graph_for_discovery(&graph);

    assert!(plan.diagnostics.iter().any(|diagnostic| {
        diagnostic.severity == Severity::Error
            && diagnostic.code == "GB0014"
            && diagnostic.path == "$.spec"
    }));
    assert!(plan.diagnostics.iter().any(|diagnostic| {
        diagnostic.severity == Severity::Warning && diagnostic.code == "GB1004"
    }));
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
            "edges": [{"from": "source.value", "to": "sink.missing.field"}]
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
            "edges": [{"from": "source.missing.field", "to": "sink.text"}]
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

    let plan = compile_graph_for_discovery(&graph);

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
fn compile_graph_rejects_alternate_numeric_callback_webhook_loopback_host() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "numeric-loopback-callback"},
        "spec": {
            "nodes": {"agent": {"block": "agent.run@1"}},
            "callbackSubscriptions": [
                {
                    "subscriptionId": "sub-loopback",
                    "scope": "run",
                    "scopeId": "run-1",
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://2130706433/events",
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay"
                        }
                    }
                }
            ]
        }
    });

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB6011"]
    );
}

#[test]
fn compile_graph_rejects_legacy_and_shared_numeric_callback_hosts() {
    for url in [
        "https://0177.0.0.1/events",
        "https://0x7f.0.0.1/events",
        "https://127.1/events",
        "https://100.64.0.1/events",
        "https://100.127.255.255/events",
    ] {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "legacy-numeric-loopback-callback"},
            "spec": {
                "nodes": {"agent": {"block": "agent.run@1"}},
                "callbackSubscriptions": [{
                    "subscriptionId": "sub-loopback",
                    "scope": "run",
                    "scopeId": "run-1",
                    "delivery": {
                        "kind": "webhook",
                        "url": url,
                        "signing": {
                            "algorithm": "hmac-sha256",
                            "secretRef": "secret://relay"
                        }
                    }
                }]
            }
        });

        let plan = compile_graph_for_discovery(&graph);
        assert!(
            plan.diagnostics
                .iter()
                .any(|diagnostic| diagnostic.severity == Severity::Error
                    && diagnostic.code == "GB6011"),
            "{url} should be rejected as an unsafe callback endpoint: {:?}",
            plan.diagnostics
        );
    }
}

#[test]
fn compile_graph_rejects_invalid_callback_webhook_host_syntax() {
    for url in [
        "https://hooks example.com/events",
        "https://hooks.example.com\t/events",
        "https://hooks.example.com%2fevil.test/events",
        "https://[not-ipv6]/events",
    ] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "invalid-callback-host"},
            "spec": {
                "nodes": {"agent": {"block": "agent.run@1"}},
                "callbackSubscriptions": [
                    {
                        "subscriptionId": "sub-invalid-host",
                        "scope": "run",
                        "scopeId": "run-1",
                        "delivery": {
                            "kind": "webhook",
                            "url": url,
                            "signing": {
                                "algorithm": "hmac-sha256",
                                "secretRef": "secret://relay"
                            }
                        }
                    }
                ]
            }
        });

        let plan = compile_graph_for_discovery(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB6011"],
            "{url} should be rejected as an unsafe callback endpoint"
        );
    }
}

#[test]
fn compile_graph_rejects_mapped_compatible_and_reserved_callback_hosts() {
    for url in [
        "https://[::ffff:127.0.0.1]/events",
        "https://[::169.254.169.254]/events",
        "https://240.0.0.1/events",
        "https://255.255.255.255/events",
    ] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "unsafe-callback-ip"},
            "spec": {
                "nodes": {"agent": {"block": "agent.run@1"}},
                "callbackSubscriptions": [
                    {
                        "subscriptionId": "sub-unsafe-ip",
                        "scope": "run",
                        "scopeId": "run-1",
                        "delivery": {
                            "kind": "webhook",
                            "url": url,
                            "signing": {
                                "algorithm": "hmac-sha256",
                                "secretRef": "secret://relay"
                            }
                        }
                    }
                ]
            }
        });

        let plan = compile_graph_for_discovery(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB6011"],
            "{url} should be rejected as an unsafe callback endpoint"
        );
    }
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

    let plan = compile_graph_for_discovery(&graph);
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

    let plan = compile_graph_for_discovery(&graph);

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

    let plan = compile_graph_for_discovery(&graph);

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

        let plan = compile_graph_for_discovery(&graph);

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

        let plan = compile_graph_for_discovery(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB1026"],
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1026"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1026"]
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
fn compile_graph_rejects_graph_interface_block_port_type_mismatch() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "text.echo",
            "version": 1,
            "inputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ],
            "outputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "interface-type-mismatch"},
        "spec": {
            "interface": {
                "inputs": {"value": "graphblocks.ai/Number@1"},
                "outputs": {"value": "graphblocks.ai/Number@1"}
            },
            "nodes": {"echo": {"block": "text.echo@1"}},
            "edges": [
                {"from": "$input.value", "to": "echo.value"},
                {"from": "echo.value", "to": "$output.value"}
            ]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1018", "GB1018"]
    );
    Ok(())
}

#[test]
fn compile_graph_accepts_matching_graph_interface_block_port_types() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "text.echo",
            "version": 1,
            "inputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ],
            "outputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "matching-interface-types"},
        "spec": {
            "interface": {
                "inputs": {"value": "graphblocks.ai/Text@1"},
                "outputs": {"value": "graphblocks.ai/Text@1"}
            },
            "nodes": {"echo": {"block": "text.echo@1"}},
            "edges": [
                {"from": "$input.value", "to": "echo.value"},
                {"from": "echo.value", "to": "$output.value"}
            ]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert!(plan.ok());
    Ok(())
}

#[test]
fn compile_graph_accepts_dynamic_pseudo_ports_when_interface_is_absent() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "text.echo",
            "version": 1,
            "inputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ],
            "outputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "dynamic-interface"},
        "spec": {
            "nodes": {"echo": {"block": "text.echo@1"}},
            "edges": [
                {"from": "$input.value", "to": "echo.value"},
                {"from": "echo.value", "to": "$output.value"}
            ]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert!(plan.ok());
    Ok(())
}

#[test]
fn compile_graph_rejects_unknown_nested_graph_interface_ports() -> Result<(), String> {
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
                {"name": "value", "type": "graphblocks.ai/Text@1", "required": false}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-nested-interface-ports"},
        "spec": {
            "interface": {
                "inputs": {"payload": "graphblocks.ai/Payload@1"},
                "outputs": {"payload": "graphblocks.ai/Payload@1"}
            },
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"}
            },
            "edges": [
                {"from": "$input.missing.field", "to": "sink.value"},
                {"from": "source.value", "to": "$output.missing.field"}
            ]
        }
    });

    for plan in [
        compile_graph_for_discovery(&graph),
        compile_graph_with_catalog(&graph, &catalog),
    ] {
        let errors = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .collect::<Vec<_>>();

        assert_eq!(
            errors
                .iter()
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB1014", "GB1013"]
        );
        assert_eq!(
            errors[0].message,
            "graph interface has no input port \"missing\""
        );
        assert_eq!(errors[0].path, "$.spec.edges[0].from");
        assert_eq!(
            errors[1].message,
            "graph interface has no output port \"missing\""
        );
        assert_eq!(errors[1].path, "$.spec.edges[1].to");
    }
    Ok(())
}

#[test]
fn compile_graph_accepts_declared_nested_interface_ports_without_field_type_inference()
-> Result<(), String> {
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
                {"name": "value", "type": "graphblocks.ai/Text@1", "required": false}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "declared-nested-interface-ports"},
        "spec": {
            "interface": {
                "inputs": {"payload": "graphblocks.ai/Payload@1"},
                "outputs": {"payload": "graphblocks.ai/Payload@1"}
            },
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"}
            },
            "edges": [
                {"from": "$input.payload.field", "to": "sink.value"},
                {"from": "source.value", "to": "$output.payload.field"}
            ]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert!(plan.ok());
    Ok(())
}

#[test]
fn compile_graph_rejects_graph_interface_pseudo_nodes_in_wrong_direction() -> Result<(), String> {
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
                {"name": "value", "type": "graphblocks.ai/Text@1", "required": false}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "wrong-pseudo-direction"},
        "spec": {
            "interface": {
                "inputs": {"value": "graphblocks.ai/Text@1"},
                "outputs": {"value": "graphblocks.ai/Text@1"}
            },
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"}
            },
            "edges": [
                {"from": "$output.value", "to": "sink.value"},
                {"from": "source.value", "to": "$input.value"}
            ]
        }
    });

    for plan in [
        compile_graph_for_discovery(&graph),
        compile_graph_with_catalog(&graph, &catalog),
    ] {
        let errors = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .collect::<Vec<_>>();

        assert_eq!(
            errors
                .iter()
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB1020", "GB1020"]
        );
        assert_eq!(
            errors[0].message,
            "$output cannot be used as an edge source"
        );
        assert_eq!(errors[0].path, "$.spec.edges[0].from");
        assert_eq!(errors[1].message, "$input cannot be used as an edge target");
        assert_eq!(errors[1].path, "$.spec.edges[1].to");
    }
    Ok(())
}

#[test]
fn compile_graph_preserves_any_wildcard_for_node_to_node_ports() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "any.source",
            "version": 1,
            "outputs": [
                {"name": "value", "type": "Any"}
            ]
        },
        {
            "typeId": "text.sink",
            "version": 1,
            "inputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "node-any-wildcard"},
        "spec": {
            "nodes": {
                "source": {"block": "any.source@1"},
                "sink": {"block": "text.sink@1"}
            },
            "edges": [{"from": "source.value", "to": "sink.value"}]
        }
    });

    let plan = compile_graph_with_catalog(&graph, &catalog);

    assert!(plan.ok());
    Ok(())
}

#[test]
fn compile_graph_rejects_edges_against_empty_descriptor_port_directions() -> Result<(), String> {
    let cases = [
        (
            json!([
                {"typeId": "test.source", "version": 1, "outputs": []},
                {
                    "typeId": "test.sink",
                    "version": 1,
                    "inputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}]
                }
            ]),
            "GB1014",
        ),
        (
            json!([
                {
                    "typeId": "test.source",
                    "version": 1,
                    "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}]
                },
                {"typeId": "test.sink", "version": 1, "inputs": []}
            ]),
            "GB1013",
        ),
    ];

    for (blocks, expected_code) in cases {
        let catalog = BlockCatalog::from_blocks(&blocks)?;
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "empty-descriptor-port-direction"},
            "spec": {
                "nodes": {
                    "source": {"block": "test.source@1"},
                    "sink": {"block": "test.sink@1"}
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
            vec![expected_code]
        );
    }
    Ok(())
}

#[test]
fn compile_graph_checks_nested_node_parent_ports_without_field_type_inference() -> Result<(), String>
{
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "test.source",
            "version": 1,
            "outputs": [
                {"name": "payload", "type": "graphblocks.ai/Payload@1"},
                {"name": "value", "type": "graphblocks.ai/Text@1"}
            ]
        },
        {
            "typeId": "test.sink",
            "version": 1,
            "inputs": [
                {"name": "payload", "type": "graphblocks.ai/Payload@1", "required": false},
                {"name": "value", "type": "graphblocks.ai/Text@1", "required": false}
            ]
        }
    ]))?;

    for edge in [
        json!({"from": "source.payload.field", "to": "sink.value"}),
        json!({"from": "source.value", "to": "sink.payload.field"}),
    ] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "nested-node-port"},
            "spec": {
                "nodes": {
                    "source": {"block": "test.source@1"},
                    "sink": {"block": "test.sink@1"}
                },
                "edges": [edge]
            }
        });

        let plan = compile_graph_with_catalog(&graph, &catalog);
        assert!(plan.ok());
    }
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
fn output_requiredness_predicates_evaluate_config_and_phase() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "branch.conditional",
            "version": 1,
            "outputs": [
                {
                    "name": "value",
                    "required": false,
                    "requiredWhen": {
                        "all": [
                            {
                                "configEquals": {
                                    "pointer": "/policy/on~1error",
                                    "value": "collect"
                                }
                            },
                            {"not": {"phase": "initial"}}
                        ]
                    }
                }
            ]
        }
    ]))?;
    let descriptor = catalog
        .get("branch.conditional@1")
        .ok_or_else(|| "missing conditional descriptor".to_owned())?;
    let output = descriptor
        .outputs
        .first()
        .ok_or_else(|| "missing conditional output".to_owned())?;
    let config = json!({"policy": {"on/error": "collect"}});

    assert!(!output.required_for(&config, ExecutionPhase::Initial));
    assert!(output.required_for(&config, ExecutionPhase::Resumed));
    Ok(())
}

#[test]
fn block_catalog_rejects_invalid_output_requiredness_predicates() {
    let mut too_deep = json!({"phase": "initial"});
    for _ in 0..16 {
        too_deep = json!({"not": too_deep});
    }
    for required_when in [
        json!({}),
        json!({"phase": "finished"}),
        json!({"configEquals": {"pointer": "onError", "value": "collect"}}),
        json!({"configEquals": {"pointer": "/bad~2escape", "value": "collect"}}),
        json!({"all": []}),
        json!({"all": vec![json!({"phase": "initial"}); 17]}),
        json!({"phase": "initial", "not": {"phase": "resumed"}}),
        too_deep,
    ] {
        let error = BlockCatalog::from_blocks(&json!([
            {
                "typeId": "branch.invalid",
                "version": 1,
                "outputs": [
                    {"name": "value", "required": false, "requiredWhen": required_when}
                ]
            }
        ]))
        .expect_err("invalid requiredWhen must fail catalog construction");
        assert!(error.contains("invalid requiredWhen"), "{error}");
    }
}

#[test]
fn block_catalog_rejects_required_when_on_input() {
    let error = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "branch.invalid-input",
            "version": 1,
            "inputs": [
                {"name": "value", "requiredWhen": {"phase": "resumed"}}
            ]
        }
    ]))
    .expect_err("input requiredWhen must fail catalog construction");

    assert!(
        error.contains("input value must not declare requiredWhen"),
        "{error}"
    );
}

#[test]
fn compile_graph_rejects_optional_block_output_to_graph_output() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([
        {
            "typeId": "branch.maybe_text",
            "version": 1,
            "outputs": [
                {"name": "value", "type": "graphblocks.ai/Text@1", "required": false}
            ]
        }
    ]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "optional-output-graph-output"},
        "spec": {
            "interface": {
                "outputs": {"value": "graphblocks.ai/Text@1"}
            },
            "nodes": {
                "maybe": {"block": "branch.maybe_text@1"}
            },
            "edges": [{"from": "maybe.value", "to": "$output.value"}]
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
fn compile_graph_with_catalog_rejects_undeclared_blocks() -> Result<(), String> {
    let catalog = BlockCatalog::from_blocks(&json!([]))?;
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unknown-block"},
        "spec": {
            "nodes": {
                "unknown": {"block": "test.unknown@1"}
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
        vec!["GB1022"]
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

        let plan = compile_graph_for_discovery(&graph);

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
fn compile_graph_rejects_invalid_node_timeout() {
    for timeout in [
        json!("soon"),
        json!("0s"),
        json!("-1s"),
        json!("nan"),
        json!("inf"),
        json!(0),
        json!(-1),
        json!(true),
    ] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "invalid-node-timeout"},
            "spec": {
                "nodes": {
                    "node": {
                        "block": "test.block@1",
                        "flow": {"timeout": timeout}
                    }
                }
            }
        });

        let plan = compile_graph_for_discovery(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB1019"]
        );
    }
}

#[test]
fn compile_graph_accepts_positive_finite_node_timeout() {
    for timeout in [json!(0.25), json!("250ms"), json!("0.5s"), json!("2")] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "valid-node-timeout"},
            "spec": {
                "nodes": {
                    "node": {
                        "block": "test.block@1",
                        "flow": {"timeout": timeout}
                    }
                }
            }
        });

        let plan = compile_graph_for_discovery(&graph);

        assert!(
            !plan
                .diagnostics
                .iter()
                .any(|diagnostic| diagnostic.code == "GB1019")
        );
    }
}

#[test]
fn compile_graph_rejects_effect_retry_with_invalid_idempotency_key() {
    for idempotency_key in [
        json!(""),
        json!(" \t"),
        json!(" request-1 "),
        json!(false),
        json!(7),
    ] {
        let graph = json!({
            "apiVersion": GRAPH_API_VERSION,
            "kind": "Graph",
            "metadata": {"name": "invalid-idempotency-retry"},
            "spec": {
                "nodes": {
                    "write": {
                        "block": "storage.write@1",
                        "effects": ["external_write"],
                        "flow": {
                            "retry": {
                                "maxAttempts": 2,
                                "idempotencyKey": idempotency_key
                            }
                        }
                    }
                }
            }
        });

        let plan = compile_graph_for_discovery(&graph);

        assert!(plan.diagnostics.iter().any(|diagnostic| {
            diagnostic.severity == Severity::Error && diagnostic.code == "GB1011"
        }));
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

        let plan = compile_graph_for_discovery(&graph);

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

    let plan = compile_graph_for_discovery(&graph);

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
        (json!({"spec": {"outputPolicy": "strict"}}), vec!["GB1034"]),
        (
            json!({"spec": {"outputPolicy": {"delivery": "bounded"}}}),
            vec!["GB1034", "GB1046"],
        ),
        (
            json!({"spec": {"outputPolicy": {"evaluation": "mandatory"}}}),
            vec!["GB1034", "GB1046"],
        ),
        (
            json!({"spec": {"outputPolicy": {"evaluation": {"enforcementPoints": "before_client_delivery"}}}}),
            vec!["GB1033", "GB1046"],
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
            vec!["GB1034"],
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

        let plan = compile_graph_for_discovery(&graph);

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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1051", "GB1046"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1051", "GB1046"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1051", "GB1046"]
    );
}

#[test]
fn compile_graph_rejects_holdback_limits_without_holdback_mode() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "mis-scoped-output-policy-bound"},
        "spec": {
            "outputPolicy": {
                "delivery": {
                    "mode": "buffer_until_commit",
                    "holdbackMaxTokens": 48,
                    "onViolation": "abort_response"
                }
            },
            "nodes": {
                "model": {"block": "model.generate@1"}
            }
        }
    });

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1054", "GB1046"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1025", "GB1046"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| matches!(diagnostic.code.as_str(), "GB1051" | "GB1025"))
    );
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1046"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1046"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1048"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| matches!(diagnostic.code.as_str(), "GB1046" | "GB1048"))
    );
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1047"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1024"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec![
            "GB1030", "GB1044", "GB1029", "GB1033", "GB1031", "GB1036", "GB1035", "GB1028",
            "GB1032"
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| matches!(diagnostic.code.as_str(), "GB1047" | "GB1024"))
    );
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1049"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1050"]
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
        let plan = compile_graph_for_discovery(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB1049"]
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
        let plan = compile_graph_for_discovery(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB1039"]
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
    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1039", "GB1039", "GB1039"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB0015"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB0015", "GB0015"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| matches!(diagnostic.code.as_str(), "GB1049" | "GB1050"))
    );
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1040"]
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

    let conflicting_plan = compile_graph_for_discovery(&conflicting_none);

    assert_eq!(
        conflicting_plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1040"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1037", "GB1042", "GB1038", "GB1043"]
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

        let plan = compile_graph_for_discovery(&graph);

        assert_eq!(
            plan.diagnostics
                .iter()
                .filter(|diagnostic| diagnostic.severity == Severity::Error)
                .map(|diagnostic| diagnostic.code.as_str())
                .collect::<Vec<_>>(),
            vec!["GB1041"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1053"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "GB1053")
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1045"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "GB1045")
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1023"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1023"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "GB1023")
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

    let plan = compile_graph_for_discovery(&graph);

    assert_eq!(
        plan.diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>(),
        vec!["GB1055"]
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

    let plan = compile_graph_for_discovery(&graph);

    assert!(
        !plan
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "GB1055")
    );
}
