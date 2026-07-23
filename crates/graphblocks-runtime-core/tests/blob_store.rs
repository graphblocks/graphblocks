use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::rc::Rc;
use std::time::{SystemTime, UNIX_EPOCH};

use graphblocks_runtime_core::blob_store::{
    BlobKey, BlobStoreError, ByteRange, LocalBlobStore, PutOptions, S3CompatibleBlobStore,
    S3CompatibleClient, S3DeleteObjectRequest, S3GetObjectRequest, S3GetObjectResponse,
    S3HeadObjectRequest, S3HeadObjectResponse, S3ListObjectsRequest, S3ListObjectsResponse,
    S3PutObjectRequest, S3PutObjectResponse,
};

fn temp_root(test_name: &str) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after Unix epoch")
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "graphblocks-{test_name}-{}-{unique}",
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&root);
    root
}

#[test]
fn local_blob_store_put_head_and_get_round_trip() -> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("put-head-get");
    let store = LocalBlobStore::new(&root)?;
    let mut metadata = BTreeMap::new();
    metadata.insert("tenant".to_owned(), "acme".to_owned());

    let artifact = store.put(
        &BlobKey::new("docs/policy.txt"),
        b"alpha policy",
        PutOptions::new()
            .with_media_type("text/plain")
            .with_filename("policy.txt")
            .with_metadata(metadata.clone()),
    )?;

    let head = store.head(&BlobKey::new("docs/policy.txt"))?;
    assert_eq!(artifact.artifact_id, "blob:docs/policy.txt");
    assert!(artifact.uri.starts_with("file://"));
    assert_eq!(artifact.media_type.as_deref(), Some("text/plain"));
    assert_eq!(artifact.size_bytes, Some(12));
    assert_eq!(
        artifact.checksum.as_deref(),
        Some("sha256:c756898a9faceb6ccccb473210b12caacad0e71afbf84855dadf3f9db1902ef2")
    );
    assert_eq!(artifact.filename.as_deref(), Some("policy.txt"));
    assert_eq!(artifact.metadata, metadata);
    assert_eq!(head.artifact, artifact);
    assert_eq!(head.etag.as_ref(), artifact.checksum.as_ref());
    assert_eq!(
        store.get(&BlobKey::new("docs/policy.txt"), None)?,
        b"alpha policy"
    );

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_rejects_reserved_metadata_namespace() -> Result<(), Box<dyn std::error::Error>>
{
    let root = temp_root("reserved-metadata-namespace");
    let store = LocalBlobStore::new(&root)?;
    for raw_key in [
        ".graphblocks-metadata/docs/policy.txt.json",
        ".GraphBlocks-Metadata/docs/policy.txt.json",
    ] {
        let key = BlobKey::new(raw_key);
        assert_eq!(
            store.put(&key, b"not metadata", PutOptions::new()),
            Err(BlobStoreError::InvalidKey {
                key: key.key.clone(),
            })
        );
        assert_eq!(
            store.get(&key, None),
            Err(BlobStoreError::InvalidKey {
                key: key.key.clone(),
            })
        );
    }

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_rejects_content_that_does_not_match_metadata()
-> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("content-integrity");
    let store = LocalBlobStore::new(&root)?;
    let key = BlobKey::new("docs/policy.txt");
    store.put(&key, b"alpha policy", PutOptions::new())?;
    fs::write(root.join("docs/policy.txt"), b"tampered content")?;

    assert!(matches!(
        store.head(&key),
        Err(BlobStoreError::Metadata { message, .. })
            if message.contains("does not match recorded checksum")
    ));
    assert!(matches!(
        store.get(&key, None),
        Err(BlobStoreError::Metadata { message, .. })
            if message.contains("does not match recorded checksum")
    ));

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_rejects_metadata_with_incorrect_content_size()
-> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("content-size-integrity");
    let store = LocalBlobStore::new(&root)?;
    let key = BlobKey::new("docs/policy.txt");
    store.put(&key, b"alpha policy", PutOptions::new())?;
    let metadata_path = root.join(".graphblocks-metadata/docs/policy.txt.json");
    let mut payload: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&metadata_path)?)?;
    payload["artifact"]["size_bytes"] = serde_json::json!(1);
    fs::write(&metadata_path, serde_json::to_string_pretty(&payload)?)?;

    assert!(matches!(
        store.head(&key),
        Err(BlobStoreError::Metadata { message, .. })
            if message.contains("does not match recorded size")
    ));
    assert!(matches!(
        store.get(&key, None),
        Err(BlobStoreError::Metadata { message, .. })
            if message.contains("does not match recorded size")
    ));

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_rejects_malformed_or_unknown_metadata_fields()
-> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("metadata-schema-integrity");
    let store = LocalBlobStore::new(&root)?;
    let key = BlobKey::new("docs/policy.txt");
    store.put(&key, b"alpha policy", PutOptions::new())?;
    let metadata_path = root.join(".graphblocks-metadata/docs/policy.txt.json");
    let mut payload: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&metadata_path)?)?;

    payload["artifact"]["media_type"] = serde_json::json!(42);
    fs::write(&metadata_path, serde_json::to_string_pretty(&payload)?)?;
    assert!(matches!(
        store.head(&key),
        Err(BlobStoreError::Metadata { message, .. })
            if message.contains("media_type") && message.contains("string")
    ));

    payload["artifact"]["media_type"] = serde_json::Value::Null;
    payload["artifact"]["unexpected"] = serde_json::json!(true);
    fs::write(&metadata_path, serde_json::to_string_pretty(&payload)?)?;
    assert!(matches!(
        store.head(&key),
        Err(BlobStoreError::Metadata { message, .. })
            if message.contains("unknown artifact field unexpected")
    ));

    payload["artifact"]
        .as_object_mut()
        .expect("artifact is an object")
        .remove("unexpected");
    payload["unexpected"] = serde_json::json!(true);
    fs::write(&metadata_path, serde_json::to_string_pretty(&payload)?)?;
    assert!(matches!(
        store.head(&key),
        Err(BlobStoreError::Metadata { message, .. })
            if message.contains("unknown metadata field unexpected")
    ));

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_supports_range_reads() -> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("range");
    let store = LocalBlobStore::new(&root)?;
    store.put(
        &BlobKey::new("data.bin"),
        b"abcdef",
        PutOptions::new().with_media_type("application/octet-stream"),
    )?;

    assert_eq!(
        store.get(
            &BlobKey::new("data.bin"),
            Some(ByteRange {
                offset: 2,
                length: Some(3),
            }),
        )?,
        b"cde"
    );
    assert_eq!(
        store.get(
            &BlobKey::new("data.bin"),
            Some(ByteRange {
                offset: 4,
                length: None,
            }),
        )?,
        b"ef"
    );

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_lists_sorted_prefix_with_cursor() -> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("list");
    let store = LocalBlobStore::new(&root)?;
    store.put(&BlobKey::new("docs/b.txt"), b"b", PutOptions::new())?;
    store.put(&BlobKey::new("docs/a.txt"), b"a", PutOptions::new())?;
    store.put(&BlobKey::new("other/c.txt"), b"c", PutOptions::new())?;

    let first_page = store.list("docs/", None, 1)?;
    let second_page = store.list("docs/", first_page.next_cursor.as_deref(), 1)?;

    assert_eq!(
        first_page
            .items
            .iter()
            .map(|item| item.key.key.as_str())
            .collect::<Vec<_>>(),
        vec!["docs/a.txt"]
    );
    assert_eq!(first_page.next_cursor.as_deref(), Some("1"));
    assert_eq!(
        second_page
            .items
            .iter()
            .map(|item| item.key.key.as_str())
            .collect::<Vec<_>>(),
        vec!["docs/b.txt"]
    );
    assert!(second_page.next_cursor.is_none());

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_rejects_noncanonical_list_cursors() -> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("invalid-list-cursor");
    let store = LocalBlobStore::new(&root)?;
    store.put(&BlobKey::new("docs/a.txt"), b"a", PutOptions::new())?;

    for cursor in ["", "+1", "01", "1.0", "one"] {
        assert!(matches!(
            store.list("", Some(cursor), 1),
            Err(BlobStoreError::InvalidCursor { .. })
        ));
    }

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_handles_maximum_list_cursor_without_overflow()
-> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("maximum-list-cursor");
    let store = LocalBlobStore::new(&root)?;
    store.put(&BlobKey::new("docs/a.txt"), b"a", PutOptions::new())?;

    let page = store.list("", Some(&usize::MAX.to_string()), 1)?;

    assert!(page.items.is_empty());
    assert!(page.next_cursor.is_none());
    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_delete_removes_blob() -> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("delete");
    let store = LocalBlobStore::new(&root)?;
    let key = BlobKey::new("docs/policy.txt");
    store.put(&key, b"alpha", PutOptions::new())?;

    store.delete(&key)?;

    assert!(matches!(
        store.head(&key),
        Err(BlobStoreError::NotFound { .. })
    ));
    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn local_blob_store_rejects_path_traversal() -> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("invalid-key");
    let store = LocalBlobStore::new(&root)?;

    assert!(matches!(
        store.put(&BlobKey::new("../escape.txt"), b"nope", PutOptions::new()),
        Err(BlobStoreError::InvalidKey { .. })
    ));
    assert!(matches!(
        store.get(&BlobKey::new("/absolute.txt"), None),
        Err(BlobStoreError::InvalidKey { .. })
    ));

    fs::remove_dir_all(root)?;
    Ok(())
}

