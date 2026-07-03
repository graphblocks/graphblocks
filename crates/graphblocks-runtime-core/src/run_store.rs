use std::{collections::BTreeMap, path::Path};

use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Map, Number, Value, json};

use crate::evaluation::ModelVisibleToolRef;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RunInvocationMode {
    Sync,
    Accepted,
    Background,
}

impl RunInvocationMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Sync => "sync",
            Self::Accepted => "accepted",
            Self::Background => "background",
        }
    }

    fn from_str(value: &str) -> Option<Self> {
        match value {
            "sync" => Some(Self::Sync),
            "accepted" => Some(Self::Accepted),
            "background" => Some(Self::Background),
            _ => None,
        }
    }

    pub fn is_durable(self) -> bool {
        matches!(self, Self::Accepted | Self::Background)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RunStatus {
    Created,
    Validating,
    AdmissionPending,
    Admitted,
    Queued,
    Running,
    WaitingInput,
    WaitingApproval,
    WaitingReview,
    WaitingCallback,
    Paused,
    PausedBudget,
    PausedCallbackDelivery,
    PausedPolicy,
    PausedOperator,
    Interrupted,
    Resuming,
    Completed,
    Failed,
    Cancelled,
    Expired,
    PolicyStopped,
}

impl RunStatus {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Completed | Self::Failed | Self::Cancelled | Self::Expired | Self::PolicyStopped
        )
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Created => "created",
            Self::Validating => "validating",
            Self::AdmissionPending => "admission_pending",
            Self::Admitted => "admitted",
            Self::Queued => "queued",
            Self::Running => "running",
            Self::WaitingInput => "waiting_input",
            Self::WaitingApproval => "waiting_approval",
            Self::WaitingReview => "waiting_review",
            Self::WaitingCallback => "waiting_callback",
            Self::Paused => "paused",
            Self::PausedBudget => "paused_budget",
            Self::PausedCallbackDelivery => "paused_callback_delivery",
            Self::PausedPolicy => "paused_policy",
            Self::PausedOperator => "paused_operator",
            Self::Interrupted => "interrupted",
            Self::Resuming => "resuming",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Expired => "expired",
            Self::PolicyStopped => "policy_stopped",
        }
    }

    fn from_str(status: &str) -> Option<Self> {
        match status {
            "created" => Some(Self::Created),
            "validating" => Some(Self::Validating),
            "admission_pending" => Some(Self::AdmissionPending),
            "admitted" => Some(Self::Admitted),
            "queued" => Some(Self::Queued),
            "running" => Some(Self::Running),
            "waiting_input" => Some(Self::WaitingInput),
            "waiting_approval" => Some(Self::WaitingApproval),
            "waiting_review" => Some(Self::WaitingReview),
            "waiting_callback" => Some(Self::WaitingCallback),
            "paused" => Some(Self::Paused),
            "paused_budget" => Some(Self::PausedBudget),
            "paused_callback_delivery" => Some(Self::PausedCallbackDelivery),
            "paused_policy" => Some(Self::PausedPolicy),
            "paused_operator" => Some(Self::PausedOperator),
            "interrupted" => Some(Self::Interrupted),
            "resuming" => Some(Self::Resuming),
            "completed" => Some(Self::Completed),
            "failed" => Some(Self::Failed),
            "cancelled" => Some(Self::Cancelled),
            "expired" => Some(Self::Expired),
            "policy_stopped" => Some(Self::PolicyStopped),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RunDeploymentProvenance {
    pub release_digest: Option<String>,
    pub deployment_revision_id: Option<String>,
    pub physical_plan_hash: Option<String>,
    pub release_signature_digest: Option<String>,
}

impl RunDeploymentProvenance {
    pub fn new() -> Self {
        Self {
            release_digest: None,
            deployment_revision_id: None,
            physical_plan_hash: None,
            release_signature_digest: None,
        }
    }

    pub fn with_release_digest(mut self, release_digest: impl Into<String>) -> Self {
        self.release_digest = Some(release_digest.into());
        self
    }

    pub fn with_deployment_revision_id(
        mut self,
        deployment_revision_id: impl Into<String>,
    ) -> Self {
        self.deployment_revision_id = Some(deployment_revision_id.into());
        self
    }

    pub fn with_physical_plan_hash(mut self, physical_plan_hash: impl Into<String>) -> Self {
        self.physical_plan_hash = Some(physical_plan_hash.into());
        self
    }

    pub fn with_release_signature_digest(
        mut self,
        release_signature_digest: impl Into<String>,
    ) -> Self {
        self.release_signature_digest = Some(release_signature_digest.into());
        self
    }

    pub fn canonical_value(&self) -> Value {
        json!({
            "release_digest": self.release_digest,
            "deployment_revision_id": self.deployment_revision_id,
            "physical_plan_hash": self.physical_plan_hash,
            "release_signature_digest": self.release_signature_digest,
        })
    }
}

impl Default for RunDeploymentProvenance {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProductionRunProvenanceDiagnostic {
    pub code: &'static str,
    pub field: &'static str,
    pub message: String,
}

impl ProductionRunProvenanceDiagnostic {
    pub fn for_provenance(provenance: &RunDeploymentProvenance) -> Vec<Self> {
        let mut diagnostics = Vec::new();
        if provenance.release_digest.as_deref().is_none_or(str::is_empty) {
            diagnostics.push(Self {
                code: "GB7101",
                field: "release_digest",
                message: "production runs must record signed release digest".to_owned(),
            });
        }
        if provenance
            .physical_plan_hash
            .as_deref()
            .is_none_or(str::is_empty)
        {
            diagnostics.push(Self {
                code: "GB7102",
                field: "physical_plan_hash",
                message: "production runs must record physical execution plan hash".to_owned(),
            });
        }
        if provenance
            .release_signature_digest
            .as_deref()
            .is_none_or(str::is_empty)
        {
            diagnostics.push(Self {
                code: "GB7103",
                field: "release_signature_digest",
                message: "production runs must record release signature digest".to_owned(),
            });
        }
        diagnostics
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RunRecord {
    pub run_id: String,
    pub sequence: u64,
    pub graph_hash: String,
    pub invocation_mode: RunInvocationMode,
    pub inputs: Value,
    pub deployment_provenance: RunDeploymentProvenance,
    pub model_visible_tools: Vec<ModelVisibleToolRef>,
    pub status: RunStatus,
    pub state: Value,
    pub state_revision: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunOwnershipLease {
    pub run_id: String,
    pub lease_id: String,
    pub owner: String,
    pub fencing_epoch: u64,
    pub acquired_at_unix_ms: u64,
    pub expires_at_unix_ms: u64,
}

impl RunOwnershipLease {
    pub fn is_active_at(&self, now_unix_ms: u64) -> bool {
        self.expires_at_unix_ms > now_unix_ms
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum RunStoreError {
    EmptyField {
        field: &'static str,
    },
    NotFound {
        run_id: String,
    },
    InvalidInvocationMode {
        run_id: String,
        invocation_mode: RunInvocationMode,
    },
    StateConflict {
        run_id: String,
        expected_revision: u64,
        current_revision: u64,
    },
    StatePatchAfterTerminal {
        run_id: String,
        status: RunStatus,
    },
    StatusAfterTerminal {
        run_id: String,
        status: RunStatus,
    },
    InvocationProvenanceAfterTerminal {
        run_id: String,
        status: RunStatus,
    },
    InvalidRunStatusSnapshot {
        run_id: String,
        reason: &'static str,
    },
    InvalidRunOwnershipLease {
        run_id: String,
        reason: &'static str,
    },
    RunOwnershipLeaseActive {
        run_id: String,
        owner: String,
        expires_at_unix_ms: u64,
    },
    RunOwnershipLeaseMismatch {
        run_id: String,
        expected_lease_id: String,
        actual_lease_id: String,
        expected_fencing_epoch: u64,
        actual_fencing_epoch: u64,
    },
    RunOwnershipLeaseExpired {
        run_id: String,
        lease_id: String,
        expires_at_unix_ms: u64,
        now_unix_ms: u64,
    },
    InvalidStatePath {
        path: Vec<String>,
    },
    StatePathConflict {
        path: Vec<String>,
        expected: &'static str,
    },
    NumericOverflow {
        path: Vec<String>,
    },
    Storage {
        message: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunInvocationResponse {
    pub run_id: String,
    pub status: String,
    pub mode: RunInvocationMode,
    pub event_stream: String,
    pub websocket: String,
    pub cancel: String,
    pub initial_cursor: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RunLifetime {
    ClientConnection,
    Session,
    Job,
    Background,
}

impl RunLifetime {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ClientConnection => "client_connection",
            Self::Session => "session",
            Self::Job => "job",
            Self::Background => "background",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunInvocationRouteConfig {
    pub route_id: String,
    pub invocation_mode: RunInvocationMode,
    pub cursor_replay: bool,
    pub lifetime: RunLifetime,
    pub event_retention_ms: Option<u64>,
    pub replay_guarantee_ms: Option<u64>,
}

impl RunInvocationRouteConfig {
    pub fn new(
        route_id: impl Into<String>,
        invocation_mode: RunInvocationMode,
        cursor_replay: bool,
    ) -> Result<Self, RunStoreError> {
        let route_id = route_id.into();
        if route_id.trim().is_empty() {
            return Err(RunStoreError::EmptyField { field: "route_id" });
        }
        Ok(Self {
            route_id,
            invocation_mode,
            cursor_replay,
            lifetime: match invocation_mode {
                RunInvocationMode::Sync => RunLifetime::ClientConnection,
                RunInvocationMode::Accepted => RunLifetime::Job,
                RunInvocationMode::Background => RunLifetime::Background,
            },
            event_retention_ms: None,
            replay_guarantee_ms: None,
        })
    }

    pub fn with_lifetime(mut self, lifetime: RunLifetime) -> Self {
        self.lifetime = lifetime;
        self
    }

    pub fn with_event_retention_ms(mut self, event_retention_ms: u64) -> Self {
        self.event_retention_ms = Some(event_retention_ms);
        self
    }

    pub fn with_replay_guarantee_ms(mut self, replay_guarantee_ms: u64) -> Self {
        self.replay_guarantee_ms = Some(replay_guarantee_ms);
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunInvocationRouteDiagnostic {
    pub code: &'static str,
    pub field: &'static str,
    pub message: String,
}

impl RunInvocationRouteDiagnostic {
    pub fn for_route(route: &RunInvocationRouteConfig) -> Vec<Self> {
        let mut diagnostics = Vec::new();
        if route.invocation_mode.is_durable() && !route.cursor_replay {
            diagnostics.push(Self {
                code: "GB6005",
                field: "cursor_replay",
                message: format!(
                    "durable run route {} uses {} invocation without a replayable ApplicationEventStream",
                    route.route_id,
                    route.invocation_mode.as_str()
                ),
            });
        }
        if route.invocation_mode.is_durable() && route.lifetime == RunLifetime::ClientConnection {
            diagnostics.push(Self {
                code: "GB6009",
                field: "lifetime",
                message: format!(
                    "durable run route {} uses {} invocation but is tied to {} lifetime",
                    route.route_id,
                    route.invocation_mode.as_str(),
                    route.lifetime.as_str()
                ),
            });
        }
        if let (Some(event_retention_ms), Some(replay_guarantee_ms)) =
            (route.event_retention_ms, route.replay_guarantee_ms)
            && event_retention_ms < replay_guarantee_ms
        {
            diagnostics.push(Self {
                code: "GB6013",
                field: "event_retention_ms",
                message: format!(
                    "run route {} retains events for {event_retention_ms}ms but declares a {replay_guarantee_ms}ms replay guarantee",
                    route.route_id
                ),
            });
        }
        diagnostics
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RunWaitReasonKind {
    Input,
    Approval,
    Review,
    Callback,
    CallbackDelivery,
    Budget,
    Policy,
    Operator,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunWaitReason {
    pub kind: RunWaitReasonKind,
    pub node_id: Option<String>,
    pub operation_id: Option<String>,
    pub message: Option<String>,
}

impl RunWaitReason {
    pub fn input(message: impl Into<String>) -> Result<Self, RunStoreError> {
        Self::message(RunWaitReasonKind::Input, message)
    }

    pub fn approval(message: impl Into<String>) -> Result<Self, RunStoreError> {
        Self::message(RunWaitReasonKind::Approval, message)
    }

    pub fn review(message: impl Into<String>) -> Result<Self, RunStoreError> {
        Self::message(RunWaitReasonKind::Review, message)
    }

    pub fn callback(
        operation_id: impl Into<String>,
        node_id: Option<impl Into<String>>,
    ) -> Result<Self, RunStoreError> {
        let operation_id = operation_id.into();
        if operation_id.trim().is_empty() {
            return Err(RunStoreError::EmptyField {
                field: "operation_id",
            });
        }
        let node_id = node_id.map(Into::into);
        if node_id
            .as_ref()
            .is_some_and(|node_id| node_id.trim().is_empty())
        {
            return Err(RunStoreError::EmptyField { field: "node_id" });
        }
        Ok(Self {
            kind: RunWaitReasonKind::Callback,
            node_id,
            operation_id: Some(operation_id),
            message: None,
        })
    }

    pub fn budget(message: impl Into<String>) -> Result<Self, RunStoreError> {
        Self::message(RunWaitReasonKind::Budget, message)
    }

    pub fn callback_delivery(delivery_id: impl Into<String>) -> Result<Self, RunStoreError> {
        Self::message(RunWaitReasonKind::CallbackDelivery, delivery_id)
    }

    pub fn policy(message: impl Into<String>) -> Result<Self, RunStoreError> {
        Self::message(RunWaitReasonKind::Policy, message)
    }

    pub fn operator(message: impl Into<String>) -> Result<Self, RunStoreError> {
        Self::message(RunWaitReasonKind::Operator, message)
    }

    fn message(
        kind: RunWaitReasonKind,
        message: impl Into<String>,
    ) -> Result<Self, RunStoreError> {
        let message = message.into();
        if message.trim().is_empty() {
            return Err(RunStoreError::EmptyField { field: "message" });
        }
        Ok(Self {
            kind,
            node_id: None,
            operation_id: None,
            message: Some(message),
        })
    }

    pub fn protocol_value(&self) -> Value {
        let mut value = Map::new();
        value.insert("kind".to_owned(), Value::String(self.kind.as_str().to_owned()));
        if let Some(node_id) = &self.node_id {
            value.insert("nodeId".to_owned(), Value::String(node_id.clone()));
        }
        if let Some(operation_id) = &self.operation_id {
            value.insert(
                "operationId".to_owned(),
                Value::String(operation_id.clone()),
            );
        }
        if let Some(message) = &self.message {
            value.insert("reason".to_owned(), Value::String(message.clone()));
        }
        Value::Object(value)
    }
}

impl RunWaitReasonKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Input => "input",
            Self::Approval => "approval",
            Self::Review => "review",
            Self::Callback => "callback",
            Self::CallbackDelivery => "callback_delivery",
            Self::Budget => "budget",
            Self::Policy => "policy",
            Self::Operator => "operator",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunStatusSnapshot {
    pub run_id: String,
    pub state: RunStatus,
    pub release_id: String,
    pub last_cursor: String,
    pub started_at_unix_ms: u64,
    pub updated_at_unix_ms: u64,
    pub completed_at_unix_ms: Option<u64>,
    pub waiting_on: Vec<RunWaitReason>,
    pub active_operations: Vec<String>,
}

impl RunStatusSnapshot {
    pub fn from_run(
        run: &RunRecord,
        last_cursor: impl Into<String>,
        started_at_unix_ms: u64,
        updated_at_unix_ms: u64,
        completed_at_unix_ms: Option<u64>,
        waiting_on: Vec<RunWaitReason>,
        active_operations: Vec<String>,
    ) -> Result<Self, RunStoreError> {
        let last_cursor = last_cursor.into();
        if last_cursor.trim().is_empty() {
            return Err(RunStoreError::EmptyField {
                field: "last_cursor",
            });
        }
        if updated_at_unix_ms < started_at_unix_ms {
            return Err(RunStoreError::InvalidRunStatusSnapshot {
                run_id: run.run_id.clone(),
                reason: "updated_at precedes started_at",
            });
        }
        match (run.status.is_terminal(), completed_at_unix_ms) {
            (true, None) => {
                return Err(RunStoreError::InvalidRunStatusSnapshot {
                    run_id: run.run_id.clone(),
                    reason: "terminal run requires completed_at",
                });
            }
            (false, Some(_)) => {
                return Err(RunStoreError::InvalidRunStatusSnapshot {
                    run_id: run.run_id.clone(),
                    reason: "nonterminal run cannot have completed_at",
                });
            }
            _ => {}
        }
        if completed_at_unix_ms.is_some_and(|completed_at| completed_at < updated_at_unix_ms) {
            return Err(RunStoreError::InvalidRunStatusSnapshot {
                run_id: run.run_id.clone(),
                reason: "completed_at precedes updated_at",
            });
        }
        if active_operations
            .iter()
            .any(|operation_id| operation_id.trim().is_empty())
        {
            return Err(RunStoreError::EmptyField {
                field: "active_operations",
            });
        }
        let mut sorted_active_operations = active_operations.clone();
        sorted_active_operations.sort();
        if sorted_active_operations
            .windows(2)
            .any(|window| window[0] == window[1])
        {
            return Err(RunStoreError::InvalidRunStatusSnapshot {
                run_id: run.run_id.clone(),
                reason: "active operations must not contain duplicates",
            });
        }
        if run.status.is_terminal() && (!waiting_on.is_empty() || !active_operations.is_empty()) {
            return Err(RunStoreError::InvalidRunStatusSnapshot {
                run_id: run.run_id.clone(),
                reason: "terminal run cannot expose wait reasons or active operations",
            });
        }
        if run.status == RunStatus::WaitingCallback {
            let callback_operation_ids: Vec<&str> = waiting_on
                .iter()
                .filter_map(|reason| {
                    (reason.kind == RunWaitReasonKind::Callback)
                        .then_some(reason.operation_id.as_deref())
                        .flatten()
                })
                .filter(|operation_id| !operation_id.trim().is_empty())
                .collect();
            if callback_operation_ids.is_empty() {
                return Err(RunStoreError::InvalidRunStatusSnapshot {
                    run_id: run.run_id.clone(),
                    reason: "waiting_callback requires callback wait reason",
                });
            }
            if callback_operation_ids
                .iter()
                .any(|operation_id| !active_operations.iter().any(|active| active == operation_id))
            {
                return Err(RunStoreError::InvalidRunStatusSnapshot {
                    run_id: run.run_id.clone(),
                    reason: "waiting_callback operation must be active",
                });
            }
        }
        if let Some((kind, reason)) = match run.status {
            RunStatus::WaitingInput => Some((
                RunWaitReasonKind::Input,
                "waiting_input requires input wait reason",
            )),
            RunStatus::WaitingApproval => Some((
                RunWaitReasonKind::Approval,
                "waiting_approval requires approval wait reason",
            )),
            RunStatus::WaitingReview => Some((
                RunWaitReasonKind::Review,
                "waiting_review requires review wait reason",
            )),
            RunStatus::PausedBudget => Some((
                RunWaitReasonKind::Budget,
                "paused_budget requires budget wait reason",
            )),
            RunStatus::PausedCallbackDelivery => Some((
                RunWaitReasonKind::CallbackDelivery,
                "paused_callback_delivery requires callback delivery wait reason",
            )),
            RunStatus::PausedPolicy => Some((
                RunWaitReasonKind::Policy,
                "paused_policy requires policy wait reason",
            )),
            RunStatus::PausedOperator => Some((
                RunWaitReasonKind::Operator,
                "paused_operator requires operator wait reason",
            )),
            _ => None,
        } && !waiting_on.iter().any(|wait_reason| wait_reason.kind == kind)
        {
            return Err(RunStoreError::InvalidRunStatusSnapshot {
                run_id: run.run_id.clone(),
                reason,
            });
        }

        let active_operations = sorted_active_operations;

        Ok(Self {
            run_id: run.run_id.clone(),
            state: run.status,
            release_id: run
                .deployment_provenance
                .release_digest
                .clone()
                .unwrap_or_else(|| run.graph_hash.clone()),
            last_cursor,
            started_at_unix_ms,
            updated_at_unix_ms,
            completed_at_unix_ms,
            waiting_on,
            active_operations,
        })
    }

    pub fn protocol_value(&self) -> Value {
        json!({
            "runId": self.run_id,
            "state": self.state.as_str(),
            "releaseId": self.release_id,
            "lastCursor": self.last_cursor,
            "startedAtUnixMs": self.started_at_unix_ms,
            "updatedAtUnixMs": self.updated_at_unix_ms,
            "completedAtUnixMs": self.completed_at_unix_ms,
            "waitingOn": self.waiting_on.iter().map(RunWaitReason::protocol_value).collect::<Vec<_>>(),
            "activeOperations": self.active_operations,
        })
    }
}

impl RunInvocationResponse {
    pub fn from_accepted_run(
        run: &RunRecord,
        base_path: impl AsRef<str>,
        initial_cursor: impl Into<String>,
    ) -> Result<Self, RunStoreError> {
        if !run.invocation_mode.is_durable() {
            return Err(RunStoreError::InvalidInvocationMode {
                run_id: run.run_id.clone(),
                invocation_mode: run.invocation_mode,
            });
        }
        let initial_cursor = initial_cursor.into();
        if initial_cursor.trim().is_empty() {
            return Err(RunStoreError::EmptyField {
                field: "initial_cursor",
            });
        }
        let base_path = base_path.as_ref().trim_end_matches('/');
        Ok(Self {
            run_id: run.run_id.clone(),
            status: "accepted".to_owned(),
            mode: run.invocation_mode,
            event_stream: format!("{base_path}/runs/{}/events", run.run_id),
            websocket: format!("{base_path}/runs/{}/ws", run.run_id),
            cancel: format!("{base_path}/runs/{}/cancel", run.run_id),
            initial_cursor,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct StatePatch {
    pub expected_revision: Option<u64>,
    pub operations: Vec<PatchOperation>,
}

impl StatePatch {
    pub fn new(expected_revision: Option<u64>) -> Self {
        Self {
            expected_revision,
            operations: Vec::new(),
        }
    }

    pub fn with(mut self, operation: PatchOperation) -> Self {
        self.operations.push(operation);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum PatchOperation {
    Set { path: Vec<String>, value: Value },
    Merge { path: Vec<String>, value: Value },
    Remove { path: Vec<String> },
    Increment { path: Vec<String>, amount: i64 },
    Append { path: Vec<String>, value: Value },
}

impl PatchOperation {
    pub fn set<I, S>(path: I, value: Value) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Set {
            path: path.into_iter().map(Into::into).collect(),
            value,
        }
    }

    pub fn merge<I, S>(path: I, value: Value) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Merge {
            path: path.into_iter().map(Into::into).collect(),
            value,
        }
    }

    pub fn remove<I, S>(path: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Remove {
            path: path.into_iter().map(Into::into).collect(),
        }
    }

    pub fn increment<I, S>(path: I, amount: i64) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Increment {
            path: path.into_iter().map(Into::into).collect(),
            amount,
        }
    }

    pub fn append<I, S>(path: I, value: Value) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::Append {
            path: path.into_iter().map(Into::into).collect(),
            value,
        }
    }
}

fn record_with_status(current: &RunRecord, status: RunStatus) -> Result<RunRecord, RunStoreError> {
    if current.status.is_terminal() {
        return Err(RunStoreError::StatusAfterTerminal {
            run_id: current.run_id.clone(),
            status: current.status,
        });
    }

    let mut updated = current.clone();
    updated.status = status;
    Ok(updated)
}

fn record_with_state_patch(
    current: &RunRecord,
    patch: &StatePatch,
) -> Result<RunRecord, RunStoreError> {
    if current.status.is_terminal() {
        return Err(RunStoreError::StatePatchAfterTerminal {
            run_id: current.run_id.clone(),
            status: current.status,
        });
    }
    if let Some(expected_revision) = patch.expected_revision
        && current.state_revision != expected_revision
    {
        return Err(RunStoreError::StateConflict {
            run_id: current.run_id.clone(),
            expected_revision,
            current_revision: current.state_revision,
        });
    }

    let mut next_state = current.state.clone();
    'operations: for operation in &patch.operations {
        match operation {
            PatchOperation::Set { path, value } => {
                if path.is_empty() {
                    return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                }
                let mut cursor = &mut next_state;
                for segment in &path[..path.len() - 1] {
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    cursor = object
                        .entry(segment.clone())
                        .or_insert_with(|| Value::Object(Map::new()));
                }
                let Value::Object(object) = cursor else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "object parent",
                    });
                };
                object.insert(path[path.len() - 1].clone(), value.clone());
            }
            PatchOperation::Merge { path, value } => {
                if path.is_empty() {
                    return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                }
                let Value::Object(source) = value else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "object value",
                    });
                };
                let mut cursor = &mut next_state;
                for segment in &path[..path.len() - 1] {
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    cursor = object
                        .entry(segment.clone())
                        .or_insert_with(|| Value::Object(Map::new()));
                }
                let Value::Object(object) = cursor else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "object parent",
                    });
                };
                let target = object
                    .entry(path[path.len() - 1].clone())
                    .or_insert_with(|| Value::Object(Map::new()));
                let Value::Object(target_object) = target else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "object target",
                    });
                };
                for (key, next_value) in source {
                    target_object.insert(key.clone(), next_value.clone());
                }
            }
            PatchOperation::Remove { path } => {
                if path.is_empty() {
                    return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                }
                let mut cursor = &mut next_state;
                for segment in &path[..path.len() - 1] {
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    let Some(next_cursor) = object.get_mut(segment) else {
                        continue 'operations;
                    };
                    cursor = next_cursor;
                }
                let Value::Object(object) = cursor else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "object parent",
                    });
                };
                object.remove(&path[path.len() - 1]);
            }
            PatchOperation::Increment { path, amount } => {
                if path.is_empty() {
                    return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                }
                let mut cursor = &mut next_state;
                for segment in &path[..path.len() - 1] {
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    cursor = object
                        .entry(segment.clone())
                        .or_insert_with(|| Value::Object(Map::new()));
                }
                let Value::Object(object) = cursor else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "object parent",
                    });
                };
                let target = object
                    .entry(path[path.len() - 1].clone())
                    .or_insert_with(|| Value::Number(Number::from(0)));
                let Some(current_number) = target.as_i64() else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "integer target",
                    });
                };
                let Some(next_number) = current_number.checked_add(*amount) else {
                    return Err(RunStoreError::NumericOverflow { path: path.clone() });
                };
                *target = Value::Number(Number::from(next_number));
            }
            PatchOperation::Append { path, value } => {
                if path.is_empty() {
                    return Err(RunStoreError::InvalidStatePath { path: path.clone() });
                }
                let mut cursor = &mut next_state;
                for segment in &path[..path.len() - 1] {
                    let Value::Object(object) = cursor else {
                        return Err(RunStoreError::StatePathConflict {
                            path: path.clone(),
                            expected: "object parent",
                        });
                    };
                    cursor = object
                        .entry(segment.clone())
                        .or_insert_with(|| Value::Object(Map::new()));
                }
                let Value::Object(object) = cursor else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "object parent",
                    });
                };
                let target = object
                    .entry(path[path.len() - 1].clone())
                    .or_insert_with(|| Value::Array(Vec::new()));
                let Value::Array(target_array) = target else {
                    return Err(RunStoreError::StatePathConflict {
                        path: path.clone(),
                        expected: "array target",
                    });
                };
                target_array.push(value.clone());
            }
        }
    }

    let mut updated = current.clone();
    updated.state = next_state;
    updated.state_revision += 1;
    Ok(updated)
}

