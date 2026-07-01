from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Literal


JsonObject = dict[str, Any]
SOURCE_KINDS = frozenset(("upload", "local", "http", "s3", "gcs", "sharepoint", "drive", "email", "record_store", "generated"))
SOURCE_TRUST_LEVELS = frozenset(
    ("authoritative", "verified", "application", "user_supplied", "retrieved_untrusted", "generated", "unknown")
)


class FrozenDict(dict[str, Any]):
    def __setitem__(self, key: str, value: Any) -> None:
        raise TypeError("frozen mapping does not support item assignment")

    def __delitem__(self, key: str) -> None:
        raise TypeError("frozen mapping does not support item deletion")

    def clear(self) -> None:
        raise TypeError("frozen mapping does not support mutation")

    def pop(self, *args: Any) -> Any:
        raise TypeError("frozen mapping does not support mutation")

    def popitem(self) -> tuple[str, Any]:
        raise TypeError("frozen mapping does not support mutation")

    def setdefault(self, *args: Any) -> Any:
        raise TypeError("frozen mapping does not support mutation")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen mapping does not support mutation")

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        return dict(self)


class FrozenList(list[Any]):
    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError("frozen list does not support item assignment")

    def __delitem__(self, key: Any) -> None:
        raise TypeError("frozen list does not support item deletion")

    def append(self, item: Any) -> None:
        raise TypeError("frozen list does not support mutation")

    def clear(self) -> None:
        raise TypeError("frozen list does not support mutation")

    def extend(self, item: Any) -> None:
        raise TypeError("frozen list does not support mutation")

    def insert(self, index: int, item: Any) -> None:
        raise TypeError("frozen list does not support mutation")

    def pop(self, *args: Any) -> Any:
        raise TypeError("frozen list does not support mutation")

    def remove(self, item: Any) -> None:
        raise TypeError("frozen list does not support mutation")

    def reverse(self) -> None:
        raise TypeError("frozen list does not support mutation")

    def sort(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen list does not support mutation")

    def __iadd__(self, item: Any) -> FrozenList:
        raise TypeError("frozen list does not support mutation")

    def __imul__(self, item: Any) -> FrozenList:
        raise TypeError("frozen list does not support mutation")

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        return list(self)


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _validate_optional_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    return value


def _validate_non_negative_int(owner: str, field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{owner} {field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{owner} {field_name} must be non-negative")
    return value


def _validate_positive_int(owner: str, field_name: str, value: object | None) -> int | None:
    value = _validate_non_negative_int(owner, field_name, value)
    if value is not None and value < 1:
        raise ValueError(f"{owner} {field_name} must be positive")
    return value


def _freeze_mapping(owner: str, field_name: str, value: object | None, *, string_values: bool = False) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} {field_name} must be a mapping")
    snapshot: dict[str, Any] = {}
    for key, item in value.items():
        key_text = _validate_non_empty_string(owner, f"{field_name} key", key)
        if string_values and not isinstance(item, str):
            raise ValueError(f"{owner} {field_name} values must be strings")
        snapshot[key_text] = _freeze_value(owner, item)
    return FrozenDict(snapshot)


def _freeze_value(owner: str, value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(owner, "nested metadata", value)
    if isinstance(value, list):
        return FrozenList(_freeze_value(owner, item) for item in value)
    if isinstance(value, tuple):
        return FrozenList(_freeze_value(owner, item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(owner, item) for item in value)
    return value


def _validate_string_tuple(owner: str, field_name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a collection of strings")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a collection of strings") from error
    for item in items:
        _validate_non_empty_string(owner, f"{field_name} item", item)
    return items


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _validate_non_empty_string("artifact", "artifact_id", self.artifact_id).strip())
        object.__setattr__(self, "uri", _validate_non_empty_string("artifact", "uri", self.uri).strip())
        for field_name in ("media_type", "checksum", "etag", "version", "filename"):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_non_empty_string("artifact", field_name, getattr(self, field_name)),
            )
        object.__setattr__(self, "size_bytes", _validate_non_negative_int("artifact", "size_bytes", self.size_bytes))
        object.__setattr__(self, "metadata", _freeze_mapping("artifact", "metadata", self.metadata, string_values=True))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", _validate_non_empty_string("source asset", "asset_id", self.asset_id).strip())
        object.__setattr__(self, "source_uri", _validate_non_empty_string("source asset", "source_uri", self.source_uri).strip())
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"invalid source asset source_kind {self.source_kind}")
        object.__setattr__(self, "tenant_id", _validate_optional_non_empty_string("source asset", "tenant_id", self.tenant_id))
        object.__setattr__(
            self,
            "current_revision_id",
            _validate_optional_non_empty_string("source asset", "current_revision_id", self.current_revision_id),
        )


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_id", _validate_non_empty_string("asset revision", "revision_id", self.revision_id).strip())
        object.__setattr__(self, "asset_id", _validate_non_empty_string("asset revision", "asset_id", self.asset_id).strip())
        object.__setattr__(self, "content_hash", _validate_non_empty_string("asset revision", "content_hash", self.content_hash).strip())
        object.__setattr__(self, "observed_at", _validate_non_empty_string("asset revision", "observed_at", self.observed_at).strip())
        if not isinstance(self.artifact, ArtifactRef):
            raise ValueError("asset revision artifact must be ArtifactRef")
        object.__setattr__(self, "modified_at", _validate_optional_non_empty_string("asset revision", "modified_at", self.modified_at))
        object.__setattr__(self, "source_metadata", _freeze_mapping("asset revision", "source_metadata", self.source_metadata))
        object.__setattr__(self, "acl", _freeze_mapping("asset revision", "acl", self.acl))


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

    def __post_init__(self) -> None:
        for field_name in ("page", "char_start", "char_end", "slide"):
            object.__setattr__(self, field_name, _validate_positive_int("source location", field_name, getattr(self, field_name)) if field_name in {"page", "slide"} else _validate_non_negative_int("source location", field_name, getattr(self, field_name)))
        if self.char_start is not None and self.char_end is not None and self.char_end < self.char_start:
            raise ValueError("source location char_end must be greater than or equal to char_start")
        object.__setattr__(self, "bbox", _freeze_mapping("source location", "bbox", self.bbox))
        object.__setattr__(self, "section_path", _validate_string_tuple("source location", "section_path", self.section_path))
        object.__setattr__(self, "sheet", _validate_optional_non_empty_string("source location", "sheet", self.sheet))
        object.__setattr__(self, "cell_range", _validate_optional_non_empty_string("source location", "cell_range", self.cell_range))


@dataclass(frozen=True, slots=True)
class DocumentElement:
    element_id: str
    kind: str
    order: int
    content: str
    location: SourceLocation
    parent_id: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "element_id", _validate_non_empty_string("document element", "element_id", self.element_id).strip())
        object.__setattr__(self, "kind", _validate_non_empty_string("document element", "kind", self.kind).strip())
        if not isinstance(self.order, int) or isinstance(self.order, bool):
            raise ValueError("document element order must be an integer")
        if self.order < 0:
            raise ValueError("document element order must be non-negative")
        if not isinstance(self.content, str):
            raise ValueError("document element content must be a string")
        if not isinstance(self.location, SourceLocation):
            raise ValueError("document element location must be SourceLocation")
        object.__setattr__(self, "parent_id", _validate_optional_non_empty_string("document element", "parent_id", self.parent_id))
        object.__setattr__(self, "metadata", _freeze_mapping("document element", "metadata", self.metadata))


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

    def __post_init__(self) -> None:
        for field_name in ("document_id", "asset_id", "revision_id"):
            object.__setattr__(self, field_name, _validate_non_empty_string("parsed document", field_name, getattr(self, field_name)).strip())
        object.__setattr__(self, "parser", _freeze_mapping("parsed document", "parser", self.parser))
        elements = tuple(self.elements)
        if any(not isinstance(element, DocumentElement) for element in elements):
            raise ValueError("parsed document elements must be DocumentElement")
        object.__setattr__(self, "elements", elements)
        object.__setattr__(self, "plain_text", _validate_optional_string("parsed document", "plain_text", self.plain_text))
        for field_name in ("language", "title"):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_non_empty_string("parsed document", field_name, getattr(self, field_name)),
            )
        object.__setattr__(self, "metadata", _freeze_mapping("parsed document", "metadata", self.metadata))


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

    def __post_init__(self) -> None:
        for field_name in ("asset_id", "revision_id", "document_id"):
            object.__setattr__(self, field_name, _validate_non_empty_string("document span", field_name, getattr(self, field_name)).strip())
        for field_name in ("element_id", "chunk_id", "sheet", "cell_range"):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_non_empty_string("document span", field_name, getattr(self, field_name)),
            )
        object.__setattr__(self, "page", _validate_positive_int("document span", "page", self.page))
        object.__setattr__(self, "slide", _validate_positive_int("document span", "slide", self.slide))
        object.__setattr__(self, "char_start", _validate_non_negative_int("document span", "char_start", self.char_start))
        object.__setattr__(self, "char_end", _validate_non_negative_int("document span", "char_end", self.char_end))
        if self.char_start is not None and self.char_end is not None and self.char_end < self.char_start:
            raise ValueError("document span char_end must be greater than or equal to char_start")
        object.__setattr__(self, "bbox", _freeze_mapping("document span", "bbox", self.bbox))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _validate_non_empty_string("source ref", "source_id", self.source_id).strip())
        object.__setattr__(self, "source_kind", _validate_non_empty_string("source ref", "source_kind", self.source_kind).strip())
        for field_name in ("revision", "digest", "observed_at", "relevant_as_of"):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_non_empty_string("source ref", field_name, getattr(self, field_name)),
            )
        if self.locator is not None and not isinstance(self.locator, DocumentSpan):
            raise ValueError("source ref locator must be DocumentSpan")
        if self.trust not in SOURCE_TRUST_LEVELS:
            raise ValueError(f"invalid source ref trust {self.trust}")
        object.__setattr__(self, "access_policy", _freeze_mapping("source ref", "access_policy", self.access_policy))
        object.__setattr__(self, "metadata", _freeze_mapping("source ref", "metadata", self.metadata))


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

    def __post_init__(self) -> None:
        for field_name in ("chunk_id", "document_id", "asset_id", "revision_id"):
            object.__setattr__(self, field_name, _validate_non_empty_string("document chunk", field_name, getattr(self, field_name)).strip())
        if not isinstance(self.text, str):
            raise ValueError("document chunk text must be a string")
        object.__setattr__(self, "element_ids", _validate_string_tuple("document chunk", "element_ids", self.element_ids))
        source_refs = tuple(self.source_refs)
        if any(not isinstance(source_ref, SourceRef) for source_ref in source_refs):
            raise ValueError("document chunk source_refs must be SourceRef")
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "chunker", _freeze_mapping("document chunk", "chunker", self.chunker))
        object.__setattr__(self, "token_count", _validate_non_negative_int("document chunk", "token_count", self.token_count))
        object.__setattr__(self, "metadata", _freeze_mapping("document chunk", "metadata", self.metadata))
        object.__setattr__(self, "acl", _freeze_mapping("document chunk", "acl", self.acl))


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
