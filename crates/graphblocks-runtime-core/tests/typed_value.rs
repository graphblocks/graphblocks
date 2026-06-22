use graphblocks_runtime_core::typed_value::{TypedValue, TypedValueError, ValueEncoding};
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
