from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import wraps
from threading import RLock
from typing import Literal, ParamSpec, TypeVar, cast

from .canonical import canonical_dumps
from .documents import ArtifactRef, AssetRevision, SourceAsset


IngestionDeletePolicy = Literal["tombstone", "hard"]
IngestionStatus = Literal["discovered", "processing", "ready", "failed", "superseded", "deleted"]
JsonObject = dict[str, object]
VALID_INGESTION_STATUSES = frozenset(
    {"discovered", "processing", "ready", "failed", "superseded", "deleted"}
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class IngestionError(RuntimeError):
    pass


def _with_ingestion_store_lock(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        store = cast("InMemoryIngestionManifestStore", args[0])
        with store._lock:
            return method(*args, **kwargs)

    return locked


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(
            f"{owner} {field_name} must contain only Unicode scalar values"
        ) from None
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
    try:
        metadata = dict(value)
    except (KeyError, RecursionError, RuntimeError, TypeError, ValueError) as error:
        raise ValueError(f"{owner} metadata must be a readable mapping") from error
    for key in metadata:
        if not isinstance(key, str):
            raise ValueError(f"{owner} metadata keys must be strings")
        if not key.strip():
            raise ValueError(f"{owner} metadata keys must not be empty")
        if key != key.strip():
            raise ValueError(f"{owner} metadata keys must not contain surrounding whitespace")
    try:
        canonical_dumps(metadata)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{owner} metadata must contain strict canonical JSON") from error
    try:
        return deepcopy(metadata)
    except (RecursionError, RuntimeError, TypeError, ValueError) as error:
        raise ValueError(f"{owner} metadata must be copyable") from error


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
        except (RecursionError, RuntimeError, TypeError, ValueError) as error:
            raise ValueError("index record ref chunk_ids must be a collection of strings") from error
        for chunk_id in chunk_ids:
            _validate_exact_non_empty_string("index record ref", "chunk_id", chunk_id)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("index record ref chunk_ids must not contain duplicates")
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
        if not isinstance(self.status, str) or self.status not in VALID_INGESTION_STATUSES:
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
        except (RecursionError, RuntimeError, TypeError, ValueError) as error:
            raise ValueError("ingestion manifest normalizers must be ProcessorRef records") from error
        for normalizer in normalizers:
            _validate_processor_ref("normalizer", normalizer)
        normalizer_identities = [
            (
                normalizer.processor_id,
                normalizer.version,
                normalizer.config_digest,
                canonical_dumps(normalizer.metadata),
            )
            for normalizer in normalizers
        ]
        if len(normalizer_identities) != len(set(normalizer_identities)):
            raise ValueError("ingestion manifest normalizers must not contain duplicates")
        if self.parsed_document_ref is not None and not isinstance(self.parsed_document_ref, ArtifactRef):
            raise ValueError("ingestion manifest parsed_document_ref must be an ArtifactRef")
        if self.chunk_set_ref is not None and not isinstance(self.chunk_set_ref, ArtifactRef):
            raise ValueError("ingestion manifest chunk_set_ref must be an ArtifactRef")
        if isinstance(self.index_records, str):
            raise ValueError("ingestion manifest index_records must be IndexRecordRef records")
        try:
            index_records = tuple(self.index_records)
        except (RecursionError, RuntimeError, TypeError, ValueError) as error:
            raise ValueError("ingestion manifest index_records must be IndexRecordRef records") from error
        for record in index_records:
            if not isinstance(record, IndexRecordRef):
                raise ValueError("ingestion manifest index_records must be IndexRecordRef records")
            if record.asset_id != self.asset_id:
                raise ValueError("ingestion manifest index record asset_id must match manifest asset_id")
            if record.revision_id != self.revision_id:
                raise ValueError("ingestion manifest index record revision_id must match manifest revision_id")
        index_record_ids = [(record.index_id, record.record_id) for record in index_records]
        if len(index_record_ids) != len(set(index_record_ids)):
            raise ValueError("ingestion manifest index_records must not contain duplicate identities")
        if (
            self.parsed_document_ref is not None
            or self.chunk_set_ref is not None
            or index_records
        ) and self.acl_revision is None:
            raise ValueError("ingestion manifest published outputs require acl_revision")
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
        if not isinstance(asset, SourceAsset):
            raise ValueError("ingestion manifest asset must be a SourceAsset")
        if not isinstance(revision, AssetRevision):
            raise ValueError("ingestion manifest revision must be an AssetRevision")
        if revision.asset_id != asset.asset_id:
            raise ValueError("ingestion manifest revision asset_id must match source asset asset_id")
        if (
            revision.artifact.checksum is not None
            and revision.artifact.checksum != revision.content_hash
        ):
            raise ValueError(
                "ingestion manifest artifact checksum must match revision content_hash"
            )
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
        try:
            snapshot = tuple(normalizers)
        except (RecursionError, RuntimeError, TypeError, ValueError) as error:
            raise ValueError(
                "ingestion manifest normalizers must be ProcessorRef records"
            ) from error
        return replace(self, normalizers=snapshot)

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
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self._manifests, Mapping):
            raise ValueError("ingestion manifest store manifests must be a mapping")
        manifests: dict[str, IngestionManifest] = {}
        try:
            manifest_items = tuple(self._manifests.items())
        except (KeyError, RecursionError, RuntimeError, TypeError, ValueError) as error:
            raise ValueError(
                "ingestion manifest store manifests must be a readable mapping"
            ) from error
        for manifest_id, manifest in manifest_items:
            _validate_exact_non_empty_string("ingestion manifest store", "manifest_id", manifest_id)
            if not isinstance(manifest, IngestionManifest):
                raise ValueError(
                    "ingestion manifest store manifests must be IngestionManifest records"
                )
            if manifest.manifest_id != manifest_id:
                raise ValueError("ingestion manifest store manifest key must match manifest_id")
            manifests[manifest_id] = _copy_ingestion_manifest(manifest)
        if not isinstance(self._current_by_asset, Mapping):
            raise ValueError("ingestion manifest store current pointers must be a mapping")
        current_by_asset: dict[str, str] = {}
        try:
            current_items = tuple(self._current_by_asset.items())
        except (KeyError, RecursionError, RuntimeError, TypeError, ValueError) as error:
            raise ValueError(
                "ingestion manifest store current pointers must be a readable mapping"
            ) from error
        for asset_id, manifest_id in current_items:
            _validate_exact_non_empty_string("ingestion manifest store", "asset_id", asset_id)
            _validate_exact_non_empty_string("ingestion manifest store", "manifest_id", manifest_id)
            manifest = manifests.get(manifest_id)
            if manifest is None or manifest.asset_id != asset_id or manifest.status != "ready":
                raise ValueError(
                    "ingestion manifest store current pointer must reference a ready manifest for the asset"
                )
            current_by_asset[asset_id] = manifest_id
        ready_by_asset = {
            manifest.asset_id: manifest.manifest_id
            for manifest in manifests.values()
            if manifest.status == "ready"
        }
        ready_count_by_asset: dict[str, int] = {}
        for manifest in manifests.values():
            if manifest.status == "ready":
                ready_count_by_asset[manifest.asset_id] = (
                    ready_count_by_asset.get(manifest.asset_id, 0) + 1
                )
        if any(count != 1 for count in ready_count_by_asset.values()):
            raise ValueError(
                "ingestion manifest store must contain at most one ready manifest per asset"
            )
        if current_by_asset != ready_by_asset:
            raise ValueError(
                "ingestion manifest store current pointers must cover every ready manifest"
            )
        self._manifests = manifests
        self._current_by_asset = current_by_asset

    @_with_ingestion_store_lock
    def create_processing(self, manifest: IngestionManifest, updated_at: str) -> IngestionManifest:
        if not isinstance(manifest, IngestionManifest):
            raise ValueError("ingestion manifest store manifest must be an IngestionManifest")
        _validate_exact_non_empty_string(
            "ingestion manifest store", "updated_at", updated_at
        )
        if manifest.manifest_id in self._manifests:
            raise IngestionError(f"ingestion manifest {manifest.manifest_id!r} already exists")
        if manifest.status != "discovered":
            raise IngestionError(
                f"ingestion manifest {manifest.manifest_id!r} cannot be created from {manifest.status!r}"
            )
        processing = _copy_ingestion_manifest(replace(manifest, status="processing", updated_at=updated_at))
        self._manifests[processing.manifest_id] = processing
        return _copy_ingestion_manifest(processing)

    @_with_ingestion_store_lock
    def commit(
        self,
        manifest_id: str,
        parsed_document_ref: ArtifactRef | None,
        chunk_set_ref: ArtifactRef | None,
        index_records: tuple[IndexRecordRef, ...],
        updated_at: str,
    ) -> IngestionManifest:
        _validate_exact_non_empty_string(
            "ingestion manifest store", "updated_at", updated_at
        )
        try:
            index_records = tuple(index_records)
        except (RecursionError, RuntimeError, TypeError, ValueError) as error:
            raise ValueError("ingestion manifest index_records must be readable") from error
        if any(not isinstance(record, IndexRecordRef) for record in index_records):
            raise ValueError("ingestion manifest index_records must be IndexRecordRef records")
        manifest = self._require_manifest(manifest_id)
        if manifest.status == "ready":
            if (
                manifest.parsed_document_ref != parsed_document_ref
                or manifest.chunk_set_ref != chunk_set_ref
                or manifest.index_records != tuple(index_records)
            ):
                raise IngestionError(
                    f"ingestion manifest {manifest_id!r} commit replay does not match stored outputs"
                )
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

    @_with_ingestion_store_lock
    def fail(self, manifest_id: str, error: str, updated_at: str) -> IngestionManifest:
        _validate_exact_non_empty_string("ingestion manifest store", "error", error)
        _validate_exact_non_empty_string(
            "ingestion manifest store", "updated_at", updated_at
        )
        manifest = self._require_manifest(manifest_id)
        if manifest.status == "failed":
            if manifest.error != error:
                raise IngestionError(
                    f"ingestion manifest {manifest_id!r} failure replay does not match stored error"
                )
            return _copy_ingestion_manifest(manifest)
        if manifest.status in {"ready", "superseded", "deleted"}:
            raise IngestionError(
                f"ingestion manifest {manifest_id!r} cannot transition from {manifest.status!r} to 'failed'"
            )
        failed = _copy_ingestion_manifest(replace(manifest, status="failed", error=error, updated_at=updated_at))
        self._manifests[manifest_id] = failed
        return _copy_ingestion_manifest(failed)

    @_with_ingestion_store_lock
    def tombstone(self, manifest_id: str, updated_at: str) -> IngestionManifest:
        deleted = self.delete(manifest_id, policy="tombstone", updated_at=updated_at)
        assert deleted is not None
        return deleted

    @_with_ingestion_store_lock
    def delete(
        self,
        manifest_id: str,
        *,
        policy: IngestionDeletePolicy = "tombstone",
        updated_at: str,
    ) -> IngestionManifest | None:
        if not isinstance(policy, str) or policy not in {"tombstone", "hard"}:
            raise ValueError("policy must be tombstone or hard")
        _validate_exact_non_empty_string(
            "ingestion manifest store", "updated_at", updated_at
        )
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

    @_with_ingestion_store_lock
    def get(self, manifest_id: str) -> IngestionManifest:
        return _copy_ingestion_manifest(self._require_manifest(manifest_id))

    @_with_ingestion_store_lock
    def current_for_asset(self, asset_id: str) -> IngestionManifest | None:
        _validate_exact_non_empty_string("ingestion manifest store", "asset_id", asset_id)
        manifest_id = self._current_by_asset.get(asset_id)
        if manifest_id is None:
            return None
        manifest = self._manifests.get(manifest_id)
        return None if manifest is None else _copy_ingestion_manifest(manifest)

    @_with_ingestion_store_lock
    def list_by_status(self, status: IngestionStatus) -> list[IngestionManifest]:
        if not isinstance(status, str) or status not in VALID_INGESTION_STATUSES:
            raise ValueError(f"invalid ingestion status {status!r}")
        return [
            _copy_ingestion_manifest(self._manifests[manifest_id])
            for manifest_id in sorted(self._manifests)
            if self._manifests[manifest_id].status == status
        ]

    def _require_manifest(self, manifest_id: str) -> IngestionManifest:
        _validate_exact_non_empty_string("ingestion manifest store", "manifest_id", manifest_id)
        try:
            return self._manifests[manifest_id]
        except KeyError as error:
            raise IngestionError(f"ingestion manifest {manifest_id!r} was not found") from error
