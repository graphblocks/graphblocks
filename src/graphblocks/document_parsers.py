from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .documents import AssetRevision, ParsedDocument, SourceAsset, parse_plain_text_document
from .documents import ArtifactRef


ParserCallable = Callable[[SourceAsset, AssetRevision, bytes], ParsedDocument]


class DocumentParserError(RuntimeError):
    pass


class DocumentParserNotFoundError(DocumentParserError):
    pass


@dataclass(frozen=True, slots=True)
class ParserDescriptor:
    processor_id: str
    version: str
    media_types: tuple[str, ...] = field(default_factory=tuple)
    extensions: tuple[str, ...] = field(default_factory=tuple)
    priority: int = 0
    supports_ocr: bool = False
    parse: ParserCallable | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParserSelectionLock:
    processor_id: str
    processor_version: str
    reason: str
    media_type: str | None = None
    filename: str | None = None
    artifact_checksum: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class DocumentParserRegistry:
    _descriptors: dict[tuple[str, str], ParserDescriptor] = field(default_factory=dict)

    def register(self, descriptor: ParserDescriptor) -> None:
        key = (descriptor.processor_id, descriptor.version)
        media_types = tuple(media_type.lower() for media_type in descriptor.media_types)
        extensions = tuple(
            extension.lower() if extension.startswith(".") else f".{extension.lower()}"
            for extension in descriptor.extensions
        )
        self._descriptors[key] = ParserDescriptor(
            processor_id=descriptor.processor_id,
            version=descriptor.version,
            media_types=media_types,
            extensions=extensions,
            priority=descriptor.priority,
            supports_ocr=descriptor.supports_ocr,
            parse=descriptor.parse,
            metadata=dict(descriptor.metadata),
        )

    def select(self, artifact: ArtifactRef, *, allow_ocr_fallback: bool = False) -> ParserSelectionLock:
        media_type = artifact.media_type.lower() if artifact.media_type else None
        filename = artifact.filename or PurePosixPath(artifact.uri).name or None
        extension = PurePosixPath(filename).suffix.lower() if filename else None
        candidates: list[tuple[str, ParserDescriptor]] = []
        for descriptor in self._descriptors.values():
            if media_type is not None and media_type in descriptor.media_types:
                candidates.append(("media_type", descriptor))
            elif extension is not None and extension in descriptor.extensions:
                candidates.append(("extension", descriptor))
        if not candidates and allow_ocr_fallback:
            candidates = [
                ("ocr_fallback", descriptor)
                for descriptor in self._descriptors.values()
                if descriptor.supports_ocr
            ]
        if not candidates:
            raise DocumentParserNotFoundError(f"no document parser for artifact {artifact.artifact_id!r}")
        candidates.sort(key=lambda item: (-item[1].priority, item[1].processor_id, item[1].version))
        reason, descriptor = candidates[0]
        return ParserSelectionLock(
            processor_id=descriptor.processor_id,
            processor_version=descriptor.version,
            reason=reason,
            media_type=media_type,
            filename=filename,
            artifact_checksum=artifact.checksum,
            metadata=dict(descriptor.metadata),
        )

    def resolve_locked(self, lock: ParserSelectionLock) -> ParserDescriptor:
        descriptor = self._descriptors.get((lock.processor_id, lock.processor_version))
        if descriptor is None:
            raise DocumentParserNotFoundError(
                f"locked parser {lock.processor_id!r}@{lock.processor_version!r} is not registered"
            )
        return descriptor

    def parse_locked(
        self,
        asset: SourceAsset,
        revision: AssetRevision,
        body: bytes,
        lock: ParserSelectionLock,
    ) -> ParsedDocument:
        descriptor = self.resolve_locked(lock)
        if descriptor.parse is None:
            raise DocumentParserNotFoundError(
                f"locked parser {lock.processor_id!r}@{lock.processor_version!r} has no local implementation"
            )
        return descriptor.parse(asset, revision, body)


def plain_text_parser_descriptor() -> ParserDescriptor:
    def parse(asset: SourceAsset, revision: AssetRevision, body: bytes) -> ParsedDocument:
        return parse_plain_text_document(asset, revision, body.decode("utf-8"))

    return ParserDescriptor(
        processor_id="plain-text",
        version="1",
        media_types=("text/plain",),
        extensions=(".txt", ".text"),
        priority=0,
        parse=parse,
    )
