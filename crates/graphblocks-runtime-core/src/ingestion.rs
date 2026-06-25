use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use serde_json::Value;

use crate::documents::{ArtifactRef, AssetRevision, SourceAsset};
use crate::outcome::BlockError;

#[derive(Clone, Debug, PartialEq)]
pub struct ProcessorRef {
    pub processor_id: String,
    pub version: String,
    pub config_digest: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl ProcessorRef {
    pub fn new(processor_id: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            processor_id: processor_id.into(),
            version: version.into(),
            config_digest: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_config_digest(mut self, config_digest: impl Into<String>) -> Self {
        self.config_digest = Some(config_digest.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct IndexRecordRef {
    pub index_id: String,
    pub record_id: String,
    pub asset_id: String,
    pub revision_id: String,
    pub chunk_ids: Vec<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl IndexRecordRef {
    pub fn new(
        index_id: impl Into<String>,
        record_id: impl Into<String>,
        asset_id: impl Into<String>,
        revision_id: impl Into<String>,
    ) -> Self {
        Self {
            index_id: index_id.into(),
            record_id: record_id.into(),
            asset_id: asset_id.into(),
            revision_id: revision_id.into(),
            chunk_ids: Vec::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_chunk_ids<I, S>(mut self, chunk_ids: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.chunk_ids = chunk_ids.into_iter().map(Into::into).collect();
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum IngestionStatus {
    Discovered,
    Processing,
    Ready,
    Failed,
    Superseded,
    Deleted,
}

#[derive(Clone, Debug, PartialEq)]
pub struct IngestionManifest {
    pub manifest_id: String,
    pub asset_id: String,
    pub revision_id: String,
    pub source_uri: String,
    pub content_hash: String,
    pub parser: ProcessorRef,
    pub ocr: Option<ProcessorRef>,
    pub normalizers: Vec<ProcessorRef>,
    pub chunker: ProcessorRef,
    pub embedding: Option<ProcessorRef>,
    pub parsed_document_ref: Option<ArtifactRef>,
    pub chunk_set_ref: Option<ArtifactRef>,
    pub index_records: Vec<IndexRecordRef>,
    pub acl_revision: Option<String>,
    pub pipeline_hash: String,
    pub status: IngestionStatus,
    pub error: Option<BlockError>,
    pub created_at: String,
    pub updated_at: String,
    pub metadata: BTreeMap<String, Value>,
}

impl IngestionManifest {
    pub fn new(
        manifest_id: impl Into<String>,
        asset: &SourceAsset,
        revision: &AssetRevision,
        parser: ProcessorRef,
        chunker: ProcessorRef,
        pipeline_hash: impl Into<String>,
        created_at: impl Into<String>,
    ) -> Self {
        let created_at = created_at.into();
        Self {
            manifest_id: manifest_id.into(),
            asset_id: asset.asset_id.clone(),
            revision_id: revision.revision_id.clone(),
            source_uri: asset.source_uri.clone(),
            content_hash: revision.content_hash.clone(),
            parser,
            ocr: None,
            normalizers: Vec::new(),
            chunker,
            embedding: None,
            parsed_document_ref: None,
            chunk_set_ref: None,
            index_records: Vec::new(),
            acl_revision: None,
            pipeline_hash: pipeline_hash.into(),
            status: IngestionStatus::Discovered,
            error: None,
            created_at: created_at.clone(),
            updated_at: created_at,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_ocr(mut self, ocr: ProcessorRef) -> Self {
        self.ocr = Some(ocr);
        self
    }

    pub fn with_embedding(mut self, embedding: ProcessorRef) -> Self {
        self.embedding = Some(embedding);
        self
    }

    pub fn with_normalizers<I>(mut self, normalizers: I) -> Self
    where
        I: IntoIterator<Item = ProcessorRef>,
    {
        self.normalizers = normalizers.into_iter().collect();
        self
    }

    pub fn with_acl_revision(mut self, acl_revision: impl Into<String>) -> Self {
        self.acl_revision = Some(acl_revision.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum IngestionError {
    DuplicateManifest {
        manifest_id: String,
    },
    ManifestNotFound {
        manifest_id: String,
    },
    InvalidTransition {
        manifest_id: String,
        from: IngestionStatus,
        to: IngestionStatus,
    },
}

impl fmt::Display for IngestionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateManifest { manifest_id } => {
                write!(
                    formatter,
                    "ingestion manifest {manifest_id:?} already exists"
                )
            }
            Self::ManifestNotFound { manifest_id } => {
                write!(
                    formatter,
                    "ingestion manifest {manifest_id:?} was not found"
                )
            }
            Self::InvalidTransition {
                manifest_id,
                from,
                to,
            } => write!(
                formatter,
                "ingestion manifest {manifest_id:?} cannot transition from {from:?} to {to:?}"
            ),
        }
    }
}

impl Error for IngestionError {}

#[derive(Clone, Debug, Default)]
pub struct InMemoryIngestionManifestStore {
    manifests: BTreeMap<String, IngestionManifest>,
    current_by_asset: BTreeMap<String, String>,
}

impl InMemoryIngestionManifestStore {
    pub fn new() -> Self {
        Self {
            manifests: BTreeMap::new(),
            current_by_asset: BTreeMap::new(),
        }
    }

    pub fn create_processing(
        &mut self,
        mut manifest: IngestionManifest,
        updated_at: impl Into<String>,
    ) -> Result<IngestionManifest, IngestionError> {
        if self.manifests.contains_key(&manifest.manifest_id) {
            return Err(IngestionError::DuplicateManifest {
                manifest_id: manifest.manifest_id,
            });
        }
        manifest.status = IngestionStatus::Processing;
        manifest.updated_at = updated_at.into();
        self.manifests
            .insert(manifest.manifest_id.clone(), manifest.clone());
        Ok(manifest)
    }

    pub fn commit(
        &mut self,
        manifest_id: &str,
        parsed_document_ref: Option<ArtifactRef>,
        chunk_set_ref: Option<ArtifactRef>,
        index_records: Vec<IndexRecordRef>,
        updated_at: impl Into<String>,
    ) -> Result<IngestionManifest, IngestionError> {
        let updated_at = updated_at.into();
        let (asset_id, previous_current) = {
            let Some(manifest) = self.manifests.get_mut(manifest_id) else {
                return Err(IngestionError::ManifestNotFound {
                    manifest_id: manifest_id.to_owned(),
                });
            };
            match manifest.status {
                IngestionStatus::Ready => return Ok(manifest.clone()),
                IngestionStatus::Discovered | IngestionStatus::Processing => {}
                _ => {
                    return Err(IngestionError::InvalidTransition {
                        manifest_id: manifest_id.to_owned(),
                        from: manifest.status.clone(),
                        to: IngestionStatus::Ready,
                    });
                }
            }
            manifest.parsed_document_ref = parsed_document_ref;
            manifest.chunk_set_ref = chunk_set_ref;
            manifest.index_records = index_records;
            manifest.status = IngestionStatus::Ready;
            manifest.error = None;
            manifest.updated_at = updated_at.clone();
            (
                manifest.asset_id.clone(),
                self.current_by_asset.get(&manifest.asset_id).cloned(),
            )
        };

        self.current_by_asset
            .insert(asset_id.clone(), manifest_id.to_owned());
        if let Some(previous_manifest_id) = previous_current
            && previous_manifest_id != manifest_id
            && let Some(previous) = self.manifests.get_mut(&previous_manifest_id)
            && previous.status == IngestionStatus::Ready
        {
            previous.status = IngestionStatus::Superseded;
            previous.updated_at = updated_at;
        }
        Ok(self.manifests[manifest_id].clone())
    }

    pub fn fail(
        &mut self,
        manifest_id: &str,
        error: BlockError,
        updated_at: impl Into<String>,
    ) -> Result<IngestionManifest, IngestionError> {
        let Some(manifest) = self.manifests.get_mut(manifest_id) else {
            return Err(IngestionError::ManifestNotFound {
                manifest_id: manifest_id.to_owned(),
            });
        };
        if matches!(
            manifest.status,
            IngestionStatus::Ready | IngestionStatus::Superseded | IngestionStatus::Deleted
        ) {
            return Err(IngestionError::InvalidTransition {
                manifest_id: manifest_id.to_owned(),
                from: manifest.status.clone(),
                to: IngestionStatus::Failed,
            });
        }
        manifest.status = IngestionStatus::Failed;
        manifest.error = Some(error);
        manifest.updated_at = updated_at.into();
        Ok(manifest.clone())
    }

    pub fn tombstone(
        &mut self,
        manifest_id: &str,
        updated_at: impl Into<String>,
    ) -> Result<IngestionManifest, IngestionError> {
        let Some(manifest) = self.manifests.get_mut(manifest_id) else {
            return Err(IngestionError::ManifestNotFound {
                manifest_id: manifest_id.to_owned(),
            });
        };
        if manifest.status == IngestionStatus::Deleted {
            return Ok(manifest.clone());
        }
        manifest.status = IngestionStatus::Deleted;
        manifest.updated_at = updated_at.into();
        let asset_id = manifest.asset_id.clone();
        if self
            .current_by_asset
            .get(&asset_id)
            .is_some_and(|current| current == manifest_id)
        {
            self.current_by_asset.remove(&asset_id);
        }
        Ok(manifest.clone())
    }

    pub fn get(&self, manifest_id: &str) -> Option<&IngestionManifest> {
        self.manifests.get(manifest_id)
    }

    pub fn current_for_asset(&self, asset_id: &str) -> Option<&IngestionManifest> {
        self.current_by_asset
            .get(asset_id)
            .and_then(|manifest_id| self.manifests.get(manifest_id))
    }

    pub fn list_by_status(&self, status: IngestionStatus) -> Vec<&IngestionManifest> {
        self.manifests
            .values()
            .filter(|manifest| manifest.status == status)
            .collect()
    }
}
