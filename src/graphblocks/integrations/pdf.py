from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from io import BytesIO
from numbers import Real

from graphblocks.document_parsers import DocumentParserError, ParserDescriptor
from graphblocks.documents import (
    AssetRevision,
    DocumentElement,
    ParsedDocument,
    SourceAsset,
    SourceLocation,
)


class PdfParserError(DocumentParserError):
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
MarkerPdfConverter = Callable[[BytesIO], object]
MarkerHtmlTextExtractor = Callable[[str], str]


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


def marker_pdf_parser_descriptor(
    *,
    converter: MarkerPdfConverter | None = None,
    html_text_extractor: MarkerHtmlTextExtractor | None = None,
    processor_id: str = "marker-pdf",
    version: str = "1",
    priority: int = 100,
) -> ParserDescriptor:
    marker_converter = converter
    marker_html_text_extractor = html_text_extractor

    def parse(asset: SourceAsset, revision: AssetRevision, body: bytes) -> ParsedDocument:
        nonlocal marker_converter, marker_html_text_extractor

        if marker_converter is None:
            try:
                from marker.config.parser import ConfigParser  # type: ignore[import-not-found]
                from marker.converters.pdf import PdfConverter  # type: ignore[import-not-found]
                from marker.models import create_model_dict  # type: ignore[import-not-found]
            except ImportError as error:
                raise PdfParserError(
                    "marker-pdf is required for marker_pdf_parser_descriptor"
                ) from error

            config_parser = ConfigParser(
                {
                    "disable_image_extraction": True,
                    "output_format": "chunks",
                    "use_llm": False,
                }
            )
            marker_converter = PdfConverter(
                artifact_dict=create_model_dict(),
                config=config_parser.generate_config_dict(),
                processor_list=config_parser.get_processors(),
                renderer=config_parser.get_renderer(),
                llm_service=config_parser.get_llm_service(),
            )

        if marker_html_text_extractor is None:
            try:
                from bs4 import BeautifulSoup  # type: ignore[import-not-found]
            except ImportError as error:
                raise PdfParserError(
                    "marker-pdf is required for Marker HTML text extraction"
                ) from error
            marker_html_text_extractor = lambda value: BeautifulSoup(
                value,
                "html.parser",
            ).get_text("\n", strip=True)

        try:
            rendered = marker_converter(BytesIO(body))
        except PdfParserError:
            raise
        except Exception as error:
            raise PdfParserError("Marker PDF conversion failed") from error

        raw_blocks = getattr(rendered, "blocks", None)
        if isinstance(raw_blocks, (str, bytes)) or raw_blocks is None:
            raise PdfParserError("Marker chunks output must contain blocks")
        try:
            marker_blocks = tuple(raw_blocks)
        except TypeError as error:
            raise PdfParserError("Marker chunks output blocks must be iterable") from error

        elements: list[DocumentElement] = []
        plain_text_parts: list[str] = []
        for order, block in enumerate(marker_blocks):
            block_id = getattr(block, "id", None)
            block_type = getattr(block, "block_type", None)
            block_html = getattr(block, "html", None)
            page = getattr(block, "page", None)
            bbox = getattr(block, "bbox", None)
            if not isinstance(block_id, str) or not block_id.strip():
                raise PdfParserError("Marker block id must be a non-empty string")
            if not isinstance(block_type, str) or not block_type.strip():
                raise PdfParserError("Marker block type must be a non-empty string")
            if not isinstance(block_html, str):
                raise PdfParserError("Marker block html must be a string")
            if isinstance(page, bool) or not isinstance(page, int) or page < 0:
                raise PdfParserError("Marker block page must be a non-negative integer")
            if (
                not isinstance(bbox, (list, tuple))
                or len(bbox) != 4
                or any(isinstance(value, bool) or not isinstance(value, Real) for value in bbox)
            ):
                raise PdfParserError("Marker block bbox must contain four numbers")

            try:
                content = marker_html_text_extractor(block_html)
            except Exception as error:
                raise PdfParserError("Marker block HTML extraction failed") from error
            if not isinstance(content, str):
                raise PdfParserError("Marker HTML text extractor must return a string")

            section_hierarchy = getattr(block, "section_hierarchy", None)
            if section_hierarchy is None:
                section_path: list[str] = []
            elif isinstance(section_hierarchy, Mapping):
                section_path = [
                    str(value)
                    for _, value in sorted(
                        section_hierarchy.items(),
                        key=lambda item: str(item[0]),
                    )
                ]
            else:
                raise PdfParserError("Marker block section hierarchy must be a mapping")

            elements.append(
                DocumentElement(
                    element_id=f"{revision.revision_id}:marker:{order:06d}",
                    kind=block_type.lower(),
                    order=order,
                    content=content,
                    location=SourceLocation(
                        page=page + 1,
                        bbox={
                            "left": bbox[0],
                            "top": bbox[1],
                            "right": bbox[2],
                            "bottom": bbox[3],
                        },
                        section_path=section_path,
                    ),
                    metadata={"marker_block_id": block_id},
                )
            )
            if content:
                plain_text_parts.append(content)

        rendered_metadata = getattr(rendered, "metadata", {})
        if not isinstance(rendered_metadata, Mapping):
            raise PdfParserError("Marker chunks output metadata must be a mapping")

        return ParsedDocument(
            document_id="doc:" + revision.revision_id,
            asset_id=asset.asset_id,
            revision_id=revision.revision_id,
            parser={
                "processor_id": processor_id,
                "version": version,
                "media_type": "application/pdf",
                "output_format": "chunks",
            },
            elements=elements,
            plain_text="\n\n".join(plain_text_parts),
            metadata={"marker": deepcopy(dict(rendered_metadata))},
        )

    return ParserDescriptor(
        processor_id=processor_id,
        version=version,
        media_types=("application/pdf",),
        extensions=(".pdf",),
        priority=priority,
        supports_ocr=True,
        parse=parse,
        metadata={
            "output_format": "chunks",
            "package": "marker-pdf",
            "parser": "marker",
        },
    )


__all__ = [
    "PdfPageText",
    "PdfParserError",
    "PdfTextExtractor",
    "MarkerHtmlTextExtractor",
    "MarkerPdfConverter",
    "marker_pdf_parser_descriptor",
    "parse_pdf_pages",
    "pdf_parser_descriptor",
    "pypdf_text_extractor",
]