fn record_with_model_visible_tools(
    current: &RunRecord,
    model_visible_tools: Vec<ModelVisibleToolRef>,
) -> Result<RunRecord, RunStoreError> {
    if current.status.is_terminal() {
        return Err(RunStoreError::InvocationProvenanceAfterTerminal {
            run_id: current.run_id.clone(),
            status: current.status,
        });
    }

    let mut updated = current.clone();
    updated.model_visible_tools = sorted_model_visible_tools(model_visible_tools);
    Ok(updated)
}

fn validate_lease_request(
    run_id: &str,
    owner: &str,
    acquired_at_unix_ms: u64,
    expires_at_unix_ms: u64,
) -> Result<(), RunStoreError> {
    if run_id.trim().is_empty() {
        return Err(RunStoreError::EmptyField { field: "run_id" });
    }
    if owner.trim().is_empty() {
        return Err(RunStoreError::EmptyField { field: "owner" });
    }
    if expires_at_unix_ms <= acquired_at_unix_ms {
        return Err(RunStoreError::InvalidRunOwnershipLease {
            run_id: run_id.to_owned(),
            reason: "lease expiration must be after acquisition",
        });
    }
    Ok(())
}

fn acquire_run_ownership_lease(
    run_id: &str,
    owner: &str,
    acquired_at_unix_ms: u64,
    expires_at_unix_ms: u64,
    current: Option<&RunOwnershipLease>,
) -> Result<RunOwnershipLease, RunStoreError> {
    validate_lease_request(run_id, owner, acquired_at_unix_ms, expires_at_unix_ms)?;
    if let Some(current) = current
        && current.is_active_at(acquired_at_unix_ms)
    {
        return Err(RunStoreError::RunOwnershipLeaseActive {
            run_id: run_id.to_owned(),
            owner: current.owner.clone(),
            expires_at_unix_ms: current.expires_at_unix_ms,
        });
    }

    let fencing_epoch = current
        .map(|lease| lease.fencing_epoch.saturating_add(1))
        .unwrap_or(1);
    Ok(RunOwnershipLease {
        run_id: run_id.to_owned(),
        lease_id: format!("{run_id}:{fencing_epoch}"),
        owner: owner.to_owned(),
        fencing_epoch,
        acquired_at_unix_ms,
        expires_at_unix_ms,
    })
}

