from __future__ import annotations

import importlib

import pytest

from graphblocks.document_parsers import DocumentParserRegistry
from graphblocks.documents import ArtifactRef, AssetRevision, SourceAsset


def test_pdf_parser_descriptor_uses_injected_extractor_and_preserves_pages(monkeypatch) -> None:
    graphblocks_pdf = importlib.import_module("graphblocks.integrations.pdf")

    def extractor(body: bytes) -> list[object]:
        assert body == b"%PDF-test"
        return [
            graphblocks_pdf.PdfPageText(page_number=1, text="Alpha", metadata={"label": "1"}),
            graphblocks_pdf.PdfPageText(page_number=2, text="Beta"),
        ]

    descriptor = graphblocks_pdf.pdf_parser_descriptor(
        extractor=extractor,
        processor_id="pdf-test",
        version="1",
    )
    registry = DocumentParserRegistry()
    registry.register(descriptor)
    artifact = ArtifactRef(
        "artifact-1",
        "file:///tmp/source.pdf",
        media_type="application/pdf",
        filename="source.pdf",
    )
    asset = SourceAsset("asset-1", "file:///tmp/source.pdf", "local", current_revision_id="rev-1")
    revision = AssetRevision(
        revision_id="rev-1",
        asset_id="asset-1",
        content_hash="sha256:pdf",
        observed_at="2026-06-25T00:00:00Z",
        artifact=artifact,
    )

    lock = registry.select(artifact)
    document = registry.parse_locked(asset, revision, b"%PDF-test", lock)

    assert lock.processor_id == "pdf-test"
    assert lock.reason == "media_type"
    assert document.document_id == "doc:rev-1"
    assert document.parser == {
        "processor_id": "pdf-test",
        "version": "1",
        "media_type": "application/pdf",
    }
    assert document.plain_text == "Alpha\n\nBeta"
    assert [element.element_id for element in document.elements] == [
        "rev-1:pdf-page:000001",
        "rev-1:pdf-page:000002",
    ]
    assert [element.location.page for element in document.elements] == [1, 2]
    assert document.elements[0].metadata == {"label": "1"}


def test_pdf_parser_descriptor_selects_by_extension_when_media_type_is_missing(monkeypatch) -> None:
    graphblocks_pdf = importlib.import_module("graphblocks.integrations.pdf")
    registry = DocumentParserRegistry()
    registry.register(graphblocks_pdf.pdf_parser_descriptor(extractor=lambda body: []))

    lock = registry.select(ArtifactRef("artifact-1", "file:///tmp/source.pdf", filename="source.pdf"))

    assert lock.processor_id == "pdf-text"
    assert lock.reason == "extension"


def test_pdf_page_text_rejects_invalid_page_number(monkeypatch) -> None:
    graphblocks_pdf = importlib.import_module("graphblocks.integrations.pdf")

    with pytest.raises(graphblocks_pdf.PdfParserError, match="page_number"):
        graphblocks_pdf.PdfPageText(page_number=0, text="bad")
