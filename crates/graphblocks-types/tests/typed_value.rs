use graphblocks_schema::SchemaIdError;
use graphblocks_types::TypedValue;
use serde_json::json;

#[test]
fn typed_value_preserves_schema_id_and_round_trips_json() -> Result<(), Box<dyn std::error::Error>>
{
    let value = TypedValue::new("schemas/Message@1", json!({"text": "hello"}))?;

    assert_eq!(value.schema_id().as_str(), "schemas/Message@1");
    assert_eq!(value.value(), &json!({"text": "hello"}));

    let encoded = serde_json::to_value(&value)?;
    assert_eq!(
        encoded,
        json!({
            "schema": "schemas/Message@1",
            "value": {"text": "hello"},
        }),
    );

    let decoded: TypedValue = serde_json::from_value(encoded)?;
    assert_eq!(decoded, value);
    Ok(())
}

#[test]
fn typed_value_rejects_invalid_schema_id() {
    assert_eq!(
        TypedValue::new("schemas/Message", json!({}))
            .expect_err("schema id without a version must be rejected"),
        SchemaIdError::MissingVersion,
    );
}
