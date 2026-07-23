from __future__ import annotations

import importlib
from io import BytesIO
from types import SimpleNamespace

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

    with pytest.raises(graphblocks_pdf.PdfParserError, match="metadata"):
        graphblocks_pdf.PdfPageText(page_number=1, text="bad", metadata=object())
    with pytest.raises(graphblocks_pdf.PdfParserError, match="strict JSON"):
        graphblocks_pdf.PdfPageText(
            page_number=1, text="bad", metadata={"confidence": float("nan")}
        )


def test_marker_pdf_parser_descriptor_preserves_block_lineage(monkeypatch) -> None:
    graphblocks_pdf = importlib.import_module("graphblocks.integrations.pdf")
    converter_calls: list[bytes] = []

    def converter(source: BytesIO) -> object:
        converter_calls.append(source.getvalue())
        return SimpleNamespace(
            blocks=[
                SimpleNamespace(
                    id="/page/0/SectionHeader/0",
                    block_type="SectionHeader",
                    html="<h1>Policy</h1>",
                    page=0,
                    bbox=[10.0, 20.0, 300.0, 60.0],
                    section_hierarchy=None,
                ),
                SimpleNamespace(
                    id="/page/1/Text/0",
                    block_type="Text",
                    html="<p>Approval is required.</p>",
                    page=1,
                    bbox=[12.0, 30.0, 310.0, 90.0],
                    section_hierarchy={1: "/page/0/SectionHeader/0"},
                ),
            ],
            metadata={"page_count": 2},
        )

    descriptor = graphblocks_pdf.marker_pdf_parser_descriptor(
        converter=converter,
        html_text_extractor=lambda value: value.split(">", 1)[1].rsplit("<", 1)[0],
    )
    registry = DocumentParserRegistry()
    registry.register(descriptor)
    artifact = ArtifactRef(
        "artifact-marker",
        "file:///tmp/policy.pdf",
        media_type="application/pdf",
        filename="policy.pdf",
    )
    asset = SourceAsset(
        "asset-marker",
        "file:///tmp/policy.pdf",
        "local",
        current_revision_id="rev-marker",
    )
    revision = AssetRevision(
        revision_id="rev-marker",
        asset_id="asset-marker",
        content_hash="sha256:marker",
        observed_at="2026-07-13T00:00:00Z",
        artifact=artifact,
    )

    lock = registry.select(artifact)
    document = registry.parse_locked(asset, revision, b"%PDF-marker", lock)

    assert converter_calls == [b"%PDF-marker"]
    assert lock.processor_id == "marker-pdf"
    assert lock.metadata == {
        "output_format": "chunks",
        "package": "marker-pdf",
        "parser": "marker",
    }
    assert descriptor.supports_ocr is True
    assert document.parser == {
        "processor_id": "marker-pdf",
        "version": "1",
        "media_type": "application/pdf",
        "output_format": "chunks",
    }
    assert document.plain_text == "Policy\n\nApproval is required."
    assert [element.kind for element in document.elements] == ["sectionheader", "text"]
    assert [element.location.page for element in document.elements] == [1, 2]
    assert document.elements[1].location.bbox == {
        "left": 12.0,
        "top": 30.0,
        "right": 310.0,
        "bottom": 90.0,
    }
    assert document.elements[1].location.section_path == (
        "/page/0/SectionHeader/0",
    )
    assert document.elements[1].metadata == {"marker_block_id": "/page/1/Text/0"}
    assert document.metadata == {"marker": {"page_count": 2}}


def test_marker_pdf_parser_failure_uses_pdf_text_fallback(monkeypatch) -> None:
    graphblocks_pdf = importlib.import_module("graphblocks.integrations.pdf")
    attempts: list[str] = []

    def marker_converter(source: BytesIO) -> object:
        attempts.append("marker-pdf")
        raise RuntimeError("quality gate failed")

    def fallback_extractor(body: bytes) -> list[object]:
        attempts.append("pdf-text")
        return [graphblocks_pdf.PdfPageText(page_number=1, text="Fallback text")]

    registry = DocumentParserRegistry()
    registry.register(
        graphblocks_pdf.marker_pdf_parser_descriptor(
            converter=marker_converter,
            html_text_extractor=lambda value: value,
        )
    )
    registry.register(graphblocks_pdf.pdf_parser_descriptor(extractor=fallback_extractor))
    artifact = ArtifactRef(
        "artifact-fallback",
        "file:///tmp/fallback.pdf",
        media_type="application/pdf",
        filename="fallback.pdf",
    )
    asset = SourceAsset(
        "asset-fallback",
        "file:///tmp/fallback.pdf",
        "local",
        current_revision_id="rev-fallback",
    )
    revision = AssetRevision(
        revision_id="rev-fallback",
        asset_id="asset-fallback",
        content_hash="sha256:fallback",
        observed_at="2026-07-13T00:00:00Z",
        artifact=artifact,
    )

    result = registry.parse_with_candidates(
        asset,
        revision,
        b"%PDF-fallback",
        (("marker-pdf", "1"), ("pdf-text", "1")),
    )

    assert attempts == ["marker-pdf", "pdf-text"]
    assert result.selected_lock.processor_id == "pdf-text"
    assert [lock.processor_id for lock in result.failed_locks] == ["marker-pdf"]
    assert result.document.plain_text == "Fallback text"


@pytest.mark.parametrize(
    "block_overrides,error_match",
    (
        ({"bbox": [0.0, 0.0, float("nan"), 1.0]}, "bbox"),
        ({"section_hierarchy": {"level": "/page/0/Text/0"}}, "section hierarchy"),
        ({"section_hierarchy": {1: object()}}, "section hierarchy"),
    ),
)
def test_marker_pdf_parser_rejects_malformed_external_block_values(
    monkeypatch,
    block_overrides: dict[str, object],
    error_match: str,
) -> None:
    graphblocks_pdf = importlib.import_module("graphblocks.integrations.pdf")
    block = {
        "id": "/page/0/Text/0",
        "block_type": "Text",
        "html": "<p>text</p>",
        "page": 0,
        "bbox": [0.0, 0.0, 1.0, 1.0],
        "section_hierarchy": None,
        **block_overrides,
    }
    descriptor = graphblocks_pdf.marker_pdf_parser_descriptor(
        converter=lambda source: SimpleNamespace(
            blocks=[SimpleNamespace(**block)], metadata={}
        ),
        html_text_extractor=lambda value: value,
    )
    artifact = ArtifactRef(
        "artifact-marker",
        "file:///tmp/source.pdf",
        media_type="application/pdf",
        filename="source.pdf",
    )
    asset = SourceAsset("asset-marker", "file:///tmp/source.pdf", "local")
    revision = AssetRevision(
        "rev-marker",
        "asset-marker",
        "sha256:marker",
        "2026-07-23T00:00:00Z",
        artifact,
    )

    with pytest.raises(graphblocks_pdf.PdfParserError, match=error_match):
        descriptor.parse(asset, revision, b"%PDF-test")
