from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gc
import json
from threading import Barrier, Lock
from weakref import ref

import pytest

import graphblocks.blob_store as blob_store_module
from graphblocks.blob_store import (
    BlobKey,
    BlobListItem,
    BlobMetadata,
    BlobNotFoundError,
    BlobStoreError,
    ByteRange,
    InvalidBlobKeyError,
    ListPage,
    LocalBlobStore,
    PutOptions,
    S3CompatibleBlobStore,
)
from graphblocks.documents import ArtifactRef


def test_local_blob_store_put_head_and_get_round_trip(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    put_metadata = {"tenant": "acme"}
    options = PutOptions(media_type="text/plain", filename="policy.txt", metadata=put_metadata)
    put_metadata["tenant"] = "mutated"

    with pytest.raises(TypeError):
        options.metadata["tenant"] = "changed"

    artifact = store.put(
        BlobKey("docs/policy.txt"),
        b"alpha policy",
        options,
    )

    metadata = store.head(BlobKey("docs/policy.txt"))
    assert artifact.artifact_id == "blob:docs/policy.txt"
    assert artifact.uri.startswith("file://")
    assert artifact.media_type == "text/plain"
    assert artifact.size_bytes == 12
    assert artifact.checksum == "sha256:c756898a9faceb6ccccb473210b12caacad0e71afbf84855dadf3f9db1902ef2"
    assert artifact.filename == "policy.txt"
    assert artifact.metadata == {"tenant": "acme"}
    assert metadata.artifact == artifact
    assert metadata.etag == artifact.checksum
    assert store.get(BlobKey("docs/policy.txt")) == b"alpha policy"


def test_local_blob_store_rejects_non_standard_metadata_json_constants(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    key = BlobKey("docs/policy.txt")
    artifact = store.put(key, b"alpha policy", PutOptions(media_type="text/plain"))
    metadata_path = store._metadata_path_for(key)
    metadata_path.write_text(
        json.dumps(
            {
                "artifact": {
                    "artifact_id": artifact.artifact_id,
                    "uri": artifact.uri,
                    "media_type": artifact.media_type,
                    "size_bytes": artifact.size_bytes,
                    "checksum": artifact.checksum,
                    "etag": artifact.etag,
                    "version": artifact.version,
                    "filename": artifact.filename,
                    "metadata": dict(artifact.metadata),
                },
                "etag": artifact.checksum,
                "ignored": float("nan"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BlobStoreError, match="strict JSON"):
        store.head(key)


def test_local_blob_store_rejects_malformed_metadata_structure(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha policy", PutOptions(media_type="text/plain"))
    store._metadata_path_for(key).write_text(
        json.dumps({"etag": "sha256:missing-artifact"}),
        encoding="utf-8",
    )

    with pytest.raises(BlobStoreError, match="valid artifact record"):
        store.head(key)


def test_local_blob_store_rejects_content_that_does_not_match_metadata(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha policy", PutOptions(media_type="text/plain"))
    store._path_for(key).write_bytes(b"tampered content")

    with pytest.raises(BlobStoreError, match="does not match recorded checksum"):
        store.head(key)
    with pytest.raises(BlobStoreError, match="does not match recorded checksum"):
        store.get(key)


def test_local_blob_store_rejects_metadata_with_incorrect_content_size(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha policy", PutOptions(media_type="text/plain"))
    metadata_path = store._metadata_path_for(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["artifact"]["size_bytes"] = 1
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BlobStoreError, match="does not match recorded size"):
        store.head(key)
    with pytest.raises(BlobStoreError, match="does not match recorded size"):
        store.get(key)


def test_local_blob_store_rejects_duplicate_and_forged_metadata_identity(
    tmp_path,
) -> None:
    store = LocalBlobStore(tmp_path)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha policy", PutOptions(media_type="text/plain"))
    metadata_path = store._metadata_path_for(key)
    encoded = metadata_path.read_text(encoding="utf-8")
    metadata_path.write_text(
        encoded[:-1] + ',"etag":"sha256:duplicate"}',
        encoding="utf-8",
    )

    with pytest.raises(BlobStoreError, match="strict JSON"):
        store.head(key)

    payload = json.loads(encoded)
    payload["artifact"]["artifact_id"] = "blob:docs/other.txt"
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BlobStoreError, match="metadata identity"):
        store.head(key)


def test_put_options_rejects_invalid_metadata() -> None:
    with pytest.raises(ValueError, match="put media_type must be a string"):
        PutOptions(media_type=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="put filename must not be empty"):
        PutOptions(filename=" ")
    with pytest.raises(ValueError, match="put metadata must be a mapping"):
        PutOptions(metadata=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="put metadata keys and values must be strings"):
        PutOptions(metadata={" ": "acme"})
    with pytest.raises(ValueError, match="put metadata keys and values must be strings"):
        PutOptions(metadata={"tenant": object()})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    ("factory", "expected_error"),
    (
        (
            lambda: PutOptions(media_type=" text/plain"),
            "put media_type must not contain surrounding whitespace",
        ),
        (
            lambda: PutOptions(filename="policy.txt "),
            "put filename must not contain surrounding whitespace",
        ),
        (
            lambda: PutOptions(metadata={" tenant": "acme"}),
            "put metadata keys must not contain surrounding whitespace",
        ),
        (
            lambda: PutOptions(metadata={"tenant": " acme"}),
            "put metadata values must not contain surrounding whitespace",
        ),
        (
            lambda: BlobMetadata(BlobKey("docs/policy.txt"), ArtifactRef("artifact-1", "file:///tmp/policy.txt"), etag=" etag-1"),
            "blob metadata etag must not contain surrounding whitespace",
        ),
        (
            lambda: ListPage(items=[], next_cursor=" 1"),
            "list page next_cursor must not contain surrounding whitespace",
        ),
        (
            lambda: S3CompatibleBlobStore(bucket=" kb-artifacts", client=_FakeS3Client()),
            "bucket must not contain surrounding whitespace",
        ),
        (
            lambda: S3CompatibleBlobStore(bucket="kb-artifacts", client=_FakeS3Client(), uri_scheme=" s3"),
            "uri_scheme must not contain surrounding whitespace",
        ),
    ),
)
def test_blob_store_records_reject_whitespace_wrapped_identities(
    factory: object,
    expected_error: str,
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        factory()


def test_local_blob_store_supports_range_reads(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put(BlobKey("data.bin"), b"abcdef", PutOptions(media_type="application/octet-stream"))

    assert store.get(BlobKey("data.bin"), ByteRange(offset=2, length=3)) == b"cde"
    assert store.get(BlobKey("data.bin"), ByteRange(offset=4)) == b"ef"
    with pytest.raises(ValueError, match="byte range offset and length must be integers"):
        ByteRange(offset=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="byte range offset and length must be non-negative"):
        ByteRange(offset=-1)
    with pytest.raises(ValueError, match="byte range offset and length must be non-negative"):
        ByteRange(offset=0, length=-1)


def test_local_blob_store_lists_sorted_prefix_with_cursor(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put(BlobKey("docs/b.txt"), b"b", PutOptions())
    store.put(BlobKey("docs/a.txt"), b"a", PutOptions())
    store.put(BlobKey("other/c.txt"), b"c", PutOptions())

    first_page = store.list("docs/", limit=1)
    second_page = store.list("docs/", cursor=first_page.next_cursor, limit=1)

    assert [item.key.key for item in first_page.items] == ["docs/a.txt"]
    assert first_page.next_cursor == "1"
    assert [item.key.key for item in second_page.items] == ["docs/b.txt"]
    assert second_page.next_cursor is None
    with pytest.raises(AttributeError):
        first_page.items.append(second_page.items[0])


@pytest.mark.parametrize("cursor", [True, "", "-1", "+1", "01", "1.0", "one"])
def test_local_blob_store_rejects_non_canonical_list_cursors(tmp_path, cursor: object) -> None:
    store = LocalBlobStore(tmp_path)
    store.put(BlobKey("docs/a.txt"), b"a", PutOptions())

    with pytest.raises(ValueError, match="cursor must be a canonical non-negative integer"):
        store.list("docs/", cursor=cursor)


@pytest.mark.parametrize("prefix", ["/absolute", "../escape", "docs/../escape", "docs//escape", "docs\\escape"])
def test_local_blob_store_rejects_invalid_list_prefixes(tmp_path, prefix: str) -> None:
    store = LocalBlobStore(tmp_path)
    store.put(BlobKey("docs/a.txt"), b"a", PutOptions())

    with pytest.raises(InvalidBlobKeyError):
        store.list(prefix)


def test_local_blob_store_delete_removes_blob(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha", PutOptions())

    store.delete(key)

    with pytest.raises(BlobNotFoundError):
        store.head(key)


def test_local_blob_store_rejects_path_traversal(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)

    with pytest.raises(InvalidBlobKeyError):
        store.put(BlobKey("../escape.txt"), b"nope", PutOptions())

    with pytest.raises(InvalidBlobKeyError):
        store.get(BlobKey("/absolute.txt"))

    with pytest.raises(InvalidBlobKeyError):
        BlobKey("docs//escape.txt")

    with pytest.raises(InvalidBlobKeyError, match="blob key must be a string"):
        BlobKey(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "reserved_directory",
    (".graphblocks-metadata", ".GraphBlocks-Metadata", ".GRAPHBLOCKS-METADATA"),
)
def test_local_blob_store_rejects_reserved_metadata_namespace(
    tmp_path,
    reserved_directory: str,
) -> None:
    store = LocalBlobStore(tmp_path)
    victim = BlobKey("docs/policy.txt")
    store.put(victim, b"alpha policy", PutOptions(media_type="text/plain"))

    with pytest.raises(InvalidBlobKeyError, match="reserved local metadata namespace"):
        store.put(
            BlobKey(f"{reserved_directory}/docs/policy.txt.json"),
            b"not metadata",
            PutOptions(),
        )

    assert store.get(victim) == b"alpha policy"
    assert store.head(victim).artifact.media_type == "text/plain"


def test_local_blob_store_recovers_interrupted_metadata_commit(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalBlobStore(tmp_path)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"old policy", PutOptions(media_type="text/plain"))
    metadata_path = store._metadata_path_for(key)
    _, pending_metadata_path = store._pending_paths_for(key)
    original_replace = type(metadata_path).replace

    def interrupt_metadata_replace(path, target):
        if path == pending_metadata_path:
            raise OSError("simulated interruption")
        return original_replace(path, target)

    monkeypatch.setattr(type(metadata_path), "replace", interrupt_metadata_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        store.put(
            key,
            b"new policy",
            PutOptions(media_type="text/markdown", filename="policy.md"),
        )
    monkeypatch.undo()

    assert store.get(key) == b"new policy"
    metadata = store.head(key)
    assert metadata.artifact.media_type == "text/markdown"
    assert metadata.artifact.filename == "policy.md"
    assert not pending_metadata_path.exists()


def test_local_blob_store_serializes_concurrent_same_key_puts_across_instances(
    tmp_path,
) -> None:
    first_store = LocalBlobStore(tmp_path)
    second_store = LocalBlobStore(tmp_path)
    shared_root_lock = first_store._root_lock
    assert second_store._root_lock is shared_root_lock

    class CoordinatedLock:
        def __init__(self) -> None:
            self.attempts = Barrier(2)
            self.lock = Lock()

        def __enter__(self):
            self.attempts.wait()
            self.lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            self.lock.release()

    coordinated_lock = CoordinatedLock()
    first_store._root_lock = coordinated_lock
    second_store._root_lock = coordinated_lock
    key = BlobKey("docs/policy.txt")
    writes = (
        (first_store, b"alpha policy", "text/plain"),
        (second_store, b"beta policy", "text/markdown"),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            artifacts = tuple(
                executor.map(
                    lambda write: write[0].put(
                        key,
                        write[1],
                        PutOptions(media_type=write[2]),
                    ),
                    writes,
                )
            )
    finally:
        first_store._root_lock = shared_root_lock
        second_store._root_lock = shared_root_lock

    expected_by_checksum = {
        artifact.checksum: (body, media_type)
        for artifact, (_, body, media_type) in zip(artifacts, writes, strict=True)
    }
    metadata = first_store.head(key)
    expected_body, expected_media_type = expected_by_checksum[
        metadata.artifact.checksum
    ]
    assert first_store.get(key) == expected_body
    assert metadata.artifact.media_type == expected_media_type
    assert not any(path.exists() for path in first_store._pending_paths_for(key))


def test_local_blob_store_does_not_retain_unused_root_locks(tmp_path) -> None:
    root = tmp_path.resolve()
    store = LocalBlobStore(root)
    root_lock = ref(store._root_lock)

    assert root in blob_store_module._LOCAL_ROOT_LOCKS

    del store
    gc.collect()

    assert root_lock() is None
    assert root not in blob_store_module._LOCAL_ROOT_LOCKS


def test_local_blob_store_rejects_metadata_symlink_escape(tmp_path, symlink_or_skip) -> None:
    root = tmp_path / "blob-root"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = LocalBlobStore(root)
    metadata_parent = root / ".graphblocks-metadata" / "docs"
    symlink_or_skip(metadata_parent, outside, target_is_directory=True)

    with pytest.raises(InvalidBlobKeyError, match="invalid blob key"):
        store.put(BlobKey("docs/policy.txt"), b"alpha policy", PutOptions())

    assert not (root / "docs" / "policy.txt").exists()
    assert not (outside / "policy.txt.json").exists()


def test_blob_metadata_and_list_page_validate_record_types() -> None:
    key = BlobKey("docs/policy.txt")
    artifact = ArtifactRef("artifact-1", "file:///tmp/policy.txt")
    metadata = BlobMetadata(key, artifact, etag="etag-1")
    item = BlobListItem(key, metadata)

    page = ListPage(items=[item], next_cursor="1")  # type: ignore[arg-type]

    assert page.items == (item,)
    with pytest.raises(ValueError, match="blob metadata key must be a BlobKey"):
        BlobMetadata(object(), artifact)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="blob list item metadata key must match key"):
        BlobListItem(BlobKey("docs/other.txt"), metadata)
    with pytest.raises(ValueError, match="list page items must be BlobListItem"):
        ListPage(items=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="list page next_cursor must not be empty"):
        ListPage(items=[], next_cursor=" ")


def test_s3_compatible_blob_store_uses_injected_client_without_sdk_dependency() -> None:
    client = _FakeS3Client()
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=client)
    options = PutOptions(media_type="text/plain", filename="policy.txt", metadata={"tenant": "acme"})

    artifact = store.put(BlobKey("docs/policy.txt"), b"alpha policy", options)
    metadata = store.head(BlobKey("docs/policy.txt"))

    assert artifact.artifact_id == "s3:kb-artifacts:docs/policy.txt"
    assert artifact.uri == "s3://kb-artifacts/docs/policy.txt"
    assert artifact.media_type == "text/plain"
    assert artifact.size_bytes == 12
    assert artifact.checksum == "sha256:c756898a9faceb6ccccb473210b12caacad0e71afbf84855dadf3f9db1902ef2"
    assert artifact.filename == "policy.txt"
    assert artifact.metadata == {"tenant": "acme"}
    assert metadata.artifact == artifact
    assert metadata.etag == artifact.checksum
    assert client.objects[("kb-artifacts", "docs/policy.txt")]["Metadata"] == {
        "graphblocks-checksum": artifact.checksum,
        "graphblocks-filename": "policy.txt",
        "tenant": "acme",
    }


def test_s3_compatible_blob_store_rejects_full_body_checksum_drift() -> None:
    client = _FakeS3Client()
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=client)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha policy", PutOptions(media_type="text/plain"))
    client.objects[("kb-artifacts", key.key)]["Body"] = b"omega policy"

    with pytest.raises(BlobStoreError, match="does not match recorded checksum"):
        store.get(key)
    assert client.head_requests == 0


def test_s3_compatible_blob_store_get_does_not_issue_follow_up_head() -> None:
    client = _FakeS3Client()
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=client)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha policy", PutOptions(media_type="text/plain"))

    assert store.get(key) == b"alpha policy"
    assert client.head_requests == 0


def test_s3_compatible_blob_store_validates_get_object_content_length() -> None:
    client = _FakeS3Client()
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=client)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha policy", PutOptions(media_type="text/plain"))
    client.get_content_length_override = 99

    with pytest.raises(BlobStoreError, match="GetObject ContentLength"):
        store.get(key)
    assert client.head_requests == 0


def test_s3_compatible_blob_store_rejects_invalid_identity_fields() -> None:
    with pytest.raises(ValueError, match="bucket must be a string"):
        S3CompatibleBlobStore(bucket=object(), client=_FakeS3Client())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bucket must not be empty"):
        S3CompatibleBlobStore(bucket=" ", client=_FakeS3Client())
    with pytest.raises(ValueError, match="uri_scheme must be a string"):
        S3CompatibleBlobStore(bucket="kb-artifacts", client=_FakeS3Client(), uri_scheme=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="uri_scheme must not be empty"):
        S3CompatibleBlobStore(bucket="kb-artifacts", client=_FakeS3Client(), uri_scheme=" ")


def test_s3_compatible_blob_store_rejects_case_colliding_metadata_keys() -> None:
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=_FakeS3Client())

    with pytest.raises(ValueError, match="metadata keys collide after S3 normalization"):
        store.put(
            BlobKey("docs/policy.txt"),
            b"alpha",
            PutOptions(metadata={"Tenant": "acme", "tenant": "other"}),
        )


@pytest.mark.parametrize("limit", (True, 1.5, "1"))
def test_blob_store_backends_reject_non_integer_list_limits(
    tmp_path,
    limit: object,
) -> None:
    stores = (
        LocalBlobStore(tmp_path),
        S3CompatibleBlobStore(bucket="kb-artifacts", client=_FakeS3Client()),
    )

    for store in stores:
        with pytest.raises(ValueError, match="limit must be an integer"):
            store.list(limit=limit)  # type: ignore[arg-type]


def test_s3_compatible_blob_store_rejects_malformed_response_metadata() -> None:
    client = _FakeS3Client()
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=client)
    key = BlobKey("docs/policy.txt")
    store.put(key, b"alpha policy", PutOptions())
    stored = client.objects[("kb-artifacts", key.key)]
    stored["Metadata"] = {"Tenant": "acme", "tenant": "other"}

    with pytest.raises(BlobStoreError, match="collide after normalization"):
        store.head(key)

    stored["Metadata"] = {"tenant": object()}
    with pytest.raises(BlobStoreError, match="keys and values must be strings"):
        store.head(key)


def test_s3_compatible_blob_store_supports_range_reads_and_pagination() -> None:
    client = _FakeS3Client()
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=client)
    store.put(BlobKey("docs/b.txt"), b"bravo", PutOptions(media_type="text/plain", filename="b.txt"))
    store.put(BlobKey("docs/a.txt"), b"alpha", PutOptions(media_type="text/plain", filename="a.txt"))
    store.put(BlobKey("other/c.txt"), b"charlie", PutOptions(media_type="text/plain", filename="c.txt"))

    assert store.get(BlobKey("docs/a.txt"), ByteRange(offset=1, length=3)) == b"lph"
    range_request_count = len(client.range_headers)
    assert store.get(BlobKey("docs/a.txt"), ByteRange(offset=1, length=0)) == b""
    assert len(client.range_headers) == range_request_count

    first_page = store.list("docs/", limit=1)
    second_page = store.list("docs/", cursor=first_page.next_cursor, limit=1)

    assert [item.key.key for item in first_page.items] == ["docs/a.txt"]
    assert first_page.next_cursor == "1"
    assert [item.key.key for item in second_page.items] == ["docs/b.txt"]
    assert second_page.next_cursor is None
    assert client.range_headers[-1] == "bytes=1-3"


def test_s3_compatible_blob_store_maps_missing_keys_and_rejects_invalid_keys() -> None:
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=_FakeS3Client())

    with pytest.raises(BlobNotFoundError):
        store.get(BlobKey("missing.txt"))

    with pytest.raises(InvalidBlobKeyError):
        store.put(BlobKey("../escape.txt"), b"nope", PutOptions())

    with pytest.raises(InvalidBlobKeyError):
        store.list("../escape")


class _FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.range_headers: list[str | None] = []
        self.head_requests = 0
        self.get_content_length_override: int | None = None

    def put_object(self, **kwargs: object) -> dict[str, object]:
        key = (str(kwargs["Bucket"]), str(kwargs["Key"]))
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[key] = {
            "Body": body,
            "ContentType": kwargs.get("ContentType"),
            "Metadata": dict(kwargs.get("Metadata", {})),
            "ETag": kwargs.get("Metadata", {}).get("graphblocks-checksum"),
        }
        return {"ETag": self.objects[key]["ETag"]}

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = (str(kwargs["Bucket"]), str(kwargs["Key"]))
        try:
            item = self.objects[key]
        except KeyError as error:
            raise _FakeClientError("NoSuchKey") from error
        body = item["Body"]
        assert isinstance(body, bytes)
        range_header = kwargs.get("Range")
        self.range_headers.append(range_header if range_header is None else str(range_header))
        if isinstance(range_header, str):
            start_text, end_text = range_header.removeprefix("bytes=").split("-", 1)
            start = int(start_text)
            body = body[start:] if end_text == "" else body[start : int(end_text) + 1]
        return {
            "Body": _FakeBody(body),
            "ContentLength": (
                len(body)
                if self.get_content_length_override is None
                else self.get_content_length_override
            ),
            "ContentType": item["ContentType"],
            "Metadata": dict(item["Metadata"]),
            "ETag": item["ETag"],
        }

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.head_requests += 1
        key = (str(kwargs["Bucket"]), str(kwargs["Key"]))
        try:
            item = self.objects[key]
        except KeyError as error:
            raise _FakeClientError("404") from error
        body = item["Body"]
        assert isinstance(body, bytes)
        return {
            "ContentLength": len(body),
            "ContentType": item["ContentType"],
            "Metadata": dict(item["Metadata"]),
            "ETag": item["ETag"],
        }

    def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)
        return {}

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        bucket = str(kwargs["Bucket"])
        prefix = str(kwargs.get("Prefix", ""))
        start = int(kwargs.get("ContinuationToken", 0))
        max_keys = int(kwargs["MaxKeys"])
        keys = sorted(key for item_bucket, key in self.objects if item_bucket == bucket and key.startswith(prefix))
        page_keys = keys[start : start + max_keys]
        payload: dict[str, object] = {"Contents": [{"Key": key} for key in page_keys]}
        if start + max_keys < len(keys):
            payload["NextContinuationToken"] = str(start + max_keys)
        return payload


class _FakeBody:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body
