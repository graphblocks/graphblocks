from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from io import BytesIO

from graphblocks.document_parsers import ParserDescriptor
from graphblocks.documents import (
    AssetRevision,
    DocumentElement,
    ParsedDocument,
    SourceAsset,
    SourceLocation,
)


class PdfParserError(RuntimeError):
    """Raised when a PDF parser adapter contract is invalid."""


@dataclass(frozen=True, slots=True)
class PdfPageText:
    page_number: int
    text: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or self.page_number < 1
        ):
            raise PdfParserError("page_number must be positive")
        if not isinstance(self.text, str):
            raise PdfParserError("text must be a string")
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))


PdfTextExtractor = Callable[[bytes], Iterable[PdfPageText]]


def parse_pdf_pages(
    asset: SourceAsset,
    revision: AssetRevision,
    pages: Iterable[PdfPageText],
    *,
    processor_id: str = "pdf-text",
    version: str = "1",
) -> ParsedDocument:
    if not processor_id.strip():
        raise PdfParserError("processor_id must not be empty")
    if not version.strip():
        raise PdfParserError("version must not be empty")

    elements: list[DocumentElement] = []
    plain_text_parts: list[str] = []
    for order, page in enumerate(pages):
        if not isinstance(page, PdfPageText):
            raise PdfParserError("PDF extractor must return PdfPageText entries")
        element_id = f"{revision.revision_id}:pdf-page:{page.page_number:06d}"
        elements.append(
            DocumentElement(
                element_id=element_id,
                kind="page",
                order=order,
                content=page.text,
                location=SourceLocation(page=page.page_number),
                metadata=deepcopy(dict(page.metadata)),
            )
        )
        plain_text_parts.append(page.text)

    return ParsedDocument(
        document_id="doc:" + revision.revision_id,
        asset_id=asset.asset_id,
        revision_id=revision.revision_id,
        parser={
            "processor_id": processor_id,
            "version": version,
            "media_type": "application/pdf",
        },
        elements=elements,
        plain_text="\n\n".join(plain_text_parts),
    )


def pypdf_text_extractor(body: bytes) -> list[PdfPageText]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as error:
        raise PdfParserError("pypdf extra is required for pypdf_text_extractor") from error

    reader = PdfReader(BytesIO(body))
    pages: list[PdfPageText] = []
    for index, page in enumerate(reader.pages, start=1):
        pages.append(PdfPageText(page_number=index, text=page.extract_text() or ""))
    return pages


def pdf_parser_descriptor(
    *,
    extractor: PdfTextExtractor | None = None,
    processor_id: str = "pdf-text",
    version: str = "1",
    priority: int = 0,
) -> ParserDescriptor:
    if extractor is None:
        extractor = pypdf_text_extractor

    def parse(asset: SourceAsset, revision: AssetRevision, body: bytes) -> ParsedDocument:
        return parse_pdf_pages(
            asset,
            revision,
            extractor(body),
            processor_id=processor_id,
            version=version,
        )

    return ParserDescriptor(
        processor_id=processor_id,
        version=version,
        media_types=("application/pdf",),
        extensions=(".pdf",),
        priority=priority,
        supports_ocr=False,
        parse=parse,
        metadata={"parser": "pdf-text"},
    )


__all__ = [
    "PdfPageText",
    "PdfParserError",
    "PdfTextExtractor",
    "parse_pdf_pages",
    "pdf_parser_descriptor",
    "pypdf_text_extractor",
]
