from __future__ import annotations

from collections.abc import Mapping
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
        if not isinstance(self.metadata, Mapping):
            raise ValueError("put metadata must be a mapping")
        metadata = dict(self.metadata)
        if any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(value, str)
            for name, value in metadata.items()
        ):
            raise ValueError("put metadata keys and values must be strings")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))


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


_GRAPHBLOCKS_CHECKSUM_METADATA = "graphblocks-checksum"
_GRAPHBLOCKS_FILENAME_METADATA = "graphblocks-filename"
_RESERVED_METADATA_KEYS = {
    _GRAPHBLOCKS_CHECKSUM_METADATA,
    _GRAPHBLOCKS_FILENAME_METADATA,
}


def _validate_blob_key(key: BlobKey) -> None:
    parsed = PurePosixPath(key.key)
    if not key.key or parsed.is_absolute() or "\\" in key.key or any(part in {"", ".", ".."} for part in parsed.parts):
        raise InvalidBlobKeyError(f"invalid blob key {key.key!r}")


def _validate_blob_prefix(prefix: str) -> None:
    if prefix == "":
        return
    if prefix.startswith("/") or "\\" in prefix:
        raise InvalidBlobKeyError(f"invalid blob prefix {prefix!r}")
    parts = [part for part in prefix.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise InvalidBlobKeyError(f"invalid blob prefix {prefix!r}")


def _normalise_etag(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip('"')


def _is_not_found_error(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    code: object = None
    if isinstance(response, Mapping):
        raw_error = response.get("Error")
        if isinstance(raw_error, Mapping):
            code = raw_error.get("Code")
    if code is None:
        code = getattr(error, "code", None)
    return str(code) in {"404", "NoSuchKey", "NotFound", "NotFoundException"}


@dataclass(slots=True)
class LocalBlobStore:
    root: str | Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".graphblocks-metadata").mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: BlobKey) -> Path:
        _validate_blob_key(key)
        parsed = PurePosixPath(key.key)
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
        _validate_blob_prefix(prefix)
        if cursor is None:
            start = 0
        else:
            if (
                not isinstance(cursor, str)
                or not cursor
                or not cursor.isascii()
                or not cursor.isdecimal()
                or (cursor != "0" and cursor.startswith("0"))
            ):
                raise ValueError("cursor must be a canonical non-negative integer")
            start = int(cursor)
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


@dataclass(slots=True)
class S3CompatibleBlobStore:
    bucket: str
    client: object
    uri_scheme: str = "s3"

    def __post_init__(self) -> None:
        if not isinstance(self.bucket, str):
            raise ValueError("bucket must be a string")
        if not self.bucket.strip():
            raise ValueError("bucket must not be empty")
        if not isinstance(self.uri_scheme, str):
            raise ValueError("uri_scheme must be a string")
        if not self.uri_scheme.strip():
            raise ValueError("uri_scheme must not be empty")

    def put(self, key: BlobKey, body: bytes, options: PutOptions) -> ArtifactRef:
        self._validate_key(key)
        checksum = "sha256:" + hashlib.sha256(body).hexdigest()
        user_metadata = self._user_metadata(options.metadata)
        metadata = {
            **user_metadata,
            _GRAPHBLOCKS_CHECKSUM_METADATA: checksum,
        }
        if options.filename is not None:
            metadata[_GRAPHBLOCKS_FILENAME_METADATA] = options.filename
        kwargs: dict[str, object] = {
            "Bucket": self.bucket,
            "Key": key.key,
            "Body": body,
            "Metadata": metadata,
        }
        if options.media_type is not None:
            kwargs["ContentType"] = options.media_type
        response = self._invoke("put_object", **kwargs)
        etag = _normalise_etag(response.get("ETag")) if isinstance(response, Mapping) else None
        return ArtifactRef(
            artifact_id=self._artifact_id(key),
            uri=self._uri(key),
            media_type=options.media_type,
            size_bytes=len(body),
            checksum=checksum,
            etag=etag or checksum,
            version=etag or checksum,
            filename=options.filename,
            metadata=user_metadata,
        )

    def get(self, key: BlobKey, byte_range: ByteRange | None = None) -> bytes:
        self._validate_key(key)
        kwargs: dict[str, object] = {"Bucket": self.bucket, "Key": key.key}
        if byte_range is not None:
            kwargs["Range"] = self._range_header(byte_range)
        response = self._invoke("get_object", **kwargs)
        if not isinstance(response, Mapping) or "Body" not in response:
            raise BlobStoreError("s3 get_object response must include Body")
        return self._read_body(response["Body"])

    def head(self, key: BlobKey) -> BlobMetadata:
        self._validate_key(key)
        response = self._invoke("head_object", Bucket=self.bucket, Key=key.key)
        if not isinstance(response, Mapping):
            raise BlobStoreError("s3 head_object response must be a mapping")
        metadata = self._response_metadata(response.get("Metadata"))
        checksum = metadata.get(_GRAPHBLOCKS_CHECKSUM_METADATA)
        filename = metadata.get(_GRAPHBLOCKS_FILENAME_METADATA)
        etag = _normalise_etag(response.get("ETag")) or checksum
        artifact = ArtifactRef(
            artifact_id=self._artifact_id(key),
            uri=self._uri(key),
            media_type=str(response["ContentType"]) if response.get("ContentType") is not None else None,
            size_bytes=self._content_length(response.get("ContentLength")),
            checksum=checksum,
            etag=etag,
            version=etag,
            filename=filename,
            metadata={
                name: value
                for name, value in metadata.items()
                if name not in _RESERVED_METADATA_KEYS
            },
        )
        return BlobMetadata(key=key, artifact=artifact, etag=etag)

    def delete(self, key: BlobKey) -> None:
        self._validate_key(key)
        self.head(key)
        self._invoke("delete_object", Bucket=self.bucket, Key=key.key)

    def list(self, prefix: str = "", cursor: str | None = None, limit: int = 100) -> ListPage:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        _validate_blob_prefix(prefix)
        kwargs: dict[str, object] = {
            "Bucket": self.bucket,
            "Prefix": prefix,
            "MaxKeys": limit,
        }
        if cursor is not None:
            kwargs["ContinuationToken"] = cursor
        response = self._invoke("list_objects_v2", **kwargs)
        if not isinstance(response, Mapping):
            raise BlobStoreError("s3 list_objects_v2 response must be a mapping")
        contents = response.get("Contents", ())
        if contents is None:
            contents = ()
        if not isinstance(contents, list | tuple):
            raise BlobStoreError("s3 list_objects_v2 Contents must be a sequence")
        items: list[BlobListItem] = []
        for index, item in enumerate(contents):
            if not isinstance(item, Mapping) or not isinstance(item.get("Key"), str):
                raise BlobStoreError(f"s3 list_objects_v2 Contents[{index}] must include string Key")
            item_key = BlobKey(item["Key"])
            items.append(BlobListItem(key=item_key, metadata=self.head(item_key)))
        next_cursor = response.get("NextContinuationToken")
        return ListPage(
            items=items,
            next_cursor=str(next_cursor) if next_cursor is not None else None,
        )

    def _validate_key(self, key: BlobKey) -> None:
        _validate_blob_key(key)

    def _artifact_id(self, key: BlobKey) -> str:
        return f"{self.uri_scheme}:{self.bucket}:{key.key}"

    def _uri(self, key: BlobKey) -> str:
        return f"{self.uri_scheme}://{self.bucket}/{key.key}"

    def _invoke(self, method_name: str, **kwargs: object) -> object:
        method = getattr(self.client, method_name)
        try:
            return method(**kwargs)
        except Exception as error:
            if _is_not_found_error(error):
                key = kwargs.get("Key")
                if isinstance(key, str):
                    raise BlobNotFoundError(f"blob {key!r} does not exist") from error
                raise BlobNotFoundError("blob does not exist") from error
            raise

    def _user_metadata(self, metadata: Mapping[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for name, value in metadata.items():
            normalized_name = str(name).lower()
            if normalized_name in _RESERVED_METADATA_KEYS:
                raise ValueError(f"metadata key {name!r} is reserved")
            normalized[normalized_name] = str(value)
        return normalized

    def _response_metadata(self, metadata: object) -> dict[str, str]:
        if metadata is None:
            return {}
        if not isinstance(metadata, Mapping):
            raise BlobStoreError("s3 response Metadata must be a mapping")
        return {str(name).lower(): str(value) for name, value in metadata.items()}

    def _content_length(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise BlobStoreError("s3 ContentLength must be an integer") from error

    def _range_header(self, byte_range: ByteRange) -> str:
        if byte_range.length is None:
            return f"bytes={byte_range.offset}-"
        end = byte_range.offset + byte_range.length - 1
        return f"bytes={byte_range.offset}-{end}"

    def _read_body(self, body: object) -> bytes:
        if isinstance(body, bytes):
            return body
        if isinstance(body, bytearray):
            return bytes(body)
        read = getattr(body, "read", None)
        if callable(read):
            data = read()
            if isinstance(data, bytes):
                return data
            if isinstance(data, bytearray):
                return bytes(data)
        raise BlobStoreError("s3 response Body must be bytes or a readable stream")
