use graphblocks_runtime_core::documents::{ArtifactRef, AssetRevision, SourceAsset};
use graphblocks_runtime_core::ingestion::{
    InMemoryIngestionManifestStore, IndexRecordRef, IngestionError, IngestionManifest,
    IngestionStatus, ProcessorRef,
};

fn source_revision(revision_id: &str) -> (SourceAsset, AssetRevision) {
    let asset = SourceAsset::new("asset-1", "file:///tmp/policy.txt", "local")
        .with_current_revision_id(revision_id);
    let mut artifact =
        ArtifactRef::new(format!("artifact-{revision_id}"), "file:///tmp/policy.txt");
    artifact.media_type = Some("text/plain".to_owned());
    artifact.checksum = Some(format!("sha256:{revision_id}"));
    artifact.filename = Some("policy.txt".to_owned());
    let revision = AssetRevision::new(
        revision_id,
        "asset-1",
        format!("sha256:{revision_id}"),
        "2026-06-22T00:00:00Z",
        artifact,
    );
    (asset, revision)
}

fn manifest(manifest_id: &str, revision_id: &str) -> IngestionManifest {
    let (asset, revision) = source_revision(revision_id);
    IngestionManifest::new(
        manifest_id,
        &asset,
        &revision,
        ProcessorRef::new("plain-text", "1").with_config_digest("sha256:parser-config"),
        ProcessorRef::new("line-chunker", "1").with_config_digest("sha256:chunker-config"),
        "sha256:pipeline",
        "2026-06-22T00:00:00Z",
    )
}

fn index_record(revision_id: &str) -> IndexRecordRef {
    IndexRecordRef::new(
        "knowledge-local",
        format!("record-{revision_id}"),
        "asset-1",
        revision_id,
    )
    .with_chunk_ids([format!("chunk-{revision_id}")])
}

#[test]
fn ingestion_manifest_records_source_processors_and_status() {
    let manifest = manifest("manifest-1", "rev-1");

    assert_eq!(manifest.manifest_id, "manifest-1");
    assert_eq!(manifest.asset_id, "asset-1");
    assert_eq!(manifest.revision_id, "rev-1");
    assert_eq!(manifest.source_uri, "file:///tmp/policy.txt");
    assert_eq!(manifest.content_hash, "sha256:rev-1");
    assert_eq!(manifest.parser.processor_id, "plain-text");
    assert_eq!(manifest.chunker.processor_id, "line-chunker");
    assert_eq!(manifest.pipeline_hash, "sha256:pipeline");
    assert_eq!(manifest.status, IngestionStatus::Discovered);
    assert_eq!(manifest.created_at, "2026-06-22T00:00:00Z");
    assert_eq!(manifest.updated_at, "2026-06-22T00:00:00Z");
}

#[test]
fn manifest_store_commit_marks_ready_and_supersedes_previous_revision()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryIngestionManifestStore::new();
    store.create_processing(manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")?;
    store.commit(
        "manifest-1",
        Some(ArtifactRef::new("parsed-rev-1", "blob://parsed/rev-1.json")),
        Some(ArtifactRef::new("chunks-rev-1", "blob://chunks/rev-1.json")),
        vec![index_record("rev-1")],
        "2026-06-22T00:02:00Z",
    )?;
    store.create_processing(manifest("manifest-2", "rev-2"), "2026-06-22T00:03:00Z")?;

    let committed = store.commit(
        "manifest-2",
        Some(ArtifactRef::new("parsed-rev-2", "blob://parsed/rev-2.json")),
        Some(ArtifactRef::new("chunks-rev-2", "blob://chunks/rev-2.json")),
        vec![index_record("rev-2")],
        "2026-06-22T00:04:00Z",
    )?;

    assert_eq!(committed.status, IngestionStatus::Ready);
    assert_eq!(committed.index_records, vec![index_record("rev-2")]);
    assert_eq!(
        store.get("manifest-1").map(|manifest| &manifest.status),
        Some(&IngestionStatus::Superseded)
    );
    assert_eq!(
        store
            .current_for_asset("asset-1")
            .map(|manifest| manifest.manifest_id.as_str()),
        Some("manifest-2")
    );
    Ok(())
}

#[test]
fn manifest_store_commit_is_idempotent_for_ready_manifest() -> Result<(), Box<dyn std::error::Error>>
{
    let mut store = InMemoryIngestionManifestStore::new();
    store.create_processing(manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")?;
    let parsed_ref = ArtifactRef::new("parsed-rev-1", "blob://parsed/rev-1.json");
    store.commit(
        "manifest-1",
        Some(parsed_ref.clone()),
        None,
        vec![index_record("rev-1")],
        "2026-06-22T00:02:00Z",
    )?;

    let committed = store.commit("manifest-1", None, None, Vec::new(), "2026-06-22T00:03:00Z")?;

    assert_eq!(committed.status, IngestionStatus::Ready);
    assert_eq!(committed.parsed_document_ref, Some(parsed_ref));
    assert_eq!(committed.index_records, vec![index_record("rev-1")]);
    assert_eq!(committed.updated_at, "2026-06-22T00:02:00Z");
    Ok(())
}

#[test]
fn manifest_store_rejects_index_records_for_different_asset_or_revision()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryIngestionManifestStore::new();
    store.create_processing(manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")?;

    let mut wrong_asset = index_record("rev-1");
    wrong_asset.asset_id = "asset-2".to_owned();
    let asset_error = store
        .commit(
            "manifest-1",
            None,
            None,
            vec![wrong_asset],
            "2026-06-22T00:02:00Z",
        )
        .expect_err("index record from another asset must be rejected");
    assert!(matches!(
        asset_error,
        IngestionError::InvalidIndexRecordLineage { field, .. } if field == "asset_id"
    ));

    let revision_error = store
        .commit(
            "manifest-1",
            None,
            None,
            vec![index_record("rev-2")],
            "2026-06-22T00:03:00Z",
        )
        .expect_err("index record from another revision must be rejected");
    assert!(matches!(
        revision_error,
        IngestionError::InvalidIndexRecordLineage { field, .. } if field == "revision_id"
    ));
    assert_eq!(
        store.get("manifest-1").map(|manifest| &manifest.status),
        Some(&IngestionStatus::Processing)
    );
    Ok(())
}

#[test]
fn manifest_store_tombstone_marks_deleted_and_clears_current()
-> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryIngestionManifestStore::new();
    store.create_processing(manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")?;
    store.commit(
        "manifest-1",
        None,
        None,
        vec![index_record("rev-1")],
        "2026-06-22T00:02:00Z",
    )?;

    let deleted = store.tombstone("manifest-1", "2026-06-22T00:03:00Z")?;

    assert_eq!(deleted.status, IngestionStatus::Deleted);
    assert!(store.current_for_asset("asset-1").is_none());
    assert_eq!(
        store.get("manifest-1").map(|manifest| &manifest.status),
        Some(&IngestionStatus::Deleted)
    );
    Ok(())
}

#[test]
fn manifest_store_rejects_commit_after_tombstone() -> Result<(), Box<dyn std::error::Error>> {
    let mut store = InMemoryIngestionManifestStore::new();
    store.create_processing(manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")?;
    store.tombstone("manifest-1", "2026-06-22T00:02:00Z")?;

    let error = store
        .commit(
            "manifest-1",
            None,
            None,
            vec![index_record("rev-1")],
            "2026-06-22T00:03:00Z",
        )
        .expect_err("deleted manifests cannot be committed");

    assert!(matches!(error, IngestionError::InvalidTransition { .. }));
    Ok(())
}
