from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal


JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    uri: str
    media_type: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    etag: str | None = None
    version: str | None = None
    filename: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceAsset:
    asset_id: str
    source_uri: str
    source_kind: Literal[
        "upload",
        "local",
        "http",
        "s3",
        "gcs",
        "sharepoint",
        "drive",
        "email",
        "record_store",
        "generated",
    ]
    tenant_id: str | None = None
    current_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class AssetRevision:
    revision_id: str
    asset_id: str
    content_hash: str
    observed_at: str
    artifact: ArtifactRef
    modified_at: str | None = None
    source_metadata: JsonObject = field(default_factory=dict)
    acl: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class SourceLocation:
    page: int | None = None
    bbox: JsonObject | None = None
    char_start: int | None = None
    char_end: int | None = None
    section_path: list[str] = field(default_factory=list)
    sheet: str | None = None
    cell_range: str | None = None
    slide: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentElement:
    element_id: str
    kind: str
    order: int
    content: str
    location: SourceLocation
    parent_id: str | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    document_id: str
    asset_id: str
    revision_id: str
    parser: JsonObject
    elements: list[DocumentElement] = field(default_factory=list)
    plain_text: str | None = None
    language: str | None = None
    title: str | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentSpan:
    asset_id: str
    revision_id: str
    document_id: str
    element_id: str | None = None
    chunk_id: str | None = None
    page: int | None = None
    bbox: JsonObject | None = None
    char_start: int | None = None
    char_end: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    slide: int | None = None


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_id: str
    source_kind: str
    revision: str | None = None
    digest: str | None = None
    locator: DocumentSpan | None = None
    observed_at: str | None = None
    relevant_as_of: str | None = None
    trust: Literal[
        "authoritative",
        "verified",
        "application",
        "user_supplied",
        "retrieved_untrusted",
        "generated",
        "unknown",
    ] = "unknown"
    access_policy: JsonObject | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    chunk_id: str
    document_id: str
    asset_id: str
    revision_id: str
    text: str
    element_ids: list[str]
    source_refs: list[SourceRef]
    chunker: JsonObject
    token_count: int | None = None
    metadata: JsonObject = field(default_factory=dict)
    acl: JsonObject | None = None


def sha256_digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def create_local_text_revision(
    source_uri: str,
    text: str,
    observed_at: str,
    filename: str | None = None,
) -> tuple[SourceAsset, AssetRevision]:
    encoded = text.encode("utf-8")
    content_hash = sha256_digest_bytes(encoded)
    asset_id = "asset:" + sha256_digest_bytes(source_uri.encode("utf-8"))
    revision_id = "rev:" + content_hash
    artifact = ArtifactRef(
        artifact_id="artifact:" + content_hash,
        uri=source_uri,
        media_type="text/plain",
        size_bytes=len(encoded),
        checksum=content_hash,
        filename=filename,
    )
    asset = SourceAsset(
        asset_id=asset_id,
        source_uri=source_uri,
        source_kind="local",
        current_revision_id=revision_id,
    )
    revision = AssetRevision(
        revision_id=revision_id,
        asset_id=asset_id,
        content_hash=content_hash,
        observed_at=observed_at,
        artifact=artifact,
    )
    return asset, revision


def parse_plain_text_document(asset: SourceAsset, revision: AssetRevision, text: str) -> ParsedDocument:
    elements: list[DocumentElement] = []
    offset = 0
    order = 0
    for raw_line in text.splitlines(keepends=True):
        line_without_newline = raw_line.rstrip("\r\n")
        line_start = offset
        line_end = line_start + len(line_without_newline)
        offset += len(raw_line)
        if not line_without_newline.strip():
            continue
        element_id = f"{revision.revision_id}:element:{order:06d}"
        elements.append(
            DocumentElement(
                element_id=element_id,
                kind="paragraph",
                order=order,
                content=line_without_newline,
                location=SourceLocation(char_start=line_start, char_end=line_end),
            )
        )
        order += 1
    if text and (not text.endswith(("\n", "\r"))):
        # splitlines(keepends=True) already handled the final unterminated line.
        pass
    return ParsedDocument(
        document_id="doc:" + revision.revision_id,
        asset_id=asset.asset_id,
        revision_id=revision.revision_id,
        parser={"processor_id": "plain-text", "version": "1"},
        elements=elements,
        plain_text=text,
    )


def chunk_document_by_lines(
    document: ParsedDocument,
    revision: AssetRevision,
    max_elements: int = 8,
) -> list[DocumentChunk]:
    if max_elements < 1:
        raise ValueError("max_elements must be at least 1")
    chunks: list[DocumentChunk] = []
    for chunk_index, start in enumerate(range(0, len(document.elements), max_elements)):
        grouped = document.elements[start : start + max_elements]
        text = "\n".join(element.content for element in grouped)
        char_starts = [element.location.char_start for element in grouped if element.location.char_start is not None]
        char_ends = [element.location.char_end for element in grouped if element.location.char_end is not None]
        char_start = min(char_starts) if char_starts else None
        char_end = max(char_ends) if char_ends else None
        chunk_id = f"{document.document_id}:chunk:{chunk_index:06d}"
        locator = DocumentSpan(
            asset_id=document.asset_id,
            revision_id=document.revision_id,
            document_id=document.document_id,
            chunk_id=chunk_id,
            char_start=char_start,
            char_end=char_end,
        )
        source_ref = SourceRef(
            source_id=chunk_id,
            source_kind="document_chunk",
            revision=document.revision_id,
            digest=revision.content_hash,
            locator=locator,
        )
        chunks.append(
            DocumentChunk(
                chunk_id=chunk_id,
                document_id=document.document_id,
                asset_id=document.asset_id,
                revision_id=document.revision_id,
                text=text,
                element_ids=[element.element_id for element in grouped],
                source_refs=[source_ref],
                chunker={"processor_id": "plain-text-lines", "version": "1"},
                token_count=len(text.split()),
                acl=revision.acl,
            )
        )
    return chunks
