use std::collections::{BTreeMap, BTreeSet};

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
    DemandExceeded {
        demand: usize,
        actual: usize,
    },
    SourcePaused,
    StaleCommit {
        current: SourceCursor,
        attempted: SourceCursor,
    },
    UnknownSourceCursor {
        cursor: SourceCursor,
    },
    InvalidWindowSize,
    LateEvent {
        event_time_unix_ms: u64,
        watermark_unix_ms: u64,
        allowed_lateness_ms: u64,
    },
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

pub struct InMemoryDurableSource {
    guarantee: DeliveryGuarantee,
    events: Vec<SourceEvent>,
    known_streams: BTreeSet<String>,
    committed_cursor: Option<SourceCursor>,
    paused: bool,
}

impl InMemoryDurableSource {
    pub fn new<I>(guarantee: DeliveryGuarantee, events: I) -> Self
    where
        I: IntoIterator<Item = SourceEvent>,
    {
        let mut events = events.into_iter().collect::<Vec<_>>();
        events.sort_by(|left, right| left.cursor.cmp(&right.cursor));
        let known_streams = events
            .iter()
            .map(|event| event.cursor.stream.clone())
            .collect::<BTreeSet<_>>();
        Self {
            guarantee,
            events,
            known_streams,
            committed_cursor: None,
            paused: false,
        }
    }

    pub fn poll(
        &self,
        cursor: Option<SourceCursor>,
        demand: usize,
    ) -> Result<SourceBatch, DurableError> {
        if self.paused {
            return Err(DurableError::SourcePaused);
        }
        if let Some(cursor) = cursor.as_ref() {
            self.validate_cursor(cursor)?;
        }
        let replay_cursor = cursor.as_ref().or(self.committed_cursor.as_ref());
        let events = self
            .events
            .iter()
            .filter(|event| replay_cursor.is_none_or(|cursor| event.cursor > *cursor))
            .take(demand)
            .cloned()
            .collect::<Vec<_>>();
        let watermark = events
            .iter()
            .filter_map(|event| event.event_time_unix_ms)
            .max()
            .map(Watermark::event_time);
        SourceBatch::new(self.guarantee, events, watermark, demand)
    }

    pub fn commit(&mut self, cursor: SourceCursor) -> Result<(), DurableError> {
        self.validate_cursor(&cursor)?;
        if let Some(current) = &self.committed_cursor
            && cursor < *current
        {
            return Err(DurableError::StaleCommit {
                current: current.clone(),
                attempted: cursor,
            });
        }
        self.committed_cursor = Some(cursor);
        Ok(())
    }

    pub fn pause(&mut self) {
        self.paused = true;
    }

    pub fn resume(&mut self) {
        self.paused = false;
    }

