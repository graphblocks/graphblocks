use graphblocks_runtime_core::document_parsers::{
    DocumentParserError, DocumentParserRegistry, OcrResult, OcrTextBlock, ParserDescriptor,
    ParserSelectionLock, apply_ocr_fallback, plain_text_parser_descriptor,
};
use graphblocks_runtime_core::documents::{
    ArtifactRef, AssetRevision, ParsedDocument, SourceAsset, parse_plain_text_document,
};
use serde_json::json;

#[test]
fn parser_registry_selects_by_media_type_and_records_lock_inputs()
-> Result<(), Box<dyn std::error::Error>> {
    let mut registry = DocumentParserRegistry::new();
    let mut descriptor = plain_text_parser_descriptor();
    descriptor
        .metadata
        .insert("config_digest".to_owned(), json!("sha256:parser-config"));
    descriptor
        .metadata
        .insert("profile".to_owned(), json!("plain-text-default"));
    registry.register(descriptor.clone());
    let mut artifact = ArtifactRef::new("artifact-1", "file:///tmp/policy.txt");
    artifact.media_type = Some("text/plain".to_owned());
    artifact.checksum = Some("sha256:content".to_owned());
    artifact.filename = Some("policy.txt".to_owned());

    let lock = registry.select(&artifact)?;
    descriptor
        .metadata
        .insert("profile".to_owned(), json!("mutated"));

    assert_eq!(lock.processor_id, "plain-text");
    assert_eq!(lock.processor_version, "1");
    assert_eq!(lock.reason, "media_type");
    assert_eq!(lock.media_type.as_deref(), Some("text/plain"));
    assert_eq!(lock.filename.as_deref(), Some("policy.txt"));
    assert_eq!(lock.artifact_checksum.as_deref(), Some("sha256:content"));
    assert_eq!(
        lock.metadata.get("config_digest"),
        Some(&json!("sha256:parser-config"))
    );
    assert_eq!(
        lock.metadata.get("profile"),
        Some(&json!("plain-text-default"))
    );
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
fn parser_registry_rejects_lock_for_different_artifact_checksum()
-> Result<(), Box<dyn std::error::Error>> {
    let mut registry = DocumentParserRegistry::new();
    registry.register(plain_text_parser_descriptor());
    let asset = SourceAsset::new("asset-1", "file:///tmp/policy.txt", "local")
        .with_current_revision_id("rev-1");
    let mut selected_artifact = ArtifactRef::new("artifact-1", "file:///tmp/policy.txt");
    selected_artifact.media_type = Some("text/plain".to_owned());
    selected_artifact.filename = Some("policy.txt".to_owned());
    selected_artifact.checksum = Some("sha256:old".to_owned());
    let lock = registry.select(&selected_artifact)?;
    let mut artifact = ArtifactRef::new("artifact-1", "file:///tmp/policy.txt");
    artifact.media_type = Some("text/plain".to_owned());
    artifact.filename = Some("policy.txt".to_owned());
    artifact.checksum = Some("sha256:new".to_owned());
    let revision = AssetRevision::new(
        "rev-1",
        "asset-1",
        "sha256:new",
        "2026-06-22T00:00:00Z",
        artifact,
    );

    assert!(matches!(
        registry.parse_locked(&asset, &revision, b"Alpha\n", &lock),
        Err(DocumentParserError::LockMismatch {
            expected_checksum,
            actual_checksum
        }) if expected_checksum == "sha256:old" && actual_checksum.as_deref() == Some("sha256:new")
    ));
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

#[test]
fn ocr_result_to_parsed_document_records_provenance_and_source_variant() {
    let result = OcrResult::new("tesseract", "5", "ocr-overlay")
        .with_language_hints(["en", "ko"])
        .with_preprocessing_config_digest("sha256:deskew")
        .with_source_artifact(ArtifactRef::new(
            "page-image-1",
            "blob://pages/policy-1.png",
        ))
        .with_text_block(
            OcrTextBlock::new("Scanned Alpha")
                .with_page(1)
                .with_confidence(0.93)
                .with_rotation_degrees(1.5),
        );

    let document = result.to_parsed_document("doc-ocr", "asset-1", "rev-1");

    assert_eq!(document.parser["processor_id"], json!("tesseract"));
    assert_eq!(document.parser["version"], json!("5"));
    assert_eq!(document.plain_text.as_deref(), Some("Scanned Alpha"));
    assert_eq!(document.metadata["source_variant"], json!("ocr-overlay"));
    assert_eq!(
        document.metadata["ocr"]["preprocessing_config_digest"],
        json!("sha256:deskew")
    );
    assert_eq!(document.elements[0].kind, "ocr_text");
    assert_eq!(document.elements[0].location.page, Some(1));
    assert_eq!(document.elements[0].metadata["confidence"], json!(0.93));
    assert_eq!(
        document.elements[0].metadata["source_artifact_id"],
        json!("page-image-1")
    );
}

#[test]
fn ocr_fallback_uses_ocr_when_primary_extraction_is_empty() {
    let empty_document = ParsedDocument::new("doc-1", "asset-1", "rev-1", "pdf-parser", "1");
    let result = OcrResult::new("tesseract", "5", "ocr-overlay")
        .with_text_block(OcrTextBlock::new("Scanned Alpha"));

    let document = apply_ocr_fallback(empty_document, result);

    assert_eq!(document.parser["processor_id"], json!("tesseract"));
    assert_eq!(document.plain_text.as_deref(), Some("Scanned Alpha"));
    assert_eq!(document.metadata["source_variant"], json!("ocr-overlay"));
}

#[test]
fn ocr_fallback_preserves_existing_text_layer_as_primary_variant() {
    let asset = SourceAsset::new("asset-1", "file:///tmp/policy.txt", "local");
    let revision = AssetRevision::new(
        "rev-1",
        "asset-1",
        "sha256:content",
        "2026-06-22T00:00:00Z",
        ArtifactRef::new("artifact-1", "file:///tmp/policy.txt"),
    );
    let parsed = parse_plain_text_document(&asset, &revision, "Native Alpha\n");
    let result = OcrResult::new("tesseract", "5", "ocr-overlay")
        .with_text_block(OcrTextBlock::new("Scanned Alpha"));

    let document = apply_ocr_fallback(parsed, result);

    assert_eq!(document.parser["processor_id"], json!("plain-text"));
    assert_eq!(document.plain_text.as_deref(), Some("Native Alpha\n"));
    assert_eq!(
        document.metadata["source_variants"]["ocr-overlay"]["text"],
        json!("Scanned Alpha")
    );
    assert_eq!(
        document.metadata["source_variants"]["ocr-overlay"]["processor"]["processor_id"],
        json!("tesseract")
    );
}
