use graphblocks_compiler::compiler::compile_graph;
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
fn compile_graph_rejects_effect_retry_without_idempotency_key() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {"name": "unsafe-retry"},
        "spec": {
            "nodes": {
                "write": {
                    "block": "storage.write@1",
                    "effects": ["external_write"],
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
        vec!["UnboundedPolicyHoldback"]
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
        vec!["ImmediateDraftWithoutRetractionSupport"]
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
