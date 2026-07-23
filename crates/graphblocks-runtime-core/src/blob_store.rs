use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::documents::ArtifactRef;

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct BlobKey {
    pub key: String,
}

impl BlobKey {
    pub fn new(key: impl Into<String>) -> Self {
        Self { key: key.into() }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ByteRange {
    pub offset: usize,
    pub length: Option<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PutOptions {
    pub media_type: Option<String>,
    pub filename: Option<String>,
    pub metadata: BTreeMap<String, String>,
}

impl PutOptions {
    pub fn new() -> Self {
        Self {
            media_type: None,
            filename: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_media_type(mut self, media_type: impl Into<String>) -> Self {
        self.media_type = Some(media_type.into());
        self
    }

    pub fn with_filename(mut self, filename: impl Into<String>) -> Self {
        self.filename = Some(filename.into());
        self
    }

    pub fn with_metadata(mut self, metadata: BTreeMap<String, String>) -> Self {
        self.metadata = metadata;
        self
    }
}

impl Default for PutOptions {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlobMetadata {
    pub key: BlobKey,
    pub artifact: ArtifactRef,
    pub etag: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlobListItem {
    pub key: BlobKey,
    pub metadata: BlobMetadata,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ListPage {
    pub items: Vec<BlobListItem>,
    pub next_cursor: Option<String>,
}

pub const GRAPHBLOCKS_CHECKSUM_METADATA: &str = "graphblocks-checksum";
pub const GRAPHBLOCKS_FILENAME_METADATA: &str = "graphblocks-filename";

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3PutObjectRequest {
    pub bucket: String,
    pub key: String,
    pub body: Vec<u8>,
    pub content_type: Option<String>,
    pub metadata: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3PutObjectResponse {
    pub etag: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3GetObjectRequest {
    pub bucket: String,
    pub key: String,
    pub range_header: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3GetObjectResponse {
    pub body: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3HeadObjectRequest {
    pub bucket: String,
    pub key: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3HeadObjectResponse {
    pub content_length: Option<usize>,
    pub content_type: Option<String>,
    pub metadata: BTreeMap<String, String>,
    pub etag: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3DeleteObjectRequest {
    pub bucket: String,
    pub key: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3ListObjectsRequest {
    pub bucket: String,
    pub prefix: String,
    pub cursor: Option<String>,
    pub limit: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct S3ListObjectsResponse {
    pub keys: Vec<String>,
    pub next_cursor: Option<String>,
}

pub trait S3CompatibleClient {
    fn put_object(
        &self,
        request: S3PutObjectRequest,
    ) -> Result<S3PutObjectResponse, BlobStoreError>;

    fn get_object(
        &self,
        request: S3GetObjectRequest,
    ) -> Result<S3GetObjectResponse, BlobStoreError>;

    fn head_object(
        &self,
        request: S3HeadObjectRequest,
    ) -> Result<S3HeadObjectResponse, BlobStoreError>;

    fn delete_object(&self, request: S3DeleteObjectRequest) -> Result<(), BlobStoreError>;

    fn list_objects(
        &self,
        request: S3ListObjectsRequest,
    ) -> Result<S3ListObjectsResponse, BlobStoreError>;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum BlobStoreError {
    InvalidBucket {
        bucket: String,
    },
    InvalidKey {
        key: String,
    },
    NotFound {
        key: String,
    },
    InvalidCursor {
        cursor: String,
    },
    InvalidLimit,
    Io {
        operation: String,
        path: PathBuf,
        message: String,
    },
    Metadata {
        key: String,
        message: String,
    },
}

impl fmt::Display for BlobStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidBucket { bucket } => write!(formatter, "invalid blob bucket {bucket:?}"),
            Self::InvalidKey { key } => write!(formatter, "invalid blob key {key:?}"),
            Self::NotFound { key } => write!(formatter, "blob {key:?} does not exist"),
            Self::InvalidCursor { cursor } => write!(formatter, "invalid blob cursor {cursor:?}"),
            Self::InvalidLimit => write!(formatter, "list limit must be at least 1"),
            Self::Io {
                operation,
                path,
                message,
            } => write!(
                formatter,
                "{operation} failed for {}: {message}",
                path.display()
            ),
            Self::Metadata { key, message } => {
                write!(formatter, "metadata for blob {key:?} is invalid: {message}")
            }
        }
    }
}

impl Error for BlobStoreError {}

pub struct S3CompatibleBlobStore<C> {
    pub bucket: String,
    pub client: C,
    pub uri_scheme: String,
}

impl<C> S3CompatibleBlobStore<C> {
    pub fn new(bucket: impl Into<String>, client: C) -> Result<Self, BlobStoreError> {
        Self::new_with_uri_scheme(bucket, client, "s3")
    }

    pub fn new_with_uri_scheme(
        bucket: impl Into<String>,
        client: C,
        uri_scheme: impl Into<String>,
    ) -> Result<Self, BlobStoreError> {
        let bucket = bucket.into();
        if bucket.trim().is_empty() {
            return Err(BlobStoreError::InvalidBucket { bucket });
        }
        let uri_scheme = uri_scheme.into();
        if uri_scheme.trim().is_empty() {
            return Err(BlobStoreError::InvalidBucket { bucket: uri_scheme });
        }
        Ok(Self {
            bucket,
            client,
            uri_scheme,
        })
    }
}

impl<C: S3CompatibleClient> S3CompatibleBlobStore<C> {
    pub fn put(
        &self,
        key: &BlobKey,
        body: &[u8],
        options: PutOptions,
    ) -> Result<ArtifactRef, BlobStoreError> {
        validate_blob_key(key)?;
        let checksum = sha256_digest(body);
        let user_metadata = user_metadata(&options.metadata, key)?;
        let mut metadata = user_metadata.clone();
        metadata.insert(GRAPHBLOCKS_CHECKSUM_METADATA.to_owned(), checksum.clone());
        if let Some(filename) = &options.filename {
            metadata.insert(GRAPHBLOCKS_FILENAME_METADATA.to_owned(), filename.clone());
        }
        let response = self.client.put_object(S3PutObjectRequest {
            bucket: self.bucket.clone(),
            key: key.key.clone(),
            body: body.to_vec(),
            content_type: options.media_type.clone(),
            metadata,
        })?;
        let etag = normalize_etag(response.etag).or_else(|| Some(checksum.clone()));
        Ok(ArtifactRef {
            artifact_id: self.artifact_id(key),
            uri: self.uri(key),
            media_type: options.media_type,
            size_bytes: Some(body.len()),
            checksum: Some(checksum),
            etag: etag.clone(),
            version: etag,
            filename: options.filename,
            metadata: user_metadata,
        })
    }

    pub fn get(
        &self,
        key: &BlobKey,
        byte_range: Option<ByteRange>,
    ) -> Result<Vec<u8>, BlobStoreError> {
        validate_blob_key(key)?;
        if byte_range.is_some_and(|byte_range| byte_range.length == Some(0)) {
            self.head(key)?;
            return Ok(Vec::new());
        }
        Ok(self
            .client
            .get_object(S3GetObjectRequest {
                bucket: self.bucket.clone(),
                key: key.key.clone(),
                range_header: byte_range.map(range_header),
            })?
            .body)
    }

    pub fn head(&self, key: &BlobKey) -> Result<BlobMetadata, BlobStoreError> {
        validate_blob_key(key)?;
        let response = self.client.head_object(S3HeadObjectRequest {
            bucket: self.bucket.clone(),
            key: key.key.clone(),
        })?;
        let metadata = normalize_metadata(response.metadata);
        let checksum = metadata.get(GRAPHBLOCKS_CHECKSUM_METADATA).cloned();
        let filename = metadata.get(GRAPHBLOCKS_FILENAME_METADATA).cloned();
        let etag = normalize_etag(response.etag).or_else(|| checksum.clone());
        let artifact_metadata = metadata
            .iter()
            .filter_map(|(name, value)| {
                (!is_reserved_metadata_key(name)).then_some((name.clone(), value.clone()))
            })
            .collect::<BTreeMap<_, _>>();
        let artifact = ArtifactRef {
            artifact_id: self.artifact_id(key),
            uri: self.uri(key),
            media_type: response.content_type,
            size_bytes: response.content_length,
            checksum,
            etag: etag.clone(),
            version: etag.clone(),
            filename,
            metadata: artifact_metadata,
        };
        Ok(BlobMetadata {
            key: key.clone(),
            artifact,
            etag,
        })
    }

    pub fn delete(&self, key: &BlobKey) -> Result<(), BlobStoreError> {
        validate_blob_key(key)?;
        self.head(key)?;
        self.client.delete_object(S3DeleteObjectRequest {
            bucket: self.bucket.clone(),
            key: key.key.clone(),
        })
    }

    pub fn list(
        &self,
        prefix: &str,
        cursor: Option<&str>,
        limit: usize,
    ) -> Result<ListPage, BlobStoreError> {
        if limit < 1 {
            return Err(BlobStoreError::InvalidLimit);
        }
        validate_blob_prefix(prefix)?;
        let response = self.client.list_objects(S3ListObjectsRequest {
            bucket: self.bucket.clone(),
            prefix: prefix.to_owned(),
            cursor: cursor.map(str::to_owned),
            limit,
        })?;
        let mut items = Vec::new();
        for key in response.keys {
            let key = BlobKey::new(key);
            validate_blob_key(&key)?;
            let metadata = self.head(&key)?;
            items.push(BlobListItem { key, metadata });
        }
        Ok(ListPage {
            items,
            next_cursor: response.next_cursor,
        })
    }

    fn artifact_id(&self, key: &BlobKey) -> String {
        format!("{}:{}:{}", self.uri_scheme, self.bucket, key.key)
    }

    fn uri(&self, key: &BlobKey) -> String {
        format!("{}://{}/{}", self.uri_scheme, self.bucket, key.key)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LocalBlobStore {
    pub root: PathBuf,
}

impl LocalBlobStore {
    pub fn new(root: impl AsRef<Path>) -> Result<Self, BlobStoreError> {
        let root = root.as_ref();
        fs::create_dir_all(root).map_err(|error| BlobStoreError::Io {
            operation: "create blob root".to_owned(),
            path: root.to_path_buf(),
            message: error.to_string(),
        })?;
        let root = root.canonicalize().map_err(|error| BlobStoreError::Io {
            operation: "canonicalize blob root".to_owned(),
            path: root.to_path_buf(),
            message: error.to_string(),
        })?;
        let metadata_root = root.join(".graphblocks-metadata");
        fs::create_dir_all(&metadata_root).map_err(|error| BlobStoreError::Io {
            operation: "create blob metadata root".to_owned(),
            path: metadata_root.clone(),
            message: error.to_string(),
        })?;
        Ok(Self { root })
    }

    pub fn put(
        &self,
        key: &BlobKey,
        body: &[u8],
        options: PutOptions,
    ) -> Result<ArtifactRef, BlobStoreError> {
        let path = self.path_for(key)?;
        let metadata_path = self.metadata_path_for(key)?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| BlobStoreError::Io {
                operation: "create blob parent".to_owned(),
                path: parent.to_path_buf(),
                message: error.to_string(),
            })?;
        }
        fs::write(&path, body).map_err(|error| BlobStoreError::Io {
            operation: "write blob".to_owned(),
            path: path.clone(),
            message: error.to_string(),
        })?;
        let checksum = sha256_digest(body);
        let artifact = ArtifactRef {
            artifact_id: format!("blob:{}", key.key),
            uri: file_uri(&path),
            media_type: options.media_type,
            size_bytes: Some(body.len()),
            checksum: Some(checksum.clone()),
            etag: Some(checksum.clone()),
            version: Some(checksum.clone()),
            filename: options.filename,
            metadata: options.metadata,
        };
        if let Some(parent) = metadata_path.parent() {
            fs::create_dir_all(parent).map_err(|error| BlobStoreError::Io {
                operation: "create blob metadata parent".to_owned(),
                path: parent.to_path_buf(),
                message: error.to_string(),
            })?;
        }
        let payload = json!({
            "artifact": {
                "artifact_id": artifact.artifact_id,
                "uri": artifact.uri,
                "media_type": artifact.media_type,
                "size_bytes": artifact.size_bytes,
                "checksum": artifact.checksum,
                "etag": artifact.etag,
                "version": artifact.version,
                "filename": artifact.filename,
                "metadata": artifact.metadata,
            },
            "etag": checksum,
        });
        let metadata_text =
            serde_json::to_string_pretty(&payload).map_err(|error| BlobStoreError::Metadata {
                key: key.key.clone(),
                message: error.to_string(),
            })?;
        fs::write(&metadata_path, metadata_text).map_err(|error| BlobStoreError::Io {
            operation: "write blob metadata".to_owned(),
            path: metadata_path,
            message: error.to_string(),
        })?;
        Ok(artifact)
    }

    pub fn get(
        &self,
        key: &BlobKey,
        byte_range: Option<ByteRange>,
    ) -> Result<Vec<u8>, BlobStoreError> {
        let path = self.path_for(key)?;
        if !path.exists() {
            return Err(BlobStoreError::NotFound {
                key: key.key.clone(),
            });
        }
        let data = fs::read(&path).map_err(|error| BlobStoreError::Io {
            operation: "read blob".to_owned(),
            path,
            message: error.to_string(),
        })?;
        self.metadata_for(key, Some(&data))?;
        let Some(byte_range) = byte_range else {
            return Ok(data);
        };
        let start = byte_range.offset.min(data.len());
        let end = byte_range
            .length
            .map(|length| start.saturating_add(length).min(data.len()))
            .unwrap_or(data.len());
        Ok(data[start..end].to_vec())
    }

    pub fn head(&self, key: &BlobKey) -> Result<BlobMetadata, BlobStoreError> {
        self.metadata_for(key, None)
    }

    fn metadata_for(
        &self,
        key: &BlobKey,
        data: Option<&[u8]>,
    ) -> Result<BlobMetadata, BlobStoreError> {
        let path = self.path_for(key)?;
        if !path.exists() {
            return Err(BlobStoreError::NotFound {
                key: key.key.clone(),
            });
        }
        let loaded_data;
        let data = match data {
            Some(data) => data,
            None => {
                loaded_data = fs::read(&path).map_err(|error| BlobStoreError::Io {
                    operation: "read blob".to_owned(),
                    path: path.clone(),
                    message: error.to_string(),
                })?;
                &loaded_data
            }
        };
        let metadata_path = self.metadata_path_for(key)?;
        if metadata_path.exists() {
            let metadata_text =
                fs::read_to_string(&metadata_path).map_err(|error| BlobStoreError::Io {
                    operation: "read blob metadata".to_owned(),
                    path: metadata_path.clone(),
                    message: error.to_string(),
                })?;
            let payload: Value =
                serde_json::from_str(&metadata_text).map_err(|error| BlobStoreError::Metadata {
                    key: key.key.clone(),
                    message: error.to_string(),
                })?;
            let payload = payload
                .as_object()
                .ok_or_else(|| BlobStoreError::Metadata {
                    key: key.key.clone(),
                    message: "metadata payload must be an object".to_owned(),
                })?;
            reject_unknown_fields(key, payload, "metadata", &["artifact", "etag"])?;
            let artifact_payload = payload
                .get("artifact")
                .and_then(Value::as_object)
                .ok_or_else(|| BlobStoreError::Metadata {
                    key: key.key.clone(),
                    message: "missing artifact object".to_owned(),
                })?;
            reject_unknown_fields(
                key,
                artifact_payload,
                "artifact",
                &[
                    "artifact_id",
                    "uri",
                    "media_type",
                    "size_bytes",
                    "checksum",
                    "etag",
                    "version",
                    "filename",
                    "metadata",
                ],
            )?;
            let artifact = ArtifactRef {
                artifact_id: required_string(key, artifact_payload, "artifact_id")?,
                uri: required_string(key, artifact_payload, "uri")?,
                media_type: optional_string(key, artifact_payload, "media_type")?,
                size_bytes: optional_usize(key, artifact_payload, "size_bytes")?,
                checksum: optional_string(key, artifact_payload, "checksum")?,
                etag: optional_string(key, artifact_payload, "etag")?,
                version: optional_string(key, artifact_payload, "version")?,
                filename: optional_string(key, artifact_payload, "filename")?,
                metadata: optional_string_map(key, artifact_payload, "metadata")?,
            };
            let checksum = sha256_digest(data);
            if artifact.checksum.as_deref() != Some(checksum.as_str()) {
                return Err(BlobStoreError::Metadata {
                    key: key.key.clone(),
                    message: "content does not match recorded checksum".to_owned(),
                });
            }
            if artifact.size_bytes != Some(data.len()) {
                return Err(BlobStoreError::Metadata {
                    key: key.key.clone(),
                    message: "content does not match recorded size".to_owned(),
                });
            }
            return Ok(BlobMetadata {
                key: key.clone(),
                artifact,
                etag: optional_string(key, payload, "etag")?,
            });
        }
        let checksum = sha256_digest(data);
        let artifact = ArtifactRef {
            artifact_id: format!("blob:{}", key.key),
            uri: file_uri(&path),
            media_type: None,
            size_bytes: Some(data.len()),
            checksum: Some(checksum.clone()),
            etag: Some(checksum.clone()),
            version: None,
            filename: path
                .file_name()
                .and_then(|filename| filename.to_str())
                .map(str::to_owned),
            metadata: BTreeMap::new(),
        };
        Ok(BlobMetadata {
            key: key.clone(),
            artifact,
            etag: Some(checksum),
        })
    }

    pub fn delete(&self, key: &BlobKey) -> Result<(), BlobStoreError> {
        let path = self.path_for(key)?;
        if !path.exists() {
            return Err(BlobStoreError::NotFound {
                key: key.key.clone(),
            });
        }
        fs::remove_file(&path).map_err(|error| BlobStoreError::Io {
            operation: "delete blob".to_owned(),
            path,
            message: error.to_string(),
        })?;
        let metadata_path = self.metadata_path_for(key)?;
        if metadata_path.exists() {
            fs::remove_file(&metadata_path).map_err(|error| BlobStoreError::Io {
                operation: "delete blob metadata".to_owned(),
                path: metadata_path,
                message: error.to_string(),
            })?;
        }
        Ok(())
    }

    pub fn list(
        &self,
        prefix: &str,
        cursor: Option<&str>,
        limit: usize,
    ) -> Result<ListPage, BlobStoreError> {
        if limit < 1 {
            return Err(BlobStoreError::InvalidLimit);
        }
        let start = match cursor {
            Some(cursor)
                if !cursor.is_empty()
                    && cursor.bytes().all(|byte| byte.is_ascii_digit())
                    && (cursor == "0" || !cursor.starts_with('0')) =>
            {
                cursor
                    .parse::<usize>()
                    .map_err(|_| BlobStoreError::InvalidCursor {
                        cursor: cursor.to_owned(),
                    })?
            }
            Some(cursor) => {
                return Err(BlobStoreError::InvalidCursor {
                    cursor: cursor.to_owned(),
                });
            }
            None => 0,
        };
        let mut keys = Vec::new();
        let mut stack = vec![self.root.clone()];
        while let Some(directory) = stack.pop() {
            for entry in fs::read_dir(&directory).map_err(|error| BlobStoreError::Io {
                operation: "read blob directory".to_owned(),
                path: directory.clone(),
                message: error.to_string(),
            })? {
                let entry = entry.map_err(|error| BlobStoreError::Io {
                    operation: "read blob directory entry".to_owned(),
                    path: directory.clone(),
                    message: error.to_string(),
                })?;
                let path = entry.path();
                let relative =
                    path.strip_prefix(&self.root)
                        .map_err(|error| BlobStoreError::Io {
                            operation: "strip blob root prefix".to_owned(),
                            path: path.clone(),
                            message: error.to_string(),
                        })?;
                if relative
                    .components()
                    .next()
                    .is_some_and(|component| component.as_os_str() == ".graphblocks-metadata")
                {
                    continue;
                }
                let file_type = entry.file_type().map_err(|error| BlobStoreError::Io {
                    operation: "read blob file type".to_owned(),
                    path: path.clone(),
                    message: error.to_string(),
                })?;
                if file_type.is_dir() {
                    stack.push(path);
                } else if file_type.is_file() {
                    let key = relative.to_string_lossy().replace('\\', "/");
                    if key.starts_with(prefix) {
                        keys.push(key);
                    }
                }
            }
        }
        keys.sort();
        let page_keys = keys
            .iter()
            .skip(start)
            .take(limit)
            .cloned()
            .collect::<Vec<_>>();
        let page_end = start.saturating_add(limit);
        let next_cursor = if page_end < keys.len() {
            Some(page_end.to_string())
        } else {
            None
        };
        let mut items = Vec::new();
        for key in page_keys {
            let key = BlobKey::new(key);
            let metadata = self.head(&key)?;
            items.push(BlobListItem { key, metadata });
        }
        Ok(ListPage { items, next_cursor })
    }

    fn path_for(&self, key: &BlobKey) -> Result<PathBuf, BlobStoreError> {
        let parts = self.key_parts(key)?;
        let mut path = self.root.clone();
        for part in parts {
            path.push(part);
        }
        self.confined_path(key, path, &self.root)
    }

    fn metadata_path_for(&self, key: &BlobKey) -> Result<PathBuf, BlobStoreError> {
        let parts = self.key_parts(key)?;
        let metadata_root = self.root.join(".graphblocks-metadata");
        let mut path = metadata_root.clone();
        for (index, part) in parts.iter().enumerate() {
            if index + 1 == parts.len() {
                path.push(format!("{part}.json"));
            } else {
                path.push(part);
            }
        }
        self.confined_path(key, path, &metadata_root)
    }

    fn confined_path(
        &self,
        key: &BlobKey,
        path: PathBuf,
        boundary: &Path,
    ) -> Result<PathBuf, BlobStoreError> {
        let resolved_boundary = boundary
            .canonicalize()
            .map_err(|error| BlobStoreError::Io {
                operation: "canonicalize blob path boundary".to_owned(),
                path: boundary.to_path_buf(),
                message: error.to_string(),
            })?;
        if !resolved_boundary.starts_with(&self.root) {
            return Err(BlobStoreError::InvalidKey {
                key: key.key.clone(),
            });
        }

        let mut existing_path = path.as_path();
        loop {
            match fs::symlink_metadata(existing_path) {
                Ok(_) => {
                    let resolved_path =
                        existing_path
                            .canonicalize()
                            .map_err(|_| BlobStoreError::InvalidKey {
                                key: key.key.clone(),
                            })?;
                    if !resolved_path.starts_with(&resolved_boundary) {
                        return Err(BlobStoreError::InvalidKey {
                            key: key.key.clone(),
                        });
                    }
                    return Ok(path);
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    existing_path =
                        existing_path
                            .parent()
                            .ok_or_else(|| BlobStoreError::InvalidKey {
                                key: key.key.clone(),
                            })?;
                }
                Err(error) => {
                    return Err(BlobStoreError::Io {
                        operation: "inspect blob path boundary".to_owned(),
                        path: existing_path.to_path_buf(),
                        message: error.to_string(),
                    });
                }
            }
        }
    }

    fn key_parts<'a>(&self, key: &'a BlobKey) -> Result<Vec<&'a str>, BlobStoreError> {
        if key.key.trim().is_empty() || key.key.starts_with('/') || key.key.contains('\\') {
            return Err(BlobStoreError::InvalidKey {
                key: key.key.clone(),
            });
        }
        let parts = key.key.split('/').collect::<Vec<_>>();
        if parts
            .first()
            .is_some_and(|part| part.eq_ignore_ascii_case(".graphblocks-metadata"))
            || parts
                .iter()
                .any(|part| part.trim().is_empty() || *part == "." || *part == "..")
        {
            return Err(BlobStoreError::InvalidKey {
                key: key.key.clone(),
            });
        }
        Ok(parts)
    }
}

fn validate_blob_key(key: &BlobKey) -> Result<(), BlobStoreError> {
    if key.key.trim().is_empty() || key.key.starts_with('/') || key.key.contains('\\') {
        return Err(BlobStoreError::InvalidKey {
            key: key.key.clone(),
        });
    }
    let parts = key.key.split('/').collect::<Vec<_>>();
    if parts
        .iter()
        .any(|part| part.trim().is_empty() || *part == "." || *part == "..")
    {
        return Err(BlobStoreError::InvalidKey {
            key: key.key.clone(),
        });
    }
    Ok(())
}

fn validate_blob_prefix(prefix: &str) -> Result<(), BlobStoreError> {
    if prefix.is_empty() {
        return Ok(());
    }
    if prefix.starts_with('/') || prefix.contains('\\') {
        return Err(BlobStoreError::InvalidKey {
            key: prefix.to_owned(),
        });
    }
    if prefix
        .split('/')
        .filter(|part| !part.is_empty())
        .any(|part| part == "." || part == "..")
    {
        return Err(BlobStoreError::InvalidKey {
            key: prefix.to_owned(),
        });
    }
    Ok(())
}

fn user_metadata(
    metadata: &BTreeMap<String, String>,
    key: &BlobKey,
) -> Result<BTreeMap<String, String>, BlobStoreError> {
    let mut normalized = BTreeMap::new();
    for (name, value) in metadata {
        let name = name.to_lowercase();
        if is_reserved_metadata_key(&name) {
            return Err(BlobStoreError::Metadata {
                key: key.key.clone(),
                message: format!("metadata key {name:?} is reserved"),
            });
        }
        normalized.insert(name, value.clone());
    }
    Ok(normalized)
}

fn normalize_metadata(metadata: BTreeMap<String, String>) -> BTreeMap<String, String> {
    metadata
        .into_iter()
        .map(|(name, value)| (name.to_lowercase(), value))
        .collect()
}

fn normalize_etag(etag: Option<String>) -> Option<String> {
    etag.map(|etag| etag.trim_matches('"').to_owned())
}

fn is_reserved_metadata_key(name: &str) -> bool {
    matches!(
        name,
        GRAPHBLOCKS_CHECKSUM_METADATA | GRAPHBLOCKS_FILENAME_METADATA
    )
}

fn range_header(byte_range: ByteRange) -> String {
    match byte_range.length {
        Some(length) => {
            let end = byte_range.offset.saturating_add(length).saturating_sub(1);
            format!("bytes={}-{}", byte_range.offset, end)
        }
        None => format!("bytes={}-", byte_range.offset),
    }
}

fn sha256_digest(data: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(data))
}

fn file_uri(path: &Path) -> String {
    format!("file://{}", path.to_string_lossy())
}

fn required_string(
    key: &BlobKey,
    payload: &Map<String, Value>,
    field: &str,
) -> Result<String, BlobStoreError> {
    payload
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| BlobStoreError::Metadata {
            key: key.key.clone(),
            message: format!("missing string field {field}"),
        })
}

fn optional_string(
    key: &BlobKey,
    payload: &Map<String, Value>,
    field: &str,
) -> Result<Option<String>, BlobStoreError> {
    let Some(value) = payload.get(field) else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_str()
        .map(str::to_owned)
        .map(Some)
        .ok_or_else(|| BlobStoreError::Metadata {
            key: key.key.clone(),
            message: format!("field {field} must be a string"),
        })
}

fn optional_usize(
    key: &BlobKey,
    payload: &Map<String, Value>,
    field: &str,
) -> Result<Option<usize>, BlobStoreError> {
    let Some(value) = payload.get(field) else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    let Some(value) = value.as_u64() else {
        return Err(BlobStoreError::Metadata {
            key: key.key.clone(),
            message: format!("field {field} must be an unsigned integer"),
        });
    };
    usize::try_from(value)
        .map(Some)
        .map_err(|error| BlobStoreError::Metadata {
            key: key.key.clone(),
            message: error.to_string(),
        })
}

fn optional_string_map(
    key: &BlobKey,
    payload: &Map<String, Value>,
    field: &str,
) -> Result<BTreeMap<String, String>, BlobStoreError> {
    let Some(value) = payload.get(field) else {
        return Ok(BTreeMap::new());
    };
    if value.is_null() {
        return Ok(BTreeMap::new());
    }
    let Some(values) = value.as_object() else {
        return Err(BlobStoreError::Metadata {
            key: key.key.clone(),
            message: format!("field {field} must be an object"),
        });
    };
    let mut output = BTreeMap::new();
    for (name, value) in values {
        let Some(value) = value.as_str() else {
            return Err(BlobStoreError::Metadata {
                key: key.key.clone(),
                message: format!("field {field}.{name} must be a string"),
            });
        };
        output.insert(name.clone(), value.to_owned());
    }
    Ok(output)
}

fn reject_unknown_fields(
    key: &BlobKey,
    payload: &Map<String, Value>,
    scope: &str,
    known_fields: &[&str],
) -> Result<(), BlobStoreError> {
    if let Some(field) = payload
        .keys()
        .find(|field| !known_fields.contains(&field.as_str()))
    {
        return Err(BlobStoreError::Metadata {
            key: key.key.clone(),
            message: format!("unknown {scope} field {field}"),
        });
    }
    Ok(())
}