fn validate_run_ownership_lease(
    run_id: &str,
    lease_id: &str,
    fencing_epoch: u64,
    now_unix_ms: u64,
    current: &RunOwnershipLease,
) -> Result<(), RunStoreError> {
    if current.lease_id != lease_id || current.fencing_epoch != fencing_epoch {
        return Err(RunStoreError::RunOwnershipLeaseMismatch {
            run_id: run_id.to_owned(),
            expected_lease_id: current.lease_id.clone(),
            actual_lease_id: lease_id.to_owned(),
            expected_fencing_epoch: current.fencing_epoch,
            actual_fencing_epoch: fencing_epoch,
        });
    }
    if !current.is_active_at(now_unix_ms) {
        return Err(RunStoreError::RunOwnershipLeaseExpired {
            run_id: run_id.to_owned(),
            lease_id: lease_id.to_owned(),
            expires_at_unix_ms: current.expires_at_unix_ms,
            now_unix_ms,
        });
    }
    Ok(())
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct InMemoryRunStore {
    runs: BTreeMap<String, RunRecord>,
    ownership_leases: BTreeMap<String, RunOwnershipLease>,
    next_sequence: u64,
}

impl InMemoryRunStore {
    pub fn new() -> Self {
        Self {
            runs: BTreeMap::new(),
            ownership_leases: BTreeMap::new(),
            next_sequence: 1,
        }
    }

    pub fn create_run(&mut self, graph_hash: impl Into<String>, inputs: Value) -> RunRecord {
        self.create_run_with_provenance(graph_hash, inputs, RunDeploymentProvenance::new())
    }

    pub fn create_run_with_invocation_mode(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        invocation_mode: RunInvocationMode,
    ) -> RunRecord {
        self.create_run_with_invocation_provenance_and_mode(
            graph_hash,
            inputs,
            invocation_mode,
            RunDeploymentProvenance::new(),
            Vec::new(),
        )
    }

    pub fn create_run_with_provenance(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        deployment_provenance: RunDeploymentProvenance,
    ) -> RunRecord {
        self.create_run_with_invocation_provenance_and_mode(
            graph_hash,
            inputs,
            RunInvocationMode::Sync,
            deployment_provenance,
            Vec::new(),
        )
    }

    pub fn create_run_with_invocation_provenance(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        deployment_provenance: RunDeploymentProvenance,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> RunRecord {
        self.create_run_with_invocation_provenance_and_mode(
            graph_hash,
            inputs,
            RunInvocationMode::Sync,
            deployment_provenance,
            model_visible_tools,
        )
    }

    pub fn create_run_with_invocation_provenance_and_mode(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        invocation_mode: RunInvocationMode,
        deployment_provenance: RunDeploymentProvenance,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> RunRecord {
        let sequence = self.next_sequence;
        self.next_sequence += 1;
        let run_id = format!("run-{sequence:06}");
        let record = RunRecord {
            run_id: run_id.clone(),
            sequence,
            graph_hash: graph_hash.into(),
            invocation_mode,
            inputs,
            deployment_provenance,
            model_visible_tools: sorted_model_visible_tools(model_visible_tools),
            status: RunStatus::Created,
            state: Value::Object(Map::new()),
            state_revision: 0,
        };
        self.runs.insert(run_id, record.clone());
        record
    }

    pub fn get_run(&self, run_id: impl AsRef<str>) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let Some(record) = self.runs.get(run_id) else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };
        Ok(record.clone())
    }

    pub fn set_status(
        &mut self,
        run_id: impl AsRef<str>,
        status: RunStatus,
    ) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let Some(current) = self.runs.get(run_id) else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };

        let updated = record_with_status(current, status)?;
        self.runs.insert(run_id.to_owned(), updated.clone());
        Ok(updated)
    }

    pub fn set_status_with_ownership_lease(
        &mut self,
        run_id: impl AsRef<str>,
        status: RunStatus,
        lease_id: impl AsRef<str>,
        fencing_epoch: u64,
        now_unix_ms: u64,
    ) -> Result<RunRecord, RunStoreError> {
        self.validate_ownership_lease(
            run_id.as_ref(),
            lease_id.as_ref(),
            fencing_epoch,
            now_unix_ms,
        )?;
        self.set_status(run_id, status)
    }

    pub fn record_model_visible_tools(
        &mut self,
        run_id: impl AsRef<str>,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let Some(current) = self.runs.get(run_id) else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };

        let updated = record_with_model_visible_tools(current, model_visible_tools)?;
        self.runs.insert(run_id.to_owned(), updated.clone());
        Ok(updated)
    }

    pub fn patch_state(
        &mut self,
        run_id: impl AsRef<str>,
        patch: StatePatch,
    ) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let Some(current) = self.runs.get(run_id) else {
            return Err(RunStoreError::NotFound {
                run_id: run_id.to_owned(),
            });
        };

        let updated = record_with_state_patch(current, &patch)?;
        self.runs.insert(run_id.to_owned(), updated.clone());
        Ok(updated)
    }

    pub fn patch_state_with_ownership_lease(
        &mut self,
        run_id: impl AsRef<str>,
        patch: StatePatch,
        lease_id: impl AsRef<str>,
        fencing_epoch: u64,
        now_unix_ms: u64,
    ) -> Result<RunRecord, RunStoreError> {
        self.validate_ownership_lease(
            run_id.as_ref(),
            lease_id.as_ref(),
            fencing_epoch,
            now_unix_ms,
        )?;
        self.patch_state(run_id, patch)
    }

    pub fn acquire_ownership_lease(
        &mut self,
        run_id: impl AsRef<str>,
        owner: impl AsRef<str>,
        acquired_at_unix_ms: u64,
        expires_at_unix_ms: u64,
    ) -> Result<RunOwnershipLease, RunStoreError> {
        let run_id = run_id.as_ref();
        self.get_run(run_id)?;
        let lease = acquire_run_ownership_lease(
            run_id,
            owner.as_ref(),
            acquired_at_unix_ms,
            expires_at_unix_ms,
            self.ownership_leases.get(run_id),
        )?;
        self.ownership_leases
            .insert(run_id.to_owned(), lease.clone());
        Ok(lease)
    }

    pub fn validate_ownership_lease(
        &self,
        run_id: impl AsRef<str>,
        lease_id: impl AsRef<str>,
        fencing_epoch: u64,
        now_unix_ms: u64,
    ) -> Result<(), RunStoreError> {
        let run_id = run_id.as_ref();
        self.get_run(run_id)?;
        let current = self.ownership_leases.get(run_id).ok_or_else(|| {
            RunStoreError::RunOwnershipLeaseMismatch {
                run_id: run_id.to_owned(),
                expected_lease_id: String::new(),
                actual_lease_id: lease_id.as_ref().to_owned(),
                expected_fencing_epoch: 0,
                actual_fencing_epoch: fencing_epoch,
            }
        })?;
        validate_run_ownership_lease(
            run_id,
            lease_id.as_ref(),
            fencing_epoch,
            now_unix_ms,
            current,
        )
    }
}