#[cfg(unix)]
#[test]
fn local_blob_store_rejects_content_symlink_escape_before_writing()
-> Result<(), Box<dyn std::error::Error>> {
    use std::os::unix::fs::symlink;

    let root = temp_root("content-symlink-root");
    let outside = temp_root("content-symlink-outside");
    let store = LocalBlobStore::new(&root)?;
    fs::create_dir_all(&outside)?;
    symlink(&outside, root.join("docs"))?;

    assert!(matches!(
        store.put(
            &BlobKey::new("docs/policy.txt"),
            b"alpha policy",
            PutOptions::new(),
        ),
        Err(BlobStoreError::InvalidKey { .. })
    ));
    assert!(!outside.join("policy.txt").exists());

    fs::remove_dir_all(root)?;
    fs::remove_dir_all(outside)?;
    Ok(())
}

#[cfg(unix)]
#[test]
fn local_blob_store_rejects_metadata_symlink_escape_before_writing()
-> Result<(), Box<dyn std::error::Error>> {
    use std::os::unix::fs::symlink;

    let root = temp_root("metadata-symlink-root");
    let outside = temp_root("metadata-symlink-outside");
    let store = LocalBlobStore::new(&root)?;
    fs::create_dir_all(&outside)?;
    symlink(&outside, root.join(".graphblocks-metadata/docs"))?;

    assert!(matches!(
        store.put(
            &BlobKey::new("docs/policy.txt"),
            b"alpha policy",
            PutOptions::new(),
        ),
        Err(BlobStoreError::InvalidKey { .. })
    ));
    assert!(!root.join("docs/policy.txt").exists());
    assert!(!outside.join("policy.txt.json").exists());

    fs::remove_dir_all(root)?;
    fs::remove_dir_all(outside)?;
    Ok(())
}

