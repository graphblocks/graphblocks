from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from .documents import ArtifactRef, AssetRevision, SourceAsset


IngestionDeletePolicy = Literal["tombstone", "hard"]
IngestionStatus = Literal["discovered", "processing", "ready", "failed", "superseded", "deleted"]
JsonObject = dict[str, object]
VALID_INGESTION_STATUSES = frozenset(
    {"discovered", "processing", "ready", "failed", "superseded", "deleted"}
)


class IngestionError(RuntimeError):
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


def _copy_metadata(owner: str, value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} metadata must be a mapping")
    metadata = dict(value)
    for key in metadata:
        if not isinstance(key, str):
            raise ValueError(f"{owner} metadata keys must be strings")
        if not key.strip():
            raise ValueError(f"{owner} metadata keys must not be empty")
        if key != key.strip():
            raise ValueError(f"{owner} metadata keys must not contain surrounding whitespace")
    return metadata


def _validate_processor_ref(owner: str, value: object) -> ProcessorRef:
    if not isinstance(value, ProcessorRef):
        raise ValueError(f"ingestion manifest {owner} must be a ProcessorRef")
    return value


def _copy_artifact_ref(artifact: ArtifactRef | None) -> ArtifactRef | None:
    if artifact is None:
        return None
    return ArtifactRef(
        artifact_id=artifact.artifact_id,
        uri=artifact.uri,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        checksum=artifact.checksum,
        etag=artifact.etag,
        version=artifact.version,
        filename=artifact.filename,
        metadata=dict(artifact.metadata),
    )


@dataclass(frozen=True, slots=True)
class ProcessorRef:
    processor_id: str
    version: str
    config_digest: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string("processor ref", "processor_id", self.processor_id)
        _validate_exact_non_empty_string("processor ref", "version", self.version)
        _validate_optional_non_empty_string("processor ref", "config_digest", self.config_digest)
        object.__setattr__(self, "metadata", _copy_metadata("processor ref", self.metadata))


def _copy_processor_ref(processor: ProcessorRef | None) -> ProcessorRef | None:
    if processor is None:
        return None
    return ProcessorRef(
        processor_id=processor.processor_id,
        version=processor.version,
        config_digest=processor.config_digest,
        metadata=dict(processor.metadata),
    )


@dataclass(frozen=True, slots=True)
class IndexRecordRef:
    index_id: str
    record_id: str
    asset_id: str
    revision_id: str
    chunk_ids: tuple[str, ...] = field(default_factory=tuple)
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("index_id", "record_id", "asset_id", "revision_id"):
            _validate_exact_non_empty_string("index record ref", field_name, getattr(self, field_name))
        if isinstance(self.chunk_ids, str):
            raise ValueError("index record ref chunk_ids must be a collection of strings")
        try:
            chunk_ids = tuple(self.chunk_ids)
        except TypeError as error:
            raise ValueError("index record ref chunk_ids must be a collection of strings") from error
        for chunk_id in chunk_ids:
            _validate_exact_non_empty_string("index record ref", "chunk_id", chunk_id)
        object.__setattr__(self, "chunk_ids", chunk_ids)
        object.__setattr__(self, "metadata", _copy_metadata("index record ref", self.metadata))


def _copy_index_record_ref(record: IndexRecordRef) -> IndexRecordRef:
    return IndexRecordRef(
        index_id=record.index_id,
        record_id=record.record_id,
        asset_id=record.asset_id,
        revision_id=record.revision_id,
        chunk_ids=tuple(record.chunk_ids),
        metadata=dict(record.metadata),
    )


