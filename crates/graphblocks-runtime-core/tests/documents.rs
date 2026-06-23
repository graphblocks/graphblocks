use graphblocks_runtime_core::documents::{
    ArtifactRef, AssetRevision, DocumentChunk, DocumentElement, DocumentSpan, ParsedDocument,
    SourceAsset, SourceLocation, SourceRef, chunk_document_by_lines, create_local_text_revision,
    parse_plain_text_document,
};

#[test]
fn create_local_text_revision_preserves_content_hash_and_artifact_metadata() {
    let (asset, revision) = create_local_text_revision(
        "file:///tmp/example.txt",
        "alpha\nbeta\n",
        "2026-06-22T00:00:00Z",
        Some("example.txt"),
    );

    assert_eq!(
        asset,
        SourceAsset {
            asset_id:
                "asset:sha256:9ef537bd0aeb4a23ae2ed37907c3ee610f289bbd95002833ce04448407ffe33f"
                    .to_owned(),
            source_uri: "file:///tmp/example.txt".to_owned(),
            source_kind: "local".to_owned(),
            tenant_id: None,
            current_revision_id: Some(
                "rev:sha256:e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee"
                    .to_owned()
            ),
        }
    );
    assert_eq!(revision.asset_id, asset.asset_id);
    assert_eq!(
        revision.content_hash,
        "sha256:e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee"
    );
    assert_eq!(
        revision.artifact,
        ArtifactRef {
            artifact_id:
                "artifact:sha256:e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee"
                    .to_owned(),
            uri: "file:///tmp/example.txt".to_owned(),
            media_type: Some("text/plain".to_owned()),
            size_bytes: Some(11),
            checksum: Some(
                "sha256:e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee"
                    .to_owned()
            ),
            etag: None,
            version: None,
            filename: Some("example.txt".to_owned()),
            metadata: Default::default(),
        }
    );
}

#[test]
fn parse_plain_text_document_creates_paragraph_elements_with_char_spans() {
    let (asset, revision) = create_local_text_revision(
        "file:///tmp/notes.txt",
        "Title\n\nFirst paragraph.\nSecond paragraph.\n",
        "2026-06-22T00:00:00Z",
        Some("notes.txt"),
    );

    let document = parse_plain_text_document(
        &asset,
        &revision,
        "Title\n\nFirst paragraph.\nSecond paragraph.\n",
    );

    assert_eq!(
        document.document_id,
        format!("doc:{}", revision.revision_id)
    );
    assert_eq!(document.asset_id, asset.asset_id);
    assert_eq!(document.revision_id, revision.revision_id);
    assert_eq!(
        document
            .elements
            .iter()
            .map(|element| element.content.as_str())
            .collect::<Vec<_>>(),
        vec!["Title", "First paragraph.", "Second paragraph."]
    );
    assert_eq!(
        document
            .elements
            .iter()
            .map(|element| (element.location.char_start, element.location.char_end))
            .collect::<Vec<_>>(),
        vec![
            (Some(0), Some(5)),
            (Some(7), Some(23)),
            (Some(24), Some(41))
        ]
    );
}

#[test]
fn chunk_document_by_lines_preserves_lineage_and_source_spans()
-> Result<(), Box<dyn std::error::Error>> {
    let (asset, revision) = create_local_text_revision(
        "file:///tmp/notes.txt",
        "Title\n\nFirst paragraph.\nSecond paragraph.\n",
        "2026-06-22T00:00:00Z",
        Some("notes.txt"),
    );
    let document = parse_plain_text_document(
        &asset,
        &revision,
        "Title\n\nFirst paragraph.\nSecond paragraph.\n",
    );

    let chunks = chunk_document_by_lines(&document, &revision, 2)?;

    assert_eq!(
        chunks
            .iter()
            .map(|chunk| chunk.text.as_str())
            .collect::<Vec<_>>(),
        vec!["Title\nFirst paragraph.", "Second paragraph."]
    );
    assert_eq!(chunks[0].asset_id, asset.asset_id);
    assert_eq!(chunks[0].revision_id, revision.revision_id);
    assert_eq!(chunks[0].document_id, document.document_id);
    assert_eq!(
        chunks[0].element_ids,
        vec![
            document.elements[0].element_id.clone(),
            document.elements[1].element_id.clone()
        ]
    );
    assert_eq!(
        chunks[0].source_refs[0].digest.as_ref(),
        Some(&revision.content_hash)
    );
    assert_eq!(
        chunks[0].source_refs[0]
            .locator
            .as_ref()
            .map(|locator| locator.asset_id.as_str()),
        Some(asset.asset_id.as_str())
    );
    assert_eq!(
        chunks[0]
            .source_refs
            .first()
            .and_then(|source| source.locator.as_ref())
            .map(|locator| (locator.char_start, locator.char_end)),
        Some((Some(0), Some(23)))
    );

    assert!(matches!(
        chunk_document_by_lines(&document, &revision, 0),
        Err(graphblocks_runtime_core::documents::DocumentError::InvalidMaxElements)
    ));
    Ok(())
}

#[test]
fn document_chunk_source_ref_contains_full_lineage_ids() {
    let asset = SourceAsset::new("asset-1", "file:///tmp/example.txt", "local")
        .with_current_revision_id("rev-1");
    let revision = AssetRevision::new(
        "rev-1",
        &asset.asset_id,
        "sha256:content",
        "2026-06-22T00:00:00Z",
        ArtifactRef::new("artifact-1", "file:///tmp/example.txt"),
    );
    let document = ParsedDocument::new(
        "doc-1",
        &asset.asset_id,
        &revision.revision_id,
        "plain-text",
        "1",
    )
    .with_element(DocumentElement::new(
        "el-1",
        "paragraph",
        0,
        "hello world",
        SourceLocation::new().with_char_span(0, 11),
    ));
    let chunk = DocumentChunk::new(
        "chunk-1",
        &document.document_id,
        &document.asset_id,
        &document.revision_id,
        "hello world",
    )
    .with_element_ids(["el-1"])
    .with_source_ref(SourceRef::document_chunk(
        "chunk-1",
        &revision.revision_id,
        &revision.content_hash,
        DocumentSpan::new(
            &asset.asset_id,
            &revision.revision_id,
            &document.document_id,
        )
        .with_element_id("el-1")
        .with_chunk_id("chunk-1")
        .with_char_span(0, 11),
    ));

    let locator = chunk.source_refs[0]
        .locator
        .as_ref()
        .expect("source ref carries document span");
    assert_eq!(locator.asset_id, asset.asset_id);
    assert_eq!(locator.revision_id, revision.revision_id);
    assert_eq!(locator.document_id, document.document_id);
    assert_eq!(locator.element_id.as_deref(), Some("el-1"));
    assert_eq!(
        chunk.source_refs[0].digest.as_ref(),
        Some(&revision.content_hash)
    );
}