#[test]
fn blob_stores_reject_blank_keys() -> Result<(), Box<dyn std::error::Error>> {
    let root = temp_root("blank-key");
    let local = LocalBlobStore::new(&root)?;
    let s3 = S3CompatibleBlobStore::new("kb-artifacts", FakeS3Client::default())?;

    assert!(matches!(
        local.put(&BlobKey::new("   "), b"nope", PutOptions::new()),
        Err(BlobStoreError::InvalidKey { .. })
    ));
    assert!(matches!(
        s3.put(&BlobKey::new("   "), b"nope", PutOptions::new()),
        Err(BlobStoreError::InvalidKey { .. })
    ));

    fs::remove_dir_all(root)?;
    Ok(())
}

#[test]
fn s3_compatible_blob_store_uses_injected_client_without_sdk_dependency()
-> Result<(), Box<dyn std::error::Error>> {
    let client = FakeS3Client::default();
    let store = S3CompatibleBlobStore::new("kb-artifacts", client.clone())?;
    let mut metadata = BTreeMap::new();
    metadata.insert("tenant".to_owned(), "acme".to_owned());

    let artifact = store.put(
        &BlobKey::new("docs/policy.txt"),
        b"alpha policy",
        PutOptions::new()
            .with_media_type("text/plain")
            .with_filename("policy.txt")
            .with_metadata(metadata.clone()),
    )?;
    let head = store.head(&BlobKey::new("docs/policy.txt"))?;

    assert_eq!(artifact.artifact_id, "s3:kb-artifacts:docs/policy.txt");
    assert_eq!(artifact.uri, "s3://kb-artifacts/docs/policy.txt");
    assert_eq!(artifact.media_type.as_deref(), Some("text/plain"));
    assert_eq!(artifact.size_bytes, Some(12));
    assert_eq!(
        artifact.checksum.as_deref(),
        Some("sha256:c756898a9faceb6ccccb473210b12caacad0e71afbf84855dadf3f9db1902ef2")
    );
    assert_eq!(artifact.filename.as_deref(), Some("policy.txt"));
    assert_eq!(artifact.metadata, metadata);
    assert_eq!(head.artifact, artifact);
    assert_eq!(head.etag.as_ref(), artifact.checksum.as_ref());
    assert_eq!(
        client.metadata_for("kb-artifacts", "docs/policy.txt"),
        BTreeMap::from([
            (
                "graphblocks-checksum".to_owned(),
                artifact.checksum.clone().expect("checksum is recorded")
            ),
            ("graphblocks-filename".to_owned(), "policy.txt".to_owned()),
            ("tenant".to_owned(), "acme".to_owned())
        ])
    );
    Ok(())
}

