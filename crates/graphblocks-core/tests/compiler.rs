use graphblocks_core::compiler::compile_graph;
use graphblocks_core::diagnostics::Severity;
use graphblocks_core::graph::GRAPH_API_VERSION;
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
