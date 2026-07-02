use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

fn require_non_empty(field: &'static str, value: &str) -> Result<(), DurableStreamError> {
    if value.trim().is_empty() {
        return Err(DurableStreamError::EmptyField { field });
    }
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DurableStreamError {
    EmptyField { field: &'static str },
    InvalidOffset { field: &'static str },
}

impl fmt::Display for DurableStreamError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyField { field } => write!(formatter, "{field} must not be empty"),
            Self::InvalidOffset { field } => write!(formatter, "{field} must be valid"),
        }
    }
}

impl Error for DurableStreamError {}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct SourceCursor {
    pub partition: String,
    pub offset: u64,
}

impl SourceCursor {
    pub fn new(partition: impl Into<String>, offset: u64) -> Self {
        Self {
            partition: partition.into(),
            offset,
        }
    }

    pub fn validate(&self) -> Result<(), DurableStreamError> {
        require_non_empty("partition", &self.partition)?;
        Ok(())
    }

    pub fn commit_key(&self) -> String {
        format!("{}:{}", self.partition, self.offset)
    }

    fn canonical_value(&self) -> Value {
        json!({
            "partition": self.partition,
            "offset": self.offset,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SourceRecord {
    pub source_id: String,
    pub cursor: SourceCursor,
    pub event_time_ms: u64,
    pub payload: Value,
}

impl SourceRecord {
    pub fn new(
        source_id: impl Into<String>,
        cursor: SourceCursor,
        event_time_ms: u64,
        payload: Value,
    ) -> Result<Self, DurableStreamError> {
        let record = Self {
            source_id: source_id.into(),
            cursor,
            event_time_ms,
            payload,
        };
        record.validate()?;
        Ok(record)
    }

    pub fn validate(&self) -> Result<(), DurableStreamError> {
        require_non_empty("source_id", &self.source_id)?;
        self.cursor.validate()
    }

    pub fn replay_after(records: &[Self], cursor: &SourceCursor) -> Vec<Self> {
        records
            .iter()
            .filter(|record| {
                record.cursor.partition == cursor.partition && record.cursor.offset > cursor.offset
            })
            .cloned()
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StreamWatermark {
    pub stream_id: String,
    pub watermark_event_time_ms: u64,
    pub allowed_lateness_ms: u64,
}

impl StreamWatermark {
    pub fn new(
        stream_id: impl Into<String>,
        watermark_event_time_ms: u64,
        allowed_lateness_ms: u64,
    ) -> Result<Self, DurableStreamError> {
        let watermark = Self {
            stream_id: stream_id.into(),
            watermark_event_time_ms,
            allowed_lateness_ms,
        };
        watermark.validate()?;
        Ok(watermark)
    }

    pub fn validate(&self) -> Result<(), DurableStreamError> {
        require_non_empty("stream_id", &self.stream_id)?;
        Ok(())
    }

    pub fn min_allowed_event_time_ms(&self) -> u64 {
        self.watermark_event_time_ms
            .saturating_sub(self.allowed_lateness_ms)
    }

    pub fn accepts_event_time(&self, event_time_ms: u64) -> bool {
        event_time_ms >= self.min_allowed_event_time_ms()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CheckpointBarrier {
    pub checkpoint_id: String,
    pub plan_hash: String,
    pub barrier_sequence: u64,
    pub source_cursors: BTreeMap<String, Vec<SourceCursor>>,
    pub operator_state_digests: BTreeMap<String, String>,
    pub pending_effect_digests: BTreeMap<String, String>,
    pub sink_commit_digests: BTreeMap<String, String>,
    pub schema_versions: BTreeMap<String, u64>,
}

impl CheckpointBarrier {
    pub fn new(
        checkpoint_id: impl Into<String>,
        plan_hash: impl Into<String>,
        barrier_sequence: u64,
    ) -> Self {
        Self {
            checkpoint_id: checkpoint_id.into(),
            plan_hash: plan_hash.into(),
            barrier_sequence,
            source_cursors: BTreeMap::new(),
            operator_state_digests: BTreeMap::new(),
            pending_effect_digests: BTreeMap::new(),
            sink_commit_digests: BTreeMap::new(),
            schema_versions: BTreeMap::new(),
        }
    }

    pub fn with_source_cursor(
        mut self,
        source_id: impl Into<String>,
        cursor: SourceCursor,
    ) -> Self {
        self.source_cursors
            .entry(source_id.into())
            .or_default()
            .push(cursor);
        self
    }

    pub fn with_operator_state_digest(
        mut self,
        operator_id: impl Into<String>,
        digest: impl Into<String>,
    ) -> Self {
        self.operator_state_digests
            .insert(operator_id.into(), digest.into());
        self
    }

    pub fn with_pending_effect_digest(
        mut self,
        effect_journal_id: impl Into<String>,
        digest: impl Into<String>,
    ) -> Self {
        self.pending_effect_digests
            .insert(effect_journal_id.into(), digest.into());
        self
    }

    pub fn with_sink_commit_digest(
        mut self,
        sink_id: impl Into<String>,
        digest: impl Into<String>,
    ) -> Self {
        self.sink_commit_digests.insert(sink_id.into(), digest.into());
        self
    }

    pub fn with_schema_version(
        mut self,
        schema_id: impl Into<String>,
        version: u64,
    ) -> Self {
        self.schema_versions.insert(schema_id.into(), version);
        self
    }

    pub fn validate(&mut self) -> Result<(), DurableStreamError> {
        require_non_empty("checkpoint_id", &self.checkpoint_id)?;
        require_non_empty("plan_hash", &self.plan_hash)?;
        for (source_id, cursors) in &mut self.source_cursors {
            require_non_empty("source_id", source_id)?;
            for cursor in cursors.iter() {
                cursor.validate()?;
            }
            cursors.sort();
            cursors.dedup();
        }
        for key in self.operator_state_digests.keys() {
            require_non_empty("operator_id", key)?;
        }
        for value in self.operator_state_digests.values() {
            require_non_empty("operator_state_digest", value)?;
        }
        for key in self.pending_effect_digests.keys() {
            require_non_empty("effect_journal_id", key)?;
        }
        for value in self.pending_effect_digests.values() {
            require_non_empty("pending_effect_digest", value)?;
        }
        for key in self.sink_commit_digests.keys() {
            require_non_empty("sink_id", key)?;
        }
        for value in self.sink_commit_digests.values() {
            require_non_empty("sink_commit_digest", value)?;
        }
        for key in self.schema_versions.keys() {
            require_non_empty("schema_id", key)?;
        }
        for version in self.schema_versions.values() {
            if *version == 0 {
                return Err(DurableStreamError::InvalidOffset {
                    field: "schema_version",
                });
            }
        }
        Ok(())
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "plan_hash": self.plan_hash,
            "barrier_sequence": self.barrier_sequence,
            "source_cursors": self.source_cursors.iter().map(|(source_id, cursors)| {
                let mut cursors = cursors.clone();
                cursors.sort();
                cursors.dedup();
                (
                    source_id.clone(),
                    cursors.iter().map(SourceCursor::canonical_value).collect::<Vec<_>>(),
                )
            }).collect::<BTreeMap<_, _>>(),
            "operator_state_digests": self.operator_state_digests,
            "pending_effect_digests": self.pending_effect_digests,
            "sink_commit_digests": self.sink_commit_digests,
            "schema_versions": self.schema_versions,
        }))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeliveryGuarantee {
    BestEffort,
    AtMostOnce,
    AtLeastOnce,
}

impl DeliveryGuarantee {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BestEffort => "best_effort",
            Self::AtMostOnce => "at_most_once",
            Self::AtLeastOnce => "at_least_once",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SinkCommitRecord {
    pub commit_id: String,
    pub sink_id: String,
    pub idempotency_key: String,
    pub metadata: Value,
    pub metadata_digest: String,
}

impl SinkCommitRecord {
    pub fn new(
        commit_id: impl Into<String>,
        sink_id: impl Into<String>,
        idempotency_key: impl Into<String>,
        metadata: Value,
    ) -> Result<Self, SinkCommitError> {
        let metadata_digest = canonical_hash(&metadata);
        let record = Self {
            commit_id: commit_id.into(),
            sink_id: sink_id.into(),
            idempotency_key: idempotency_key.into(),
            metadata,
            metadata_digest,
        };
        record.validate()?;
        Ok(record)
    }

    fn validate(&self) -> Result<(), SinkCommitError> {
        if self.commit_id.trim().is_empty() {
            return Err(SinkCommitError::EmptyField { field: "commit_id" });
        }
        if self.sink_id.trim().is_empty() {
            return Err(SinkCommitError::EmptyField { field: "sink_id" });
        }
        if self.idempotency_key.trim().is_empty() {
            return Err(SinkCommitError::EmptyField {
                field: "idempotency_key",
            });
        }
        if self.metadata_digest != canonical_hash(&self.metadata) {
            return Err(SinkCommitError::MetadataDigestMismatch {
                sink_id: self.sink_id.clone(),
                idempotency_key: self.idempotency_key.clone(),
            });
        }
        Ok(())
    }

    fn canonical_value(&self) -> Value {
        json!({
            "sink_id": self.sink_id,
            "idempotency_key": self.idempotency_key,
            "metadata_digest": self.metadata_digest,
            "metadata": self.metadata,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SinkCommitOutcome {
    pub committed: bool,
    pub duplicate: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SinkCommitError {
    EmptyField {
        field: &'static str,
    },
    SinkMismatch {
        expected: String,
        actual: String,
    },
    IdempotencyConflict {
        sink_id: String,
        idempotency_key: String,
    },
    MetadataDigestMismatch {
        sink_id: String,
        idempotency_key: String,
    },
}

impl fmt::Display for SinkCommitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyField { field } => write!(formatter, "{field} must not be empty"),
            Self::SinkMismatch { expected, actual } => {
                write!(formatter, "sink mismatch: expected {expected:?}, got {actual:?}")
            }
            Self::IdempotencyConflict {
                sink_id,
                idempotency_key,
            } => write!(
                formatter,
                "sink {sink_id:?} idempotency key {idempotency_key:?} has conflicting metadata"
            ),
            Self::MetadataDigestMismatch {
                sink_id,
                idempotency_key,
            } => write!(
                formatter,
                "sink {sink_id:?} idempotency key {idempotency_key:?} metadata digest mismatch"
            ),
        }
    }
}

impl Error for SinkCommitError {}

#[derive(Clone, Debug, PartialEq)]
pub struct SinkCommitLog {
    pub sink_id: String,
    pub delivery_guarantee: DeliveryGuarantee,
    records: BTreeMap<String, SinkCommitRecord>,
}

impl SinkCommitLog {
    pub fn new(
        sink_id: impl Into<String>,
        delivery_guarantee: DeliveryGuarantee,
    ) -> Result<Self, SinkCommitError> {
        let log = Self {
            sink_id: sink_id.into(),
            delivery_guarantee,
            records: BTreeMap::new(),
        };
        if log.sink_id.trim().is_empty() {
            return Err(SinkCommitError::EmptyField { field: "sink_id" });
        }
        Ok(log)
    }

    pub fn commit(
        &self,
        record: SinkCommitRecord,
    ) -> Result<(Self, SinkCommitOutcome), SinkCommitError> {
        record.validate()?;
        if record.sink_id != self.sink_id {
            return Err(SinkCommitError::SinkMismatch {
                expected: self.sink_id.clone(),
                actual: record.sink_id,
            });
        }
        if let Some(existing) = self.records.get(&record.idempotency_key) {
            if existing.metadata_digest == record.metadata_digest {
                return Ok((
                    self.clone(),
                    SinkCommitOutcome {
                        committed: false,
                        duplicate: true,
                    },
                ));
            }
            return Err(SinkCommitError::IdempotencyConflict {
                sink_id: record.sink_id,
                idempotency_key: record.idempotency_key,
            });
        }
        let mut committed = self.clone();
        committed
            .records
            .insert(record.idempotency_key.clone(), record);
        Ok((
            committed,
            SinkCommitOutcome {
                committed: true,
                duplicate: false,
            },
        ))
    }

    pub fn records(&self) -> Vec<&SinkCommitRecord> {
        self.records.values().collect()
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "sink_id": self.sink_id,
            "delivery_guarantee": self.delivery_guarantee.as_str(),
            "records": self.records.values().map(SinkCommitRecord::canonical_value).collect::<Vec<_>>(),
        }))
    }
}
