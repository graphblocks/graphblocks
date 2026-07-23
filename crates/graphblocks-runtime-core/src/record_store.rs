use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

#[derive(Clone, Debug, PartialEq)]
pub struct Record {
    pub collection: String,
    pub key: String,
    pub value: Value,
    pub revision: u64,
    pub etag: String,
    pub metadata: BTreeMap<String, Value>,
}

impl Record {
    pub fn new(key: impl Into<String>, value: Value) -> Self {
        Self {
            collection: String::new(),
            key: key.into(),
            value,
            revision: 0,
            etag: String::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WriteOptions {
    pub expected_revision: Option<u64>,
    pub create_only: bool,
}

impl WriteOptions {
    pub fn new() -> Self {
        Self {
            expected_revision: None,
            create_only: false,
        }
    }

    pub fn create_only() -> Self {
        Self {
            expected_revision: None,
            create_only: true,
        }
    }

    pub fn with_expected_revision(mut self, expected_revision: u64) -> Self {
        self.expected_revision = Some(expected_revision);
        self
    }
}

impl Default for WriteOptions {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeleteOptions {
    pub expected_revision: Option<u64>,
    pub allow_missing: bool,
}

impl DeleteOptions {
    pub fn new() -> Self {
        Self {
            expected_revision: None,
            allow_missing: false,
        }
    }

    pub fn with_expected_revision(mut self, expected_revision: u64) -> Self {
        self.expected_revision = Some(expected_revision);
        self
    }

    pub fn with_allow_missing(mut self, allow_missing: bool) -> Self {
        self.allow_missing = allow_missing;
        self
    }
}

impl Default for DeleteOptions {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RecordFilter {
    pub path: Vec<String>,
    pub value: Value,
}

impl RecordFilter {
    pub fn equals<I, S>(path: I, value: Value) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self {
            path: path.into_iter().map(Into::into).collect(),
            value,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RecordQuery {
    pub collection: String,
    pub filters: Vec<RecordFilter>,
    pub limit: usize,
    pub cursor: Option<String>,
}

impl RecordQuery {
    pub fn new(collection: impl Into<String>) -> Self {
        Self {
            collection: collection.into(),
            filters: Vec::new(),
            limit: 100,
            cursor: None,
        }
    }

    pub fn with_filter(mut self, filter: RecordFilter) -> Self {
        self.filters.push(filter);
        self
    }

    pub fn with_limit(mut self, limit: usize) -> Self {
        self.limit = limit;
        self
    }

    pub fn with_cursor(mut self, cursor: impl Into<String>) -> Self {
        self.cursor = Some(cursor.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RecordPage {
    pub records: Vec<Record>,
    pub next_cursor: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RecordStoreError {
    InvalidCollection,
    InvalidKey,
    AlreadyExists {
        collection: String,
        key: String,
    },
    NotFound {
        collection: String,
        key: String,
    },
    RevisionConflict {
        collection: String,
        key: String,
        expected_revision: u64,
        current_revision: u64,
    },
    RevisionOverflow {
        collection: String,
        key: String,
    },
    InvalidLimit,
    InvalidCursor {
        collection: String,
        cursor: String,
    },
}

impl fmt::Display for RecordStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidCollection => write!(formatter, "record collection must not be empty"),
            Self::InvalidKey => write!(formatter, "record key must not be empty"),
            Self::AlreadyExists { collection, key } => {
                write!(formatter, "record {collection:?}/{key:?} already exists")
            }
            Self::NotFound { collection, key } => {
                write!(formatter, "record {collection:?}/{key:?} does not exist")
            }
            Self::RevisionConflict {
                collection,
                key,
                expected_revision,
                current_revision,
            } => write!(
                formatter,
                "record {collection:?}/{key:?} revision conflict: expected {expected_revision}, current {current_revision}"
            ),
            Self::RevisionOverflow { collection, key } => write!(
                formatter,
                "record {collection:?}/{key:?} revision is exhausted"
            ),
            Self::InvalidLimit => write!(formatter, "record query limit must be at least 1"),
            Self::InvalidCursor { collection, cursor } => {
                write!(
                    formatter,
                    "record query cursor {cursor:?} is invalid for {collection:?}"
                )
            }
        }
    }
}

impl Error for RecordStoreError {}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct InMemoryRecordStore {
    records: BTreeMap<(String, String), Record>,
    last_revisions: BTreeMap<(String, String), u64>,
}

impl InMemoryRecordStore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn get(&self, collection: &str, key: &str) -> Result<Option<Record>, RecordStoreError> {
        if collection.trim().is_empty() {
            return Err(RecordStoreError::InvalidCollection);
        }
        if key.trim().is_empty() {
            return Err(RecordStoreError::InvalidKey);
        }
        Ok(self
            .records
            .get(&(collection.to_owned(), key.to_owned()))
            .cloned())
    }

    pub fn put(
        &mut self,
        collection: &str,
        record: Record,
        options: WriteOptions,
    ) -> Result<Record, RecordStoreError> {
        if collection.trim().is_empty() {
            return Err(RecordStoreError::InvalidCollection);
        }
        if record.key.trim().is_empty() {
            return Err(RecordStoreError::InvalidKey);
        }
        let storage_key = (collection.to_owned(), record.key.clone());
        let current_revision = self
            .records
            .get(&storage_key)
            .map(|current| current.revision)
            .unwrap_or(0);
        if options.create_only && current_revision != 0 {
            return Err(RecordStoreError::AlreadyExists {
                collection: collection.to_owned(),
                key: record.key,
            });
        }
        if let Some(expected_revision) = options.expected_revision
            && expected_revision != current_revision
        {
            return Err(RecordStoreError::RevisionConflict {
                collection: collection.to_owned(),
                key: record.key,
                expected_revision,
                current_revision,
            });
        }

        let revision = self
            .last_revisions
            .get(&storage_key)
            .copied()
            .unwrap_or(current_revision)
            .checked_add(1)
            .ok_or_else(|| RecordStoreError::RevisionOverflow {
                collection: collection.to_owned(),
                key: record.key.clone(),
            })?;
        let mut stored = record;
        stored.collection = collection.to_owned();
        stored.revision = revision;
        stored.etag = canonical_hash(&json!({
            "collection": stored.collection,
            "key": stored.key,
            "value": stored.value,
            "revision": stored.revision,
            "metadata": stored.metadata,
        }));
        self.last_revisions.insert(storage_key.clone(), revision);
        self.records.insert(storage_key, stored.clone());
        Ok(stored)
    }

    pub fn query(&self, request: RecordQuery) -> Result<RecordPage, RecordStoreError> {
        if request.collection.trim().is_empty() {
            return Err(RecordStoreError::InvalidCollection);
        }
        if request.limit == 0 {
            return Err(RecordStoreError::InvalidLimit);
        }
        if let Some(cursor) = &request.cursor
            && !self
                .records
                .contains_key(&(request.collection.clone(), cursor.clone()))
        {
            return Err(RecordStoreError::InvalidCursor {
                collection: request.collection,
                cursor: cursor.clone(),
            });
        }

        let mut matches = Vec::new();
        'records: for ((collection, key), record) in &self.records {
            if collection != &request.collection {
                continue;
            }
            if request.cursor.as_ref().is_some_and(|cursor| key <= cursor) {
                continue;
            }
            for filter in &request.filters {
                let mut cursor = &record.value;
                for segment in &filter.path {
                    let Value::Object(object) = cursor else {
                        continue 'records;
                    };
                    let Some(next) = object.get(segment) else {
                        continue 'records;
                    };
                    cursor = next;
                }
                if cursor != &filter.value {
                    continue 'records;
                }
            }
            matches.push(record.clone());
        }

        let next_cursor = if matches.len() > request.limit {
            Some(matches[request.limit - 1].key.clone())
        } else {
            None
        };
        matches.truncate(request.limit);
        Ok(RecordPage {
            records: matches,
            next_cursor,
        })
    }

    pub fn delete(
        &mut self,
        collection: &str,
        key: &str,
        options: DeleteOptions,
    ) -> Result<(), RecordStoreError> {
        if collection.trim().is_empty() {
            return Err(RecordStoreError::InvalidCollection);
        }
        if key.trim().is_empty() {
            return Err(RecordStoreError::InvalidKey);
        }
        let storage_key = (collection.to_owned(), key.to_owned());
        let Some(current) = self.records.get(&storage_key) else {
            return if options.allow_missing {
                Ok(())
            } else {
                Err(RecordStoreError::NotFound {
                    collection: collection.to_owned(),
                    key: key.to_owned(),
                })
            };
        };
        if let Some(expected_revision) = options.expected_revision
            && expected_revision != current.revision
        {
            return Err(RecordStoreError::RevisionConflict {
                collection: collection.to_owned(),
                key: key.to_owned(),
                expected_revision,
                current_revision: current.revision,
            });
        }
        self.records.remove(&storage_key);
        Ok(())
    }
}
