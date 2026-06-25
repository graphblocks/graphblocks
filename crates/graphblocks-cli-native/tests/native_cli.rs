use graphblocks_cli_native::{NativeCliMode, run_compiler_workflow};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_compiler::graph::GRAPH_API_VERSION;
use serde_json::json;

#[test]
fn native_validate_reports_ok_and_plan_hash_without_expanded_plan() {
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

    let report = run_compiler_workflow(&graph, NativeCliMode::Validate);

    assert!(report.ok);
    assert_eq!(
        report.graph_hash.as_deref(),
        Some("sha256:0b2636678ee1af1446500624da2f5db0dab238aceb858058a6f3b60f9e06f3a8")
    );
    assert_eq!(report.normalized, None);
    assert!(report.diagnostics.is_empty());
}

#[test]
fn native_plan_can_include_normalized_graph_document() {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha2",
        "kind": "Graph",
        "metadata": {"name": "legacy"},
        "spec": {"nodes": {}}
    });

    let report = run_compiler_workflow(&graph, NativeCliMode::Plan { expand: true });

    assert!(report.ok);
    assert_eq!(
        report
            .normalized
            .as_ref()
            .and_then(|value| value.pointer("/metadata/annotations/graphblocks.ai~1migratedFrom"))
            .and_then(serde_json::Value::as_str),
        Some("graphblocks.ai/v1alpha2")
    );
}

#[test]
fn native_validate_returns_structured_diagnostics() {
    let graph = json!({
        "apiVersion": GRAPH_API_VERSION,
        "kind": "Graph",
        "metadata": {},
        "spec": {"nodes": {}}
    });

    let report = run_compiler_workflow(&graph, NativeCliMode::Validate);

    assert!(!report.ok);
    assert_eq!(report.graph_hash, None);
    assert_eq!(report.normalized, None);
    assert_eq!(report.diagnostics[0].code, "GB0003");
    assert_eq!(report.diagnostics[0].severity, Severity::Error);
}
