from __future__ import annotations

from dataclasses import replace

import pytest

import graphblocks
from graphblocks import ArtifactRef, AssetRevision, SourceAsset
from graphblocks.ingestion import (
    IngestionError,
    IngestionManifest,
    IngestionStatus,
    InMemoryIngestionManifestStore,
    IndexRecordRef,
    ProcessorRef,
)


def test_root_facade_exports_ingestion_manifest_types() -> None:
    expected = {
        "IndexRecordRef",
        "IngestionDeletePolicy",
        "IngestionError",
        "IngestionManifest",
        "IngestionStatus",
        "InMemoryIngestionManifestStore",
        "ProcessorRef",
    }

    assert sorted(name for name in expected if name not in graphblocks.__all__) == []
    for name in expected:
        assert hasattr(graphblocks, name)


def _source_revision(revision_id: str) -> tuple[SourceAsset, AssetRevision]:
    artifact = ArtifactRef(
        artifact_id=f"artifact-{revision_id}",
        uri="file:///tmp/policy.txt",
        media_type="text/plain",
        checksum=f"sha256:{revision_id}",
        filename="policy.txt",
    )
    asset = SourceAsset(
        asset_id="asset-1",
        source_uri="file:///tmp/policy.txt",
        source_kind="local",
        current_revision_id=revision_id,
    )
    revision = AssetRevision(
        revision_id=revision_id,
        asset_id="asset-1",
        content_hash=f"sha256:{revision_id}",
        observed_at="2026-06-22T00:00:00Z",
        artifact=artifact,
    )
    return asset, revision


def _manifest(manifest_id: str, revision_id: str) -> IngestionManifest:
    asset, revision = _source_revision(revision_id)
    return IngestionManifest.new(
        manifest_id,
        asset,
        revision,
        ProcessorRef("plain-text", "1", config_digest="sha256:parser-config"),
        ProcessorRef("line-chunker", "1", config_digest="sha256:chunker-config"),
        "sha256:pipeline",
        "2026-06-22T00:00:00Z",
    ).with_acl_revision(f"acl-{revision_id}")


def _index_record(revision_id: str) -> IndexRecordRef:
    return IndexRecordRef(
        index_id="knowledge-local",
        record_id=f"record-{revision_id}",
        asset_id="asset-1",
        revision_id=revision_id,
        chunk_ids=(f"chunk-{revision_id}",),
    )


def test_ingestion_manifest_records_source_processors_and_status() -> None:
    manifest = _manifest("manifest-1", "rev-1")

    assert manifest.manifest_id == "manifest-1"
    assert manifest.asset_id == "asset-1"
    assert manifest.revision_id == "rev-1"
    assert manifest.source_uri == "file:///tmp/policy.txt"
    assert manifest.content_hash == "sha256:rev-1"
    assert manifest.parser.processor_id == "plain-text"
    assert manifest.chunker.processor_id == "line-chunker"
    assert manifest.pipeline_hash == "sha256:pipeline"
    assert manifest.status == "discovered"
    assert manifest.created_at == "2026-06-22T00:00:00Z"
    assert manifest.updated_at == "2026-06-22T00:00:00Z"


