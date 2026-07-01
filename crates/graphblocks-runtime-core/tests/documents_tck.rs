use graphblocks_runtime_core::document_parsers::{DocumentParserRegistry, ParserDescriptor};
use graphblocks_runtime_core::documents::{
    chunk_document_by_lines, create_local_text_revision, parse_plain_text_document, ArtifactRef,
    DocumentError,
};
use serde_json::{json, Value};

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("documents TCK case missing string {key}"))
}

fn run_case(case: &Value) -> Result<Value, String> {
    let kind = required_str(case, "kind")?;
    let source_uri = case
        .get("sourceUri")
        .or_else(|| case.get("source_uri"))
        .and_then(Value::as_str)
        .unwrap_or("file:///tmp/document.txt");
    let text = case.get("text").and_then(Value::as_str).unwrap_or_default();
    let observed_at = case
        .get("observedAt")
        .or_else(|| case.get("observed_at"))
        .and_then(Value::as_str)
        .unwrap_or("2026-06-22T00:00:00Z");
    let filename = case.get("filename").and_then(Value::as_str);
    let (asset, mut revision) = create_local_text_revision(source_uri, text, observed_at, filename);
    if let Some(acl) = case.get("acl") {
        revision.acl = Some(acl.clone());
    }
    let document = parse_plain_text_document(&asset, &revision, text);

    match kind {
        "plain_text_parse" => Ok(json!({
            "contentHash": revision.content_hash.clone(),
            "assetId": asset.asset_id.clone(),
            "artifactMediaType": revision.artifact.media_type.clone(),
            "artifactSizeBytes": revision.artifact.size_bytes,
            "parserProcessorId": document.parser.get("processor_id").cloned().unwrap_or(Value::Null),
            "elementTexts": document.elements.iter().map(|element| element.content.as_str()).collect::<Vec<_>>(),
            "elementSpans": document.elements.iter().map(|element| {
                json!([element.location.char_start, element.location.char_end])
            }).collect::<Vec<_>>(),
            "documentLineageConsistent": document.asset_id == asset.asset_id
                && document.revision_id == revision.revision_id
                && document.document_id == format!("doc:{}", revision.revision_id),
            "assetCurrentRevisionMatches": asset.current_revision_id.as_deref() == Some(revision.revision_id.as_str()),
        })),
        "line_chunks" => {
            let max_elements = case
                .get("maxElements")
                .or_else(|| case.get("max_elements"))
                .and_then(Value::as_u64)
                .ok_or_else(|| "documents TCK line_chunks requires maxElements".to_owned())?
                as usize;
            let chunks = chunk_document_by_lines(&document, &revision, max_elements)
                .map_err(|error| error.to_string())?;
            Ok(json!({
                "chunkTexts": chunks.iter().map(|chunk| chunk.text.as_str()).collect::<Vec<_>>(),
                "chunkSpans": chunks.iter().map(|chunk| {
                    let locator = chunk.source_refs.first().and_then(|source| source.locator.as_ref());
                    json!([
                        locator.and_then(|item| item.char_start),
                        locator.and_then(|item| item.char_end),
                    ])
                }).collect::<Vec<_>>(),
                "chunkElementCounts": chunks.iter().map(|chunk| chunk.element_ids.len()).collect::<Vec<_>>(),
                "sourceRefKinds": chunks.iter().map(|chunk| {
                    chunk.source_refs.first().map(|source| source.source_kind.as_str())
                }).collect::<Vec<_>>(),
                "sourceRefDigestMatches": chunks.iter().all(|chunk| {
                    chunk.source_refs.first().and_then(|source| source.digest.as_deref())
                        == Some(revision.content_hash.as_str())
                }),
                "sourceRefLocatorConsistent": chunks.iter().all(|chunk| {
                    chunk.source_refs.first().and_then(|source| source.locator.as_ref()).is_some_and(|locator| {
                        locator.asset_id == chunk.asset_id
                            && locator.revision_id == chunk.revision_id
                            && locator.document_id == chunk.document_id
                            && locator.chunk_id.as_deref() == Some(chunk.chunk_id.as_str())
                    })
                }),
                "chunkAcls": chunks.iter().map(|chunk| {
                    chunk.acl.clone().unwrap_or(Value::Null)
                }).collect::<Vec<_>>(),
            }))
        }
        "invalid_chunk_size" => {
            let max_elements = case
                .get("maxElements")
                .or_else(|| case.get("max_elements"))
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize;
            let error = match chunk_document_by_lines(&document, &revision, max_elements) {
                Ok(_) => Value::Null,
                Err(DocumentError::InvalidMaxElements) => json!("invalid_max_elements"),
            };
            Ok(json!({ "error": error }))
        }
        "parser_selection_lock" => {
            let raw_artifact =
                case.get("artifact")
                    .and_then(Value::as_object)
                    .ok_or_else(|| {
                        "documents TCK parser_selection_lock requires artifact".to_owned()
                    })?;
            let mut artifact = ArtifactRef::new(
                raw_artifact
                    .get("artifactId")
                    .or_else(|| raw_artifact.get("artifact_id"))
                    .and_then(Value::as_str)
                    .unwrap_or("artifact-1"),
                raw_artifact
                    .get("uri")
                    .and_then(Value::as_str)
                    .unwrap_or("file:///tmp/document.txt"),
            );
            artifact.media_type = raw_artifact
                .get("mediaType")
                .or_else(|| raw_artifact.get("media_type"))
                .and_then(Value::as_str)
                .map(str::to_owned);
            artifact.filename = raw_artifact
                .get("filename")
                .and_then(Value::as_str)
                .map(str::to_owned);
            artifact.checksum = raw_artifact
                .get("checksum")
                .and_then(Value::as_str)
                .map(str::to_owned);
            let raw_descriptors = case
                .get("descriptors")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    "documents TCK parser_selection_lock requires descriptors".to_owned()
                })?;
            let mut registry = DocumentParserRegistry::new();
            for (index, raw_descriptor) in raw_descriptors.iter().enumerate() {
                let mapping = raw_descriptor.as_object().ok_or_else(|| {
                    format!("documents TCK parser descriptor {index} must be an object")
                })?;
                let mut descriptor = ParserDescriptor::new(
                    mapping
                        .get("processorId")
                        .or_else(|| mapping.get("processor_id"))
                        .and_then(Value::as_str)
                        .unwrap_or(""),
                    mapping.get("version").and_then(Value::as_str).unwrap_or(""),
                );
                if let Some(media_types) = mapping
                    .get("mediaTypes")
                    .or_else(|| mapping.get("media_types"))
                    .and_then(Value::as_array)
                {
                    descriptor.media_types = media_types
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect();
                }
                if let Some(extensions) = mapping.get("extensions").and_then(Value::as_array) {
                    descriptor.extensions = extensions
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect();
                }
                descriptor.priority = mapping
                    .get("priority")
                    .and_then(Value::as_i64)
                    .map(|priority| priority as i32)
                    .unwrap_or(0);
                if let Some(metadata) = mapping.get("metadata").and_then(Value::as_object) {
                    descriptor.metadata = metadata
                        .iter()
                        .map(|(key, value)| (key.clone(), value.clone()))
                        .collect();
                }
                registry
                    .register(descriptor)
                    .map_err(|error| error.to_string())?;
            }
            let lock = registry
                .select(&artifact)
                .map_err(|error| error.to_string())?;
            let resolved = registry
                .resolve_locked(&lock)
                .map_err(|error| error.to_string())?;
            Ok(json!({
                "processorId": lock.processor_id,
                "processorVersion": lock.processor_version,
                "reason": lock.reason,
                "mediaType": lock.media_type,
                "filename": lock.filename,
                "artifactChecksum": lock.artifact_checksum,
                "metadata": lock.metadata,
                "resolvedMetadata": resolved.metadata,
            }))
        }
        other => Err(format!("unsupported documents TCK kind {other:?}")),
    }
}

#[test]
fn rust_documents_matches_shared_tck_cases() -> Result<(), String> {
    let cases: Value = serde_json::from_str(include_str!("../../../tck/documents/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "documents TCK root must be an array".to_owned())?;

    for case in cases {
        let case_name = required_str(case, "name")?;
        let observed = run_case(case).map_err(|error| format!("{case_name}: {error}"))?;
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("documents TCK case {case_name} missing expected"))?;
        for (key, expected_value) in expected {
            assert_eq!(
                observed.get(key).unwrap_or(&Value::Null),
                expected_value,
                "documents TCK case {case_name} expected {key}"
            );
        }
    }

    Ok(())
}
