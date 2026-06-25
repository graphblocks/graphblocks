from __future__ import annotations

import pytest

from graphblocks.blob_store import (
    BlobKey,
    BlobNotFoundError,
    ByteRange,
    InvalidBlobKeyError,
    LocalBlobStore,
    PutOptions,
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