@dataclass(frozen=True, slots=True)
class IngestionManifest:
    manifest_id: str
    asset_id: str
    revision_id: str
    source_uri: str
    content_hash: str
    parser: ProcessorRef
    chunker: ProcessorRef
    pipeline_hash: str
    status: IngestionStatus
    created_at: str
    updated_at: str
    ocr: ProcessorRef | None = None
    normalizers: tuple[ProcessorRef, ...] = field(default_factory=tuple)
    embedding: ProcessorRef | None = None
    parsed_document_ref: ArtifactRef | None = None
    chunk_set_ref: ArtifactRef | None = None
    index_records: tuple[IndexRecordRef, ...] = field(default_factory=tuple)
    acl_revision: str | None = None
    error: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "manifest_id",
            "asset_id",
            "revision_id",
            "source_uri",
            "content_hash",
            "pipeline_hash",
            "created_at",
            "updated_at",
        ):
            _validate_exact_non_empty_string("ingestion manifest", field_name, getattr(self, field_name))
        if self.status not in VALID_INGESTION_STATUSES:
            raise ValueError(f"invalid ingestion status {self.status!r}")
        _validate_optional_non_empty_string("ingestion manifest", "acl_revision", self.acl_revision)
        _validate_optional_non_empty_string("ingestion manifest", "error", self.error)
        if self.status == "failed" and self.error is None:
            raise ValueError("failed ingestion manifest requires error")
        if self.status != "failed" and self.error is not None:
            raise ValueError("non-failed ingestion manifest must not include error")
        _validate_processor_ref("parser", self.parser)
        _validate_processor_ref("chunker", self.chunker)
        if self.ocr is not None:
            _validate_processor_ref("ocr", self.ocr)
        if self.embedding is not None:
            _validate_processor_ref("embedding", self.embedding)
        if isinstance(self.normalizers, str):
            raise ValueError("ingestion manifest normalizers must be ProcessorRef records")
        try:
            normalizers = tuple(self.normalizers)
        except TypeError as error:
            raise ValueError("ingestion manifest normalizers must be ProcessorRef records") from error
        for normalizer in normalizers:
            _validate_processor_ref("normalizer", normalizer)
        if self.parsed_document_ref is not None and not isinstance(self.parsed_document_ref, ArtifactRef):
            raise ValueError("ingestion manifest parsed_document_ref must be an ArtifactRef")
        if self.chunk_set_ref is not None and not isinstance(self.chunk_set_ref, ArtifactRef):
            raise ValueError("ingestion manifest chunk_set_ref must be an ArtifactRef")
        if isinstance(self.index_records, str):
            raise ValueError("ingestion manifest index_records must be IndexRecordRef records")
        try:
            index_records = tuple(self.index_records)
        except TypeError as error:
            raise ValueError("ingestion manifest index_records must be IndexRecordRef records") from error
        for record in index_records:
            if not isinstance(record, IndexRecordRef):
                raise ValueError("ingestion manifest index_records must be IndexRecordRef records")
            if record.asset_id != self.asset_id:
                raise ValueError("ingestion manifest index record asset_id must match manifest asset_id")
            if record.revision_id != self.revision_id:
                raise ValueError("ingestion manifest index record revision_id must match manifest revision_id")
        object.__setattr__(self, "parser", _copy_processor_ref(self.parser))
        object.__setattr__(self, "chunker", _copy_processor_ref(self.chunker))
        object.__setattr__(self, "ocr", _copy_processor_ref(self.ocr))
        object.__setattr__(
            self,
            "normalizers",
            tuple(_copy_processor_ref(processor) for processor in normalizers),
        )
        object.__setattr__(self, "embedding", _copy_processor_ref(self.embedding))
        object.__setattr__(self, "parsed_document_ref", _copy_artifact_ref(self.parsed_document_ref))
        object.__setattr__(self, "chunk_set_ref", _copy_artifact_ref(self.chunk_set_ref))
        object.__setattr__(
            self,
            "index_records",
            tuple(_copy_index_record_ref(record) for record in index_records),
        )
        object.__setattr__(self, "metadata", _copy_metadata("ingestion manifest", self.metadata))

    @classmethod
    def new(
        cls,
        manifest_id: str,
        asset: SourceAsset,
        revision: AssetRevision,
        parser: ProcessorRef,
        chunker: ProcessorRef,
        pipeline_hash: str,
        created_at: str,
    ) -> IngestionManifest:
        return cls(
            manifest_id=manifest_id,
            asset_id=asset.asset_id,
            revision_id=revision.revision_id,
            source_uri=asset.source_uri,
            content_hash=revision.content_hash,
            parser=parser,
            chunker=chunker,
            pipeline_hash=pipeline_hash,
            status="discovered",
            created_at=created_at,
            updated_at=created_at,
        )

    def with_ocr(self, ocr: ProcessorRef) -> IngestionManifest:
        return replace(self, ocr=ocr)

    def with_embedding(self, embedding: ProcessorRef) -> IngestionManifest:
        return replace(self, embedding=embedding)

    def with_normalizers(self, normalizers: tuple[ProcessorRef, ...]) -> IngestionManifest:
        return replace(self, normalizers=tuple(normalizers))

    def with_acl_revision(self, acl_revision: str) -> IngestionManifest:
        return replace(self, acl_revision=acl_revision)


def _copy_ingestion_manifest(manifest: IngestionManifest) -> IngestionManifest:
    return IngestionManifest(
        manifest_id=manifest.manifest_id,
        asset_id=manifest.asset_id,
        revision_id=manifest.revision_id,
        source_uri=manifest.source_uri,
        content_hash=manifest.content_hash,
        parser=manifest.parser,
        chunker=manifest.chunker,
        pipeline_hash=manifest.pipeline_hash,
        status=manifest.status,
        created_at=manifest.created_at,
        updated_at=manifest.updated_at,
        ocr=manifest.ocr,
        normalizers=tuple(manifest.normalizers),
        embedding=manifest.embedding,
        parsed_document_ref=manifest.parsed_document_ref,
        chunk_set_ref=manifest.chunk_set_ref,
        index_records=tuple(manifest.index_records),
        acl_revision=manifest.acl_revision,
        error=manifest.error,
        metadata=dict(manifest.metadata),
    )


