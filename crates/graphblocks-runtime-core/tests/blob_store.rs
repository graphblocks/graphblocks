use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use graphblocks_runtime_core::blob_store::{
    BlobKey, BlobStoreError, ByteRange, LocalBlobStore, PutOptions,
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
