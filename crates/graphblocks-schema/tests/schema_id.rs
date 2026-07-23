use graphblocks_schema::{SchemaId, SchemaIdError, TypedValue};
use serde_json::json;

#[test]
fn schema_id_accepts_canonical_major_version_reference() -> Result<(), SchemaIdError> {
    let schema_id = SchemaId::parse("schemas/Message@1")?;

    assert_eq!(schema_id.as_str(), "schemas/Message@1");
    assert_eq!(schema_id.name(), "schemas/Message");
    assert_eq!(schema_id.major_version(), 1);
    assert_eq!(schema_id.to_string(), "schemas/Message@1");
    Ok(())
}

#[test]
fn schema_id_rejects_missing_or_invalid_version() {
    assert_eq!(SchemaId::parse(""), Err(SchemaIdError::Empty));
    assert_eq!(
        SchemaId::parse("schemas/Message"),
        Err(SchemaIdError::MissingVersion),
    );
    assert_eq!(SchemaId::parse("@1"), Err(SchemaIdError::EmptyName));
    assert_eq!(
        SchemaId::parse("schemas/Message@0"),
        Err(SchemaIdError::InvalidMajorVersion),
    );
    assert_eq!(
        SchemaId::parse("schemas/Message@01"),
        Err(SchemaIdError::NonCanonicalVersion),
    );
    assert_eq!(
        SchemaId::parse("schemas/Chat Message@1"),
        Err(SchemaIdError::InvalidName),
    );
    assert_eq!(
        SchemaId::parse("schemas/Chat\tMessage@1"),
        Err(SchemaIdError::InvalidName),
    );
}

#[test]
fn typed_value_preserves_schema_id_and_round_trips_json() -> Result<(), Box<dyn std::error::Error>>
{
    let value = TypedValue::new("schemas/Message@1", json!({"text": "hello"}))?;

    assert_eq!(value.schema_id().as_str(), "schemas/Message@1");
    assert_eq!(value.value(), &json!({"text": "hello"}));
    assert_eq!(
        value.canonical_value(),
        json!({"schema": "schemas/Message@1", "value": {"text": "hello"}}),
    );

    let encoded = serde_json::to_value(&value)?;
    let decoded: TypedValue = serde_json::from_value(encoded)?;

    assert_eq!(decoded, value);
    Ok(())
}
