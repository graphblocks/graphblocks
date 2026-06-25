from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .documents import ArtifactRef


class BlobStoreError(RuntimeError):
    pass


class BlobNotFoundError(BlobStoreError):
    pass


class InvalidBlobKeyError(BlobStoreError):
    pass


@dataclass(frozen=True, slots=True)
class BlobKey:
    key: str


@dataclass(frozen=True, slots=True)
class ByteRange:
    offset: int
    length: int | None = None

    def __post_init__(self) -> None:
        if self.offset < 0 or (self.length is not None and self.length < 0):
            raise ValueError("byte range offset and length must be non-negative")


@dataclass(frozen=True, slots=True)
class PutOptions:
    media_type: str | None = None
    filename: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class BlobMetadata:
    key: BlobKey
    artifact: ArtifactRef
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class BlobListItem:
    key: BlobKey
    metadata: BlobMetadata


@dataclass(frozen=True, slots=True)
class ListPage:
    items: tuple[BlobListItem, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(slots=True)
class LocalBlobStore:
    root: str | Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".graphblocks-metadata").mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: BlobKey) -> Path:
        parsed = PurePosixPath(key.key)
        if not key.key or parsed.is_absolute() or "\\" in key.key or any(part in {"", ".", ".."} for part in parsed.parts):
            raise InvalidBlobKeyError(f"invalid blob key {key.key!r}")
        path = (self.root / Path(*parsed.parts)).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise InvalidBlobKeyError(f"invalid blob key {key.key!r}") from error
        return path

    def _metadata_path_for(self, key: BlobKey) -> Path:
        parsed = PurePosixPath(key.key)
        path = (self.root / ".graphblocks-metadata" / Path(*parsed.parts)).with_suffix(
            Path(parsed.name).suffix + ".json"
        )
        return path.resolve()

    def _metadata_for(self, key: BlobKey) -> BlobMetadata:
        path = self._path_for(key)
        if not path.exists():
            raise BlobNotFoundError(f"blob {key.key!r} does not exist")
        metadata_path = self._metadata_path_for(key)
        if metadata_path.exists():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            artifact = ArtifactRef(**payload["artifact"])
            return BlobMetadata(key=key, artifact=artifact, etag=payload.get("etag"))
        data = path.read_bytes()
        checksum = "sha256:" + hashlib.sha256(data).hexdigest()
        artifact = ArtifactRef(
            artifact_id=f"blob:{key.key}",
            uri=path.as_uri(),
            size_bytes=len(data),
            checksum=checksum,
            etag=checksum,
            filename=path.name,
        )
        return BlobMetadata(key=key, artifact=artifact, etag=checksum)

    def put(self, key: BlobKey, body: bytes, options: PutOptions) -> ArtifactRef:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        checksum = "sha256:" + hashlib.sha256(body).hexdigest()
        artifact = ArtifactRef(
            artifact_id=f"blob:{key.key}",
            uri=path.as_uri(),
            media_type=options.media_type,
            size_bytes=len(body),
            checksum=checksum,
            etag=checksum,
            version=checksum,
            filename=options.filename,
            metadata=dict(options.metadata),
        )
        metadata_path = self._metadata_path_for(key)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps({"artifact": asdict(artifact), "etag": checksum}, sort_keys=True),
            encoding="utf-8",
        )
        return artifact

    def get(self, key: BlobKey, byte_range: ByteRange | None = None) -> bytes:
        path = self._path_for(key)
        if not path.exists():
            raise BlobNotFoundError(f"blob {key.key!r} does not exist")
        data = path.read_bytes()
        if byte_range is None:
            return data
        if byte_range.length is None:
            return data[byte_range.offset :]
        return data[byte_range.offset : byte_range.offset + byte_range.length]

    def head(self, key: BlobKey) -> BlobMetadata:
        return self._metadata_for(key)

    def delete(self, key: BlobKey) -> None:
        path = self._path_for(key)
        if not path.exists():
            raise BlobNotFoundError(f"blob {key.key!r} does not exist")
        path.unlink()
        metadata_path = self._metadata_path_for(key)
        if metadata_path.exists():
            metadata_path.unlink()

    def list(self, prefix: str = "", cursor: str | None = None, limit: int = 100) -> ListPage:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        start = int(cursor) if cursor is not None else 0
        if start < 0:
            raise ValueError("cursor must be non-negative")
        keys: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if relative.parts and relative.parts[0] == ".graphblocks-metadata":
                continue
            key = relative.as_posix()
            if key.startswith(prefix):
                keys.append(key)
        keys.sort()
        page_keys = keys[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(keys) else None
        return ListPage(
            items=[BlobListItem(key=BlobKey(key), metadata=self.head(BlobKey(key))) for key in page_keys],
            next_cursor=next_cursor,
        )
