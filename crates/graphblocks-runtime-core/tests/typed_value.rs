use graphblocks_runtime_core::typed_value::{
    RemoteBoundaryValuePolicy, RemoteBoundaryValuePolicyError, TypedValue, TypedValueError,
    ValueEncoding,
};
use serde_json::json;

#[test]
fn typed_value_json_preserves_schema_and_round_trips_payload() -> Result<(), TypedValueError> {
    let value = TypedValue::json("graphblocks.ai/Message", 1, json!({"text": "hello"}))?;

    assert_eq!(value.schema_id(), "graphblocks.ai/Message");
    assert_eq!(value.schema_version(), 1);
    assert_eq!(value.encoding(), ValueEncoding::Json);
    assert_eq!(value.decode_json()?, json!({"text": "hello"}));
    Ok(())
}

#[test]
fn typed_value_rejects_json_decode_for_non_json_encoding() {
    let value = TypedValue::new(
        "graphblocks.ai/Binary",
        1,
        ValueEncoding::RawBytes,
        vec![1, 2, 3],
    );

    assert_eq!(
        value.decode_json(),
        Err(TypedValueError::UnexpectedEncoding {
            expected: ValueEncoding::Json,
            actual: ValueEncoding::RawBytes,
        }),
    );
}

#[test]
fn typed_value_rejects_empty_schema_identity() {
    assert_eq!(
        TypedValue::try_new("", 1, ValueEncoding::Json, b"{}".to_vec()),
        Err(TypedValueError::InvalidSchemaId),
    );
    assert_eq!(
        TypedValue::try_new(
            "graphblocks.ai/Message",
            0,
            ValueEncoding::Json,
            b"{}".to_vec()
        ),
        Err(TypedValueError::InvalidSchemaVersion),
    );
}

#[test]
fn remote_boundary_policy_rejects_non_serializable_inline_bytes() {
    let policy = RemoteBoundaryValuePolicy::new(1024);
    let value = TypedValue::new(
        "graphblocks.ai/Binary",
        1,
        ValueEncoding::RawBytes,
        vec![1, 2, 3],
    );

    assert_eq!(
        policy.validate("node-1", "output", &value),
        Err(RemoteBoundaryValuePolicyError::NonSerializableInlineValue {
            node_id: "node-1".to_owned(),
            port: "output".to_owned(),
            encoding: ValueEncoding::RawBytes,
        })
    );
}

#[test]
fn remote_boundary_policy_rejects_oversized_inline_values() -> Result<(), TypedValueError> {
    let policy = RemoteBoundaryValuePolicy::new(16);
    let value = TypedValue::json("graphblocks.ai/Message", 1, json!({"text": "too large"}))?;

    assert_eq!(
        policy.validate("node-1", "output", &value),
        Err(RemoteBoundaryValuePolicyError::InlineValueTooLarge {
            node_id: "node-1".to_owned(),
            port: "output".to_owned(),
            size_bytes: value.payload().len(),
            max_inline_bytes: 16,
        })
    );
    Ok(())
}

#[test]
fn remote_boundary_policy_allows_artifact_references_over_inline_limit() {
    let policy = RemoteBoundaryValuePolicy::new(16);
    let artifact_ref = TypedValue::new(
        "graphblocks.ai/ArtifactRef",
        1,
        ValueEncoding::ArtifactRef,
        br#"{"artifactId":"artifact-1","uri":"blob://large"}"#.to_vec(),
    );

    policy
        .validate("node-1", "output", &artifact_ref)
        .expect("artifact references cross remote boundaries by reference");
}
