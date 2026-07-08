use std::collections::BTreeSet;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::json;

use crate::application_event::{
    ApplicationProtocolEvent, ApplicationProtocolEventKind, AttachToRunReplay,
};
use crate::run_store::{RunStatus, RunStatusSnapshot};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TuiAttachState {
    Attached,
    CursorExpired,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TuiRowSeverity {
    Info,
    Warning,
    Error,
}

impl TuiRowSeverity {
    fn as_str(self) -> &'static str {
        match self {
            Self::Info => "info",
            Self::Warning => "warning",
            Self::Error => "error",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TuiRunRow {
    pub event_id: String,
    pub sequence: u64,
    pub cursor: Option<String>,
    pub kind: String,
    pub summary: String,
    pub severity: TuiRowSeverity,
    pub occurred_at_unix_ms: u64,
}

impl TuiRunRow {
    fn from_event(event: &ApplicationProtocolEvent) -> Self {
        Self {
            event_id: event.metadata.event_id.clone(),
            sequence: event.metadata.sequence,
            cursor: event
                .metadata
                .cursor
                .clone()
                .or_else(|| Some(event.metadata.sequence.to_string())),
            kind: event.kind.as_str().to_owned(),
            summary: event_summary(event),
            severity: event_severity(event.kind),
            occurred_at_unix_ms: event.metadata.occurred_at_unix_ms,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CursorExpiredProjection {
    pub requested_cursor: String,
    pub earliest_available_cursor: Option<String>,
    pub last_cursor: Option<String>,
    pub last_sequence: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TuiRunView {
    run_id: String,
    state: RunStatus,
    release_id: String,
    last_cursor: Option<String>,
    last_sequence: Option<u64>,
    waiting_count: usize,
    active_operations: Vec<String>,
    rows: Vec<TuiRunRow>,
    seen_event_ids: BTreeSet<String>,
    cursor_expired: Option<CursorExpiredProjection>,
}

impl TuiRunView {
    pub fn from_status(status: RunStatusSnapshot) -> Self {
        Self {
            run_id: status.run_id,
            state: status.state,
            release_id: status.release_id,
            last_cursor: Some(status.last_cursor),
            last_sequence: None,
            waiting_count: status.waiting_on.len(),
            active_operations: status.active_operations,
            rows: Vec::new(),
            seen_event_ids: BTreeSet::new(),
            cursor_expired: None,
        }
    }

    pub fn apply_attach_replay(&mut self, replay: AttachToRunReplay) -> TuiAttachState {
        match replay {
            AttachToRunReplay::Attached {
                replayed_events,
                live_cursor,
            } => {
                for event in replayed_events {
                    self.apply_event(event);
                }
                if live_cursor.is_some() {
                    self.last_cursor = live_cursor;
                }
                TuiAttachState::Attached
            }
            AttachToRunReplay::CursorExpired {
                requested_cursor,
                earliest_available_cursor,
                last_cursor,
                last_sequence,
                run_status,
            } => {
                if let Some(status) = run_status {
                    if status.run_id == self.run_id {
                        self.state = status.state;
                        self.release_id = status.release_id;
                        self.last_cursor = Some(status.last_cursor);
                        self.waiting_count = status.waiting_on.len();
                        self.active_operations = status.active_operations;
                    }
                }
                self.cursor_expired = Some(CursorExpiredProjection {
                    requested_cursor,
                    earliest_available_cursor,
                    last_cursor: last_cursor.clone(),
                    last_sequence,
                });
                if last_cursor.is_some() {
                    self.last_cursor = last_cursor;
                }
                if last_sequence.is_some() {
                    self.last_sequence = last_sequence;
                }
                TuiAttachState::CursorExpired
            }
        }
    }

    pub fn apply_event(&mut self, event: ApplicationProtocolEvent) -> bool {
        if event.metadata.run_id != self.run_id {
            return false;
        }
        if !self.seen_event_ids.insert(event.metadata.event_id.clone()) {
            return false;
        }

        let row = TuiRunRow::from_event(&event);
        self.last_sequence = Some(row.sequence);
        self.last_cursor = row.cursor.clone();
        self.state = state_after_event(event.kind, self.state);
        self.rows.push(row);
        true
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    pub fn state(&self) -> RunStatus {
        self.state
    }

    pub fn release_id(&self) -> &str {
        &self.release_id
    }

    pub fn last_cursor(&self) -> Option<&str> {
        self.last_cursor.as_deref()
    }

    pub fn last_sequence(&self) -> Option<u64> {
        self.last_sequence
    }

    pub fn rows(&self) -> &[TuiRunRow] {
        &self.rows
    }

    pub fn active_operations(&self) -> &[String] {
        &self.active_operations
    }

    pub fn cursor_expired(&self) -> Option<&CursorExpiredProjection> {
        self.cursor_expired.as_ref()
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "run_id": self.run_id,
            "state": run_status_name(self.state),
            "release_id": self.release_id,
            "last_cursor": self.last_cursor,
            "last_sequence": self.last_sequence,
            "waiting_count": self.waiting_count,
            "active_operations": self.active_operations,
            "rows": self.rows.iter().map(|row| {
                json!({
                    "event_id": row.event_id,
                    "sequence": row.sequence,
                    "cursor": row.cursor,
                    "kind": row.kind,
                    "summary": row.summary,
                    "severity": row.severity.as_str(),
                    "occurred_at_unix_ms": row.occurred_at_unix_ms,
                })
            }).collect::<Vec<_>>(),
            "cursor_expired": self.cursor_expired.as_ref().map(|expired| {
                json!({
                    "requested_cursor": expired.requested_cursor,
                    "earliest_available_cursor": expired.earliest_available_cursor,
                    "last_cursor": expired.last_cursor,
                    "last_sequence": expired.last_sequence,
                })
            }),
        }))
    }
}

fn event_summary(event: &ApplicationProtocolEvent) -> String {
    for field in ["message", "summary", "reason", "title"] {
        if let Some(value) = event.payload.get(field).and_then(|value| value.as_str()) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                return trimmed.to_owned();
            }
        }
    }
    event.kind.as_str().to_owned()
}

fn event_severity(kind: ApplicationProtocolEventKind) -> TuiRowSeverity {
    match kind {
        ApplicationProtocolEventKind::RunFailed
        | ApplicationProtocolEventKind::RunCancelled
        | ApplicationProtocolEventKind::RunPolicyStopped
        | ApplicationProtocolEventKind::RunExpired
        | ApplicationProtocolEventKind::OutputCutoff => TuiRowSeverity::Error,
        ApplicationProtocolEventKind::BudgetConstrained
        | ApplicationProtocolEventKind::BudgetExhausted
        | ApplicationProtocolEventKind::ExecutionDegraded
        | ApplicationProtocolEventKind::PolicyDecisionRequired
        | ApplicationProtocolEventKind::AssistantIncomplete
        | ApplicationProtocolEventKind::AssistantRetracted => TuiRowSeverity::Warning,
        _ => TuiRowSeverity::Info,
    }
}

fn state_after_event(kind: ApplicationProtocolEventKind, current: RunStatus) -> RunStatus {
    match kind {
        ApplicationProtocolEventKind::RunStarted => RunStatus::Running,
        ApplicationProtocolEventKind::RunCompleted => RunStatus::Completed,
        ApplicationProtocolEventKind::RunFailed => RunStatus::Failed,
        ApplicationProtocolEventKind::RunCancelled => RunStatus::Cancelled,
        ApplicationProtocolEventKind::RunPolicyStopped => RunStatus::PolicyStopped,
        ApplicationProtocolEventKind::RunExpired => RunStatus::Expired,
        ApplicationProtocolEventKind::BudgetExhausted => RunStatus::PausedBudget,
        ApplicationProtocolEventKind::PolicyDecisionRequired => RunStatus::PausedPolicy,
        _ => current,
    }
}

fn run_status_name(status: RunStatus) -> &'static str {
    match status {
        RunStatus::Created => "created",
        RunStatus::Validating => "validating",
        RunStatus::AdmissionPending => "admission_pending",
        RunStatus::Admitted => "admitted",
        RunStatus::Queued => "queued",
        RunStatus::Running => "running",
        RunStatus::WaitingInput => "waiting_input",
        RunStatus::WaitingApproval => "waiting_approval",
        RunStatus::WaitingReview => "waiting_review",
        RunStatus::WaitingCallback => "waiting_callback",
        RunStatus::Paused => "paused",
        RunStatus::PausedBudget => "paused_budget",
        RunStatus::PausedCallbackDelivery => "paused_callback_delivery",
        RunStatus::PausedPolicy => "paused_policy",
        RunStatus::PausedOperator => "paused_operator",
        RunStatus::Interrupted => "interrupted",
        RunStatus::Resuming => "resuming",
        RunStatus::Completed => "completed",
        RunStatus::Failed => "failed",
        RunStatus::Cancelled => "cancelled",
        RunStatus::Expired => "expired",
        RunStatus::PolicyStopped => "policy_stopped",
    }
}