    fn validate_cursor(&self, cursor: &SourceCursor) -> Result<(), DurableError> {
        if !self.known_streams.is_empty() && !self.known_streams.contains(&cursor.stream) {
            return Err(DurableError::UnknownSourceCursor {
                cursor: cursor.clone(),
            });
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AccumulationMode {
    Discarding,
    Accumulating,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct WindowPolicy {
    pub size_ms: u64,
    pub allowed_lateness_ms: u64,
    pub accumulation_mode: AccumulationMode,
}

impl WindowPolicy {
    pub fn tumbling_event_time(
        size_ms: u64,
        allowed_lateness_ms: u64,
        accumulation_mode: AccumulationMode,
    ) -> Result<Self, DurableError> {
        if size_ms == 0 {
            return Err(DurableError::InvalidWindowSize);
        }
        Ok(Self {
            size_ms,
            allowed_lateness_ms,
            accumulation_mode,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WindowPane {
    pub start_unix_ms: u64,
    pub end_unix_ms: u64,
    pub events: Vec<SourceEvent>,
}

pub struct WindowAccumulator {
    policy: WindowPolicy,
    watermark: Option<Watermark>,
    windows: BTreeMap<u64, Vec<SourceEvent>>,
}

impl WindowAccumulator {
    pub fn new(policy: WindowPolicy) -> Self {
        Self {
            policy,
            watermark: None,
            windows: BTreeMap::new(),
        }
    }

    pub fn ingest(&mut self, event: SourceEvent) -> Result<(), DurableError> {
        let event_time_unix_ms = event.event_time_unix_ms.unwrap_or(0);
        if let Some(watermark) = self.watermark
            && event_time_unix_ms.saturating_add(self.policy.allowed_lateness_ms)
                < watermark.unix_ms
        {
            return Err(DurableError::LateEvent {
                event_time_unix_ms,
                watermark_unix_ms: watermark.unix_ms,
                allowed_lateness_ms: self.policy.allowed_lateness_ms,
            });
        }
        let start_unix_ms = event_time_unix_ms - (event_time_unix_ms % self.policy.size_ms);
        self.windows.entry(start_unix_ms).or_default().push(event);
        Ok(())
    }

    pub fn advance_watermark(&mut self, watermark: Watermark) -> Vec<WindowPane> {
        self.watermark = Some(watermark);
        let closable = self
            .windows
            .keys()
            .copied()
            .filter(|start_unix_ms| {
                start_unix_ms
                    .saturating_add(self.policy.size_ms)
                    .saturating_add(self.policy.allowed_lateness_ms)
                    <= watermark.unix_ms
            })
            .collect::<Vec<_>>();
        let mut closed = Vec::new();
        for start_unix_ms in closable {
            if let Some(events) = self.windows.remove(&start_unix_ms) {
                closed.push(WindowPane {
                    start_unix_ms,
                    end_unix_ms: start_unix_ms.saturating_add(self.policy.size_ms),
                    events,
                });
            }
        }
        closed
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SinkCommitRequest {
    pub run_id: String,
    pub node_id: String,
    pub node_attempt_id: String,
    pub idempotency_key: String,
    pub precondition_digest: Option<String>,
    pub payload: Value,
}

impl SinkCommitRequest {
    pub fn new(
        run_id: impl Into<String>,
        node_id: impl Into<String>,
        node_attempt_id: impl Into<String>,
        idempotency_key: impl Into<String>,
        payload: Value,
    ) -> Self {
        Self {
            run_id: run_id.into(),
            node_id: node_id.into(),
            node_attempt_id: node_attempt_id.into(),
            idempotency_key: idempotency_key.into(),
            precondition_digest: None,
            payload,
        }
    }

    pub fn with_precondition_digest(mut self, precondition_digest: impl Into<String>) -> Self {
        self.precondition_digest = Some(precondition_digest.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SinkCommitResult {
    pub sink_id: String,
    pub idempotency_key: String,
    pub precondition_digest: Option<String>,
    pub sequence: u64,
    pub metadata: Value,
    pub replayed: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SinkCommitError {
    MissingRunId,
    MissingNodeId,
    MissingNodeAttemptId,
    MissingIdempotencyKey,
    IdempotencyConflict { idempotency_key: String },
}

struct SinkCommitRecord {
    request: SinkCommitRequest,
    result: SinkCommitResult,
}

#[derive(Default)]
pub struct InMemoryDurableSink {
    sink_id: String,
    next_sequence: u64,
    commits_by_idempotency_key: BTreeMap<String, SinkCommitRecord>,
}

impl InMemoryDurableSink {
    pub fn new(sink_id: impl Into<String>) -> Self {
        Self {
            sink_id: sink_id.into(),
            next_sequence: 1,
            commits_by_idempotency_key: BTreeMap::new(),
        }
    }

    pub fn commit(
        &mut self,
        request: SinkCommitRequest,
    ) -> Result<SinkCommitResult, SinkCommitError> {
        if request.run_id.is_empty() {
            return Err(SinkCommitError::MissingRunId);
        }
        if request.node_id.is_empty() {
            return Err(SinkCommitError::MissingNodeId);
        }
        if request.node_attempt_id.is_empty() {
            return Err(SinkCommitError::MissingNodeAttemptId);
        }
        if request.idempotency_key.is_empty() {
            return Err(SinkCommitError::MissingIdempotencyKey);
        }
        if let Some(record) = self
            .commits_by_idempotency_key
            .get(&request.idempotency_key)
        {
            if record.request != request {
                return Err(SinkCommitError::IdempotencyConflict {
                    idempotency_key: request.idempotency_key,
                });
            }
            let mut result = record.result.clone();
            result.replayed = true;
            return Ok(result);
        }

        let result = SinkCommitResult {
            sink_id: self.sink_id.clone(),
            idempotency_key: request.idempotency_key.clone(),
            precondition_digest: request.precondition_digest.clone(),
            sequence: self.next_sequence,
            metadata: request.payload.clone(),
            replayed: false,
        };
        self.next_sequence = self.next_sequence.saturating_add(1);
        self.commits_by_idempotency_key.insert(
            request.idempotency_key.clone(),
            SinkCommitRecord {
                request,
                result: result.clone(),
            },
        );
        Ok(result)
    }

    pub fn committed_count(&self) -> usize {
        self.commits_by_idempotency_key.len()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DurableToolTerminalState {
    Completed,
    Failed,
    Denied,
    Cancelled,
    PolicyStopped,
    Incomplete,
    Expired,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DurableToolTerminalRecord {
    pub run_id: String,
    pub response_id: String,
    pub tool_call_id: String,
    pub revision: u32,
    pub terminal_state: DurableToolTerminalState,
    pub arguments_digest: String,
    pub output_digest: Option<String>,
    pub idempotency_key: Option<String>,
    pub effect_committed: bool,
    pub durable_result_committed: bool,
    pub completed_at_unix_ms: u64,
}

impl DurableToolTerminalRecord {
    pub fn new(
        run_id: impl Into<String>,
        response_id: impl Into<String>,
        tool_call_id: impl Into<String>,
        revision: u32,
        terminal_state: DurableToolTerminalState,
        arguments_digest: impl Into<String>,
        completed_at_unix_ms: u64,
    ) -> Self {
        Self {
            run_id: run_id.into(),
            response_id: response_id.into(),
            tool_call_id: tool_call_id.into(),
            revision,
            terminal_state,
            arguments_digest: arguments_digest.into(),
            output_digest: None,
            idempotency_key: None,
            effect_committed: false,
            durable_result_committed: false,
            completed_at_unix_ms,
        }
    }

    pub fn with_output_digest(mut self, output_digest: impl Into<String>) -> Self {
        self.output_digest = Some(output_digest.into());
        self
    }

    pub fn with_idempotency_key(mut self, idempotency_key: impl Into<String>) -> Self {
        self.idempotency_key = Some(idempotency_key.into());
        self
    }

    pub fn with_effect_committed(mut self) -> Self {
        self.effect_committed = true;
        self
    }

    pub fn with_durable_result_committed(mut self) -> Self {
        self.durable_result_committed = true;
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DurableToolTerminalCommit {
    pub sequence: u64,
    pub record: DurableToolTerminalRecord,
    pub replayed: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DurableResponsePolicyStopRecord {
    pub response_id: String,
    pub policy_decision_id: String,
    pub last_policy_accepted_sequence: u64,
    pub occurred_at_unix_ms: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DurableResponsePolicyStopCommit {
    pub sequence: u64,
    pub record: DurableResponsePolicyStopRecord,
    pub replayed: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolTerminalStoreError {
    MissingRunId,
    MissingResponseId,
    MissingToolCallId,
    MissingArgumentsDigest,
    MissingPolicyDecisionId,
    InvalidRevision,
    InvalidCompletedAt,
    TerminalStateConflict {
        response_id: String,
        tool_call_id: String,
        revision: u32,
    },
    ResponsePolicyStopConflict {
        response_id: String,
    },
    DurableResultAlreadyCommitted {
        response_id: String,
    },
    ResponsePolicyStopped {
        response_id: String,
    },
}

#[derive(Default)]
pub struct InMemoryDurableToolTerminalStore {
    next_sequence: u64,
    terminal_records: BTreeMap<(String, String, u32), DurableToolTerminalCommit>,
    policy_stopped_responses: BTreeMap<String, DurableResponsePolicyStopCommit>,
}

impl InMemoryDurableToolTerminalStore {
    pub fn new() -> Self {
        Self {
            next_sequence: 1,
            terminal_records: BTreeMap::new(),
            policy_stopped_responses: BTreeMap::new(),
        }
    }

    pub fn record_tool_terminal(
        &mut self,
        record: DurableToolTerminalRecord,
    ) -> Result<DurableToolTerminalCommit, ToolTerminalStoreError> {
        if record.run_id.is_empty() {
            return Err(ToolTerminalStoreError::MissingRunId);
        }
        if record.response_id.is_empty() {
            return Err(ToolTerminalStoreError::MissingResponseId);
        }
        if record.tool_call_id.is_empty() {
            return Err(ToolTerminalStoreError::MissingToolCallId);
        }
        if record.revision == 0 {
            return Err(ToolTerminalStoreError::InvalidRevision);
        }
        if record.arguments_digest.is_empty() {
            return Err(ToolTerminalStoreError::MissingArgumentsDigest);
        }
        if record.completed_at_unix_ms == 0 {
            return Err(ToolTerminalStoreError::InvalidCompletedAt);
        }

        let key = (
            record.response_id.clone(),
            record.tool_call_id.clone(),
            record.revision,
        );
        if let Some(committed) = self.terminal_records.get(&key) {
            if committed.record != record {
                return Err(ToolTerminalStoreError::TerminalStateConflict {
                    response_id: record.response_id,
                    tool_call_id: record.tool_call_id,
                    revision: record.revision,
                });
            }
            let mut replayed = committed.clone();
            replayed.replayed = true;
            return Ok(replayed);
        }

        if record.durable_result_committed
            && self
                .policy_stopped_responses
                .contains_key(&record.response_id)
        {
            return Err(ToolTerminalStoreError::ResponsePolicyStopped {
                response_id: record.response_id,
            });
        }

        let committed = DurableToolTerminalCommit {
            sequence: self.next_sequence,
            record,
            replayed: false,
        };
        self.next_sequence = self.next_sequence.saturating_add(1);
        self.terminal_records.insert(key, committed.clone());
        Ok(committed)
    }

    pub fn record_response_policy_stopped(
        &mut self,
        response_id: impl Into<String>,
        policy_decision_id: impl Into<String>,
        last_policy_accepted_sequence: u64,
        occurred_at_unix_ms: u64,
    ) -> Result<DurableResponsePolicyStopCommit, ToolTerminalStoreError> {
        let record = DurableResponsePolicyStopRecord {
            response_id: response_id.into(),
            policy_decision_id: policy_decision_id.into(),
            last_policy_accepted_sequence,
            occurred_at_unix_ms,
        };
        if record.response_id.is_empty() {
            return Err(ToolTerminalStoreError::MissingResponseId);
        }
        if record.policy_decision_id.is_empty() {
            return Err(ToolTerminalStoreError::MissingPolicyDecisionId);
        }
        if record.occurred_at_unix_ms == 0 {
            return Err(ToolTerminalStoreError::InvalidCompletedAt);
        }

        if let Some(committed) = self.policy_stopped_responses.get(&record.response_id) {
            if committed.record != record {
                return Err(ToolTerminalStoreError::ResponsePolicyStopConflict {
                    response_id: record.response_id,
                });
            }
            let mut replayed = committed.clone();
            replayed.replayed = true;
            return Ok(replayed);
        }
        if self.terminal_records.values().any(|commit| {
            commit.record.response_id == record.response_id
                && commit.record.durable_result_committed
        }) {
            return Err(ToolTerminalStoreError::DurableResultAlreadyCommitted {
                response_id: record.response_id,
            });
        }

        let committed = DurableResponsePolicyStopCommit {
            sequence: self.next_sequence,
            record,
            replayed: false,
        };
        self.next_sequence = self.next_sequence.saturating_add(1);
        self.policy_stopped_responses
            .insert(committed.record.response_id.clone(), committed.clone());
        Ok(committed)
    }

    pub fn tool_terminal_count(&self) -> usize {
        self.terminal_records.len()
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