#[test]
fn s3_compatible_blob_store_supports_range_reads_and_pagination()
-> Result<(), Box<dyn std::error::Error>> {
    let client = FakeS3Client::default();
    let store = S3CompatibleBlobStore::new("kb-artifacts", client.clone())?;
    store.put(
        &BlobKey::new("docs/b.txt"),
        b"bravo",
        PutOptions::new()
            .with_media_type("text/plain")
            .with_filename("b.txt"),
    )?;
    store.put(
        &BlobKey::new("docs/a.txt"),
        b"alpha",
        PutOptions::new()
            .with_media_type("text/plain")
            .with_filename("a.txt"),
    )?;
    store.put(
        &BlobKey::new("other/c.txt"),
        b"charlie",
        PutOptions::new()
            .with_media_type("text/plain")
            .with_filename("c.txt"),
    )?;

    assert_eq!(
        store.get(
            &BlobKey::new("docs/a.txt"),
            Some(ByteRange {
                offset: 1,
                length: Some(3),
            }),
        )?,
        b"lph"
    );
    let range_request_count = client.range_headers().len();
    assert_eq!(
        store.get(
            &BlobKey::new("docs/a.txt"),
            Some(ByteRange {
                offset: 1,
                length: Some(0),
            }),
        )?,
        b""
    );
    assert_eq!(client.range_headers().len(), range_request_count);
    assert!(matches!(
        store.get(
            &BlobKey::new("missing.txt"),
            Some(ByteRange {
                offset: 1,
                length: Some(0),
            }),
        ),
        Err(BlobStoreError::NotFound { .. })
    ));
    let first_page = store.list("docs/", None, 1)?;
    let second_page = store.list("docs/", first_page.next_cursor.as_deref(), 1)?;

    assert_eq!(
        first_page
            .items
            .iter()
            .map(|item| item.key.key.as_str())
            .collect::<Vec<_>>(),
        vec!["docs/a.txt"]
    );
    assert_eq!(first_page.next_cursor.as_deref(), Some("1"));
    assert_eq!(
        second_page
            .items
            .iter()
            .map(|item| item.key.key.as_str())
            .collect::<Vec<_>>(),
        vec!["docs/b.txt"]
    );
    assert!(second_page.next_cursor.is_none());
    assert_eq!(
        client.range_headers().last().cloned().flatten().as_deref(),
        Some("bytes=1-3")
    );
    Ok(())
}

#[test]
fn s3_compatible_blob_store_maps_missing_keys_and_rejects_invalid_keys()
-> Result<(), Box<dyn std::error::Error>> {
    let store = S3CompatibleBlobStore::new("kb-artifacts", FakeS3Client::default())?;

    assert!(matches!(
        store.get(&BlobKey::new("missing.txt"), None),
        Err(BlobStoreError::NotFound { .. })
    ));
    assert!(matches!(
        store.put(&BlobKey::new("../escape.txt"), b"nope", PutOptions::new()),
        Err(BlobStoreError::InvalidKey { .. })
    ));
    Ok(())
}

#[derive(Clone, Default)]
struct FakeS3Client {
    inner: Rc<RefCell<FakeS3State>>,
}