pub struct SqliteRunStore {
    connection: Connection,
}

impl SqliteRunStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, RunStoreError> {
        let connection = Connection::open(path).map_err(storage_error)?;
        let store = Self { connection };
        store.initialize()?;
        Ok(store)
    }

    pub fn open_in_memory() -> Result<Self, RunStoreError> {
        let connection = Connection::open_in_memory().map_err(storage_error)?;
        let store = Self { connection };
        store.initialize()?;
        Ok(store)
    }

    fn initialize(&self) -> Result<(), RunStoreError> {
        self.connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS runs (
                    sequence INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    graph_hash TEXT NOT NULL,
                    invocation_mode TEXT NOT NULL DEFAULT 'sync',
                    inputs_json TEXT NOT NULL,
                    deployment_provenance_json TEXT NOT NULL,
                    model_visible_tools_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    state_revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_ownership_leases (
                    run_id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    fencing_epoch INTEGER NOT NULL,
                    acquired_at_unix_ms INTEGER NOT NULL,
                    expires_at_unix_ms INTEGER NOT NULL
                );
                ",
            )
            .map_err(storage_error)?;
        let columns = self
            .connection
            .prepare("PRAGMA table_info(runs)")
            .map_err(storage_error)?
            .query_map([], |row| row.get::<_, String>(1))
            .map_err(storage_error)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(storage_error)?;
        if !columns
            .iter()
            .any(|name| name == "deployment_provenance_json")
        {
            self.connection
                .execute(
                    "ALTER TABLE runs ADD COLUMN deployment_provenance_json TEXT",
                    [],
                )
                .map_err(storage_error)?;
            self.connection
                .execute(
                    "UPDATE runs SET deployment_provenance_json = ? WHERE deployment_provenance_json IS NULL",
                    params![storage_json(&RunDeploymentProvenance::new().canonical_value())?],
                )
                .map_err(storage_error)?;
        }
        if !columns
            .iter()
            .any(|name| name == "model_visible_tools_json")
        {
            self.connection
                .execute(
                    "ALTER TABLE runs ADD COLUMN model_visible_tools_json TEXT",
                    [],
                )
                .map_err(storage_error)?;
            self.connection
                .execute(
                    "UPDATE runs SET model_visible_tools_json = ? WHERE model_visible_tools_json IS NULL",
                    params![storage_json(&Value::Array(Vec::new()))?],
                )
                .map_err(storage_error)?;
        }
        if !columns.iter().any(|name| name == "invocation_mode") {
            self.connection
                .execute(
                    "ALTER TABLE runs ADD COLUMN invocation_mode TEXT NOT NULL DEFAULT 'sync'",
                    [],
                )
                .map_err(storage_error)?;
        }
        Ok(())
    }

    pub fn create_run(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
    ) -> Result<RunRecord, RunStoreError> {
        self.create_run_with_provenance(graph_hash, inputs, RunDeploymentProvenance::new())
    }

    pub fn create_run_with_invocation_mode(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        invocation_mode: RunInvocationMode,
    ) -> Result<RunRecord, RunStoreError> {
        self.create_run_with_invocation_provenance_and_mode(
            graph_hash,
            inputs,
            invocation_mode,
            RunDeploymentProvenance::new(),
            Vec::new(),
        )
    }

    pub fn create_run_with_provenance(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        deployment_provenance: RunDeploymentProvenance,
    ) -> Result<RunRecord, RunStoreError> {
        self.create_run_with_invocation_provenance_and_mode(
            graph_hash,
            inputs,
            RunInvocationMode::Sync,
            deployment_provenance,
            Vec::new(),
        )
    }

    pub fn create_run_with_invocation_provenance(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        deployment_provenance: RunDeploymentProvenance,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> Result<RunRecord, RunStoreError> {
        self.create_run_with_invocation_provenance_and_mode(
            graph_hash,
            inputs,
            RunInvocationMode::Sync,
            deployment_provenance,
            model_visible_tools,
        )
    }

    pub fn create_run_with_invocation_provenance_and_mode(
        &mut self,
        graph_hash: impl Into<String>,
        inputs: Value,
        invocation_mode: RunInvocationMode,
        deployment_provenance: RunDeploymentProvenance,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> Result<RunRecord, RunStoreError> {
        let transaction = self.connection.transaction().map_err(storage_error)?;
        let next_sequence = transaction
            .query_row(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runs",
                [],
                |row| row.get::<_, i64>(0),
            )
            .map_err(storage_error)?;
        let sequence = sqlite_i64_to_u64(next_sequence, "run sequence")?;
        let run_id = format!("run-{sequence:06}");
        let record = RunRecord {
            run_id,
            sequence,
            graph_hash: graph_hash.into(),
            invocation_mode,
            inputs,
            deployment_provenance,
            model_visible_tools: sorted_model_visible_tools(model_visible_tools),
            status: RunStatus::Created,
            state: Value::Object(Map::new()),
            state_revision: 0,
        };
        transaction
            .execute(
                "
                INSERT INTO runs (
                    sequence,
                    run_id,
                    graph_hash,
                    invocation_mode,
                    inputs_json,
                    deployment_provenance_json,
                    model_visible_tools_json,
                    status,
                    state_json,
                    state_revision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    sqlite_u64_to_i64(record.sequence, "run sequence")?,
                    &record.run_id,
                    &record.graph_hash,
                    record.invocation_mode.as_str(),
                    storage_json(&record.inputs)?,
                    storage_json(&record.deployment_provenance.canonical_value())?,
                    storage_json(&model_visible_tools_value(&record.model_visible_tools))?,
                    record.status.as_str(),
                    storage_json(&record.state)?,
                    sqlite_u64_to_i64(record.state_revision, "state revision")?,
                ],
            )
            .map_err(storage_error)?;
        transaction.commit().map_err(storage_error)?;
        Ok(record)
    }

    pub fn get_run(&self, run_id: impl AsRef<str>) -> Result<RunRecord, RunStoreError> {
        sqlite_get_run(&self.connection, run_id.as_ref())
    }

    pub fn set_status(
        &mut self,
        run_id: impl AsRef<str>,
        status: RunStatus,
    ) -> Result<RunRecord, RunStoreError> {
        let current = self.get_run(run_id.as_ref())?;
        let updated = record_with_status(&current, status)?;
        self.connection
            .execute(
                "UPDATE runs SET status = ? WHERE run_id = ?",
                params![updated.status.as_str(), &updated.run_id],
            )
            .map_err(storage_error)?;
        Ok(updated)
    }

    pub fn set_status_with_ownership_lease(
        &mut self,
        run_id: impl AsRef<str>,
        status: RunStatus,
        lease_id: impl AsRef<str>,
        fencing_epoch: u64,
        now_unix_ms: u64,
    ) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let transaction = self.connection.transaction().map_err(storage_error)?;
        let current_lease =
            sqlite_load_run_ownership_lease(&transaction, run_id)?.ok_or_else(|| {
                RunStoreError::RunOwnershipLeaseMismatch {
                    run_id: run_id.to_owned(),
                    expected_lease_id: String::new(),
                    actual_lease_id: lease_id.as_ref().to_owned(),
                    expected_fencing_epoch: 0,
                    actual_fencing_epoch: fencing_epoch,
                }
            })?;
        validate_run_ownership_lease(
            run_id,
            lease_id.as_ref(),
            fencing_epoch,
            now_unix_ms,
            &current_lease,
        )?;
        let current = sqlite_get_run(&transaction, run_id)?;
        let updated = record_with_status(&current, status)?;
        transaction
            .execute(
                "UPDATE runs SET status = ? WHERE run_id = ?",
                params![updated.status.as_str(), &updated.run_id],
            )
            .map_err(storage_error)?;
        transaction.commit().map_err(storage_error)?;
        Ok(updated)
    }

    pub fn record_model_visible_tools(
        &mut self,
        run_id: impl AsRef<str>,
        model_visible_tools: Vec<ModelVisibleToolRef>,
    ) -> Result<RunRecord, RunStoreError> {
        let current = self.get_run(run_id.as_ref())?;
        let updated = record_with_model_visible_tools(&current, model_visible_tools)?;
        self.connection
            .execute(
                "
                UPDATE runs
                SET model_visible_tools_json = ?
                WHERE run_id = ?
                ",
                params![
                    storage_json(&model_visible_tools_value(&updated.model_visible_tools))?,
                    &updated.run_id,
                ],
            )
            .map_err(storage_error)?;
        Ok(updated)
    }

    pub fn patch_state(
        &mut self,
        run_id: impl AsRef<str>,
        patch: StatePatch,
    ) -> Result<RunRecord, RunStoreError> {
        let current = self.get_run(run_id.as_ref())?;
        let updated = record_with_state_patch(&current, &patch)?;
        self.connection
            .execute(
                "
                UPDATE runs
                SET state_json = ?, state_revision = ?
                WHERE run_id = ?
                ",
                params![
                    storage_json(&updated.state)?,
                    sqlite_u64_to_i64(updated.state_revision, "state revision")?,
                    &updated.run_id,
                ],
            )
            .map_err(storage_error)?;
        Ok(updated)
    }

    pub fn patch_state_with_ownership_lease(
        &mut self,
        run_id: impl AsRef<str>,
        patch: StatePatch,
        lease_id: impl AsRef<str>,
        fencing_epoch: u64,
        now_unix_ms: u64,
    ) -> Result<RunRecord, RunStoreError> {
        let run_id = run_id.as_ref();
        let transaction = self.connection.transaction().map_err(storage_error)?;
        let current_lease =
            sqlite_load_run_ownership_lease(&transaction, run_id)?.ok_or_else(|| {
                RunStoreError::RunOwnershipLeaseMismatch {
                    run_id: run_id.to_owned(),
                    expected_lease_id: String::new(),
                    actual_lease_id: lease_id.as_ref().to_owned(),
                    expected_fencing_epoch: 0,
                    actual_fencing_epoch: fencing_epoch,
                }
            })?;
        validate_run_ownership_lease(
            run_id,
            lease_id.as_ref(),
            fencing_epoch,
            now_unix_ms,
            &current_lease,
        )?;
        let current = sqlite_get_run(&transaction, run_id)?;
        let updated = record_with_state_patch(&current, &patch)?;
        transaction
            .execute(
                "
                UPDATE runs
                SET state_json = ?, state_revision = ?
                WHERE run_id = ?
                ",
                params![
                    storage_json(&updated.state)?,
                    sqlite_u64_to_i64(updated.state_revision, "state revision")?,
                    &updated.run_id,
                ],
            )
            .map_err(storage_error)?;
        transaction.commit().map_err(storage_error)?;
        Ok(updated)
    }

    pub fn acquire_ownership_lease(
        &mut self,
        run_id: impl AsRef<str>,
        owner: impl AsRef<str>,
        acquired_at_unix_ms: u64,
        expires_at_unix_ms: u64,
    ) -> Result<RunOwnershipLease, RunStoreError> {
        let run_id = run_id.as_ref();
        self.get_run(run_id)?;
        let transaction = self.connection.transaction().map_err(storage_error)?;
        let current = sqlite_load_run_ownership_lease(&transaction, run_id)?;
        let lease = acquire_run_ownership_lease(
            run_id,
            owner.as_ref(),
            acquired_at_unix_ms,
            expires_at_unix_ms,
            current.as_ref(),
        )?;
        sqlite_upsert_run_ownership_lease(&transaction, &lease)?;
        transaction.commit().map_err(storage_error)?;
        Ok(lease)
    }

    pub fn validate_ownership_lease(
        &self,
        run_id: impl AsRef<str>,
        lease_id: impl AsRef<str>,
        fencing_epoch: u64,
        now_unix_ms: u64,
    ) -> Result<(), RunStoreError> {
        let run_id = run_id.as_ref();
        self.get_run(run_id)?;
        let current =
            sqlite_load_run_ownership_lease(&self.connection, run_id)?.ok_or_else(|| {
                RunStoreError::RunOwnershipLeaseMismatch {
                    run_id: run_id.to_owned(),
                    expected_lease_id: String::new(),
                    actual_lease_id: lease_id.as_ref().to_owned(),
                    expected_fencing_epoch: 0,
                    actual_fencing_epoch: fencing_epoch,
                }
            })?;
        validate_run_ownership_lease(
            run_id,
            lease_id.as_ref(),
            fencing_epoch,
            now_unix_ms,
            &current,
        )
    }
}

