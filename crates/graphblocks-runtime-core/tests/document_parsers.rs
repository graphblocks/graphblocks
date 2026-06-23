use graphblocks_runtime_core::document_parsers::{
    DocumentParserError, DocumentParserRegistry, ParserDescriptor, ParserSelectionLock,
    plain_text_parser_descriptor,
};
use graphblocks_runtime_core::documents::{ArtifactRef, AssetRevision, SourceAsset};
use serde_json::json;

#[test]
fn parser_registry_selects_by_media_type_and_records_lock_inputs()
-> Result<(), Box<dyn std::error::Error>> {
    let mut registry = DocumentParserRegistry::new();
    registry.register(plain_text_parser_descriptor());
    let mut artifact = ArtifactRef::new("artifact-1", "file:///tmp/policy.txt");
    artifact.media_type = Some("text/plain".to_owned());
    artifact.checksum = Some("sha256:content".to_owned());
    artifact.filename = Some("policy.txt".to_owned());

    let lock = registry.select(&artifact)?;

    assert_eq!(lock.processor_id, "plain-text");
    assert_eq!(lock.processor_version, "1");
    assert_eq!(lock.reason, "media_type");
    assert_eq!(lock.media_type.as_deref(), Some("text/plain"));
    assert_eq!(lock.filename.as_deref(), Some("policy.txt"));
    assert_eq!(lock.artifact_checksum.as_deref(), Some("sha256:content"));
    Ok(())
}

#[test]
fn parser_registry_uses_extension_when_media_type_is_missing()
-> Result<(), Box<dyn std::error::Error>> {
    let mut registry = DocumentParserRegistry::new();
    registry.register(plain_text_parser_descriptor());
    let mut artifact = ArtifactRef::new("artifact-1", "file:///tmp/policy.txt");
    artifact.filename = Some("policy.txt".to_owned());

    let lock = registry.select(&artifact)?;

    assert_eq!(lock.processor_id, "plain-text");
    assert_eq!(lock.reason, "extension");
    Ok(())
}

#[test]
fn parser_registry_selection_is_deterministic_for_equal_priority()
-> Result<(), Box<dyn std::error::Error>> {
    let mut registry = DocumentParserRegistry::new();
    registry.register(
        ParserDescriptor::new("z-parser", "1")
            .with_media_types(["text/plain"])
            .with_priority(10),
    );
    registry.register(
        ParserDescriptor::new("a-parser", "2")
            .with_media_types(["text/plain"])
            .with_priority(10),
    );
    let mut artifact = ArtifactRef::new("artifact-1", "file:///tmp/policy.txt");
    artifact.media_type = Some("text/plain".to_owned());

    let lock = registry.select(&artifact)?;

    assert_eq!(lock.processor_id, "a-parser");
    assert_eq!(lock.processor_version, "2");
    Ok(())
}

#[test]
fn parser_registry_parse_locked_uses_locked_parser_version()
-> Result<(), Box<dyn std::error::Error>> {
    let mut registry = DocumentParserRegistry::new();
    registry.register(plain_text_parser_descriptor());
    let asset = SourceAsset::new("asset-1", "file:///tmp/policy.txt", "local")
        .with_current_revision_id("rev-1");
    let mut artifact = ArtifactRef::new("artifact-1", "file:///tmp/policy.txt");
    artifact.media_type = Some("text/plain".to_owned());
    artifact.filename = Some("policy.txt".to_owned());
    let revision = AssetRevision::new(
        "rev-1",
        "asset-1",
        "sha256:content",
        "2026-06-22T00:00:00Z",
        artifact,
    );

    let lock = registry.select(&revision.artifact)?;
    let document = registry.parse_locked(&asset, &revision, b"Alpha\n\nBeta\n", &lock)?;

    assert_eq!(document.parser["processor_id"], json!("plain-text"));
    assert_eq!(document.parser["version"], json!("1"));
    assert_eq!(
        document
            .elements
            .iter()
            .map(|element| element.content.as_str())
            .collect::<Vec<_>>(),
        vec!["Alpha", "Beta"]
    );
    Ok(())
}

#[test]
fn parser_registry_rejects_unknown_locked_parser() {
    let registry = DocumentParserRegistry::new();
    let lock = ParserSelectionLock::new("missing", "1", "media_type").with_media_type("text/plain");
    let asset = SourceAsset::new("asset-1", "file:///tmp/policy.txt", "local");
    let revision = AssetRevision::new(
        "rev-1",
        "asset-1",
        "sha256:content",
        "2026-06-22T00:00:00Z",
        ArtifactRef::new("artifact-1", "file:///tmp/policy.txt"),
    );

    assert!(matches!(
        registry.parse_locked(&asset, &revision, b"Alpha\n", &lock),
        Err(DocumentParserError::NotFound { .. })
    ));
}