def test_ingestion_records_validate_identity_metadata_and_nested_types() -> None:
    with pytest.raises(ValueError, match="processor ref processor_id must not be empty"):
        ProcessorRef(" ", "1")
    with pytest.raises(ValueError, match="processor ref version must be a string"):
        ProcessorRef("plain-text", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="processor ref metadata keys must not be empty"):
        ProcessorRef("plain-text", "1", metadata={" ": "value"})
    with pytest.raises(ValueError, match="processor ref metadata must be a mapping"):
        ProcessorRef("plain-text", "1", metadata=object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="index record ref index_id must not be empty"):
        IndexRecordRef("", "record-1", "asset-1", "rev-1")
    with pytest.raises(ValueError, match="index record ref chunk_ids must be a collection of strings"):
        IndexRecordRef("knowledge", "record-1", "asset-1", "rev-1", chunk_ids="chunk-1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="index record ref chunk_id must not be empty"):
        IndexRecordRef("knowledge", "record-1", "asset-1", "rev-1", chunk_ids=(" ",))
    with pytest.raises(ValueError, match="index record ref metadata keys must be strings"):
        IndexRecordRef("knowledge", "record-1", "asset-1", "rev-1", metadata={object(): "value"})  # type: ignore[dict-item]

    manifest = _manifest("manifest-1", "rev-1")
    with pytest.raises(ValueError, match="ingestion manifest manifest_id must not be empty"):
        replace(manifest, manifest_id=" ")
    with pytest.raises(ValueError, match="invalid ingestion status"):
        replace(manifest, status="paused")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ingestion manifest parser must be a ProcessorRef"):
        replace(manifest, parser=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ingestion manifest normalizer must be a ProcessorRef"):
        replace(manifest, normalizers=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ingestion manifest parsed_document_ref must be an ArtifactRef"):
        replace(manifest, parsed_document_ref=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ingestion manifest index_records must be IndexRecordRef records"):
        replace(manifest, index_records=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ingestion manifest index record asset_id must match"):
        replace(manifest, index_records=(replace(_index_record("rev-1"), asset_id="asset-2"),))
    with pytest.raises(ValueError, match="failed ingestion manifest requires error"):
        replace(manifest, status="failed")
    with pytest.raises(ValueError, match="non-failed ingestion manifest must not include error"):
        replace(manifest, error="temporary failure")


@pytest.mark.parametrize(
    ("factory", "expected_error"),
    (
        (
            lambda: ProcessorRef(" plain-text", "1"),
            "processor ref processor_id must not contain surrounding whitespace",
        ),
        (
            lambda: ProcessorRef("plain-text", "1", config_digest=" sha256:parser-config"),
            "processor ref config_digest must not contain surrounding whitespace",
        ),
        (
            lambda: ProcessorRef("plain-text", "1", metadata={" profile": "plain-text"}),
            "processor ref metadata keys must not contain surrounding whitespace",
        ),
        (
            lambda: IndexRecordRef(" knowledge-local", "record-1", "asset-1", "rev-1"),
            "index record ref index_id must not contain surrounding whitespace",
        ),
        (
            lambda: IndexRecordRef("knowledge-local", "record-1", "asset-1", "rev-1", chunk_ids=(" chunk-1",)),
            "index record ref chunk_id must not contain surrounding whitespace",
        ),
        (
            lambda: IndexRecordRef("knowledge-local", "record-1", "asset-1", "rev-1", metadata={" source": "kb"}),
            "index record ref metadata keys must not contain surrounding whitespace",
        ),
        (
            lambda: replace(_manifest("manifest-1", "rev-1"), manifest_id=" manifest-1"),
            "ingestion manifest manifest_id must not contain surrounding whitespace",
        ),
        (
            lambda: replace(_manifest("manifest-1", "rev-1"), acl_revision=" acl-rev-1"),
            "ingestion manifest acl_revision must not contain surrounding whitespace",
        ),
        (
            lambda: replace(_manifest("manifest-1", "rev-1"), status="failed", error=" parser failed"),
            "ingestion manifest error must not contain surrounding whitespace",
        ),
        (
            lambda: replace(_manifest("manifest-1", "rev-1"), metadata={" phase": "initial"}),
            "ingestion manifest metadata keys must not contain surrounding whitespace",
        ),
    ),
)
def test_ingestion_records_reject_whitespace_wrapped_identities(
    factory: object,
    expected_error: str,
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        factory()


def test_ingestion_manifest_store_commit_supersedes_previous_revision() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")
    store.commit(
        "manifest-1",
        parsed_document_ref=ArtifactRef("parsed-rev-1", "blob://parsed/rev-1.json"),
        chunk_set_ref=ArtifactRef("chunks-rev-1", "blob://chunks/rev-1.json"),
        index_records=(_index_record("rev-1"),),
        updated_at="2026-06-22T00:02:00Z",
    )
    store.create_processing(_manifest("manifest-2", "rev-2"), "2026-06-22T00:03:00Z")

    committed = store.commit(
        "manifest-2",
        parsed_document_ref=ArtifactRef("parsed-rev-2", "blob://parsed/rev-2.json"),
        chunk_set_ref=ArtifactRef("chunks-rev-2", "blob://chunks/rev-2.json"),
        index_records=(_index_record("rev-2"),),
        updated_at="2026-06-22T00:04:00Z",
    )

    assert committed.status == "ready"
    assert committed.index_records == (_index_record("rev-2"),)
    assert store.get("manifest-1").status == "superseded"
    assert store.current_for_asset("asset-1").manifest_id == "manifest-2"


def test_ingestion_manifest_store_commit_is_idempotent_for_ready_manifest() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")
    parsed_ref = ArtifactRef("parsed-rev-1", "blob://parsed/rev-1.json")
    store.commit(
        "manifest-1",
        parsed_document_ref=parsed_ref,
        chunk_set_ref=None,
        index_records=(_index_record("rev-1"),),
        updated_at="2026-06-22T00:02:00Z",
    )

    committed = store.commit("manifest-1", None, None, (), "2026-06-22T00:03:00Z")

    assert committed.status == "ready"
    assert committed.parsed_document_ref == parsed_ref
    assert committed.index_records == (_index_record("rev-1"),)
    assert committed.updated_at == "2026-06-22T00:02:00Z"


def test_ingestion_manifest_store_rejects_index_records_for_different_asset_or_revision() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")

    with pytest.raises(IngestionError, match="asset_id"):
        store.commit(
            "manifest-1",
            parsed_document_ref=None,
            chunk_set_ref=None,
            index_records=(replace(_index_record("rev-1"), asset_id="asset-2"),),
            updated_at="2026-06-22T00:02:00Z",
        )

    with pytest.raises(IngestionError, match="revision_id"):
        store.commit(
            "manifest-1",
            parsed_document_ref=None,
            chunk_set_ref=None,
            index_records=(_index_record("rev-2"),),
            updated_at="2026-06-22T00:03:00Z",
        )

    assert store.get("manifest-1").status == "processing"


def test_ingestion_manifest_store_rejects_publish_without_acl_revision() -> None:
    store = InMemoryIngestionManifestStore()
    manifest = replace(_manifest("manifest-1", "rev-1"), acl_revision=None)
    store.create_processing(manifest, "2026-06-22T00:01:00Z")

    with pytest.raises(IngestionError, match="acl_revision"):
        store.commit(
            "manifest-1",
            parsed_document_ref=ArtifactRef("parsed-rev-1", "blob://parsed/rev-1.json"),
            chunk_set_ref=ArtifactRef("chunks-rev-1", "blob://chunks/rev-1.json"),
            index_records=(_index_record("rev-1"),),
            updated_at="2026-06-22T00:02:00Z",
        )

    assert store.get("manifest-1").status == "processing"


def test_ingestion_manifest_store_copies_manifests_and_index_metadata_at_boundaries() -> None:
    store = InMemoryIngestionManifestStore()
    manifest = replace(
        _manifest("manifest-1", "rev-1"),
        parser=ProcessorRef("plain-text", "1", metadata={"profile": "initial"}),
        metadata={"phase": "initial"},
    )

    processing = store.create_processing(manifest, "2026-06-22T00:01:00Z")
    manifest.metadata["phase"] = "manifest-mutated"
    manifest.parser.metadata["profile"] = "manifest-mutated"
    processing.metadata["phase"] = "returned-mutated"
    processing.parser.metadata["profile"] = "returned-mutated"

    fresh_processing = store.get("manifest-1")
    assert fresh_processing.metadata == {"phase": "initial"}
    assert fresh_processing.parser.metadata == {"profile": "initial"}

    index_record = IndexRecordRef(
        index_id="knowledge-local",
        record_id="record-rev-1",
        asset_id="asset-1",
        revision_id="rev-1",
        chunk_ids=("chunk-rev-1",),
        metadata={"source": "initial"},
    )
    committed = store.commit("manifest-1", None, None, (index_record,), "2026-06-22T00:02:00Z")
    index_record.metadata["source"] = "index-mutated"
    committed.index_records[0].metadata["source"] = "returned-mutated"

    fresh_ready = store.get("manifest-1")
    assert fresh_ready.index_records[0].metadata == {"source": "initial"}


def test_ingestion_manifest_store_tombstone_marks_deleted_and_clears_current() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")
    store.commit("manifest-1", None, None, (_index_record("rev-1"),), "2026-06-22T00:02:00Z")

    deleted = store.tombstone("manifest-1", "2026-06-22T00:03:00Z")

    assert deleted.status == "deleted"
    assert store.current_for_asset("asset-1") is None
    assert store.get("manifest-1").status == "deleted"


def test_ingestion_manifest_store_delete_hard_removes_manifest_and_current_pointer() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")
    store.commit("manifest-1", None, None, (_index_record("rev-1"),), "2026-06-22T00:02:00Z")

    deleted = store.delete("manifest-1", policy="hard", updated_at="2026-06-22T00:03:00Z")

    assert deleted is None
    assert store.current_for_asset("asset-1") is None
    assert store.list_by_status("ready") == []
    with pytest.raises(IngestionError, match="was not found"):
        store.get("manifest-1")


def test_ingestion_manifest_store_delete_tombstone_retains_manifest_snapshot() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")

    deleted = store.delete("manifest-1", policy="tombstone", updated_at="2026-06-22T00:02:00Z")

    assert deleted is not None
    assert deleted.status == "deleted"
    assert store.get("manifest-1").status == "deleted"


def test_ingestion_manifest_store_rejects_invalid_delete_policy() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")

    with pytest.raises(ValueError, match="policy must be tombstone or hard"):
        store.delete("manifest-1", policy="archive", updated_at="2026-06-22T00:02:00Z")  # type: ignore[arg-type]

    assert store.get("manifest-1").status == "processing"


def test_ingestion_manifest_store_rejects_commit_after_tombstone() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")
    store.tombstone("manifest-1", "2026-06-22T00:02:00Z")

    with pytest.raises(IngestionError, match="cannot transition"):
        store.commit("manifest-1", None, None, (_index_record("rev-1"),), "2026-06-22T00:03:00Z")


def test_ingestion_manifest_store_tombstone_failed_manifest_clears_error() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-1", "rev-1"), "2026-06-22T00:01:00Z")
    failed = store.fail("manifest-1", "parser failed", "2026-06-22T00:02:00Z")

    deleted = store.tombstone(failed.manifest_id, "2026-06-22T00:03:00Z")

    assert deleted.status == "deleted"
    assert deleted.error is None


def test_ingestion_manifest_status_listing_is_snapshot_ordered() -> None:
    store = InMemoryIngestionManifestStore()
    store.create_processing(_manifest("manifest-b", "rev-b"), "2026-06-22T00:01:00Z")
    store.create_processing(_manifest("manifest-a", "rev-a"), "2026-06-22T00:01:00Z")

    assert [manifest.manifest_id for manifest in store.list_by_status("processing")] == [
        "manifest-a",
        "manifest-b",
    ]
