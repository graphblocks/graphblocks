from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .documents import ArtifactRef, AssetRevision, SourceAsset


IngestionStatus = Literal["discovered", "processing", "ready", "failed", "superseded", "deleted"]
JsonObject = dict[str, object]


class IngestionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessorRef:
    processor_id: str
    version: str
    config_digest: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class IndexRecordRef:
    index_id: str
    record_id: str
    asset_id: str
    revision_id: str
    chunk_ids: tuple[str, ...] = field(default_factory=tuple)
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunk_ids", tuple(self.chunk_ids))
        object.__setattr__(self, "metadata", dict(self.metadata))


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
        object.__setattr__(self, "normalizers", tuple(self.normalizers))
        object.__setattr__(self, "index_records", tuple(self.index_records))
        object.__setattr__(self, "metadata", dict(self.metadata))

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


@dataclass(slots=True)
class InMemoryIngestionManifestStore:
    _manifests: dict[str, IngestionManifest] = field(default_factory=dict)
    _current_by_asset: dict[str, str] = field(default_factory=dict)

    def create_processing(self, manifest: IngestionManifest, updated_at: str) -> IngestionManifest:
        if manifest.manifest_id in self._manifests:
            raise IngestionError(f"ingestion manifest {manifest.manifest_id!r} already exists")
        processing = replace(manifest, status="processing", updated_at=updated_at)
        self._manifests[processing.manifest_id] = processing
        return processing

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
            return manifest
        if manifest.status not in {"discovered", "processing"}:
            raise IngestionError(
                f"ingestion manifest {manifest_id!r} cannot transition from {manifest.status!r} to 'ready'"
            )
        ready = replace(
            manifest,
            parsed_document_ref=parsed_document_ref,
            chunk_set_ref=chunk_set_ref,
            index_records=tuple(index_records),
            status="ready",
            error=None,
            updated_at=updated_at,
        )
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
        return ready

    def fail(self, manifest_id: str, error: str, updated_at: str) -> IngestionManifest:
        manifest = self._require_manifest(manifest_id)
        if manifest.status in {"ready", "superseded", "deleted"}:
            raise IngestionError(
                f"ingestion manifest {manifest_id!r} cannot transition from {manifest.status!r} to 'failed'"
            )
        failed = replace(manifest, status="failed", error=error, updated_at=updated_at)
        self._manifests[manifest_id] = failed
        return failed

    def tombstone(self, manifest_id: str, updated_at: str) -> IngestionManifest:
        manifest = self._require_manifest(manifest_id)
        if manifest.status == "deleted":
            return manifest
        deleted = replace(manifest, status="deleted", updated_at=updated_at)
        self._manifests[manifest_id] = deleted
        if self._current_by_asset.get(deleted.asset_id) == manifest_id:
            self._current_by_asset.pop(deleted.asset_id, None)
        return deleted

    def get(self, manifest_id: str) -> IngestionManifest:
        return self._require_manifest(manifest_id)

    def current_for_asset(self, asset_id: str) -> IngestionManifest | None:
        manifest_id = self._current_by_asset.get(asset_id)
        if manifest_id is None:
            return None
        return self._manifests.get(manifest_id)

    def list_by_status(self, status: IngestionStatus) -> list[IngestionManifest]:
        return [
            self._manifests[manifest_id]
            for manifest_id in sorted(self._manifests)
            if self._manifests[manifest_id].status == status
        ]

    def _require_manifest(self, manifest_id: str) -> IngestionManifest:
        try:
            return self._manifests[manifest_id]
        except KeyError as error:
            raise IngestionError(f"ingestion manifest {manifest_id!r} was not found") from error
