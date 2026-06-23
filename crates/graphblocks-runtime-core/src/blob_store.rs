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

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum BlobStoreError {
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
        let metadata_path = self.metadata_path_for(key)?;
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
        let path = self.path_for(key)?;
        if !path.exists() {
            return Err(BlobStoreError::NotFound {
                key: key.key.clone(),
            });
        }
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
            let artifact_payload = payload
                .get("artifact")
                .and_then(Value::as_object)
                .ok_or_else(|| BlobStoreError::Metadata {
                    key: key.key.clone(),
                    message: "missing artifact object".to_owned(),
                })?;
            let artifact = ArtifactRef {
                artifact_id: required_string(key, artifact_payload, "artifact_id")?,
                uri: required_string(key, artifact_payload, "uri")?,
                media_type: optional_string(artifact_payload, "media_type"),
                size_bytes: optional_usize(key, artifact_payload, "size_bytes")?,
                checksum: optional_string(artifact_payload, "checksum"),
                etag: optional_string(artifact_payload, "etag"),
                version: optional_string(artifact_payload, "version"),
                filename: optional_string(artifact_payload, "filename"),
                metadata: optional_string_map(key, artifact_payload, "metadata")?,
            };
            return Ok(BlobMetadata {
                key: key.clone(),
                artifact,
                etag: payload
                    .get("etag")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            });
        }
        let data = fs::read(&path).map_err(|error| BlobStoreError::Io {
            operation: "read blob".to_owned(),
            path: path.clone(),
            message: error.to_string(),
        })?;
        let checksum = sha256_digest(&data);
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
            Some(cursor) => cursor
                .parse::<usize>()
                .map_err(|_| BlobStoreError::InvalidCursor {
                    cursor: cursor.to_owned(),
                })?,
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
        let next_cursor = if start + limit < keys.len() {
            Some((start + limit).to_string())
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
        Ok(path)
    }

    fn metadata_path_for(&self, key: &BlobKey) -> Result<PathBuf, BlobStoreError> {
        let parts = self.key_parts(key)?;
        let mut path = self.root.join(".graphblocks-metadata");
        for (index, part) in parts.iter().enumerate() {
            if index + 1 == parts.len() {
                path.push(format!("{part}.json"));
            } else {
                path.push(part);
            }
        }
        Ok(path)
    }

    fn key_parts<'a>(&self, key: &'a BlobKey) -> Result<Vec<&'a str>, BlobStoreError> {
        if key.key.is_empty() || key.key.starts_with('/') || key.key.contains('\\') {
            return Err(BlobStoreError::InvalidKey {
                key: key.key.clone(),
            });
        }
        let parts = key.key.split('/').collect::<Vec<_>>();
        if parts
            .iter()
            .any(|part| part.is_empty() || *part == "." || *part == "..")
        {
            return Err(BlobStoreError::InvalidKey {
                key: key.key.clone(),
            });
        }
        Ok(parts)
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

fn optional_string(payload: &Map<String, Value>, field: &str) -> Option<String> {
    payload
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
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
