from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_documents_package_exposes_local_text_lineage_helpers(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-documents" / "src"))
    graphblocks_documents = importlib.import_module("graphblocks_documents")

    asset, revision = graphblocks_documents.create_local_text_revision(
        "file:///notes/support.txt",
        "First line\n\nSecond line\n",
        "2026-06-23T00:00:00Z",
        filename="support.txt",
    )
    document = graphblocks_documents.parse_plain_text_document(
        asset,
        revision,
        "First line\n\nSecond line\n",
    )
    chunks = graphblocks_documents.chunk_document_by_lines(document, revision, max_elements=1)

    assert asset.source_kind == "local"
    assert revision.artifact.media_type == "text/plain"
    assert revision.artifact.filename == "support.txt"
    assert [element.content for element in document.elements] == ["First line", "Second line"]
    assert [chunk.text for chunk in chunks] == ["First line", "Second line"]
    assert chunks[0].source_refs[0].locator is not None
    assert chunks[0].source_refs[0].locator.asset_id == asset.asset_id


def test_documents_package_exposes_ingestion_manifest_facade(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-documents" / "src"))
    graphblocks_documents = importlib.import_module("graphblocks_documents")

    for name in (
        "IndexRecordRef",
        "IngestionManifest",
        "IngestionStatus",
        "InMemoryIngestionManifestStore",
        "ProcessorRef",
    ):
        assert name in graphblocks_documents.__all__
        assert hasattr(graphblocks_documents, name)


def test_documents_package_exposes_parser_spi_and_ocr_fallback(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-documents" / "src"))
    graphblocks_documents = importlib.import_module("graphblocks_documents")

    registry = graphblocks_documents.DocumentParserRegistry()
    registry.register(graphblocks_documents.plain_text_parser_descriptor())
    registry.register(graphblocks_documents.ParserDescriptor("ocr-z", "1", supports_ocr=True, priority=10))
    registry.register(graphblocks_documents.ParserDescriptor("ocr-a", "2", supports_ocr=True, priority=10))
    text_lock = registry.select(
        graphblocks_documents.ArtifactRef(
            "artifact-text",
            "file:///notes/support.txt",
            media_type="text/plain",
            filename="support.txt",
            checksum="sha256:text",
        )
    )
    ocr_lock = registry.select(
        graphblocks_documents.ArtifactRef(
            "artifact-scan",
            "file:///notes/scan.bin",
            media_type="application/octet-stream",
            filename="scan.bin",
            checksum="sha256:scan",
        ),
        allow_ocr_fallback=True,
    )

    assert text_lock.processor_id == "plain-text"
    assert text_lock.reason == "media_type"
    assert ocr_lock.processor_id == "ocr-a"
    assert ocr_lock.reason == "ocr_fallback"
    for name in (
        "DocumentParserError",
        "DocumentParserNotFoundError",
        "DocumentParserRegistry",
        "ParserDescriptor",
        "ParserSelectionLock",
        "plain_text_parser_descriptor",
    ):
        assert name in graphblocks_documents.__all__
        assert hasattr(graphblocks_documents, name)


def test_documents_package_exposes_blob_store_adapters(monkeypatch, tmp_path) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-documents" / "src"))
    graphblocks_documents = importlib.import_module("graphblocks_documents")

    store = graphblocks_documents.LocalBlobStore(tmp_path)
    artifact = store.put(
        graphblocks_documents.BlobKey("docs/support.txt"),
        b"alpha beta",
        graphblocks_documents.PutOptions(media_type="text/plain", filename="support.txt"),
    )
    metadata = store.head(graphblocks_documents.BlobKey("docs/support.txt"))
    page = store.list("docs/", limit=1)

    assert artifact.artifact_id == "blob:docs/support.txt"
    assert metadata.artifact == artifact
    assert store.get(graphblocks_documents.BlobKey("docs/support.txt"), graphblocks_documents.ByteRange(6)) == b"beta"
    assert [item.key.key for item in page.items] == ["docs/support.txt"]
    for name in (
        "BlobKey",
        "BlobListItem",
        "BlobMetadata",
        "BlobNotFoundError",
        "BlobStoreError",
        "ByteRange",
        "ListPage",
        "LocalBlobStore",
        "PutOptions",
        "S3CompatibleBlobStore",
    ):
        assert name in graphblocks_documents.__all__
        assert hasattr(graphblocks_documents, name)
