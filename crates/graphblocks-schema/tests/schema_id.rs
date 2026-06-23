use graphblocks_schema::{SchemaId, SchemaIdError};

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
}
