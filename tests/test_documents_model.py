from __future__ import annotations

import pytest

from graphblocks.documents import (
    ArtifactRef,
    AssetRevision,
    DocumentChunk,
    DocumentElement,
    DocumentSpan,
    ParsedDocument,
    SourceAsset,
    SourceLocation,
    SourceRef,
    create_local_text_revision,
)


def test_artifact_ref_rejects_empty_identity_fields() -> None:
    with pytest.raises(ValueError, match="artifact artifact_id must not be empty"):
        ArtifactRef(" ", "file:///tmp/example.txt")
    with pytest.raises(ValueError, match="artifact uri must not be empty"):
        ArtifactRef("artifact-1", "")


def test_create_local_text_revision_preserves_content_hash_and_artifact_metadata() -> None:
    asset, revision = create_local_text_revision(
        source_uri="file:///tmp/example.txt",
        text="alpha\nbeta\n",
        observed_at="2026-06-22T00:00:00Z",
        filename="example.txt",
    )

    assert asset == SourceAsset(
        asset_id="asset:sha256:9ef537bd0aeb4a23ae2ed37907c3ee610f289bbd95002833ce04448407ffe33f",
        source_uri="file:///tmp/example.txt",
        source_kind="local",
        current_revision_id="rev:sha256:e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee",
    )
    assert revision.asset_id == asset.asset_id
    assert revision.content_hash == "sha256:e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee"
    assert revision.artifact == ArtifactRef(
        artifact_id="artifact:sha256:e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee",
        uri="file:///tmp/example.txt",
        media_type="text/plain",
        size_bytes=11,
        checksum="sha256:e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee",
        filename="example.txt",
    )


def test_document_chunk_source_ref_contains_full_lineage_ids() -> None:
    asset = SourceAsset("asset-1", "file:///tmp/example.txt", "local", current_revision_id="rev-1")
    revision = AssetRevision(
        revision_id="rev-1",
        asset_id=asset.asset_id,
        content_hash="sha256:content",
        observed_at="2026-06-22T00:00:00Z",
        artifact=ArtifactRef("artifact-1", "file:///tmp/example.txt"),
    )
    document = ParsedDocument(
        document_id="doc-1",
        asset_id=asset.asset_id,
        revision_id=revision.revision_id,
        parser={"processor_id": "plain-text", "version": "1"},
        elements=[
            DocumentElement(
                element_id="el-1",
                kind="paragraph",
                order=0,
                content="hello world",
                location=SourceLocation(char_start=0, char_end=11),
            )
        ],
    )
    chunk = DocumentChunk(
        chunk_id="chunk-1",
        document_id=document.document_id,
        asset_id=document.asset_id,
        revision_id=document.revision_id,
        text="hello world",
        element_ids=["el-1"],
        source_refs=[
            SourceRef(
                source_id="chunk-1",
                source_kind="document_chunk",
                revision=revision.revision_id,
                digest=revision.content_hash,
                locator=DocumentSpan(
                    asset_id=asset.asset_id,
                    revision_id=revision.revision_id,
                    document_id=document.document_id,
                    element_id="el-1",
                    chunk_id="chunk-1",
                    char_start=0,
                    char_end=11,
                ),
            )
        ],
        chunker={"processor_id": "plain-text-lines", "version": "1"},
    )

    assert chunk.source_refs[0].locator.asset_id == asset.asset_id
    assert chunk.source_refs[0].locator.revision_id == revision.revision_id
    assert chunk.source_refs[0].locator.document_id == document.document_id
    assert chunk.source_refs[0].locator.element_id == "el-1"
    assert chunk.source_refs[0].digest == revision.content_hash
