use std::mem;

use graphblocks_schema::{
    CanonicalJsonError, MAX_CANONICAL_JSON_DEPTH, MAX_RESOURCE_DOCUMENT_DEPTH, TypedValue,
    TypedValueError, resource_schema_errors, try_canonical_json,
};
use serde_json::{Map, Value};

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

    let mut metadata = Map::new();
    metadata.insert("name".to_owned(), Value::String("json-depth".to_owned()));

    let mut graph = Map::new();
    graph.insert(
        "apiVersion".to_owned(),
        Value::String("graphblocks.ai/v1".to_owned()),
    );
    graph.insert("kind".to_owned(), Value::String("Graph".to_owned()));
    graph.insert("metadata".to_owned(), Value::Object(metadata));
    graph.insert("spec".to_owned(), Value::Object(spec));
    Value::Object(graph)
}

fn graph_with_extensions(entries: [(&str, Value); 2]) -> Value {
    let mut extensions = Map::new();
    for (key, value) in entries {
        extensions.insert(key.to_owned(), value);
    }

    let mut spec = Map::new();
    spec.insert("nodes".to_owned(), Value::Object(Map::new()));
    spec.insert("extensions".to_owned(), Value::Object(extensions));

    let mut graph = Map::new();
    graph.insert(
        "apiVersion".to_owned(),
        Value::String("graphblocks.ai/v1".to_owned()),
    );
    graph.insert("kind".to_owned(), Value::String("Graph".to_owned()));
    graph.insert("metadata".to_owned(), Value::Object(Map::new()));
    graph.insert("spec".to_owned(), Value::Object(spec));
    Value::Object(graph)
}

#[test]
fn canonical_json_accepts_depth_64_and_rejects_depth_65() {
    let at_limit = nested_object(MAX_CANONICAL_JSON_DEPTH);
    let over_limit = nested_object(MAX_CANONICAL_JSON_DEPTH + 1);

    assert!(try_canonical_json(&at_limit).is_ok());
    assert_eq!(
        try_canonical_json(&over_limit),
        Err(CanonicalJsonError::NestingTooDeep {
            max_depth: MAX_CANONICAL_JSON_DEPTH,
        })
    );
}

#[test]
fn typed_value_checked_envelope_paths_report_the_depth_error()
-> Result<(), Box<dyn std::error::Error>> {
    let value = TypedValue::new("schemas/Message@1", nested_object(MAX_CANONICAL_JSON_DEPTH))?;
    let expected = CanonicalJsonError::NestingTooDeep {
        max_depth: MAX_CANONICAL_JSON_DEPTH,
    };

    assert_eq!(value.try_canonical_value(), Err(expected.clone()));
    assert_eq!(value.try_canonical_json(), Err(expected.clone()));
    assert_eq!(value.try_to_canonical_json(), Err(expected));
    Ok(())
}

#[test]
fn resource_validation_matches_python_depth_diagnostic_at_the_boundary()
-> Result<(), Box<dyn std::error::Error>> {
    // The payload starts at resource depth 5, so 59 nested properties end at depth 64.
    let at_limit = graph_with_payload(nested_object(MAX_RESOURCE_DOCUMENT_DEPTH - 5));
    let over_limit = graph_with_payload(nested_object(MAX_RESOURCE_DOCUMENT_DEPTH - 4));

    assert!(resource_schema_errors(&at_limit)?.is_empty());
    let errors = resource_schema_errors(&over_limit)?;
    assert_eq!(errors.len(), 1);
    assert_eq!(errors[0].code, "GB0014");
    assert_eq!(errors[0].keyword, "maxDepth");
    assert_eq!(
        errors[0].message,
        "resource nesting must not exceed 64 levels"
    );
    assert_eq!(
        errors[0].path,
        format!(
            "$.spec.nodes.n.config.payload{}",
            ".next".repeat(MAX_RESOURCE_DOCUMENT_DEPTH - 4)
        )
    );
    assert_eq!(errors[0].schema_path, "$");
    Ok(())
}

#[test]
fn resource_depth_diagnostic_matches_python_object_key_order()
-> Result<(), Box<dyn std::error::Error>> {
    let left = graph_with_extensions([
        ("b", nested_object(MAX_RESOURCE_DOCUMENT_DEPTH - 2)),
        ("a", nested_object(MAX_RESOURCE_DOCUMENT_DEPTH - 2)),
    ]);
    let right = graph_with_extensions([
        ("a", nested_object(MAX_RESOURCE_DOCUMENT_DEPTH - 2)),
        ("b", nested_object(MAX_RESOURCE_DOCUMENT_DEPTH - 2)),
    ]);

    let left_errors = resource_schema_errors(&left)?;
    let right_errors = resource_schema_errors(&right)?;
    assert_eq!(left_errors, right_errors);
    assert_eq!(left_errors.len(), 1);
    assert_eq!(
        left_errors[0].path,
        format!(
            "$.spec.extensions.a{}",
            ".next".repeat(MAX_RESOURCE_DOCUMENT_DEPTH - 2)
        )
    );
    Ok(())
}

#[test]
fn resource_validation_matches_python_root_type_precedence()
-> Result<(), Box<dyn std::error::Error>> {
    let not_a_resource = Value::Array(vec![nested_object(100_000)]);
    let errors = resource_schema_errors(&not_a_resource)?;

    assert_eq!(errors.len(), 1);
    assert_eq!(errors[0].code, "GB0012");
    assert_eq!(errors[0].keyword, "type");
    assert_eq!(errors[0].message, "resource must be an object");
    assert_eq!(errors[0].path, "$");

    mem::forget(not_a_resource);
    Ok(())
}

#[test]
fn very_deep_values_return_errors_without_recursive_abort() -> Result<(), Box<dyn std::error::Error>>
{
    let canonical_value = nested_object(100_000);
    assert_eq!(
        try_canonical_json(&canonical_value),
        Err(CanonicalJsonError::NestingTooDeep {
            max_depth: MAX_CANONICAL_JSON_DEPTH,
        })
    );
    mem::forget(canonical_value);

    let resource = graph_with_payload(nested_object(100_000));
    let errors = resource_schema_errors(&resource)?;
    assert_eq!(errors.len(), 1);
    assert_eq!(errors[0].keyword, "maxDepth");
    mem::forget(resource);

    let error = TypedValue::new("schemas/Message@1", nested_object(100_000))
        .expect_err("typed values must reject excessive nesting");
    assert_eq!(
        error,
        TypedValueError::CanonicalJson(CanonicalJsonError::NestingTooDeep {
            max_depth: MAX_CANONICAL_JSON_DEPTH,
        })
    );
    Ok(())
}
