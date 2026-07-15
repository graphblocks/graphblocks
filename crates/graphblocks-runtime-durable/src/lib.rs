use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use graphblocks_runtime_core::output_policy::{
    DraftDisposition, DurableResult, OutputCutoff, TerminalReason,
};
use graphblocks_runtime_core::tool_result::{ToolResult, ToolResultStatus};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeliveryGuarantee {
    BestEffort,
    AtMostOnce,
    AtLeastOnce,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DurableError {
    InvalidSourceCursor {
        field: &'static str,
    },
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
    MissingEventTime {
        cursor: SourceCursor,
    },
    LateEvent {
        event_time_unix_ms: u64,
        watermark_unix_ms: u64,
        allowed_lateness_ms: u64,
    },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Ord, PartialOrd, Serialize)]
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

fn validate_source_cursor(cursor: &SourceCursor) -> Result<(), DurableError> {
    if cursor.stream.trim().is_empty() {
        return Err(DurableError::InvalidSourceCursor { field: "stream" });
    }
    Ok(())
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
        for event in &events {
            validate_source_cursor(&event.cursor)?;
        }
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
    known_partitions: BTreeSet<(String, u32)>,
    committed_cursors: BTreeMap<(String, u32), SourceCursor>,
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
        let known_partitions = events
            .iter()
            .map(|event| (event.cursor.stream.clone(), event.cursor.partition))
            .collect::<BTreeSet<_>>();
        Self {
            guarantee,
            events,
            known_streams,
            known_partitions,
            committed_cursors: BTreeMap::new(),
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
        let events = self
            .events
            .iter()
            .filter(|event| {
                let partition_key = (event.cursor.stream.clone(), event.cursor.partition);
                let replay_cursor = match cursor.as_ref() {
                    Some(cursor)
                        if cursor.stream == event.cursor.stream
                            && cursor.partition == event.cursor.partition =>
                    {
                        Some(cursor)
                    }
                    _ => self.committed_cursors.get(&partition_key),
                };
                replay_cursor.is_none_or(|cursor| event.cursor > *cursor)
            })
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
        let partition_key = (cursor.stream.clone(), cursor.partition);
        if let Some(current) = self.committed_cursors.get(&partition_key)
            && cursor < *current
        {
            return Err(DurableError::StaleCommit {
                current: current.clone(),
                attempted: cursor,
            });
        }
        self.committed_cursors.insert(partition_key, cursor);
        Ok(())
    }

    pub fn pause(&mut self) {
        self.paused = true;
    }

    pub fn resume(&mut self) {
        self.paused = false;
    }

    fn validate_cursor(&self, cursor: &SourceCursor) -> Result<(), DurableError> {
        validate_source_cursor(cursor)?;
        if !self.known_streams.is_empty() && !self.known_streams.contains(&cursor.stream) {
            return Err(DurableError::UnknownSourceCursor {
                cursor: cursor.clone(),
            });
        }
        if !self.known_partitions.is_empty()
            && !self
                .known_partitions
                .contains(&(cursor.stream.clone(), cursor.partition))
        {
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
    pub revision: u64,
    pub is_final: bool,
}

pub struct WindowAccumulator {
    policy: WindowPolicy,
    watermark: Option<Watermark>,
    windows: BTreeMap<u64, Vec<SourceEvent>>,
    on_time_emitted: BTreeSet<u64>,
}

impl WindowAccumulator {
    pub fn new(policy: WindowPolicy) -> Self {
        Self {
            policy,
            watermark: None,
            windows: BTreeMap::new(),
            on_time_emitted: BTreeSet::new(),
        }
    }

    pub fn ingest(&mut self, event: SourceEvent) -> Result<(), DurableError> {
        if self.policy.size_ms == 0 {
            return Err(DurableError::InvalidWindowSize);
        }
        let Some(event_time_unix_ms) = event.event_time_unix_ms else {
            return Err(DurableError::MissingEventTime {
                cursor: event.cursor,
            });
        };
        let start_unix_ms = event_time_unix_ms - (event_time_unix_ms % self.policy.size_ms);
        let deadline_unix_ms = start_unix_ms
            .saturating_add(self.policy.size_ms)
            .saturating_add(self.policy.allowed_lateness_ms);
        if let Some(watermark) = self.watermark
            && deadline_unix_ms <= watermark.unix_ms
        {
            return Err(DurableError::LateEvent {
                event_time_unix_ms,
                watermark_unix_ms: watermark.unix_ms,
                allowed_lateness_ms: self.policy.allowed_lateness_ms,
            });
        }
        self.windows.entry(start_unix_ms).or_default().push(event);
        Ok(())
    }

    pub fn advance_watermark(&mut self, watermark: Watermark) -> Vec<WindowPane> {
        if watermark.kind != WatermarkKind::EventTime {
            return Vec::new();
        }
        if self
            .watermark
            .is_some_and(|current| current.unix_ms >= watermark.unix_ms)
        {
            return Vec::new();
        }
        self.watermark = Some(watermark);
        let triggerable = self
            .windows
            .keys()
            .copied()
            .filter(|start_unix_ms| {
                start_unix_ms.saturating_add(self.policy.size_ms) <= watermark.unix_ms
            })
            .collect::<Vec<_>>();
        let mut emitted = Vec::new();
        for start_unix_ms in triggerable {
            let end_unix_ms = start_unix_ms.saturating_add(self.policy.size_ms);
            let deadline_unix_ms = end_unix_ms.saturating_add(self.policy.allowed_lateness_ms);
            if deadline_unix_ms <= watermark.unix_ms {
                if let Some(mut events) = self.windows.remove(&start_unix_ms) {
                    events.sort_by(|left, right| left.cursor.cmp(&right.cursor));
                    let revision = u64::from(self.on_time_emitted.remove(&start_unix_ms));
                    emitted.push(WindowPane {
                        start_unix_ms,
                        end_unix_ms,
                        events,
                        revision,
                        is_final: true,
                    });
                }
            } else if self.policy.accumulation_mode == AccumulationMode::Accumulating
                && !self.on_time_emitted.contains(&start_unix_ms)
                && let Some(events) = self.windows.get(&start_unix_ms)
            {
                let mut events = events.clone();
                events.sort_by(|left, right| left.cursor.cmp(&right.cursor));
                self.on_time_emitted.insert(start_unix_ms);
                emitted.push(WindowPane {
                    start_unix_ms,
                    end_unix_ms,
                    events,
                    revision: 0,
                    is_final: false,
                });
            }
        }
        emitted
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
        if request.run_id.trim().is_empty() {
            return Err(SinkCommitError::MissingRunId);
        }
        if request.node_id.trim().is_empty() {
            return Err(SinkCommitError::MissingNodeId);
        }
        if request.node_attempt_id.trim().is_empty() {
            return Err(SinkCommitError::MissingNodeAttemptId);
        }
        if request.idempotency_key.trim().is_empty() {
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DurableOutputCutoffTerminalReason {
    PolicyDenied,
    BudgetExhausted,
    Cancelled,
    ClientDisconnected,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DurableOutputCutoffDraftDisposition {
    Keep,
    MarkIncomplete,
    Retract,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DurableOutputCutoffDurableResult {
    None,
    Incomplete,
    Partial,
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

    pub fn from_tool_result(
        run_id: impl Into<String>,
        response_id: impl Into<String>,
        revision: u32,
        arguments_digest: impl Into<String>,
        result: &ToolResult,
        completed_at_unix_ms: u64,
    ) -> Self {
        let mut record = Self::new(
            run_id,
            response_id,
            result.tool_call_id.clone(),
            revision,
            DurableToolTerminalState::from(result.status),
            arguments_digest,
            result.completed_at_unix_ms.unwrap_or(completed_at_unix_ms),
        );
        record.output_digest = result.output_digest.clone();
        record.effect_committed = result.effect_was_committed();
        record
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

impl From<ToolResultStatus> for DurableToolTerminalState {
    fn from(status: ToolResultStatus) -> Self {
        match status {
            ToolResultStatus::Completed => Self::Completed,
            ToolResultStatus::Failed => Self::Failed,
            ToolResultStatus::Denied => Self::Denied,
            ToolResultStatus::Cancelled => Self::Cancelled,
            ToolResultStatus::PolicyStopped => Self::PolicyStopped,
            ToolResultStatus::Incomplete => Self::Incomplete,
        }
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
    pub stream_id: String,
    pub turn_id: Option<String>,
    pub policy_decision_id: String,
    pub last_generated_sequence: u64,
    pub last_policy_accepted_sequence: u64,
    pub last_client_delivered_sequence: u64,
    pub terminal_reason: DurableOutputCutoffTerminalReason,
    pub draft_disposition: DurableOutputCutoffDraftDisposition,
    pub durable_result: DurableOutputCutoffDurableResult,
    pub occurred_at_unix_ms: u64,
}

impl DurableResponsePolicyStopRecord {
    pub fn new(
        response_id: impl Into<String>,
        policy_decision_id: impl Into<String>,
        last_policy_accepted_sequence: u64,
        occurred_at_unix_ms: u64,
    ) -> Self {
        let response_id = response_id.into();
        Self {
            stream_id: response_id.clone(),
            response_id,
            turn_id: None,
            policy_decision_id: policy_decision_id.into(),
            last_generated_sequence: last_policy_accepted_sequence,
            last_policy_accepted_sequence,
            last_client_delivered_sequence: last_policy_accepted_sequence,
            terminal_reason: DurableOutputCutoffTerminalReason::PolicyDenied,
            draft_disposition: DurableOutputCutoffDraftDisposition::Retract,
            durable_result: DurableOutputCutoffDurableResult::None,
            occurred_at_unix_ms,
        }
    }

    pub fn with_stream_id(mut self, stream_id: impl Into<String>) -> Self {
        self.stream_id = stream_id.into();
        self
    }

    pub fn with_turn_id(mut self, turn_id: impl Into<String>) -> Self {
        self.turn_id = Some(turn_id.into());
        self
    }

    pub fn with_last_generated_sequence(mut self, sequence: u64) -> Self {
        self.last_generated_sequence = sequence;
        self
    }

    pub fn with_last_client_delivered_sequence(mut self, sequence: u64) -> Self {
        self.last_client_delivered_sequence = sequence;
        self
    }

    pub fn with_terminal_reason(
        mut self,
        terminal_reason: DurableOutputCutoffTerminalReason,
    ) -> Self {
        self.terminal_reason = terminal_reason;
        self
    }

    pub fn with_draft_disposition(
        mut self,
        draft_disposition: DurableOutputCutoffDraftDisposition,
    ) -> Self {
        self.draft_disposition = draft_disposition;
        self
    }

    pub fn with_durable_result(mut self, durable_result: DurableOutputCutoffDurableResult) -> Self {
        self.durable_result = durable_result;
        self
    }

    pub fn to_output_cutoff(&self) -> Result<OutputCutoff, ToolTerminalStoreError> {
        validate_response_policy_stop_record(self)?;

        let terminal_reason = match self.terminal_reason {
            DurableOutputCutoffTerminalReason::PolicyDenied => TerminalReason::PolicyDenied,
            DurableOutputCutoffTerminalReason::BudgetExhausted => TerminalReason::BudgetExhausted,
            DurableOutputCutoffTerminalReason::Cancelled => TerminalReason::Cancelled,
            DurableOutputCutoffTerminalReason::ClientDisconnected => {
                TerminalReason::ClientDisconnected
            }
        };
        let draft_disposition = match self.draft_disposition {
            DurableOutputCutoffDraftDisposition::Keep => DraftDisposition::Keep,
            DurableOutputCutoffDraftDisposition::MarkIncomplete => DraftDisposition::MarkIncomplete,
            DurableOutputCutoffDraftDisposition::Retract => DraftDisposition::Retract,
        };
        let durable_result = match self.durable_result {
            DurableOutputCutoffDurableResult::None => DurableResult::None,
            DurableOutputCutoffDurableResult::Incomplete => DurableResult::Incomplete,
            DurableOutputCutoffDurableResult::Partial => DurableResult::Partial,
        };

        Ok(OutputCutoff {
            stream_id: self.stream_id.clone(),
            response_id: self.response_id.clone(),
            turn_id: self.turn_id.clone(),
            last_generated_sequence: self.last_generated_sequence,
            last_policy_accepted_sequence: self.last_policy_accepted_sequence,
            last_client_delivered_sequence: self.last_client_delivered_sequence,
            terminal_reason,
            draft_disposition,
            durable_result,
            policy_decision_id: Some(self.policy_decision_id.clone()),
            occurred_at_unix_ms: self.occurred_at_unix_ms,
        })
    }
}

fn validate_response_policy_stop_record(
    record: &DurableResponsePolicyStopRecord,
) -> Result<(), ToolTerminalStoreError> {
    if record.response_id.trim().is_empty() {
        return Err(ToolTerminalStoreError::MissingResponseId);
    }
    if record.stream_id.trim().is_empty() {
        return Err(ToolTerminalStoreError::MissingStreamId);
    }
    if record
        .turn_id
        .as_deref()
        .is_some_and(|turn_id| turn_id.trim().is_empty())
    {
        return Err(ToolTerminalStoreError::MissingTurnId);
    }
    if record.policy_decision_id.trim().is_empty() {
        return Err(ToolTerminalStoreError::MissingPolicyDecisionId);
    }
    if record.last_policy_accepted_sequence > record.last_generated_sequence {
        return Err(
            ToolTerminalStoreError::PolicyAcceptedSequenceBeyondGenerated {
                last_generated_sequence: record.last_generated_sequence,
                last_policy_accepted_sequence: record.last_policy_accepted_sequence,
            },
        );
    }
    if record.last_client_delivered_sequence > record.last_generated_sequence {
        return Err(
            ToolTerminalStoreError::ClientDeliveredSequenceBeyondGenerated {
                last_generated_sequence: record.last_generated_sequence,
                last_client_delivered_sequence: record.last_client_delivered_sequence,
            },
        );
    }
    if record.last_client_delivered_sequence > record.last_policy_accepted_sequence
        && record.draft_disposition == DurableOutputCutoffDraftDisposition::Keep
    {
        return Err(
            ToolTerminalStoreError::DeliveredDraftBeyondPolicyAcceptanceKept {
                last_policy_accepted_sequence: record.last_policy_accepted_sequence,
                last_client_delivered_sequence: record.last_client_delivered_sequence,
            },
        );
    }
    if record.occurred_at_unix_ms == 0 {
        return Err(ToolTerminalStoreError::InvalidCompletedAt);
    }
    Ok(())
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
    MissingOutputDigest,
    MissingIdempotencyKey,
    MissingPolicyDecisionId,
    MissingStreamId,
    MissingTurnId,
    InvalidRevision,
    InvalidCompletedAt,
    DeniedEffectCommitted {
        response_id: String,
        tool_call_id: String,
        revision: u32,
    },
    ExpiredEffectCommitted {
        response_id: String,
        tool_call_id: String,
        revision: u32,
    },
    PolicyAcceptedSequenceBeyondGenerated {
        last_generated_sequence: u64,
        last_policy_accepted_sequence: u64,
    },
    ClientDeliveredSequenceBeyondGenerated {
        last_generated_sequence: u64,
        last_client_delivered_sequence: u64,
    },
    DeliveredDraftBeyondPolicyAcceptanceKept {
        last_policy_accepted_sequence: u64,
        last_client_delivered_sequence: u64,
    },
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
        if record.run_id.trim().is_empty() {
            return Err(ToolTerminalStoreError::MissingRunId);
        }
        if record.response_id.trim().is_empty() {
            return Err(ToolTerminalStoreError::MissingResponseId);
        }
        if record.tool_call_id.trim().is_empty() {
            return Err(ToolTerminalStoreError::MissingToolCallId);
        }
        if record.revision == 0 {
            return Err(ToolTerminalStoreError::InvalidRevision);
        }
        if record.arguments_digest.trim().is_empty() {
            return Err(ToolTerminalStoreError::MissingArgumentsDigest);
        }
        if record
            .output_digest
            .as_deref()
            .is_some_and(|output_digest| output_digest.trim().is_empty())
        {
            return Err(ToolTerminalStoreError::MissingOutputDigest);
        }
        if record
            .idempotency_key
            .as_deref()
            .is_some_and(|idempotency_key| idempotency_key.trim().is_empty())
        {
            return Err(ToolTerminalStoreError::MissingIdempotencyKey);
        }
        if record.completed_at_unix_ms == 0 {
            return Err(ToolTerminalStoreError::InvalidCompletedAt);
        }
        if record.terminal_state == DurableToolTerminalState::Denied && record.effect_committed {
            return Err(ToolTerminalStoreError::DeniedEffectCommitted {
                response_id: record.response_id,
                tool_call_id: record.tool_call_id,
                revision: record.revision,
            });
        }
        if record.terminal_state == DurableToolTerminalState::Expired && record.effect_committed {
            return Err(ToolTerminalStoreError::ExpiredEffectCommitted {
                response_id: record.response_id,
                tool_call_id: record.tool_call_id,
                revision: record.revision,
            });
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
        self.record_response_policy_stop(DurableResponsePolicyStopRecord::new(
            response_id,
            policy_decision_id,
            last_policy_accepted_sequence,
            occurred_at_unix_ms,
        ))
    }

    pub fn record_response_policy_stop(
        &mut self,
        record: DurableResponsePolicyStopRecord,
    ) -> Result<DurableResponsePolicyStopCommit, ToolTerminalStoreError> {
        validate_response_policy_stop_record(&record)?;

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

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
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

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
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
        if self.checkpoint_id.trim().is_empty() {
            return Err(CheckpointBarrierError::MissingCheckpointId);
        }
        if self.run_id.trim().is_empty() {
            return Err(CheckpointBarrierError::MissingRunId);
        }
        if self.release_id.trim().is_empty() {
            return Err(CheckpointBarrierError::MissingReleaseId);
        }
        if self.deployment_revision_id.trim().is_empty() {
            return Err(CheckpointBarrierError::MissingDeploymentRevisionId);
        }
        if self.plan_hash.trim().is_empty() {
            return Err(CheckpointBarrierError::MissingPlanHash);
        }
        if self.checkpoint_schema.schema_id.trim().is_empty()
            || self.checkpoint_schema.schema_version == 0
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CheckpointRecoveryClaim {
    pub run_id: String,
    pub checkpoint_id: String,
    pub worker_id: String,
    pub lease_id: String,
    pub fencing_epoch: u64,
    pub claimed_at_unix_ms: u64,
    pub expires_at_unix_ms: u64,
}

impl CheckpointRecoveryClaim {
    pub fn is_active_at(&self, now_unix_ms: u64) -> bool {
        self.claimed_at_unix_ms <= now_unix_ms && self.expires_at_unix_ms > now_unix_ms
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CheckpointRecoveryClaimIdentity {
    pub checkpoint_id: String,
    pub worker_id: String,
    pub lease_id: String,
    pub fencing_epoch: u64,
}

impl CheckpointRecoveryClaimIdentity {
    pub fn from_claim(claim: &CheckpointRecoveryClaim) -> Self {
        Self {
            checkpoint_id: claim.checkpoint_id.clone(),
            worker_id: claim.worker_id.clone(),
            lease_id: claim.lease_id.clone(),
            fencing_epoch: claim.fencing_epoch,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct CheckpointRecovery {
    pub checkpoint: CheckpointBarrier,
    pub claim: CheckpointRecoveryClaim,
}

#[derive(Default)]
pub struct InMemoryCheckpointStore {
    checkpoints_by_run: BTreeMap<String, Vec<CheckpointBarrier>>,
    active_claims_by_run: BTreeMap<String, CheckpointRecoveryClaim>,
    next_fencing_epoch_by_run: BTreeMap<String, u64>,
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

    #[allow(clippy::too_many_arguments)]
    pub fn claim_latest_compatible(
        &mut self,
        run_id: &str,
        release_id: &str,
        deployment_revision_id: &str,
        plan_hash: &str,
        worker_id: &str,
        lease_id: &str,
        now_unix_ms: u64,
        expires_at_unix_ms: u64,
    ) -> Result<CheckpointRecovery, CheckpointStoreError> {
        if worker_id.trim().is_empty() {
            return Err(CheckpointStoreError::InvalidRecoveryClaim { field: "worker_id" });
        }
        if lease_id.trim().is_empty() {
            return Err(CheckpointStoreError::InvalidRecoveryClaim { field: "lease_id" });
        }
        if expires_at_unix_ms <= now_unix_ms {
            return Err(CheckpointStoreError::InvalidRecoveryClaim {
                field: "expires_at_unix_ms",
            });
        }
        let checkpoint = self
            .latest_compatible(run_id, release_id, deployment_revision_id, plan_hash)
            .ok_or_else(|| CheckpointStoreError::CompatibleCheckpointNotFound {
                run_id: run_id.to_owned(),
                release_id: release_id.to_owned(),
                deployment_revision_id: deployment_revision_id.to_owned(),
                plan_hash: plan_hash.to_owned(),
            })?;
        if let Some(active) = self.active_claims_by_run.get(run_id)
            && active.expires_at_unix_ms > now_unix_ms
        {
            return Err(CheckpointStoreError::ActiveRecoveryClaim {
                run_id: run_id.to_owned(),
                worker_id: active.worker_id.clone(),
                lease_id: active.lease_id.clone(),
                expires_at_unix_ms: active.expires_at_unix_ms,
            });
        }

        let next_fencing_epoch = self
            .next_fencing_epoch_by_run
            .entry(run_id.to_owned())
            .or_insert(1);
        let claim = CheckpointRecoveryClaim {
            run_id: run_id.to_owned(),
            checkpoint_id: checkpoint.checkpoint_id.clone(),
            worker_id: worker_id.to_owned(),
            lease_id: lease_id.to_owned(),
            fencing_epoch: *next_fencing_epoch,
            claimed_at_unix_ms: now_unix_ms,
            expires_at_unix_ms,
        };
        *next_fencing_epoch = next_fencing_epoch.saturating_add(1);
        self.active_claims_by_run
            .insert(run_id.to_owned(), claim.clone());
        Ok(CheckpointRecovery { checkpoint, claim })
    }

    pub fn complete_claim(
        &mut self,
        claim: &CheckpointRecoveryClaim,
        now_unix_ms: u64,
    ) -> Result<(), CheckpointStoreError> {
        let active = self
            .active_claims_by_run
            .get(&claim.run_id)
            .ok_or_else(|| CheckpointStoreError::RecoveryClaimNotFound {
                run_id: claim.run_id.clone(),
            })?;
        if active.checkpoint_id != claim.checkpoint_id
            || active.worker_id != claim.worker_id
            || active.lease_id != claim.lease_id
            || active.fencing_epoch != claim.fencing_epoch
        {
            return Err(CheckpointStoreError::RecoveryClaimMismatch {
                run_id: claim.run_id.clone(),
                expected: Box::new(CheckpointRecoveryClaimIdentity::from_claim(claim)),
                actual: Box::new(CheckpointRecoveryClaimIdentity::from_claim(active)),
            });
        }
        if now_unix_ms < active.claimed_at_unix_ms {
            return Err(CheckpointStoreError::RecoveryClaimNotYetActive {
                run_id: claim.run_id.clone(),
                lease_id: claim.lease_id.clone(),
                claimed_at_unix_ms: active.claimed_at_unix_ms,
                now_unix_ms,
            });
        }
        if !active.is_active_at(now_unix_ms) {
            return Err(CheckpointStoreError::RecoveryClaimExpired {
                run_id: claim.run_id.clone(),
                lease_id: claim.lease_id.clone(),
                expires_at_unix_ms: active.expires_at_unix_ms,
                now_unix_ms,
            });
        }
        self.active_claims_by_run.remove(&claim.run_id);
        Ok(())
    }

    pub fn renew_claim(
        &mut self,
        claim: &CheckpointRecoveryClaim,
        now_unix_ms: u64,
        expires_at_unix_ms: u64,
    ) -> Result<CheckpointRecoveryClaim, CheckpointStoreError> {
        if expires_at_unix_ms <= now_unix_ms {
            return Err(CheckpointStoreError::InvalidRecoveryClaim {
                field: "expires_at_unix_ms",
            });
        }
        let active = self
            .active_claims_by_run
            .get(&claim.run_id)
            .ok_or_else(|| CheckpointStoreError::RecoveryClaimNotFound {
                run_id: claim.run_id.clone(),
            })?;
        if active.checkpoint_id != claim.checkpoint_id
            || active.worker_id != claim.worker_id
            || active.lease_id != claim.lease_id
            || active.fencing_epoch != claim.fencing_epoch
        {
            return Err(CheckpointStoreError::RecoveryClaimMismatch {
                run_id: claim.run_id.clone(),
                expected: Box::new(CheckpointRecoveryClaimIdentity::from_claim(claim)),
                actual: Box::new(CheckpointRecoveryClaimIdentity::from_claim(active)),
            });
        }
        if now_unix_ms < active.claimed_at_unix_ms {
            return Err(CheckpointStoreError::RecoveryClaimNotYetActive {
                run_id: claim.run_id.clone(),
                lease_id: claim.lease_id.clone(),
                claimed_at_unix_ms: active.claimed_at_unix_ms,
                now_unix_ms,
            });
        }
        if !active.is_active_at(now_unix_ms) {
            return Err(CheckpointStoreError::RecoveryClaimExpired {
                run_id: claim.run_id.clone(),
                lease_id: claim.lease_id.clone(),
                expires_at_unix_ms: active.expires_at_unix_ms,
                now_unix_ms,
            });
        }
        if expires_at_unix_ms <= active.expires_at_unix_ms {
            return Err(CheckpointStoreError::InvalidRecoveryClaim {
                field: "expires_at_unix_ms",
            });
        }
        let renewed = CheckpointRecoveryClaim {
            expires_at_unix_ms,
            ..active.clone()
        };
        self.active_claims_by_run
            .insert(claim.run_id.clone(), renewed.clone());
        Ok(renewed)
    }
}

pub struct SqliteCheckpointStore {
    connection: Connection,
}

impl SqliteCheckpointStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, CheckpointStoreError> {
        let connection = Connection::open(path).map_err(checkpoint_storage_error)?;
        connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS checkpoint_barriers (
                    checkpoint_id TEXT PRIMARY KEY NOT NULL,
                    run_id TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    deployment_revision_id TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    state_revision INTEGER NOT NULL,
                    barrier_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS checkpoint_barriers_run_lookup
                    ON checkpoint_barriers (
                        run_id,
                        release_id,
                        deployment_revision_id,
                        plan_hash,
                        state_revision
                    );
                CREATE TABLE IF NOT EXISTS checkpoint_recovery_claims (
                    run_id TEXT PRIMARY KEY NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    fencing_epoch INTEGER NOT NULL,
                    claimed_at_unix_ms INTEGER NOT NULL,
                    expires_at_unix_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoint_recovery_epochs (
                    run_id TEXT PRIMARY KEY NOT NULL,
                    next_fencing_epoch INTEGER NOT NULL
                );
                ",
            )
            .map_err(checkpoint_storage_error)?;
        Ok(Self { connection })
    }

    pub fn put(&mut self, barrier: CheckpointBarrier) -> Result<(), CheckpointStoreError> {
        barrier
            .validate()
            .map_err(CheckpointStoreError::InvalidBarrier)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(checkpoint_storage_error)?;
        let current = transaction
            .query_row(
                "SELECT MAX(state_revision) FROM checkpoint_barriers WHERE run_id = ?1",
                params![barrier.run_id],
                |row| row.get::<_, Option<i64>>(0),
            )
            .map_err(checkpoint_storage_error)?
            .map(|value| sqlite_i64_to_u64(value, "state_revision"))
            .transpose()?;
        if let Some(current) = current
            && barrier.state_revision <= current
        {
            return Err(CheckpointStoreError::StaleStateRevision {
                run_id: barrier.run_id,
                current,
                attempted: barrier.state_revision,
            });
        }
        let barrier_json = serde_json::to_string(&barrier).map_err(checkpoint_json_error)?;
        transaction
            .execute(
                "
                INSERT INTO checkpoint_barriers (
                    checkpoint_id,
                    run_id,
                    release_id,
                    deployment_revision_id,
                    plan_hash,
                    state_revision,
                    barrier_json
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
                ",
                params![
                    barrier.checkpoint_id,
                    barrier.run_id,
                    barrier.release_id,
                    barrier.deployment_revision_id,
                    barrier.plan_hash,
                    sqlite_u64_to_i64(barrier.state_revision, "state_revision")?,
                    barrier_json,
                ],
            )
            .map_err(checkpoint_storage_error)?;
        transaction.commit().map_err(checkpoint_storage_error)?;
        Ok(())
    }

    pub fn latest_compatible(
        &self,
        run_id: &str,
        release_id: &str,
        deployment_revision_id: &str,
        plan_hash: &str,
    ) -> Result<Option<CheckpointBarrier>, CheckpointStoreError> {
        self.connection
            .query_row(
                "
                SELECT checkpoint_id,
                       state_revision,
                       barrier_json
                  FROM checkpoint_barriers
                 WHERE run_id = ?1
                   AND release_id = ?2
                   AND deployment_revision_id = ?3
                   AND plan_hash = ?4
                 ORDER BY state_revision DESC
                 LIMIT 1
                ",
                params![run_id, release_id, deployment_revision_id, plan_hash],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(checkpoint_storage_error)?
            .map(|(checkpoint_id, state_revision, raw)| {
                decode_stored_checkpoint_barrier(
                    &raw,
                    &checkpoint_id,
                    run_id,
                    release_id,
                    deployment_revision_id,
                    plan_hash,
                    sqlite_i64_to_u64(state_revision, "state_revision")?,
                )
            })
            .transpose()
    }

    #[allow(clippy::too_many_arguments)]
    pub fn claim_latest_compatible(
        &mut self,
        run_id: &str,
        release_id: &str,
        deployment_revision_id: &str,
        plan_hash: &str,
        worker_id: &str,
        lease_id: &str,
        now_unix_ms: u64,
        expires_at_unix_ms: u64,
    ) -> Result<CheckpointRecovery, CheckpointStoreError> {
        if worker_id.trim().is_empty() {
            return Err(CheckpointStoreError::InvalidRecoveryClaim { field: "worker_id" });
        }
        if lease_id.trim().is_empty() {
            return Err(CheckpointStoreError::InvalidRecoveryClaim { field: "lease_id" });
        }
        if expires_at_unix_ms <= now_unix_ms {
            return Err(CheckpointStoreError::InvalidRecoveryClaim {
                field: "expires_at_unix_ms",
            });
        }

        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(checkpoint_storage_error)?;
        let checkpoint = transaction
            .query_row(
                "
                SELECT checkpoint_id,
                       state_revision,
                       barrier_json
                  FROM checkpoint_barriers
                 WHERE run_id = ?1
                   AND release_id = ?2
                   AND deployment_revision_id = ?3
                   AND plan_hash = ?4
                 ORDER BY state_revision DESC
                 LIMIT 1
                ",
                params![run_id, release_id, deployment_revision_id, plan_hash],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(checkpoint_storage_error)?
            .ok_or_else(|| CheckpointStoreError::CompatibleCheckpointNotFound {
                run_id: run_id.to_owned(),
                release_id: release_id.to_owned(),
                deployment_revision_id: deployment_revision_id.to_owned(),
                plan_hash: plan_hash.to_owned(),
            })
            .and_then(|(checkpoint_id, state_revision, raw)| {
                decode_stored_checkpoint_barrier(
                    &raw,
                    &checkpoint_id,
                    run_id,
                    release_id,
                    deployment_revision_id,
                    plan_hash,
                    sqlite_i64_to_u64(state_revision, "state_revision")?,
                )
            })?;
        let active_claim = transaction
            .query_row(
                "
                SELECT checkpoint_id,
                       worker_id,
                       lease_id,
                       fencing_epoch,
                       claimed_at_unix_ms,
                       expires_at_unix_ms
                  FROM checkpoint_recovery_claims
                 WHERE run_id = ?1
                ",
                params![run_id],
                |row| {
                    Ok(CheckpointRecoveryClaim {
                        run_id: run_id.to_owned(),
                        checkpoint_id: row.get(0)?,
                        worker_id: row.get(1)?,
                        lease_id: row.get(2)?,
                        fencing_epoch: sqlite_i64_to_u64(row.get(3)?, "fencing_epoch")
                            .map_err(rusqlite_error_from_checkpoint)?,
                        claimed_at_unix_ms: sqlite_i64_to_u64(row.get(4)?, "claimed_at_unix_ms")
                            .map_err(rusqlite_error_from_checkpoint)?,
                        expires_at_unix_ms: sqlite_i64_to_u64(row.get(5)?, "expires_at_unix_ms")
                            .map_err(rusqlite_error_from_checkpoint)?,
                    })
                },
            )
            .optional()
            .map_err(checkpoint_storage_error)?;
        if let Some(active) = active_claim
            && active.expires_at_unix_ms > now_unix_ms
        {
            return Err(CheckpointStoreError::ActiveRecoveryClaim {
                run_id: run_id.to_owned(),
                worker_id: active.worker_id,
                lease_id: active.lease_id,
                expires_at_unix_ms: active.expires_at_unix_ms,
            });
        }
        let next_fencing_epoch = transaction
            .query_row(
                "SELECT next_fencing_epoch FROM checkpoint_recovery_epochs WHERE run_id = ?1",
                params![run_id],
                |row| {
                    sqlite_i64_to_u64(row.get(0)?, "next_fencing_epoch")
                        .map_err(rusqlite_error_from_checkpoint)
                },
            )
            .optional()
            .map_err(checkpoint_storage_error)?
            .unwrap_or(1);
        let claim = CheckpointRecoveryClaim {
            run_id: run_id.to_owned(),
            checkpoint_id: checkpoint.checkpoint_id.clone(),
            worker_id: worker_id.to_owned(),
            lease_id: lease_id.to_owned(),
            fencing_epoch: next_fencing_epoch,
            claimed_at_unix_ms: now_unix_ms,
            expires_at_unix_ms,
        };
        transaction
            .execute(
                "
                INSERT INTO checkpoint_recovery_claims (
                    run_id,
                    checkpoint_id,
                    worker_id,
                    lease_id,
                    fencing_epoch,
                    claimed_at_unix_ms,
                    expires_at_unix_ms
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
                ON CONFLICT(run_id) DO UPDATE SET
                    checkpoint_id = excluded.checkpoint_id,
                    worker_id = excluded.worker_id,
                    lease_id = excluded.lease_id,
                    fencing_epoch = excluded.fencing_epoch,
                    claimed_at_unix_ms = excluded.claimed_at_unix_ms,
                    expires_at_unix_ms = excluded.expires_at_unix_ms
                ",
                params![
                    claim.run_id,
                    claim.checkpoint_id,
                    claim.worker_id,
                    claim.lease_id,
                    sqlite_u64_to_i64(claim.fencing_epoch, "fencing_epoch")?,
                    sqlite_u64_to_i64(claim.claimed_at_unix_ms, "claimed_at_unix_ms")?,
                    sqlite_u64_to_i64(claim.expires_at_unix_ms, "expires_at_unix_ms")?,
                ],
            )
            .map_err(checkpoint_storage_error)?;
        transaction
            .execute(
                "
                INSERT INTO checkpoint_recovery_epochs (
                    run_id,
                    next_fencing_epoch
                ) VALUES (?1, ?2)
                ON CONFLICT(run_id) DO UPDATE SET
                    next_fencing_epoch = excluded.next_fencing_epoch
                ",
                params![
                    run_id,
                    sqlite_u64_to_i64(next_fencing_epoch.saturating_add(1), "next_fencing_epoch")?,
                ],
            )
            .map_err(checkpoint_storage_error)?;
        transaction.commit().map_err(checkpoint_storage_error)?;
        Ok(CheckpointRecovery { checkpoint, claim })
    }

    pub fn complete_claim(
        &mut self,
        claim: &CheckpointRecoveryClaim,
        now_unix_ms: u64,
    ) -> Result<(), CheckpointStoreError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(checkpoint_storage_error)?;
        let active = transaction
            .query_row(
                "
                SELECT checkpoint_id,
                       worker_id,
                       lease_id,
                       fencing_epoch,
                       claimed_at_unix_ms,
                       expires_at_unix_ms
                  FROM checkpoint_recovery_claims
                 WHERE run_id = ?1
                ",
                params![claim.run_id],
                |row| {
                    Ok(CheckpointRecoveryClaim {
                        run_id: claim.run_id.clone(),
                        checkpoint_id: row.get(0)?,
                        worker_id: row.get(1)?,
                        lease_id: row.get(2)?,
                        fencing_epoch: sqlite_i64_to_u64(row.get(3)?, "fencing_epoch")
                            .map_err(rusqlite_error_from_checkpoint)?,
                        claimed_at_unix_ms: sqlite_i64_to_u64(row.get(4)?, "claimed_at_unix_ms")
                            .map_err(rusqlite_error_from_checkpoint)?,
                        expires_at_unix_ms: sqlite_i64_to_u64(row.get(5)?, "expires_at_unix_ms")
                            .map_err(rusqlite_error_from_checkpoint)?,
                    })
                },
            )
            .optional()
            .map_err(checkpoint_storage_error)?
            .ok_or_else(|| CheckpointStoreError::RecoveryClaimNotFound {
                run_id: claim.run_id.clone(),
            })?;
        if active.checkpoint_id != claim.checkpoint_id
            || active.worker_id != claim.worker_id
            || active.lease_id != claim.lease_id
            || active.fencing_epoch != claim.fencing_epoch
        {
            return Err(CheckpointStoreError::RecoveryClaimMismatch {
                run_id: claim.run_id.clone(),
                expected: Box::new(CheckpointRecoveryClaimIdentity::from_claim(claim)),
                actual: Box::new(CheckpointRecoveryClaimIdentity::from_claim(&active)),
            });
        }
        if now_unix_ms < active.claimed_at_unix_ms {
            return Err(CheckpointStoreError::RecoveryClaimNotYetActive {
                run_id: claim.run_id.clone(),
                lease_id: claim.lease_id.clone(),
                claimed_at_unix_ms: active.claimed_at_unix_ms,
                now_unix_ms,
            });
        }
        if !active.is_active_at(now_unix_ms) {
            return Err(CheckpointStoreError::RecoveryClaimExpired {
                run_id: claim.run_id.clone(),
                lease_id: claim.lease_id.clone(),
                expires_at_unix_ms: active.expires_at_unix_ms,
                now_unix_ms,
            });
        }
        transaction
            .execute(
                "DELETE FROM checkpoint_recovery_claims WHERE run_id = ?1",
                params![claim.run_id],
            )
            .map_err(checkpoint_storage_error)?;
        transaction.commit().map_err(checkpoint_storage_error)?;
        Ok(())
    }

    pub fn renew_claim(
        &mut self,
        claim: &CheckpointRecoveryClaim,
        now_unix_ms: u64,
        expires_at_unix_ms: u64,
    ) -> Result<CheckpointRecoveryClaim, CheckpointStoreError> {
        if expires_at_unix_ms <= now_unix_ms {
            return Err(CheckpointStoreError::InvalidRecoveryClaim {
                field: "expires_at_unix_ms",
            });
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(checkpoint_storage_error)?;
        let active = transaction
            .query_row(
                "
                SELECT checkpoint_id,
                       worker_id,
                       lease_id,
                       fencing_epoch,
                       claimed_at_unix_ms,
                       expires_at_unix_ms
                  FROM checkpoint_recovery_claims
                 WHERE run_id = ?1
                ",
                params![claim.run_id],
                |row| {
                    Ok(CheckpointRecoveryClaim {
                        run_id: claim.run_id.clone(),
                        checkpoint_id: row.get(0)?,
                        worker_id: row.get(1)?,
                        lease_id: row.get(2)?,
                        fencing_epoch: sqlite_i64_to_u64(row.get(3)?, "fencing_epoch")
                            .map_err(rusqlite_error_from_checkpoint)?,
                        claimed_at_unix_ms: sqlite_i64_to_u64(row.get(4)?, "claimed_at_unix_ms")
                            .map_err(rusqlite_error_from_checkpoint)?,
                        expires_at_unix_ms: sqlite_i64_to_u64(row.get(5)?, "expires_at_unix_ms")
                            .map_err(rusqlite_error_from_checkpoint)?,
                    })
                },
            )
            .optional()
            .map_err(checkpoint_storage_error)?
            .ok_or_else(|| CheckpointStoreError::RecoveryClaimNotFound {
                run_id: claim.run_id.clone(),
            })?;
        if active.checkpoint_id != claim.checkpoint_id
            || active.worker_id != claim.worker_id
            || active.lease_id != claim.lease_id
            || active.fencing_epoch != claim.fencing_epoch
        {
            return Err(CheckpointStoreError::RecoveryClaimMismatch {
                run_id: claim.run_id.clone(),
                expected: Box::new(CheckpointRecoveryClaimIdentity::from_claim(claim)),
                actual: Box::new(CheckpointRecoveryClaimIdentity::from_claim(&active)),
            });
        }
        if now_unix_ms < active.claimed_at_unix_ms {
            return Err(CheckpointStoreError::RecoveryClaimNotYetActive {
                run_id: claim.run_id.clone(),
                lease_id: claim.lease_id.clone(),
                claimed_at_unix_ms: active.claimed_at_unix_ms,
                now_unix_ms,
            });
        }
        if !active.is_active_at(now_unix_ms) {
            return Err(CheckpointStoreError::RecoveryClaimExpired {
                run_id: claim.run_id.clone(),
                lease_id: claim.lease_id.clone(),
                expires_at_unix_ms: active.expires_at_unix_ms,
                now_unix_ms,
            });
        }
        if expires_at_unix_ms <= active.expires_at_unix_ms {
            return Err(CheckpointStoreError::InvalidRecoveryClaim {
                field: "expires_at_unix_ms",
            });
        }
        let renewed = CheckpointRecoveryClaim {
            expires_at_unix_ms,
            ..active
        };
        transaction
            .execute(
                "
                UPDATE checkpoint_recovery_claims
                   SET expires_at_unix_ms = ?2
                 WHERE run_id = ?1
                ",
                params![
                    renewed.run_id,
                    sqlite_u64_to_i64(renewed.expires_at_unix_ms, "expires_at_unix_ms")?,
                ],
            )
            .map_err(checkpoint_storage_error)?;
        transaction.commit().map_err(checkpoint_storage_error)?;
        Ok(renewed)
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
    CompatibleCheckpointNotFound {
        run_id: String,
        release_id: String,
        deployment_revision_id: String,
        plan_hash: String,
    },
    InvalidRecoveryClaim {
        field: &'static str,
    },
    ActiveRecoveryClaim {
        run_id: String,
        worker_id: String,
        lease_id: String,
        expires_at_unix_ms: u64,
    },
    RecoveryClaimNotFound {
        run_id: String,
    },
    RecoveryClaimMismatch {
        run_id: String,
        expected: Box<CheckpointRecoveryClaimIdentity>,
        actual: Box<CheckpointRecoveryClaimIdentity>,
    },
    RecoveryClaimExpired {
        run_id: String,
        lease_id: String,
        expires_at_unix_ms: u64,
        now_unix_ms: u64,
    },
    RecoveryClaimNotYetActive {
        run_id: String,
        lease_id: String,
        claimed_at_unix_ms: u64,
        now_unix_ms: u64,
    },
    Storage {
        message: String,
    },
}

#[allow(clippy::too_many_arguments)]
fn decode_stored_checkpoint_barrier(
    raw: &str,
    expected_checkpoint_id: &str,
    expected_run_id: &str,
    expected_release_id: &str,
    expected_deployment_revision_id: &str,
    expected_plan_hash: &str,
    expected_state_revision: u64,
) -> Result<CheckpointBarrier, CheckpointStoreError> {
    let barrier = serde_json::from_str::<CheckpointBarrier>(raw).map_err(checkpoint_json_error)?;
    barrier
        .validate()
        .map_err(CheckpointStoreError::InvalidBarrier)?;
    for (field, expected, actual) in [
        (
            "checkpoint_id",
            expected_checkpoint_id,
            barrier.checkpoint_id.as_str(),
        ),
        ("run_id", expected_run_id, barrier.run_id.as_str()),
        (
            "release_id",
            expected_release_id,
            barrier.release_id.as_str(),
        ),
        (
            "deployment_revision_id",
            expected_deployment_revision_id,
            barrier.deployment_revision_id.as_str(),
        ),
        ("plan_hash", expected_plan_hash, barrier.plan_hash.as_str()),
    ] {
        if actual != expected {
            return Err(CheckpointStoreError::Storage {
                message: format!(
                    "checkpoint barrier row/payload mismatch for {field}: expected {expected:?}, got {actual:?}"
                ),
            });
        }
    }
    if barrier.state_revision != expected_state_revision {
        return Err(CheckpointStoreError::Storage {
            message: format!(
                "checkpoint barrier row/payload mismatch for state_revision: expected {expected_state_revision}, got {}",
                barrier.state_revision
            ),
        });
    }
    Ok(barrier)
}

fn sqlite_u64_to_i64(value: u64, field: &'static str) -> Result<i64, CheckpointStoreError> {
    i64::try_from(value).map_err(|_| CheckpointStoreError::Storage {
        message: format!("checkpoint field {field} exceeds sqlite integer range"),
    })
}

fn sqlite_i64_to_u64(value: i64, field: &'static str) -> Result<u64, CheckpointStoreError> {
    u64::try_from(value).map_err(|_| CheckpointStoreError::Storage {
        message: format!("stored checkpoint field {field} is negative"),
    })
}

fn checkpoint_json_error(error: serde_json::Error) -> CheckpointStoreError {
    CheckpointStoreError::Storage {
        message: error.to_string(),
    }
}

fn checkpoint_storage_error(error: rusqlite::Error) -> CheckpointStoreError {
    CheckpointStoreError::Storage {
        message: error.to_string(),
    }
}

fn rusqlite_error_from_checkpoint(error: CheckpointStoreError) -> rusqlite::Error {
    rusqlite::Error::ToSqlConversionFailure(Box::new(std::io::Error::other(format!("{error:?}"))))
}
