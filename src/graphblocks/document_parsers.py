from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType

from .documents import AssetRevision, ParsedDocument, SourceAsset, parse_plain_text_document
from .documents import ArtifactRef


ParserCallable = Callable[[SourceAsset, AssetRevision, bytes], ParsedDocument]


class DocumentParserError(RuntimeError):
    pass


class DocumentParserNotFoundError(DocumentParserError):
    pass


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    return value


def _validate_exact_non_empty_string(owner: str, field_name: str, value: object) -> str:
    text = _validate_non_empty_string(owner, field_name, value)
    if text != text.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return text


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_exact_non_empty_string(owner, field_name, value)


def _freeze_metadata(owner: str, metadata: object) -> Mapping[str, object]:
    if not isinstance(metadata, Mapping):
        raise ValueError(f"{owner} metadata must be a mapping")
    snapshot: dict[str, object] = {}
    for key, value in metadata.items():
        key_text = _validate_exact_non_empty_string(owner, "metadata key", key)
        snapshot[key_text] = _freeze_metadata_value(owner, value)
    return MappingProxyType(snapshot)


def _freeze_metadata_value(owner: str, value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_metadata(owner, value)
    if isinstance(value, tuple):
        return tuple(_freeze_metadata_value(owner, item) for item in value)
    if isinstance(value, list):
        return tuple(_freeze_metadata_value(owner, item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_metadata_value(owner, item) for item in value)
    return value


def _normalize_media_types(owner: str, media_types: object) -> tuple[str, ...]:
    if isinstance(media_types, str):
        raise ValueError(f"{owner} media_types must be a collection of strings")
    normalized: list[str] = []
    try:
        iterator = iter(media_types)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} media_types must be a collection of strings") from error
    for media_type in iterator:
        normalized_media_type = _validate_non_empty_string(owner, "media_types item", media_type).strip().lower()
        if normalized_media_type not in normalized:
            normalized.append(normalized_media_type)
    return tuple(normalized)


def _normalize_extensions(owner: str, extensions: object) -> tuple[str, ...]:
    if isinstance(extensions, str):
        raise ValueError(f"{owner} extensions must be a collection of strings")
    normalized: list[str] = []
    try:
        iterator = iter(extensions)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} extensions must be a collection of strings") from error
    for extension in iterator:
        normalized_extension = _validate_non_empty_string(owner, "extensions item", extension).strip().lower()
        if not normalized_extension.startswith("."):
            normalized_extension = f".{normalized_extension}"
        if normalized_extension == ".":
            raise ValueError(f"{owner} extensions item must not be empty")
        if normalized_extension not in normalized:
            normalized.append(normalized_extension)
    return tuple(normalized)


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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "processor_id",
            _validate_exact_non_empty_string("parser descriptor", "processor_id", self.processor_id),
        )
        object.__setattr__(
            self,
            "version",
            _validate_exact_non_empty_string("parser descriptor", "version", self.version),
        )
        object.__setattr__(
            self,
            "media_types",
            _normalize_media_types("parser descriptor", self.media_types),
        )
        object.__setattr__(
            self,
            "extensions",
            _normalize_extensions("parser descriptor", self.extensions),
        )
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("parser descriptor priority must be an integer")
        if not isinstance(self.supports_ocr, bool):
            raise ValueError("parser descriptor supports_ocr must be a boolean")
        if self.parse is not None and not callable(self.parse):
            raise ValueError("parser descriptor parse must be callable")
        object.__setattr__(self, "metadata", _freeze_metadata("parser descriptor", self.metadata))


