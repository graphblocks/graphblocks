from __future__ import annotations

import pytest

from graphblocks.blob_store import (
    BlobKey,
    BlobNotFoundError,
    ByteRange,
    InvalidBlobKeyError,
    LocalBlobStore,
    PutOptions,
    S3CompatibleBlobStore,
)


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


def test_local_blob_store_supports_range_reads(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put(BlobKey("data.bin"), b"abcdef", PutOptions(media_type="application/octet-stream"))

    assert store.get(BlobKey("data.bin"), ByteRange(offset=2, length=3)) == b"cde"
    assert store.get(BlobKey("data.bin"), ByteRange(offset=4)) == b"ef"
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


def test_s3_compatible_blob_store_rejects_invalid_identity_fields() -> None:
    with pytest.raises(ValueError, match="bucket must be a string"):
        S3CompatibleBlobStore(bucket=object(), client=_FakeS3Client())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bucket must not be empty"):
        S3CompatibleBlobStore(bucket=" ", client=_FakeS3Client())
    with pytest.raises(ValueError, match="uri_scheme must be a string"):
        S3CompatibleBlobStore(bucket="kb-artifacts", client=_FakeS3Client(), uri_scheme=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="uri_scheme must not be empty"):
        S3CompatibleBlobStore(bucket="kb-artifacts", client=_FakeS3Client(), uri_scheme=" ")


def test_s3_compatible_blob_store_supports_range_reads_and_pagination() -> None:
    client = _FakeS3Client()
    store = S3CompatibleBlobStore(bucket="kb-artifacts", client=client)
    store.put(BlobKey("docs/b.txt"), b"bravo", PutOptions(media_type="text/plain", filename="b.txt"))
    store.put(BlobKey("docs/a.txt"), b"alpha", PutOptions(media_type="text/plain", filename="a.txt"))
    store.put(BlobKey("other/c.txt"), b"charlie", PutOptions(media_type="text/plain", filename="c.txt"))

    assert store.get(BlobKey("docs/a.txt"), ByteRange(offset=1, length=3)) == b"lph"

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


class _FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.range_headers: list[str | None] = []

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
        return {"Body": _FakeBody(body)}

    def head_object(self, **kwargs: object) -> dict[str, object]:
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
