use std::collections::BTreeMap;

use serde_json::Value;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeliveryGuarantee {
    BestEffort,
    AtMostOnce,
    AtLeastOnce,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DurableError {
    InvalidDemand,
    DemandExceeded { demand: usize, actual: usize },
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct SourceCursor {
    pub stream: String,
    pub partition: u32,
    pub offset: u64,
}

impl SourceCursor {
    pub fn new(stream: impl Into<String>, partition: u32, offset: u64) -> Self {
        Self {
            stream: stream.into(),
            partition,
            offset,
        }
    }

    pub fn partition_key(&self) -> String {
        format!("{}:{}", self.stream, self.partition)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WatermarkKind {
    EventTime,
    ProcessingTime,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Watermark {
    pub kind: WatermarkKind,
    pub unix_ms: u64,
}

impl Watermark {
    pub fn event_time(unix_ms: u64) -> Self {
        Self {
            kind: WatermarkKind::EventTime,
            unix_ms,
        }
    }

    pub fn processing_time(unix_ms: u64) -> Self {
        Self {
            kind: WatermarkKind::ProcessingTime,
            unix_ms,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SourceEvent {
    pub cursor: SourceCursor,
    pub payload: Value,
    pub event_time_unix_ms: Option<u64>,
}

impl SourceEvent {
    pub fn new(cursor: SourceCursor, payload: Value, event_time_unix_ms: Option<u64>) -> Self {
        Self {
            cursor,
            payload,
            event_time_unix_ms,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SourceBatch {
    pub guarantee: DeliveryGuarantee,
    pub events: Vec<SourceEvent>,
    pub watermark: Option<Watermark>,
}

impl SourceBatch {
    pub fn new<I>(
        guarantee: DeliveryGuarantee,
        events: I,
        watermark: Option<Watermark>,
        demand: usize,
    ) -> Result<Self, DurableError>
    where
        I: IntoIterator<Item = SourceEvent>,
    {
        if demand == 0 {
            return Err(DurableError::InvalidDemand);
        }
        let events = events.into_iter().collect::<Vec<_>>();
        if events.len() > demand {
            return Err(DurableError::DemandExceeded {
                demand,
                actual: events.len(),
            });
        }
        Ok(Self {
            guarantee,
            events,
            watermark,
        })
    }

    pub fn high_cursor(&self) -> Option<&SourceCursor> {
        self.events.iter().map(|event| &event.cursor).max()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SchemaRef {
    pub schema_id: String,
    pub schema_version: u32,
}

impl SchemaRef {
    pub fn new(schema_id: impl Into<String>, schema_version: u32) -> Self {
        Self {
            schema_id: schema_id.into(),
            schema_version,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct CheckpointBarrier {
    pub checkpoint_id: String,
    pub run_id: String,
    pub release_id: String,
    pub deployment_revision_id: String,
    pub plan_hash: String,
    pub checkpoint_schema: SchemaRef,
    pub state_revision: u64,
    pub completed_nodes: Vec<String>,
    pub pending_nodes: Vec<String>,
    pub source_cursors: BTreeMap<String, SourceCursor>,
    pub operator_state: BTreeMap<String, Value>,
    pub sink_commit_metadata: BTreeMap<String, Value>,
    pub schema_versions: BTreeMap<String, u32>,
    pub created_at_unix_ms: u64,
}

impl CheckpointBarrier {
    pub fn validate(&self) -> Result<(), CheckpointBarrierError> {
        if self.checkpoint_id.is_empty() {
            return Err(CheckpointBarrierError::MissingCheckpointId);
        }
        if self.run_id.is_empty() {
            return Err(CheckpointBarrierError::MissingRunId);
        }
        if self.release_id.is_empty() {
            return Err(CheckpointBarrierError::MissingReleaseId);
        }
        if self.deployment_revision_id.is_empty() {
            return Err(CheckpointBarrierError::MissingDeploymentRevisionId);
        }
        if self.plan_hash.is_empty() {
            return Err(CheckpointBarrierError::MissingPlanHash);
        }
        if self.checkpoint_schema.schema_id.is_empty() || self.checkpoint_schema.schema_version == 0
        {
            return Err(CheckpointBarrierError::InvalidCheckpointSchema);
        }
        if self.schema_versions.is_empty() {
            return Err(CheckpointBarrierError::MissingSchemaVersions);
        }
        Ok(())
    }

    pub fn source_commit_plan(&self) -> SourceCursorCommitPlan {
        SourceCursorCommitPlan {
            cursors: self
                .source_cursors
                .iter()
                .map(|(source_id, cursor)| (source_id.clone(), cursor.clone()))
                .collect(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CheckpointBarrierError {
    MissingCheckpointId,
    MissingRunId,
    MissingReleaseId,
    MissingDeploymentRevisionId,
    MissingPlanHash,
    InvalidCheckpointSchema,
    MissingSchemaVersions,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceCursorCommitPlan {
    pub cursors: Vec<(String, SourceCursor)>,
}

#[derive(Default)]
pub struct InMemoryCheckpointStore {
    checkpoints_by_run: BTreeMap<String, Vec<CheckpointBarrier>>,
}

impl InMemoryCheckpointStore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn put(&mut self, barrier: CheckpointBarrier) -> Result<(), CheckpointStoreError> {
        barrier
            .validate()
            .map_err(CheckpointStoreError::InvalidBarrier)?;
        let checkpoints = self
            .checkpoints_by_run
            .entry(barrier.run_id.clone())
            .or_default();
        if let Some(current) = checkpoints
            .iter()
            .map(|checkpoint| checkpoint.state_revision)
            .max()
            && barrier.state_revision <= current
        {
            return Err(CheckpointStoreError::StaleStateRevision {
                run_id: barrier.run_id,
                current,
                attempted: barrier.state_revision,
            });
        }
        checkpoints.push(barrier);
        Ok(())
    }

    pub fn latest_compatible(
        &self,
        run_id: &str,
        release_id: &str,
        deployment_revision_id: &str,
        plan_hash: &str,
    ) -> Option<CheckpointBarrier> {
        self.checkpoints_by_run.get(run_id).and_then(|checkpoints| {
            checkpoints
                .iter()
                .filter(|checkpoint| {
                    checkpoint.release_id == release_id
                        && checkpoint.deployment_revision_id == deployment_revision_id
                        && checkpoint.plan_hash == plan_hash
                })
                .max_by_key(|checkpoint| checkpoint.state_revision)
                .cloned()
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CheckpointStoreError {
    InvalidBarrier(CheckpointBarrierError),
    StaleStateRevision {
        run_id: String,
        current: u64,
        attempted: u64,
    },
}