@dataclass(frozen=True, slots=True)
class ParserSelectionLock:
    processor_id: str
    processor_version: str
    reason: str
    media_type: str | None = None
    filename: str | None = None
    artifact_checksum: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "processor_id",
            _validate_exact_non_empty_string("parser selection lock", "processor_id", self.processor_id),
        )
        object.__setattr__(
            self,
            "processor_version",
            _validate_exact_non_empty_string("parser selection lock", "processor_version", self.processor_version),
        )
        object.__setattr__(
            self,
            "reason",
            _validate_exact_non_empty_string("parser selection lock", "reason", self.reason),
        )
        for field_name in ("media_type", "filename", "artifact_checksum"):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_non_empty_string(
                    "parser selection lock",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        object.__setattr__(self, "metadata", _freeze_metadata("parser selection lock", self.metadata))


@dataclass(frozen=True, slots=True)
class ParserCandidateParseResult:
    document: ParsedDocument
    selected_lock: ParserSelectionLock
    failed_locks: tuple[ParserSelectionLock, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.document, ParsedDocument):
            raise ValueError("parser candidate result document must be a ParsedDocument")
        if not isinstance(self.selected_lock, ParserSelectionLock):
            raise ValueError("parser candidate result selected_lock must be a ParserSelectionLock")
        failed_locks = tuple(self.failed_locks)
        if any(not isinstance(lock, ParserSelectionLock) for lock in failed_locks):
            raise ValueError("parser candidate result failed_locks must be ParserSelectionLock records")
        selected_identity = (
            self.selected_lock.processor_id,
            self.selected_lock.processor_version,
        )
        failed_identities = [
            (lock.processor_id, lock.processor_version)
            for lock in failed_locks
        ]
        if selected_identity in failed_identities:
            raise ValueError("parser candidate result selected parser must not also be failed")
        if len(failed_identities) != len(set(failed_identities)):
            raise ValueError("parser candidate result failed_locks must not contain duplicates")
        object.__setattr__(self, "failed_locks", failed_locks)


@dataclass(slots=True)
class DocumentParserRegistry:
    _descriptors: dict[tuple[str, str], ParserDescriptor] = field(default_factory=dict)

    def register(self, descriptor: ParserDescriptor) -> None:
        if not isinstance(descriptor, ParserDescriptor):
            raise DocumentParserError("parser descriptor must be ParserDescriptor")
        key = (descriptor.processor_id, descriptor.version)
        self._descriptors[key] = ParserDescriptor(
            processor_id=descriptor.processor_id,
            version=descriptor.version,
            media_types=descriptor.media_types,
            extensions=descriptor.extensions,
            priority=descriptor.priority,
            supports_ocr=descriptor.supports_ocr,
            parse=descriptor.parse,
            metadata=descriptor.metadata,
        )

    def select(self, artifact: ArtifactRef, *, allow_ocr_fallback: bool = False) -> ParserSelectionLock:
        media_type = artifact.media_type.strip().lower() if artifact.media_type else None
        filename = (
            artifact.filename.strip()
            if artifact.filename
            else PurePosixPath(artifact.uri).name.strip()
        ) or None
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
        if (
            lock.artifact_checksum is not None
            and revision.artifact.checksum != lock.artifact_checksum
        ):
            raise DocumentParserError(
                "locked parser artifact checksum does not match revision artifact checksum"
            )
        descriptor = self.resolve_locked(lock)
        if descriptor.parse is None:
            raise DocumentParserNotFoundError(
                f"locked parser {lock.processor_id!r}@{lock.processor_version!r} has no local implementation"
            )
        return descriptor.parse(asset, revision, body)

    def parse_with_candidates(
        self,
        asset: SourceAsset,
        revision: AssetRevision,
        body: bytes,
        candidates: object,
    ) -> ParserCandidateParseResult:
        if isinstance(candidates, (str, bytes)):
            raise ValueError("parser candidate chain must be a collection")
        try:
            candidate_records = tuple(candidates)  # type: ignore[arg-type]
        except TypeError as error:
            raise ValueError("parser candidate chain must be a collection") from error
        if not candidate_records:
            raise ValueError("parser candidate chain must not be empty")
        normalized_candidates: list[tuple[str, str]] = []
        for candidate in candidate_records:
            if isinstance(candidate, (str, bytes)):
                raise ValueError("parser candidate must contain processor_id and version")
            try:
                processor_id, version = candidate
            except (TypeError, ValueError) as error:
                raise ValueError("parser candidate must contain processor_id and version") from error
            identity = (
                _validate_exact_non_empty_string("parser candidate", "processor_id", processor_id),
                _validate_exact_non_empty_string("parser candidate", "version", version),
            )
            if identity in normalized_candidates:
                raise ValueError("parser candidate chain must not contain duplicates")
            normalized_candidates.append(identity)

        failed_locks: list[ParserSelectionLock] = []
        for index, (processor_id, version) in enumerate(normalized_candidates):
            lock = ParserSelectionLock(
                processor_id=processor_id,
                processor_version=version,
                reason="candidate_primary" if index == 0 else "candidate_fallback",
                media_type=(
                    revision.artifact.media_type.strip().lower()
                    if revision.artifact.media_type is not None
                    else None
                ),
                filename=(
                    revision.artifact.filename
                    or PurePosixPath(revision.artifact.uri).name
                    or None
                ),
                artifact_checksum=revision.artifact.checksum,
                metadata={"candidate_index": index},
            )
            try:
                document = self.parse_locked(asset, revision, body, lock)
            except DocumentParserError:
                failed_locks.append(lock)
                continue
            return ParserCandidateParseResult(
                document=document,
                selected_lock=lock,
                failed_locks=tuple(failed_locks),
            )
        attempted = ", ".join(
            f"{processor_id}@{version}"
            for processor_id, version in normalized_candidates
        )
        raise DocumentParserError(
            f"document parser candidates exhausted after {attempted}"
        )


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
