use std::mem;

use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_compiler::compiler::compile_graph_for_discovery;
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_schema::MAX_RESOURCE_DOCUMENT_DEPTH;
use serde_json::{Map, Value, json};

fn nested_object(depth: usize) -> Value {
    let mut value = Value::Null;
    for _ in 0..depth {
        let mut object = Map::new();
        object.insert("next".to_owned(), value);
        value = Value::Object(object);
    }
    value
}

fn graph_with_payload(payload: Value) -> Value {
    let mut config = Map::new();
    config.insert("payload".to_owned(), payload);

    let mut node = Map::new();
    node.insert("block".to_owned(), Value::String("test.node@1".to_owned()));
    node.insert("config".to_owned(), Value::Object(config));

    let mut nodes = Map::new();
    nodes.insert("n".to_owned(), Value::Object(node));

    let mut spec = Map::new();
    spec.insert("nodes".to_owned(), Value::Object(nodes));
    spec.insert("edges".to_owned(), Value::Array(Vec::new()));

    let mut graph = Map::new();
    graph.insert(
        "apiVersion".to_owned(),
        Value::String("graphblocks.ai/v1".to_owned()),
    );
    graph.insert("kind".to_owned(), Value::String("Graph".to_owned()));
    let mut metadata = Map::new();
    metadata.insert("name".to_owned(), Value::String("json-depth".to_owned()));
    graph.insert("metadata".to_owned(), Value::Object(metadata));
    graph.insert("spec".to_owned(), Value::Object(spec));
    Value::Object(graph)
}

#[test]
fn compiler_accepts_resource_depth_64_and_rejects_depth_65_before_cloning() {
    // The payload begins at depth 5: $.spec.nodes.n.config.payload.
    let at_limit = graph_with_payload(nested_object(MAX_RESOURCE_DOCUMENT_DEPTH - 5));
    let accepted = compile_graph_for_discovery(&at_limit);
    assert!(
        accepted
            .diagnostics
            .iter()
            .all(|diagnostic| diagnostic.code != "GB0014"),
        "depth-64 resource should pass the depth admission check: {:?}",
        accepted.diagnostics
    );

    let nested_depth = MAX_RESOURCE_DOCUMENT_DEPTH - 4;
    let over_limit = graph_with_payload(nested_object(nested_depth));
    let rejected = compile_graph_for_discovery(&over_limit);
    assert_eq!(rejected.diagnostics.len(), 1);
    assert_eq!(rejected.diagnostics[0].severity, Severity::Error);
    assert_eq!(rejected.diagnostics[0].code, "GB0014");
    assert_eq!(
        rejected.diagnostics[0].message,
        "resource nesting must not exceed 64 levels"
    );
    assert_eq!(
        rejected.diagnostics[0].path,
        format!(
            "$.spec.nodes.n.config.payload{}",
            ".next".repeat(nested_depth)
        )
    );

    let invalid_resource_identity = json!({
        "invalidResource": [{
            "code": "GB0014",
            "keyword": "maxDepth",
            "message": "resource nesting must not exceed 64 levels",
            "path": rejected.diagnostics[0].path,
        }]
    });
    assert_eq!(rejected.normalized, invalid_resource_identity);
    assert_eq!(
        rejected.graph_hash,
        canonical_hash(&invalid_resource_identity)
    );
}

#[test]
fn compiler_rejects_very_deep_resource_without_recursive_abort() {
    let document = graph_with_payload(nested_object(100_000));
    let plan = compile_graph_for_discovery(&document);

    assert_eq!(plan.diagnostics.len(), 1);
    assert_eq!(plan.diagnostics[0].code, "GB0014");
    assert_eq!(
        plan.diagnostics[0].message,
        "resource nesting must not exceed 64 levels"
    );

    // serde_json::Value has a recursive destructor, so keep the intentionally
    // pathological fixture from obscuring the admission-path assertion.
    mem::forget(document);
}
