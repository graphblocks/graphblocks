from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
from threading import Lock, RLock
from types import MappingProxyType
from typing import Any
from weakref import WeakValueDictionary

from .documents import ArtifactRef


class BlobStoreError(RuntimeError):
    pass


class BlobNotFoundError(BlobStoreError):
    pass


class InvalidBlobKeyError(BlobStoreError):
    pass


def _validate_exact_non_empty_string(owner: str, field_name: str, value: object) -> str:
    label = f"{owner} {field_name}" if owner else field_name
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value.strip():
        raise ValueError(f"{label} must not be empty")
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    return value


def _validate_list_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if value < 1:
        raise ValueError("limit must be at least 1")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _loads_strict_json(value: str) -> object:
    return json.loads(
        value,
        parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        object_pairs_hook=_reject_duplicate_json_keys,
    )


@dataclass(frozen=True, slots=True)
class BlobKey:
    key: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str):
            raise InvalidBlobKeyError("blob key must be a string")
        _validate_blob_key(self)


@dataclass(frozen=True, slots=True)
class ByteRange:
    offset: int
    length: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or (self.length is not None and (isinstance(self.length, bool) or not isinstance(self.length, int)))
        ):
            raise ValueError("byte range offset and length must be integers")
        if self.offset < 0 or (self.length is not None and self.length < 0):
            raise ValueError("byte range offset and length must be non-negative")


@dataclass(frozen=True, slots=True)
class PutOptions:
    media_type: str | None = None
    filename: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("media_type", "filename"):
            value = getattr(self, field_name)
            if value is not None:
                _validate_exact_non_empty_string("put", field_name, value)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("put metadata must be a mapping")
        metadata = dict(self.metadata)
        for name, value in metadata.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, str) or not value.strip():
                raise ValueError("put metadata keys and values must be strings")
            if name != name.strip():
                raise ValueError("put metadata keys must not contain surrounding whitespace")
            if value != value.strip():
                raise ValueError("put metadata values must not contain surrounding whitespace")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))


@dataclass(frozen=True, slots=True)
class BlobMetadata:
    key: BlobKey
    artifact: ArtifactRef
    etag: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, BlobKey):
            raise ValueError("blob metadata key must be a BlobKey")
        if not isinstance(self.artifact, ArtifactRef):
            raise ValueError("blob metadata artifact must be an ArtifactRef")
        if self.etag is not None and not isinstance(self.etag, str):
            raise ValueError("blob metadata etag must be a string")
        if self.etag is not None and not self.etag.strip():
            raise ValueError("blob metadata etag must not be empty")
        if self.etag is not None and self.etag != self.etag.strip():
            raise ValueError("blob metadata etag must not contain surrounding whitespace")


@dataclass(frozen=True, slots=True)
class BlobListItem:
    key: BlobKey
    metadata: BlobMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.key, BlobKey):
            raise ValueError("blob list item key must be a BlobKey")
        if not isinstance(self.metadata, BlobMetadata):
            raise ValueError("blob list item metadata must be BlobMetadata")
        if self.metadata.key != self.key:
            raise ValueError("blob list item metadata key must match key")


