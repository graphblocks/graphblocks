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