fn sqlite_load_run_ownership_lease(
    connection: &Connection,
    run_id: &str,
) -> Result<Option<RunOwnershipLease>, RunStoreError> {
    connection
        .query_row(
            "
            SELECT
                run_id,
                lease_id,
                owner,
                fencing_epoch,
                acquired_at_unix_ms,
                expires_at_unix_ms
            FROM run_ownership_leases
            WHERE run_id = ?
            ",
            params![run_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, i64>(5)?,
                ))
            },
        )
        .optional()
        .map_err(storage_error)?
        .map(
            |(run_id, lease_id, owner, fencing_epoch, acquired_at_unix_ms, expires_at_unix_ms)| {
                Ok(RunOwnershipLease {
                    run_id,
                    lease_id,
                    owner,
                    fencing_epoch: sqlite_i64_to_u64(fencing_epoch, "run ownership fencing epoch")?,
                    acquired_at_unix_ms: sqlite_i64_to_u64(
                        acquired_at_unix_ms,
                        "run ownership acquired_at",
                    )?,
                    expires_at_unix_ms: sqlite_i64_to_u64(
                        expires_at_unix_ms,
                        "run ownership expires_at",
                    )?,
                })
            },
        )
        .transpose()
}

fn sqlite_get_run(connection: &Connection, run_id: &str) -> Result<RunRecord, RunStoreError> {
    let row = connection
        .query_row(
            "
            SELECT
                sequence,
                run_id,
                graph_hash,
                invocation_mode,
                inputs_json,
                deployment_provenance_json,
                model_visible_tools_json,
                status,
                state_json,
                state_revision
            FROM runs
            WHERE run_id = ?
            ",
            params![run_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, String>(8)?,
                    row.get::<_, i64>(9)?,
                ))
            },
        )
        .optional()
        .map_err(storage_error)?;
    let Some((
        sequence,
        run_id,
        graph_hash,
        invocation_mode,
        inputs,
        deployment_provenance,
        model_visible_tools,
        status,
        state,
        state_revision,
    )) = row
    else {
        return Err(RunStoreError::NotFound {
            run_id: run_id.to_owned(),
        });
    };
    record_from_storage(
        sequence,
        run_id,
        graph_hash,
        invocation_mode,
        inputs,
        deployment_provenance,
        model_visible_tools,
        status,
        state,
        state_revision,
    )
}

