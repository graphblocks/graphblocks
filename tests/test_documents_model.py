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


def test_artifact_ref_rejects_invalid_string_fields() -> None:
    with pytest.raises(ValueError, match="artifact artifact_id must be a string"):
        ArtifactRef(1, "file:///tmp/example.txt")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="artifact uri must be a string"):
        ArtifactRef("artifact-1", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="artifact media_type must be a string"):
        ArtifactRef("artifact-1", "file:///tmp/example.txt", media_type=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="artifact checksum must not be empty"):
        ArtifactRef("artifact-1", "file:///tmp/example.txt", checksum=" ")
    with pytest.raises(ValueError, match="artifact size_bytes must be non-negative"):
        ArtifactRef("artifact-1", "file:///tmp/example.txt", size_bytes=-1)
    with pytest.raises(ValueError, match="artifact metadata values must be strings"):
        ArtifactRef("artifact-1", "file:///tmp/example.txt", metadata={"owner": object()})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    ("constructor", "expected_error"),
    (
        (
            lambda: ArtifactRef(" artifact-1", "file:///tmp/example.txt"),
            "artifact artifact_id must not contain surrounding whitespace",
        ),
        (
            lambda: ArtifactRef("artifact-1", " file:///tmp/example.txt"),
            "artifact uri must not contain surrounding whitespace",
        ),
        (
            lambda: ArtifactRef("artifact-1", "file:///tmp/example.txt", media_type=" text/plain"),
            "artifact media_type must not contain surrounding whitespace",
        ),
        (
            lambda: ArtifactRef("artifact-1", "file:///tmp/example.txt", metadata={" owner": "docs"}),
            "artifact metadata key must not contain surrounding whitespace",
        ),
        (
            lambda: SourceAsset(" asset-1", "file:///tmp/example.txt", "local"),
            "source asset asset_id must not contain surrounding whitespace",
        ),
        (
            lambda: SourceAsset("asset-1", "file:///tmp/example.txt", "local", current_revision_id=" rev-1"),
            "source asset current_revision_id must not contain surrounding whitespace",
        ),
        (
            lambda: AssetRevision(
                "rev-1",
                "asset-1",
                " sha256:content",
                "2026-06-22T00:00:00Z",
                ArtifactRef("artifact-1", "file:///tmp/example.txt"),
            ),
            "asset revision content_hash must not contain surrounding whitespace",
        ),
        (
            lambda: DocumentElement("el-1", " paragraph", 0, "hello", SourceLocation()),
            "document element kind must not contain surrounding whitespace",
        ),
        (
            lambda: ParsedDocument(" doc-1", "asset-1", "rev-1", {}),
            "parsed document document_id must not contain surrounding whitespace",
        ),
        (
            lambda: DocumentSpan("asset-1", "rev-1", "doc-1", element_id=" el-1"),
            "document span element_id must not contain surrounding whitespace",
        ),
        (
            lambda: SourceRef("source-1", "document_chunk", digest=" sha256:content"),
            "source ref digest must not contain surrounding whitespace",
        ),
        (
            lambda: DocumentChunk("chunk-1", "doc-1", "asset-1", "rev-1", "hello", [" el-1"], [], {}),
            "document chunk element_ids item must not contain surrounding whitespace",
        ),
    ),
)
def test_document_lineage_records_reject_whitespace_wrapped_identities(
    constructor: object,
    expected_error: str,
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        constructor()


def test_document_lineage_records_validate_identity_types_and_snapshots() -> None:
    artifact_metadata = {"owner": "docs"}
    artifact = ArtifactRef("artifact-1", "file:///tmp/example.txt", metadata=artifact_metadata)
    artifact_metadata["owner"] = "mutated"

    assert artifact.artifact_id == "artifact-1"
    assert artifact.uri == "file:///tmp/example.txt"
    assert artifact.metadata == {"owner": "docs"}
    with pytest.raises(TypeError):
        artifact.metadata["owner"] = "changed"
    metadata = artifact.metadata
    with pytest.raises(TypeError):
        metadata |= {"owner": "changed"}
    with pytest.raises(ValueError, match="invalid source asset source_kind"):
        SourceAsset("asset-1", "file:///tmp/example.txt", "ftp")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="asset revision artifact must be ArtifactRef"):
        AssetRevision("rev-1", "asset-1", "sha256:content", "2026-06-22T00:00:00Z", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source location char_end must be greater than or equal to char_start"):
        SourceLocation(char_start=10, char_end=2)
    with pytest.raises(ValueError, match="source location page must be positive"):
        SourceLocation(page=0)
    with pytest.raises(ValueError, match="document element order must be non-negative"):
        DocumentElement("el-1", "paragraph", -1, "hello", SourceLocation())


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


def test_document_payload_records_validate_nested_types_and_copy_collections() -> None:
    element = DocumentElement("el-1", "paragraph", 0, "hello", SourceLocation(section_path=["body"]))
    parser = {"processor_id": "plain-text", "version": "1", "config": {"enabled": True}}
    document = ParsedDocument(
        "doc-1",
        "asset-1",
        "rev-1",
        parser,
        elements=[element],  # type: ignore[arg-type]
        metadata={"tags": ["policy"]},
    )
    parser["processor_id"] = "mutated"

    assert element.element_id == "el-1"
    assert element.kind == "paragraph"
    assert element.location.section_path == ("body",)
    assert document.parser["processor_id"] == "plain-text"
    assert document.metadata["tags"] == ["policy"]
    assert document.elements == (element,)
    with pytest.raises(TypeError):
        document.parser["processor_id"] = "changed"
    with pytest.raises(TypeError):
        document.metadata["tags"].append("mutated")
    with pytest.raises(ValueError, match="parsed document elements must be DocumentElement"):
        ParsedDocument("doc-1", "asset-1", "rev-1", {}, elements=(object(),))  # type: ignore[arg-type]

    locator = DocumentSpan("asset-1", "rev-1", "doc-1", char_start=0, char_end=5)
    source_ref = SourceRef("source-1", "document_chunk", locator=locator, metadata={"labels": ["safe"]})
    chunk = DocumentChunk(
        "chunk-1",
        "doc-1",
        "asset-1",
        "rev-1",
        "hello",
        element_ids=["el-1"],  # type: ignore[arg-type]
        source_refs=[source_ref],  # type: ignore[arg-type]
        chunker={"processor_id": "line-chunker", "version": "1"},
        token_count=1,
        acl={"tenant": "tenant-1"},
    )

    assert chunk.element_ids == ("el-1",)
    assert chunk.source_refs == (source_ref,)
    assert chunk.acl == {"tenant": "tenant-1"}
    with pytest.raises(ValueError, match="invalid source ref trust"):
        SourceRef("source-1", "document_chunk", trust="private")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="document span char_end must be greater than or equal to char_start"):
        DocumentSpan("asset-1", "rev-1", "doc-1", char_start=5, char_end=1)
    with pytest.raises(ValueError, match="document chunk source_refs must be SourceRef"):
        DocumentChunk("chunk-1", "doc-1", "asset-1", "rev-1", "hello", ("el-1",), (object(),), {})  # type: ignore[arg-type]