@dataclass(frozen=True, slots=True)
class ListPage:
    items: tuple[BlobListItem, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if any(not isinstance(item, BlobListItem) for item in items):
            raise ValueError("list page items must be BlobListItem")
        if self.next_cursor is not None and not isinstance(self.next_cursor, str):
            raise ValueError("list page next_cursor must be a string")
        if self.next_cursor is not None and not self.next_cursor.strip():
            raise ValueError("list page next_cursor must not be empty")
        if self.next_cursor is not None and self.next_cursor != self.next_cursor.strip():
            raise ValueError("list page next_cursor must not contain surrounding whitespace")
        object.__setattr__(self, "items", items)


_GRAPHBLOCKS_CHECKSUM_METADATA = "graphblocks-checksum"
_GRAPHBLOCKS_FILENAME_METADATA = "graphblocks-filename"
_LOCAL_METADATA_DIRECTORY = ".graphblocks-metadata"
_LOCAL_ROOT_LOCKS_GUARD = Lock()
_LOCAL_ROOT_LOCKS: WeakValueDictionary[Path, Any] = WeakValueDictionary()
_RESERVED_METADATA_KEYS = {
    _GRAPHBLOCKS_CHECKSUM_METADATA,
    _GRAPHBLOCKS_FILENAME_METADATA,
}


def _validate_blob_key(key: BlobKey) -> None:
    if not isinstance(key, BlobKey):
        raise InvalidBlobKeyError("blob key must be a BlobKey")
    parts = key.key.split("/")
    parsed = PurePosixPath(key.key)
    if (
        not key.key
        or parsed.is_absolute()
        or "\\" in key.key
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise InvalidBlobKeyError(f"invalid blob key {key.key!r}")


def _validate_blob_prefix(prefix: str) -> None:
    if not isinstance(prefix, str):
        raise InvalidBlobKeyError("blob prefix must be a string")
    if prefix == "":
        return
    if prefix.startswith("/") or "\\" in prefix:
        raise InvalidBlobKeyError(f"invalid blob prefix {prefix!r}")
    normalized = prefix[:-1] if prefix.endswith("/") else prefix
    parts = normalized.split("/")
    if any(part in {".", ".."} for part in parts):
        raise InvalidBlobKeyError(f"invalid blob prefix {prefix!r}")
    if any(part == "" for part in parts):
        raise InvalidBlobKeyError(f"invalid blob prefix {prefix!r}")


def _normalise_etag(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BlobStoreError("s3 ETag must be a string")
    normalized = value.strip('"')
    if not normalized:
        raise BlobStoreError("s3 ETag must not be empty")
    return normalized


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
    _metadata_root: Path = field(init=False, repr=False)
    _root_lock: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        with _LOCAL_ROOT_LOCKS_GUARD:
            root_lock = _LOCAL_ROOT_LOCKS.get(self.root)
            if root_lock is None:
                root_lock = RLock()
                _LOCAL_ROOT_LOCKS[self.root] = root_lock
            self._root_lock = root_lock
        with self._root_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            metadata_root = self.root / _LOCAL_METADATA_DIRECTORY
            metadata_root.mkdir(parents=True, exist_ok=True)
            self._metadata_root = metadata_root.resolve()
            try:
                self._metadata_root.relative_to(self.root)
            except ValueError as error:
                raise BlobStoreError(
                    "local blob metadata root must remain within blob root"
                ) from error

    def _path_for(self, key: BlobKey) -> Path:
        _validate_blob_key(key)
        parsed = PurePosixPath(key.key)
        if parsed.parts[0].casefold() == _LOCAL_METADATA_DIRECTORY.casefold():
            raise InvalidBlobKeyError(
                f"blob key {key.key!r} uses the reserved local metadata namespace"
            )
        path = (self.root / Path(*parsed.parts)).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise InvalidBlobKeyError(f"invalid blob key {key.key!r}") from error
        return path

    def _metadata_path_for(self, key: BlobKey) -> Path:
        _validate_blob_key(key)
        parsed = PurePosixPath(key.key)
        if parsed.parts[0].casefold() == _LOCAL_METADATA_DIRECTORY.casefold():
            raise InvalidBlobKeyError(
                f"blob key {key.key!r} uses the reserved local metadata namespace"
            )
        path = (self._metadata_root / Path(*parsed.parts)).with_suffix(
            Path(parsed.name).suffix + ".json"
        )
        path = path.resolve()
        try:
            path.relative_to(self._metadata_root)
        except ValueError as error:
            raise InvalidBlobKeyError(f"invalid blob key {key.key!r}") from error
        return path

    def _pending_paths_for(self, key: BlobKey) -> tuple[Path, Path]:
        metadata_path = self._metadata_path_for(key)
        return (
            metadata_path.with_name(metadata_path.name + ".body.pending"),
            metadata_path.with_name(metadata_path.name + ".pending"),
        )

    def _metadata_for(self, key: BlobKey, data: bytes | None = None) -> BlobMetadata:
        path = self._path_for(key)
        if not path.exists():
            raise BlobNotFoundError(f"blob {key.key!r} does not exist")
        if data is None:
            data = path.read_bytes()
        metadata_path = self._metadata_path_for(key)
        _, pending_metadata_path = self._pending_paths_for(key)
        metadata: BlobMetadata | None = None
        if pending_metadata_path.exists() and not pending_metadata_path.is_symlink():
            try:
                pending_payload = _loads_strict_json(
                    pending_metadata_path.read_text(encoding="utf-8")
                )
                if not isinstance(pending_payload, Mapping):
                    raise TypeError("pending metadata must be a mapping")
                if set(pending_payload) != {"artifact", "etag"}:
                    raise TypeError("pending metadata has invalid fields")
                pending_artifact_payload = pending_payload["artifact"]
                if not isinstance(pending_artifact_payload, Mapping):
                    raise TypeError("pending artifact must be a mapping")
                pending_artifact = ArtifactRef(**pending_artifact_payload)
                pending_metadata = BlobMetadata(
                    key=key,
                    artifact=pending_artifact,
                    etag=pending_payload.get("etag"),
                )
                checksum = "sha256:" + hashlib.sha256(data).hexdigest()
                if (
                    pending_artifact.checksum == checksum
                    and pending_artifact.size_bytes == len(data)
                    and pending_artifact.artifact_id == f"blob:{key.key}"
                    and pending_artifact.uri == path.as_uri()
                ):
                    pending_metadata_path.replace(metadata_path)
                    metadata = pending_metadata
            except (
                KeyError,
                OSError,
                RecursionError,
                TypeError,
                UnicodeError,
                ValueError,
            ):
                pass
        if metadata is None and metadata_path.exists():
            try:
                payload = _loads_strict_json(
                    metadata_path.read_text(encoding="utf-8")
                )
            except (OSError, RecursionError, UnicodeError, ValueError) as error:
                raise BlobStoreError("local blob metadata must be valid strict JSON") from error
            if not isinstance(payload, Mapping):
                raise BlobStoreError("local blob metadata must be a JSON object")
            try:
                if set(payload) != {"artifact", "etag"}:
                    raise TypeError("metadata has invalid fields")
                artifact_payload = payload["artifact"]
                if not isinstance(artifact_payload, Mapping):
                    raise TypeError("artifact must be a mapping")
                artifact = ArtifactRef(**artifact_payload)
                metadata = BlobMetadata(key=key, artifact=artifact, etag=payload.get("etag"))
            except (KeyError, TypeError, ValueError) as error:
                raise BlobStoreError("local blob metadata must contain a valid artifact record") from error
            checksum = "sha256:" + hashlib.sha256(data).hexdigest()
            if artifact.checksum != checksum:
                raise BlobStoreError(f"blob {key.key!r} does not match recorded checksum")
            if artifact.size_bytes != len(data):
                raise BlobStoreError(f"blob {key.key!r} does not match recorded size")
            if artifact.artifact_id != f"blob:{key.key}" or artifact.uri != path.as_uri():
                raise BlobStoreError(
                    f"blob {key.key!r} metadata identity does not match local blob"
                )
            return metadata
        if metadata is not None:
            return metadata
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
        with self._root_lock:
            path = self._path_for(key)
            metadata_path = self._metadata_path_for(key)
            pending_body_path, pending_metadata_path = self._pending_paths_for(key)
            path.parent.mkdir(parents=True, exist_ok=True)
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
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            pending_body_path.unlink(missing_ok=True)
            pending_metadata_path.unlink(missing_ok=True)
            pending_body_path.write_bytes(body)
            pending_metadata_path.write_text(
                json.dumps(
                    {"artifact": asdict(artifact), "etag": checksum},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            pending_body_path.replace(path)
            pending_metadata_path.replace(metadata_path)
            return artifact

    def get(self, key: BlobKey, byte_range: ByteRange | None = None) -> bytes:
        with self._root_lock:
            path = self._path_for(key)
            if not path.exists():
                raise BlobNotFoundError(f"blob {key.key!r} does not exist")
            data = path.read_bytes()
            self._metadata_for(key, data)
            if byte_range is None:
                return data
            if byte_range.length is None:
                return data[byte_range.offset :]
            return data[byte_range.offset : byte_range.offset + byte_range.length]

    def head(self, key: BlobKey) -> BlobMetadata:
        with self._root_lock:
            return self._metadata_for(key)

    def delete(self, key: BlobKey) -> None:
        with self._root_lock:
            path = self._path_for(key)
            if not path.exists():
                raise BlobNotFoundError(f"blob {key.key!r} does not exist")
            path.unlink()
            metadata_path = self._metadata_path_for(key)
            if metadata_path.exists():
                metadata_path.unlink()
            for pending_path in self._pending_paths_for(key):
                pending_path.unlink(missing_ok=True)

    def list(self, prefix: str = "", cursor: str | None = None, limit: int = 100) -> ListPage:
        limit = _validate_list_limit(limit)
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
        with self._root_lock:
            keys: list[str] = []
            for path in self.root.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.root)
                if (
                    relative.parts
                    and relative.parts[0] == _LOCAL_METADATA_DIRECTORY
                ):
                    continue
                key = relative.as_posix()
                if key.startswith(prefix):
                    keys.append(key)
            keys.sort()
            page_keys = keys[start : start + limit]
            next_cursor = (
                str(start + limit) if start + limit < len(keys) else None
            )
            return ListPage(
                items=[
                    BlobListItem(
                        key=BlobKey(key),
                        metadata=self.head(BlobKey(key)),
                    )
                    for key in page_keys
                ],
                next_cursor=next_cursor,
            )


@dataclass(slots=True)
class S3CompatibleBlobStore:
    bucket: str
    client: object
    uri_scheme: str = "s3"

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string("", "bucket", self.bucket)
        _validate_exact_non_empty_string("", "uri_scheme", self.uri_scheme)

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
        if byte_range is not None and byte_range.length == 0:
            self.head(key)
            return b""
        kwargs: dict[str, object] = {"Bucket": self.bucket, "Key": key.key}
        if byte_range is not None:
            kwargs["Range"] = self._range_header(byte_range)
        response = self._invoke("get_object", **kwargs)
        if not isinstance(response, Mapping) or "Body" not in response:
            raise BlobStoreError("s3 get_object response must include Body")
        data = self._read_body(response["Body"])
        content_length = self._content_length(response.get("ContentLength"))
        if content_length is not None and content_length != len(data):
            raise BlobStoreError(
                f"blob {key.key!r} does not match s3 GetObject ContentLength"
            )
        if byte_range is None:
            metadata = self._response_metadata(response.get("Metadata"))
            recorded_checksum = metadata.get(_GRAPHBLOCKS_CHECKSUM_METADATA)
            actual_checksum = "sha256:" + hashlib.sha256(data).hexdigest()
            if recorded_checksum is not None and recorded_checksum != actual_checksum:
                raise BlobStoreError(f"blob {key.key!r} does not match recorded checksum")
        return data

    def head(self, key: BlobKey) -> BlobMetadata:
        self._validate_key(key)
        response = self._invoke("head_object", Bucket=self.bucket, Key=key.key)
        if not isinstance(response, Mapping):
            raise BlobStoreError("s3 head_object response must be a mapping")
        metadata = self._response_metadata(response.get("Metadata"))
        checksum = metadata.get(_GRAPHBLOCKS_CHECKSUM_METADATA)
        filename = metadata.get(_GRAPHBLOCKS_FILENAME_METADATA)
        etag = _normalise_etag(response.get("ETag")) or checksum
        content_type = response.get("ContentType")
        if content_type is not None:
            if not isinstance(content_type, str) or not content_type.strip():
                raise BlobStoreError("s3 ContentType must be a non-empty string")
            if content_type != content_type.strip():
                raise BlobStoreError(
                    "s3 ContentType must not contain surrounding whitespace"
                )
        artifact = ArtifactRef(
            artifact_id=self._artifact_id(key),
            uri=self._uri(key),
            media_type=content_type,
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
        limit = _validate_list_limit(limit)
        _validate_blob_prefix(prefix)
        kwargs: dict[str, object] = {
            "Bucket": self.bucket,
            "Prefix": prefix,
            "MaxKeys": limit,
        }
        if cursor is not None:
            kwargs["ContinuationToken"] = _validate_exact_non_empty_string(
                "list",
                "cursor",
                cursor,
            )
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
            if not item_key.key.startswith(prefix):
                raise BlobStoreError(
                    "s3 list_objects_v2 returned a key outside the requested prefix"
                )
            items.append(BlobListItem(key=item_key, metadata=self.head(item_key)))
        next_cursor = response.get("NextContinuationToken")
        if next_cursor is not None:
            next_cursor = _validate_exact_non_empty_string(
                "s3 list response",
                "next cursor",
                next_cursor,
            )
        is_truncated = response.get("IsTruncated")
        if is_truncated is not None and not isinstance(is_truncated, bool):
            raise BlobStoreError("s3 list response IsTruncated must be a boolean")
        if is_truncated is True and next_cursor is None:
            raise BlobStoreError(
                "truncated s3 list response must include a next cursor"
            )
        if is_truncated is False and next_cursor is not None:
            raise BlobStoreError(
                "non-truncated s3 list response must not include a next cursor"
            )
        return ListPage(
            items=items,
            next_cursor=next_cursor,
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
            if normalized_name in normalized:
                raise ValueError("metadata keys collide after S3 normalization")
            normalized[normalized_name] = str(value)
        return normalized

    def _response_metadata(self, metadata: object) -> dict[str, str]:
        if metadata is None:
            return {}
        if not isinstance(metadata, Mapping):
            raise BlobStoreError("s3 response Metadata must be a mapping")
        normalized: dict[str, str] = {}
        for name, value in metadata.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise BlobStoreError("s3 response Metadata keys and values must be strings")
            normalized_name = name.lower()
            if normalized_name in normalized:
                raise BlobStoreError(
                    "s3 response Metadata keys collide after normalization"
                )
            normalized[normalized_name] = value
        return normalized

    def _content_length(self, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise BlobStoreError("s3 ContentLength must be an integer")
        if value < 0:
            raise BlobStoreError("s3 ContentLength must be non-negative")
        return value

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