fn sqlite_upsert_run_ownership_lease(
    connection: &Connection,
    lease: &RunOwnershipLease,
) -> Result<(), RunStoreError> {
    connection
        .execute(
            "
            INSERT INTO run_ownership_leases (
                run_id,
                lease_id,
                owner,
                fencing_epoch,
                acquired_at_unix_ms,
                expires_at_unix_ms
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                lease_id = excluded.lease_id,
                owner = excluded.owner,
                fencing_epoch = excluded.fencing_epoch,
                acquired_at_unix_ms = excluded.acquired_at_unix_ms,
                expires_at_unix_ms = excluded.expires_at_unix_ms
            ",
            params![
                &lease.run_id,
                &lease.lease_id,
                &lease.owner,
                sqlite_u64_to_i64(lease.fencing_epoch, "run ownership fencing epoch")?,
                sqlite_u64_to_i64(lease.acquired_at_unix_ms, "run ownership acquired_at")?,
                sqlite_u64_to_i64(lease.expires_at_unix_ms, "run ownership expires_at")?,
            ],
        )
        .map_err(storage_error)?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn record_from_storage(
    sequence: i64,
    run_id: String,
    graph_hash: String,
    invocation_mode: String,
    inputs: String,
    deployment_provenance: String,
    model_visible_tools: String,
    status: String,
    state: String,
    state_revision: i64,
) -> Result<RunRecord, RunStoreError> {
    let invocation_mode =
        RunInvocationMode::from_str(&invocation_mode).ok_or_else(|| RunStoreError::Storage {
            message: format!("unknown stored run invocation mode {invocation_mode:?}"),
        })?;
    let status = RunStatus::from_str(&status).ok_or_else(|| RunStoreError::Storage {
        message: format!("unknown stored run status {status:?}"),
    })?;
    Ok(RunRecord {
        run_id,
        sequence: sqlite_i64_to_u64(sequence, "run sequence")?,
        graph_hash,
        invocation_mode,
        inputs: parse_storage_json(&inputs)?,
        deployment_provenance: deployment_provenance_from_storage(&deployment_provenance)?,
        model_visible_tools: model_visible_tools_from_storage(&model_visible_tools)?,
        status,
        state: parse_storage_json(&state)?,
        state_revision: sqlite_i64_to_u64(state_revision, "state revision")?,
    })
}

fn storage_json(value: &Value) -> Result<String, RunStoreError> {
    serde_json::to_string(value).map_err(storage_error)
}

fn parse_storage_json(text: &str) -> Result<Value, RunStoreError> {
    serde_json::from_str(text).map_err(storage_error)
}

fn deployment_provenance_from_storage(
    text: &str,
) -> Result<RunDeploymentProvenance, RunStoreError> {
    let value = parse_storage_json(text)?;
    let Some(object) = value.as_object() else {
        return Ok(RunDeploymentProvenance::new());
    };
    Ok(RunDeploymentProvenance {
        release_digest: optional_string(object, "release_digest"),
        deployment_revision_id: optional_string(object, "deployment_revision_id"),
        physical_plan_hash: optional_string(object, "physical_plan_hash"),
        release_signature_digest: optional_string(object, "release_signature_digest"),
    })
}

fn model_visible_tools_value(tools: &[ModelVisibleToolRef]) -> Value {
    Value::Array(
        tools
            .iter()
            .map(|tool| {
                json!({
                    "tool_name": tool.tool_name,
                    "resolved_tool_id": tool.resolved_tool_id,
                    "definition_digest": tool.definition_digest,
                    "binding_digest": tool.binding_digest,
                    "effective_policy_snapshot_id": tool.effective_policy_snapshot_id,
                    "allowed_for_principal": tool.allowed_for_principal,
                    "valid_until_unix_ms": tool.valid_until_unix_ms,
                })
            })
            .collect(),
    )
}

fn model_visible_tools_from_storage(text: &str) -> Result<Vec<ModelVisibleToolRef>, RunStoreError> {
    let value = parse_storage_json(text)?;
    let Some(array) = value.as_array() else {
        return Ok(Vec::new());
    };
    let mut tools = Vec::with_capacity(array.len());
    for item in array {
        let Some(object) = item.as_object() else {
            return Err(RunStoreError::Storage {
                message: "stored model-visible tool provenance must be an array of objects"
                    .to_owned(),
            });
        };
        let valid_until_unix_ms = match object.get("valid_until_unix_ms") {
            Some(Value::Null) | None => None,
            Some(value) => value.as_u64(),
        };
        tools.push(ModelVisibleToolRef {
            tool_name: required_storage_string(object, "tool_name")?,
            resolved_tool_id: required_storage_string(object, "resolved_tool_id")?,
            definition_digest: required_storage_string(object, "definition_digest")?,
            binding_digest: required_storage_string(object, "binding_digest")?,
            effective_policy_snapshot_id: required_storage_string(
                object,
                "effective_policy_snapshot_id",
            )?,
            allowed_for_principal: object
                .get("allowed_for_principal")
                .and_then(Value::as_bool)
                .ok_or_else(|| RunStoreError::Storage {
                    message:
                        "stored model-visible tool provenance is missing allowed_for_principal"
                            .to_owned(),
                })?,
            valid_until_unix_ms,
        });
    }
    Ok(sorted_model_visible_tools(tools))
}

fn sorted_model_visible_tools(mut tools: Vec<ModelVisibleToolRef>) -> Vec<ModelVisibleToolRef> {
    tools.sort();
    tools
}

fn optional_string(object: &Map<String, Value>, key: &str) -> Option<String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
}

fn required_storage_string(
    object: &Map<String, Value>,
    key: &'static str,
) -> Result<String, RunStoreError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| RunStoreError::Storage {
            message: format!("stored model-visible tool provenance is missing {key}"),
        })
}

fn sqlite_u64_to_i64(value: u64, label: &'static str) -> Result<i64, RunStoreError> {
    i64::try_from(value).map_err(|_| RunStoreError::Storage {
        message: format!("{label} exceeds SQLite integer range"),
    })
}

fn sqlite_i64_to_u64(value: i64, label: &'static str) -> Result<u64, RunStoreError> {
    u64::try_from(value).map_err(|_| RunStoreError::Storage {
        message: format!("{label} must be non-negative"),
    })
}

fn storage_error(error: impl std::fmt::Display) -> RunStoreError {
    RunStoreError::Storage {
        message: error.to_string(),
    }
}
