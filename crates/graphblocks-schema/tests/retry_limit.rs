use graphblocks_schema::resource_schema_errors;
use serde_json::{Value, json};

fn stable_graph_with_retry(retry: Value) -> Value {
    json!({
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "bounded-retry"},
        "spec": {
            "nodes": {
                "worker": {
                    "block": "test.worker@1",
                    "flow": {"retry": retry}
                }
            }
        }
    })
}

#[test]
fn stable_schema_accepts_retry_attempt_limit() -> Result<(), Box<dyn std::error::Error>> {
    for retry in [json!(100), json!({"maxAttempts": 100})] {
        let errors = resource_schema_errors(&stable_graph_with_retry(retry))?;
        assert!(errors.is_empty(), "{errors:?}");
    }
    Ok(())
}

#[test]
fn stable_schema_rejects_retry_attempts_above_limit() -> Result<(), Box<dyn std::error::Error>> {
    for retry in [
        json!(101),
        json!(u64::MAX),
        json!({"maxAttempts": 101}),
        json!({"maxAttempts": u64::MAX}),
    ] {
        let errors = resource_schema_errors(&stable_graph_with_retry(retry))?;
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert_eq!(errors[0].path, "$.spec.nodes.worker.flow.retry");
        assert_eq!(errors[0].keyword, "oneOf");
    }
    Ok(())
}

#[test]
fn stable_schema_preserves_retry_type_rejection() -> Result<(), Box<dyn std::error::Error>> {
    for retry in [
        json!(true),
        json!("100"),
        json!({"maxAttempts": true}),
        json!({"maxAttempts": "100"}),
    ] {
        assert!(!resource_schema_errors(&stable_graph_with_retry(retry))?.is_empty());
    }
    Ok(())
}