#[derive(Default)]
struct FakeS3State {
    objects: BTreeMap<(String, String), FakeS3Object>,
    range_headers: Vec<Option<String>>,
}

#[derive(Clone)]
struct FakeS3Object {
    body: Vec<u8>,
    content_type: Option<String>,
    metadata: BTreeMap<String, String>,
    etag: Option<String>,
}

impl FakeS3Client {
    fn metadata_for(&self, bucket: &str, key: &str) -> BTreeMap<String, String> {
        self.inner
            .borrow()
            .objects
            .get(&(bucket.to_owned(), key.to_owned()))
            .expect("object exists")
            .metadata
            .clone()
    }

    fn range_headers(&self) -> Vec<Option<String>> {
        self.inner.borrow().range_headers.clone()
    }
}

impl S3CompatibleClient for FakeS3Client {
    fn put_object(
        &self,
        request: S3PutObjectRequest,
    ) -> Result<S3PutObjectResponse, BlobStoreError> {
        let etag = request
            .metadata
            .get("graphblocks-checksum")
            .cloned()
            .expect("checksum metadata is recorded");
        self.inner.borrow_mut().objects.insert(
            (request.bucket, request.key),
            FakeS3Object {
                body: request.body,
                content_type: request.content_type,
                metadata: request.metadata,
                etag: Some(etag.clone()),
            },
        );
        Ok(S3PutObjectResponse { etag: Some(etag) })
    }

    fn get_object(
        &self,
        request: S3GetObjectRequest,
    ) -> Result<S3GetObjectResponse, BlobStoreError> {
        let mut state = self.inner.borrow_mut();
        let object = state
            .objects
            .get(&(request.bucket, request.key.clone()))
            .ok_or_else(|| BlobStoreError::NotFound {
                key: request.key.clone(),
            })?
            .clone();
        state.range_headers.push(request.range_header.clone());
        let body = match request.range_header.as_deref() {
            Some(range_header) => {
                let range = range_header.trim_start_matches("bytes=");
                let (start, end) = range
                    .split_once('-')
                    .expect("range header contains start and end");
                let start = start.parse::<usize>().expect("range start is numeric");
                if end.is_empty() {
                    object.body[start..].to_vec()
                } else {
                    object.body[start..=end.parse::<usize>().expect("range end is numeric")]
                        .to_vec()
                }
            }
            None => object.body,
        };
        Ok(S3GetObjectResponse { body })
    }

    fn head_object(
        &self,
        request: S3HeadObjectRequest,
    ) -> Result<S3HeadObjectResponse, BlobStoreError> {
        let object = self
            .inner
            .borrow()
            .objects
            .get(&(request.bucket, request.key.clone()))
            .ok_or_else(|| BlobStoreError::NotFound {
                key: request.key.clone(),
            })?
            .clone();
        Ok(S3HeadObjectResponse {
            content_length: Some(object.body.len()),
            content_type: object.content_type,
            metadata: object.metadata,
            etag: object.etag,
        })
    }

    fn delete_object(&self, request: S3DeleteObjectRequest) -> Result<(), BlobStoreError> {
        self.inner
            .borrow_mut()
            .objects
            .remove(&(request.bucket, request.key));
        Ok(())
    }

    fn list_objects(
        &self,
        request: S3ListObjectsRequest,
    ) -> Result<S3ListObjectsResponse, BlobStoreError> {
        let start = request
            .cursor
            .as_deref()
            .unwrap_or("0")
            .parse::<usize>()
            .map_err(|_| BlobStoreError::InvalidCursor {
                cursor: request.cursor.clone().unwrap_or_default(),
            })?;
        let keys = self
            .inner
            .borrow()
            .objects
            .keys()
            .filter_map(|(bucket, key)| {
                (bucket == &request.bucket && key.starts_with(&request.prefix))
                    .then_some(key.clone())
            })
            .collect::<Vec<_>>();
        let page_keys = keys
            .iter()
            .skip(start)
            .take(request.limit)
            .cloned()
            .collect::<Vec<_>>();
        let next_cursor =
            (start + request.limit < keys.len()).then(|| (start + request.limit).to_string());
        Ok(S3ListObjectsResponse {
            keys: page_keys,
            next_cursor,
        })
    }
}