@dataclass(slots=True)
class InMemoryIngestionManifestStore:
    _manifests: dict[str, IngestionManifest] = field(default_factory=dict)
    _current_by_asset: dict[str, str] = field(default_factory=dict)

    def create_processing(self, manifest: IngestionManifest, updated_at: str) -> IngestionManifest:
        if manifest.manifest_id in self._manifests:
            raise IngestionError(f"ingestion manifest {manifest.manifest_id!r} already exists")
        processing = _copy_ingestion_manifest(replace(manifest, status="processing", updated_at=updated_at))
        self._manifests[processing.manifest_id] = processing
        return _copy_ingestion_manifest(processing)

    def commit(
        self,
        manifest_id: str,
        parsed_document_ref: ArtifactRef | None,
        chunk_set_ref: ArtifactRef | None,
        index_records: tuple[IndexRecordRef, ...],
        updated_at: str,
    ) -> IngestionManifest:
        manifest = self._require_manifest(manifest_id)
        if manifest.status == "ready":
            return _copy_ingestion_manifest(manifest)
        if manifest.status not in {"discovered", "processing"}:
            raise IngestionError(
                f"ingestion manifest {manifest_id!r} cannot transition from {manifest.status!r} to 'ready'"
            )
        if (
            parsed_document_ref is not None
            or chunk_set_ref is not None
            or index_records
        ) and (manifest.acl_revision is None or not manifest.acl_revision.strip()):
            raise IngestionError(
                f"ingestion manifest {manifest_id!r} cannot publish outputs without acl_revision"
            )
        for record in index_records:
            if record.asset_id != manifest.asset_id:
                raise IngestionError(
                    f"index record {record.record_id!r} asset_id {record.asset_id!r} "
                    f"does not match ingestion manifest asset_id {manifest.asset_id!r}"
                )
            if record.revision_id != manifest.revision_id:
                raise IngestionError(
                    f"index record {record.record_id!r} revision_id {record.revision_id!r} "
                    f"does not match ingestion manifest revision_id {manifest.revision_id!r}"
                )
        ready = replace(
            manifest,
            parsed_document_ref=parsed_document_ref,
            chunk_set_ref=chunk_set_ref,
            index_records=tuple(_copy_index_record_ref(record) for record in index_records),
            status="ready",
            error=None,
            updated_at=updated_at,
        )
        ready = _copy_ingestion_manifest(ready)
        self._manifests[manifest_id] = ready
        previous_current = self._current_by_asset.get(ready.asset_id)
        self._current_by_asset[ready.asset_id] = manifest_id
        if previous_current is not None and previous_current != manifest_id:
            previous = self._manifests.get(previous_current)
            if previous is not None and previous.status == "ready":
                self._manifests[previous_current] = replace(
                    previous,
                    status="superseded",
                    updated_at=updated_at,
                )
        return _copy_ingestion_manifest(ready)

    def fail(self, manifest_id: str, error: str, updated_at: str) -> IngestionManifest:
        manifest = self._require_manifest(manifest_id)
        if manifest.status in {"ready", "superseded", "deleted"}:
            raise IngestionError(
                f"ingestion manifest {manifest_id!r} cannot transition from {manifest.status!r} to 'failed'"
            )
        failed = _copy_ingestion_manifest(replace(manifest, status="failed", error=error, updated_at=updated_at))
        self._manifests[manifest_id] = failed
        return _copy_ingestion_manifest(failed)

    def tombstone(self, manifest_id: str, updated_at: str) -> IngestionManifest:
        deleted = self.delete(manifest_id, policy="tombstone", updated_at=updated_at)
        assert deleted is not None
        return deleted

    def delete(
        self,
        manifest_id: str,
        *,
        policy: IngestionDeletePolicy = "tombstone",
        updated_at: str,
    ) -> IngestionManifest | None:
        if policy not in {"tombstone", "hard"}:
            raise ValueError("policy must be tombstone or hard")
        manifest = self._require_manifest(manifest_id)
        if policy == "hard":
            self._manifests.pop(manifest_id, None)
            if self._current_by_asset.get(manifest.asset_id) == manifest_id:
                self._current_by_asset.pop(manifest.asset_id, None)
            return None
        if manifest.status == "deleted":
            return _copy_ingestion_manifest(manifest)
        deleted = _copy_ingestion_manifest(replace(manifest, status="deleted", error=None, updated_at=updated_at))
        self._manifests[manifest_id] = deleted
        if self._current_by_asset.get(deleted.asset_id) == manifest_id:
            self._current_by_asset.pop(deleted.asset_id, None)
        return _copy_ingestion_manifest(deleted)

    def get(self, manifest_id: str) -> IngestionManifest:
        return _copy_ingestion_manifest(self._require_manifest(manifest_id))

    def current_for_asset(self, asset_id: str) -> IngestionManifest | None:
        manifest_id = self._current_by_asset.get(asset_id)
        if manifest_id is None:
            return None
        manifest = self._manifests.get(manifest_id)
        return None if manifest is None else _copy_ingestion_manifest(manifest)

    def list_by_status(self, status: IngestionStatus) -> list[IngestionManifest]:
        return [
            _copy_ingestion_manifest(self._manifests[manifest_id])
            for manifest_id in sorted(self._manifests)
            if self._manifests[manifest_id].status == status
        ]

    def _require_manifest(self, manifest_id: str) -> IngestionManifest:
        try:
            return self._manifests[manifest_id]
        except KeyError as error:
            raise IngestionError(f"ingestion manifest {manifest_id!r} was not found") from error
