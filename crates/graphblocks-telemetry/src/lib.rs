use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

pub use graphblocks_runtime_core::observability::*;
use graphblocks_runtime_core::output_policy::{
    DraftDisposition, DurableResult, OutputDisposition, PendingToolCallsDisposition, TerminalReason,
};
use graphblocks_runtime_core::policy::EnforcementPoint;
use graphblocks_runtime_core::tool::{ToolEffect, ToolResultMode};
use graphblocks_runtime_core::tool_call::ToolCallStatus;
use graphblocks_runtime_core::tool_result::{ToolEffectOutcome, ToolResultStatus};
use serde_json::{Value, json};

pub const DEFAULT_BLOCKED_METRIC_LABELS: &[&str] = &[
    "attempt_id",
    "conversation_id",
    "record_id",
    "run_id",
    "span_id",
    "trace_id",
    "turn_id",
    "user_id",
];

pub const DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS: &[&str] = &[
    "access_token",
    "api_key",
    "authorization",
    "bearer_token",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
];

const TOKEN_USAGE_TELEMETRY_ATTRIBUTE_KEYS: &[&str] = &[
    "cachedtokencount",
    "cachedtokens",
    "completiontokencount",
    "completiontokens",
    "inputtokencount",
    "inputtokens",
    "outputtokencount",
    "outputtokens",
    "prompttokencount",
    "prompttokens",
    "reasoningtokencount",
    "reasoningtokens",
    "tokencount",
    "totaltokencount",
    "totaltokens",
];

pub const DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS: &[&str] = &[
    "completion",
    "input",
    "messages",
    "output",
    "prompt",
    "tool_result",
];

fn normalized_telemetry_key(key: &str) -> String {
    key.chars()
        .filter(|character| character.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

fn telemetry_key_matches<'a>(key: &str, protected_keys: impl IntoIterator<Item = &'a str>) -> bool {
    let normalized_key = normalized_telemetry_key(key);
    protected_keys.into_iter().any(|protected_key| {
        let normalized_protected_key = normalized_telemetry_key(protected_key);
        if normalized_protected_key.is_empty() {
            return false;
        }
        if normalized_key == normalized_protected_key {
            return true;
        }
        if (normalized_protected_key == "token"
            || matches!(
                normalized_protected_key.as_str(),
                "completion" | "input" | "output" | "prompt"
            ))
            && TOKEN_USAGE_TELEMETRY_ATTRIBUTE_KEYS.contains(&normalized_key.as_str())
        {
            return false;
        }
        normalized_key.contains(&normalized_protected_key)
    })
}

const ENFORCEMENT_POINTS: &[EnforcementPoint] = &[
    EnforcementPoint::Compile,
    EnforcementPoint::Release,
    EnforcementPoint::Admission,
    EnforcementPoint::BeforeNode,
    EnforcementPoint::BeforeProviderCall,
    EnforcementPoint::OnGenerationChunk,
    EnforcementPoint::BeforeClientDelivery,
    EnforcementPoint::BeforeOutputCommit,
    EnforcementPoint::OnUsageDelta,
    EnforcementPoint::BeforeToolOrEffect,
    EnforcementPoint::BeforeCommit,
    EnforcementPoint::BeforePublish,
    EnforcementPoint::OnResume,
];

const OUTPUT_DISPOSITIONS: &[OutputDisposition] = &[
    OutputDisposition::Allow,
    OutputDisposition::Hold,
    OutputDisposition::Redact,
    OutputDisposition::Replace,
    OutputDisposition::AbortResponse,
    OutputDisposition::AbortTurn,
    OutputDisposition::DenyCommit,
];

const TERMINAL_REASONS: &[TerminalReason] = &[
    TerminalReason::PolicyDenied,
    TerminalReason::BudgetExhausted,
    TerminalReason::Cancelled,
    TerminalReason::ClientDisconnected,
];

const DRAFT_DISPOSITIONS: &[DraftDisposition] = &[
    DraftDisposition::Keep,
    DraftDisposition::MarkIncomplete,
    DraftDisposition::Retract,
];

const PENDING_TOOL_CALLS_DISPOSITIONS: &[PendingToolCallsDisposition] = &[
    PendingToolCallsDisposition::Keep,
    PendingToolCallsDisposition::Deny,
    PendingToolCallsDisposition::CancelAdmitted,
];

const DURABLE_RESULTS: &[DurableResult] = &[
    DurableResult::None,
    DurableResult::Incomplete,
    DurableResult::Partial,
];

const TOOL_CALL_STATUSES: &[ToolCallStatus] = &[
    ToolCallStatus::Validated,
    ToolCallStatus::PolicyPending,
    ToolCallStatus::ApprovalPending,
    ToolCallStatus::Admitted,
    ToolCallStatus::Running,
    ToolCallStatus::Completed,
    ToolCallStatus::Failed,
    ToolCallStatus::Denied,
    ToolCallStatus::Cancelled,
    ToolCallStatus::PolicyStopped,
    ToolCallStatus::Expired,
];

const TOOL_RESULT_STATUSES: &[ToolResultStatus] = &[
    ToolResultStatus::Completed,
    ToolResultStatus::Failed,
    ToolResultStatus::Denied,
    ToolResultStatus::Cancelled,
    ToolResultStatus::PolicyStopped,
    ToolResultStatus::Incomplete,
];

const TOOL_RESULT_MODES: &[ToolResultMode] = &[
    ToolResultMode::Value,
    ToolResultMode::Incremental,
    ToolResultMode::BoundedSequence,
    ToolResultMode::ArtifactReference,
];

const TOOL_EFFECT_OUTCOMES: &[ToolEffectOutcome] = &[
    ToolEffectOutcome::NoExternalEffect,
    ToolEffectOutcome::Committed,
    ToolEffectOutcome::NotCommitted,
    ToolEffectOutcome::Unknown,
];

const TOOL_EFFECTS: &[ToolEffect] = &[
    ToolEffect::None,
    ToolEffect::ExternalRead,
    ToolEffect::ExternalWrite,
    ToolEffect::FilesystemRead,
    ToolEffect::FilesystemWrite,
    ToolEffect::Process,
    ToolEffect::Network,
    ToolEffect::Destructive,
];

#[derive(Clone, Debug, PartialEq)]
pub struct GenerationTelemetryRecord {
    pub record_id: String,
    pub run_id: String,
    pub span_id: String,
    pub node_id: String,
    pub provider: String,
    pub model: String,
    pub release_id: Option<String>,
    pub input_digest: Option<String>,
    pub output_digest: Option<String>,
    pub usage: BTreeMap<String, u64>,
    pub timing_ms: BTreeMap<String, u64>,
    pub attributes: BTreeMap<String, Value>,
}

impl GenerationTelemetryRecord {
    pub fn new(
        record_id: impl Into<String>,
        run_id: impl Into<String>,
        span_id: impl Into<String>,
        node_id: impl Into<String>,
        provider: impl Into<String>,
        model: impl Into<String>,
    ) -> Self {
        Self {
            record_id: record_id.into(),
            run_id: run_id.into(),
            span_id: span_id.into(),
            node_id: node_id.into(),
            provider: provider.into(),
            model: model.into(),
            release_id: None,
            input_digest: None,
            output_digest: None,
            usage: BTreeMap::new(),
            timing_ms: BTreeMap::new(),
            attributes: BTreeMap::new(),
        }
    }

    pub fn from_generation_observation(
        record_id: impl Into<String>,
        run_id: impl Into<String>,
        observation: &GenerationObservation,
    ) -> Self {
        let mut timing_ms = BTreeMap::new();
        if let Some(duration) = observation.timing.queue_wait_ms() {
            timing_ms.insert("queue_wait".to_owned(), duration);
        }
        if let Some(duration) = observation.timing.flow_wait_ms() {
            timing_ms.insert("flow_wait".to_owned(), duration);
        }
        if let Some(duration) = observation.timing.time_to_first_output_ms() {
            timing_ms.insert("time_to_first_output".to_owned(), duration);
        }
        if let Some(duration) = observation.timing.execution_ms() {
            timing_ms.insert("execution".to_owned(), duration);
        }
        if let Some(duration) = observation.timing.streaming_ms() {
            timing_ms.insert("streaming".to_owned(), duration);
        }

        Self {
            record_id: record_id.into(),
            run_id: run_id.into(),
            span_id: observation.span_id.clone(),
            node_id: observation.node_id.clone(),
            provider: observation.provider.clone(),
            model: observation.model.clone(),
            release_id: None,
            input_digest: None,
            output_digest: None,
            usage: observation.usage.clone(),
            timing_ms,
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_release_id(mut self, release_id: impl Into<String>) -> Self {
        self.release_id = Some(release_id.into());
        self
    }

    pub fn with_input_digest(mut self, input_digest: impl Into<String>) -> Self {
        self.input_digest = Some(input_digest.into());
        self
    }

    pub fn with_output_digest(mut self, output_digest: impl Into<String>) -> Self {
        self.output_digest = Some(output_digest.into());
        self
    }

    pub fn with_usage(mut self, unit: impl Into<String>, amount: u64) -> Self {
        self.usage.insert(unit.into(), amount);
        self
    }

    pub fn with_timing_ms(mut self, name: impl Into<String>, duration_ms: u64) -> Self {
        self.timing_ms.insert(name.into(), duration_ms);
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: impl Into<Value>) -> Self {
        self.attributes.insert(key.into(), value.into());
        self
    }

    pub fn validate(&self) -> Result<(), TelemetryProjectionError> {
        for (field, value) in [
            ("record_id", self.record_id.as_str()),
            ("run_id", self.run_id.as_str()),
            ("span_id", self.span_id.as_str()),
            ("node_id", self.node_id.as_str()),
            ("provider", self.provider.as_str()),
            ("model", self.model.as_str()),
        ] {
            require_non_empty(field, value)?;
        }
        for (field, value) in [
            ("release_id", self.release_id.as_deref()),
            ("input_digest", self.input_digest.as_deref()),
            ("output_digest", self.output_digest.as_deref()),
        ] {
            require_optional_non_empty(field, value)?;
        }
        require_non_empty_keys("usage_unit", self.usage.keys().map(String::as_str))?;
        require_non_empty_keys("timing_name", self.timing_ms.keys().map(String::as_str))?;
        require_non_empty_keys("attribute_key", self.attributes.keys().map(String::as_str))?;
        Ok(())
    }

    pub fn observation_contract(&self) -> Value {
        json!({
            "record_id": self.record_id,
            "run_id": self.run_id,
            "span_id": self.span_id,
            "node_id": self.node_id,
            "provider": self.provider,
            "model": self.model,
            "release_id": self.release_id,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "usage": self.usage,
            "timing_ms": self.timing_ms,
            "attributes": self.attributes,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct OutputPolicyTelemetryRecord {
    pub record_id: String,
    pub run_id: String,
    pub stream_id: String,
    pub response_id: String,
    pub enforcement_point: String,
    pub disposition: String,
    pub release_id: Option<String>,
    pub policy_snapshot_id: Option<String>,
    pub terminal_reason: Option<String>,
    pub draft_disposition: Option<String>,
    pub pending_tool_calls: Option<String>,
    pub durable_result: Option<String>,
    pub accepted_through_sequence: Option<u64>,
    pub last_client_delivered_sequence: Option<u64>,
    pub attributes: BTreeMap<String, Value>,
}

impl OutputPolicyTelemetryRecord {
    pub fn new(
        record_id: impl Into<String>,
        run_id: impl Into<String>,
        stream_id: impl Into<String>,
        response_id: impl Into<String>,
        enforcement_point: impl Into<String>,
        disposition: impl Into<String>,
    ) -> Self {
        Self {
            record_id: record_id.into(),
            run_id: run_id.into(),
            stream_id: stream_id.into(),
            response_id: response_id.into(),
            enforcement_point: enforcement_point.into(),
            disposition: disposition.into(),
            release_id: None,
            policy_snapshot_id: None,
            terminal_reason: None,
            draft_disposition: None,
            pending_tool_calls: None,
            durable_result: None,
            accepted_through_sequence: None,
            last_client_delivered_sequence: None,
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_release_id(mut self, release_id: impl Into<String>) -> Self {
        self.release_id = Some(release_id.into());
        self
    }

    pub fn with_policy_snapshot_id(mut self, policy_snapshot_id: impl Into<String>) -> Self {
        self.policy_snapshot_id = Some(policy_snapshot_id.into());
        self
    }

    pub fn with_terminal_reason(mut self, terminal_reason: impl Into<String>) -> Self {
        self.terminal_reason = Some(terminal_reason.into());
        self
    }

    pub fn with_draft_disposition(mut self, draft_disposition: impl Into<String>) -> Self {
        self.draft_disposition = Some(draft_disposition.into());
        self
    }

    pub fn with_pending_tool_calls(mut self, pending_tool_calls: impl Into<String>) -> Self {
        self.pending_tool_calls = Some(pending_tool_calls.into());
        self
    }

    pub fn with_durable_result(mut self, durable_result: impl Into<String>) -> Self {
        self.durable_result = Some(durable_result.into());
        self
    }

    pub fn with_accepted_through_sequence(mut self, sequence: u64) -> Self {
        self.accepted_through_sequence = Some(sequence);
        self
    }

    pub fn with_last_client_delivered_sequence(mut self, sequence: u64) -> Self {
        self.last_client_delivered_sequence = Some(sequence);
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: impl Into<Value>) -> Self {
        self.attributes.insert(key.into(), value.into());
        self
    }

    pub fn validate(&self) -> Result<(), TelemetryProjectionError> {
        for (field, value) in [
            ("record_id", self.record_id.as_str()),
            ("run_id", self.run_id.as_str()),
            ("stream_id", self.stream_id.as_str()),
            ("response_id", self.response_id.as_str()),
        ] {
            require_non_empty(field, value)?;
        }
        require_one_of(
            "enforcement_point",
            &self.enforcement_point,
            ENFORCEMENT_POINTS
                .iter()
                .copied()
                .map(EnforcementPoint::as_str),
        )?;
        require_one_of(
            "disposition",
            &self.disposition,
            OUTPUT_DISPOSITIONS
                .iter()
                .copied()
                .map(OutputDisposition::as_str),
        )?;
        for (field, value) in [
            ("release_id", self.release_id.as_deref()),
            ("policy_snapshot_id", self.policy_snapshot_id.as_deref()),
        ] {
            require_optional_non_empty(field, value)?;
        }
        require_optional_one_of(
            "terminal_reason",
            self.terminal_reason.as_deref(),
            TERMINAL_REASONS.iter().copied().map(TerminalReason::as_str),
        )?;
        require_optional_one_of(
            "draft_disposition",
            self.draft_disposition.as_deref(),
            DRAFT_DISPOSITIONS
                .iter()
                .copied()
                .map(DraftDisposition::as_str),
        )?;
        require_optional_one_of(
            "pending_tool_calls",
            self.pending_tool_calls.as_deref(),
            PENDING_TOOL_CALLS_DISPOSITIONS
                .iter()
                .copied()
                .map(PendingToolCallsDisposition::as_str),
        )?;
        require_optional_one_of(
            "durable_result",
            self.durable_result.as_deref(),
            DURABLE_RESULTS.iter().copied().map(DurableResult::as_str),
        )?;
        if self.accepted_through_sequence == Some(0) {
            return Err(TelemetryProjectionError::InvalidAcceptedThroughSequence);
        }
        if let (Some(accepted), Some(delivered)) = (
            self.accepted_through_sequence,
            self.last_client_delivered_sequence,
        ) && delivered > accepted
        {
            return Err(TelemetryProjectionError::DeliveredBeyondAccepted {
                accepted,
                delivered,
            });
        }
        require_non_empty_keys("attribute_key", self.attributes.keys().map(String::as_str))?;
        Ok(())
    }

    pub fn observation_contract(&self) -> Value {
        json!({
            "record_id": self.record_id,
            "run_id": self.run_id,
            "stream_id": self.stream_id,
            "response_id": self.response_id,
            "enforcement_point": self.enforcement_point,
            "disposition": self.disposition,
            "release_id": self.release_id,
            "policy_snapshot_id": self.policy_snapshot_id,
            "terminal_reason": self.terminal_reason,
            "draft_disposition": self.draft_disposition,
            "pending_tool_calls": self.pending_tool_calls,
            "durable_result": self.durable_result,
            "accepted_through_sequence": self.accepted_through_sequence,
            "last_client_delivered_sequence": self.last_client_delivered_sequence,
            "attributes": self.attributes,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolExecutionTelemetryRecord {
    pub record_id: String,
    pub run_id: String,
    pub tool_call_id: String,
    pub tool_name: String,
    pub status: String,
    pub release_id: Option<String>,
    pub result_mode: Option<String>,
    pub effect_outcome: Option<String>,
    pub effects: BTreeSet<String>,
    pub duration_ms: Option<u64>,
    pub attributes: BTreeMap<String, Value>,
}

impl ToolExecutionTelemetryRecord {
    pub fn new(
        record_id: impl Into<String>,
        run_id: impl Into<String>,
        tool_call_id: impl Into<String>,
        tool_name: impl Into<String>,
        status: impl Into<String>,
    ) -> Self {
        Self {
            record_id: record_id.into(),
            run_id: run_id.into(),
            tool_call_id: tool_call_id.into(),
            tool_name: tool_name.into(),
            status: status.into(),
            release_id: None,
            result_mode: None,
            effect_outcome: None,
            effects: BTreeSet::new(),
            duration_ms: None,
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_release_id(mut self, release_id: impl Into<String>) -> Self {
        self.release_id = Some(release_id.into());
        self
    }

    pub fn with_result_mode(mut self, result_mode: impl Into<String>) -> Self {
        self.result_mode = Some(result_mode.into());
        self
    }

    pub fn with_effect_outcome(mut self, effect_outcome: impl Into<String>) -> Self {
        self.effect_outcome = Some(effect_outcome.into());
        self
    }

    pub fn with_effect(mut self, effect: impl Into<String>) -> Self {
        self.effects.insert(effect.into());
        self
    }

    pub fn with_duration_ms(mut self, duration_ms: u64) -> Self {
        self.duration_ms = Some(duration_ms);
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: impl Into<Value>) -> Self {
        self.attributes.insert(key.into(), value.into());
        self
    }

    pub fn validate(&self) -> Result<(), TelemetryProjectionError> {
        for (field, value) in [
            ("record_id", self.record_id.as_str()),
            ("run_id", self.run_id.as_str()),
            ("tool_call_id", self.tool_call_id.as_str()),
            ("tool_name", self.tool_name.as_str()),
        ] {
            require_non_empty(field, value)?;
        }
        require_tool_status(&self.status)?;
        require_optional_non_empty("release_id", self.release_id.as_deref())?;
        require_optional_one_of(
            "result_mode",
            self.result_mode.as_deref(),
            TOOL_RESULT_MODES
                .iter()
                .copied()
                .map(ToolResultMode::as_str),
        )?;
        require_optional_one_of(
            "effect_outcome",
            self.effect_outcome.as_deref(),
            TOOL_EFFECT_OUTCOMES
                .iter()
                .copied()
                .map(ToolEffectOutcome::as_str),
        )?;
        for effect in &self.effects {
            require_one_of(
                "effect",
                effect,
                TOOL_EFFECTS.iter().copied().map(ToolEffect::as_str),
            )?;
        }
        require_non_empty_keys("attribute_key", self.attributes.keys().map(String::as_str))?;
        Ok(())
    }

    pub fn observation_contract(&self) -> Value {
        let effects = self.effects.iter().cloned().collect::<Vec<_>>();
        json!({
            "record_id": self.record_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "release_id": self.release_id,
            "result_mode": self.result_mode,
            "effect_outcome": self.effect_outcome,
            "effects": effects,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryCapturePolicy {
    pub redacted_attribute_keys: BTreeSet<String>,
    pub dropped_attribute_keys: BTreeSet<String>,
    pub replacement: String,
    pub capture_input_digest: bool,
    pub capture_output_digest: bool,
}

impl Default for TelemetryCapturePolicy {
    fn default() -> Self {
        Self::new()
    }
}

impl TelemetryCapturePolicy {
    pub fn new() -> Self {
        Self {
            redacted_attribute_keys: BTreeSet::new(),
            dropped_attribute_keys: BTreeSet::new(),
            replacement: "[redacted]".to_owned(),
            capture_input_digest: true,
            capture_output_digest: true,
        }
    }

    pub fn with_redacted_attribute_key(mut self, key: impl Into<String>) -> Self {
        self.redacted_attribute_keys.insert(key.into());
        self
    }

    pub fn with_dropped_attribute_key(mut self, key: impl Into<String>) -> Self {
        self.dropped_attribute_keys.insert(key.into());
        self
    }

    pub fn with_replacement(mut self, replacement: impl Into<String>) -> Self {
        self.replacement = replacement.into();
        self
    }

    pub fn without_input_digest(mut self) -> Self {
        self.capture_input_digest = false;
        self
    }

    pub fn without_output_digest(mut self) -> Self {
        self.capture_output_digest = false;
        self
    }

    pub fn apply_generation(
        &self,
        record: &GenerationTelemetryRecord,
    ) -> GenerationTelemetryRecord {
        GenerationTelemetryRecord {
            input_digest: if self.capture_input_digest {
                record.input_digest.clone()
            } else {
                None
            },
            output_digest: if self.capture_output_digest {
                record.output_digest.clone()
            } else {
                None
            },
            attributes: self.protected_attributes(&record.attributes),
            ..record.clone()
        }
    }

    pub fn apply_output_policy(
        &self,
        record: &OutputPolicyTelemetryRecord,
    ) -> OutputPolicyTelemetryRecord {
        OutputPolicyTelemetryRecord {
            attributes: self.protected_attributes(&record.attributes),
            ..record.clone()
        }
    }

    pub fn apply_tool_execution(
        &self,
        record: &ToolExecutionTelemetryRecord,
    ) -> ToolExecutionTelemetryRecord {
        ToolExecutionTelemetryRecord {
            attributes: self.protected_attributes(&record.attributes),
            ..record.clone()
        }
    }

    fn protected_attributes(
        &self,
        attributes: &BTreeMap<String, Value>,
    ) -> BTreeMap<String, Value> {
        attributes
            .iter()
            .filter_map(|(key, value)| {
                if telemetry_key_matches(
                    key,
                    self.dropped_attribute_keys.iter().map(String::as_str),
                ) {
                    None
                } else if telemetry_key_matches(
                    key,
                    self.redacted_attribute_keys.iter().map(String::as_str),
                ) {
                    Some((key.clone(), Value::String(self.replacement.clone())))
                } else {
                    Some((key.clone(), value.clone()))
                }
            })
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryCapturePolicyIssue {
    pub attribute_key: String,
    pub reason: String,
    pub required_action: String,
}

impl TelemetryCapturePolicyIssue {
    pub fn new(
        attribute_key: impl Into<String>,
        reason: impl Into<String>,
        required_action: impl Into<String>,
    ) -> Self {
        Self {
            attribute_key: attribute_key.into(),
            reason: reason.into(),
            required_action: required_action.into(),
        }
    }

    pub fn issue_contract(&self) -> Value {
        json!({
            "attribute_key": self.attribute_key,
            "reason": self.reason,
            "required_action": self.required_action,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryCapturePolicyLintResult {
    pub issues: Vec<TelemetryCapturePolicyIssue>,
}

impl TelemetryCapturePolicyLintResult {
    pub fn new(mut issues: Vec<TelemetryCapturePolicyIssue>) -> Self {
        issues.sort_by(|left, right| {
            (left.attribute_key.as_str(), left.reason.as_str())
                .cmp(&(right.attribute_key.as_str(), right.reason.as_str()))
        });
        Self { issues }
    }

    pub fn passed(&self) -> bool {
        self.issues.is_empty()
    }

    pub fn issue_contracts(&self) -> Vec<Value> {
        self.issues
            .iter()
            .map(TelemetryCapturePolicyIssue::issue_contract)
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryCapturePolicyLinter {
    pub sensitive_attribute_keys: BTreeSet<String>,
    pub content_attribute_keys: BTreeSet<String>,
}

impl Default for TelemetryCapturePolicyLinter {
    fn default() -> Self {
        Self::new()
    }
}

impl TelemetryCapturePolicyLinter {
    pub fn new() -> Self {
        Self::from_keys(
            DEFAULT_SENSITIVE_TELEMETRY_ATTRIBUTE_KEYS.iter().copied(),
            DEFAULT_CONTENT_TELEMETRY_ATTRIBUTE_KEYS.iter().copied(),
        )
    }

    pub fn from_keys(
        sensitive_attribute_keys: impl IntoIterator<Item = impl Into<String>>,
        content_attribute_keys: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            sensitive_attribute_keys: sensitive_attribute_keys
                .into_iter()
                .map(Into::into)
                .collect(),
            content_attribute_keys: content_attribute_keys.into_iter().map(Into::into).collect(),
        }
    }

    pub fn lint_policy(&self, policy: &TelemetryCapturePolicy) -> TelemetryCapturePolicyLintResult {
        let protected = policy
            .redacted_attribute_keys
            .union(&policy.dropped_attribute_keys)
            .map(String::as_str)
            .collect::<Vec<_>>();
        let mut issues = Vec::new();

        for attribute_key in &self.sensitive_attribute_keys {
            if !telemetry_key_matches(attribute_key, protected.iter().copied()) {
                issues.push(TelemetryCapturePolicyIssue::new(
                    attribute_key,
                    "sensitive_attribute_not_protected",
                    "redact_or_drop",
                ));
            }
        }
        for attribute_key in &self.content_attribute_keys {
            if !telemetry_key_matches(attribute_key, protected.iter().copied()) {
                issues.push(TelemetryCapturePolicyIssue::new(
                    attribute_key,
                    "content_attribute_not_protected",
                    "redact_or_drop",
                ));
            }
        }
        if !policy.redacted_attribute_keys.is_empty() && policy.replacement.trim().is_empty() {
            for attribute_key in &policy.redacted_attribute_keys {
                issues.push(TelemetryCapturePolicyIssue::new(
                    attribute_key,
                    "redaction_replacement_empty",
                    "set_non_empty_replacement",
                ));
            }
        }

        TelemetryCapturePolicyLintResult::new(issues)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryExportResult {
    pub exporter: String,
    pub status: String,
    pub record_ids: Vec<String>,
    pub error_type: Option<String>,
    pub retryable: bool,
    pub run_impact: String,
}

impl TelemetryExportResult {
    pub fn new(
        exporter: impl Into<String>,
        status: impl Into<String>,
        record_ids: impl IntoIterator<Item = impl Into<String>>,
        error_type: Option<impl Into<String>>,
        retryable: bool,
        run_impact: impl Into<String>,
    ) -> Result<Self, TelemetryProjectionError> {
        let run_impact = run_impact.into();
        if run_impact != "none" {
            return Err(TelemetryProjectionError::ExportAffectsRunCorrectness);
        }
        Ok(Self {
            exporter: exporter.into(),
            status: status.into(),
            record_ids: record_ids.into_iter().map(Into::into).collect(),
            error_type: error_type.map(Into::into),
            retryable,
            run_impact,
        })
    }

    pub fn completed(
        exporter: impl Into<String>,
        record_ids: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            exporter: exporter.into(),
            status: "completed".to_owned(),
            record_ids: record_ids.into_iter().map(Into::into).collect(),
            error_type: None,
            retryable: false,
            run_impact: "none".to_owned(),
        }
    }

    pub fn failed(
        exporter: impl Into<String>,
        record_ids: impl IntoIterator<Item = impl Into<String>>,
        error_type: impl Into<String>,
        retryable: bool,
    ) -> Self {
        Self {
            exporter: exporter.into(),
            status: "failed".to_owned(),
            record_ids: record_ids.into_iter().map(Into::into).collect(),
            error_type: Some(error_type.into()),
            retryable,
            run_impact: "none".to_owned(),
        }
    }

    pub fn result_contract(&self) -> Value {
        json!({
            "exporter": self.exporter,
            "status": self.status,
            "record_ids": self.record_ids,
            "error_type": self.error_type,
            "retryable": self.retryable,
            "run_impact": self.run_impact,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetricSample {
    pub name: String,
    pub labels: BTreeMap<String, String>,
}

impl MetricSample {
    pub fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            labels: BTreeMap::new(),
        }
    }

    pub fn with_label(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.labels.insert(key.into(), value.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetricCardinalityIssue {
    pub metric_name: String,
    pub label: String,
    pub distinct_values: usize,
    pub limit: usize,
    pub reason: String,
}

impl MetricCardinalityIssue {
    pub fn new(
        metric_name: impl Into<String>,
        label: impl Into<String>,
        distinct_values: usize,
        limit: usize,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            metric_name: metric_name.into(),
            label: label.into(),
            distinct_values,
            limit,
            reason: reason.into(),
        }
    }

    pub fn issue_contract(&self) -> Value {
        json!({
            "metric_name": self.metric_name,
            "label": self.label,
            "distinct_values": self.distinct_values,
            "limit": self.limit,
            "reason": self.reason,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetricCardinalityLintResult {
    pub issues: Vec<MetricCardinalityIssue>,
}

impl MetricCardinalityLintResult {
    pub fn new(mut issues: Vec<MetricCardinalityIssue>) -> Self {
        issues.sort_by(|left, right| {
            (
                left.metric_name.as_str(),
                left.label.as_str(),
                left.reason.as_str(),
            )
                .cmp(&(
                    right.metric_name.as_str(),
                    right.label.as_str(),
                    right.reason.as_str(),
                ))
        });
        Self { issues }
    }

    pub fn passed(&self) -> bool {
        self.issues.is_empty()
    }

    pub fn issue_contracts(&self) -> Vec<Value> {
        self.issues
            .iter()
            .map(MetricCardinalityIssue::issue_contract)
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MetricCardinalityLinter {
    pub max_distinct_values_per_label: usize,
    pub blocked_labels: BTreeSet<String>,
}

impl Default for MetricCardinalityLinter {
    fn default() -> Self {
        Self::new()
    }
}

impl MetricCardinalityLinter {
    pub fn new() -> Self {
        Self {
            max_distinct_values_per_label: 32,
            blocked_labels: DEFAULT_BLOCKED_METRIC_LABELS
                .iter()
                .map(|label| (*label).to_owned())
                .collect(),
        }
    }

    pub fn with_max_distinct_values_per_label(mut self, limit: usize) -> Self {
        self.max_distinct_values_per_label = limit;
        self
    }

    pub fn with_blocked_label(mut self, label: impl Into<String>) -> Self {
        self.blocked_labels.insert(label.into());
        self
    }

    pub fn lint_samples<'a>(
        &self,
        samples: impl IntoIterator<Item = &'a MetricSample>,
    ) -> Result<MetricCardinalityLintResult, TelemetryProjectionError> {
        let mut label_values: BTreeMap<(String, String), BTreeSet<String>> = BTreeMap::new();
        let mut blocked_label_values: BTreeMap<(String, String), BTreeSet<String>> =
            BTreeMap::new();

        for sample in samples {
            if sample.name.trim().is_empty() {
                return Err(TelemetryProjectionError::InvalidMetricSampleName);
            }
            for (label, value) in &sample.labels {
                let key = (sample.name.clone(), label.clone());
                if telemetry_key_matches(label, self.blocked_labels.iter().map(String::as_str)) {
                    blocked_label_values
                        .entry(key)
                        .or_default()
                        .insert(value.clone());
                } else {
                    label_values.entry(key).or_default().insert(value.clone());
                }
            }
        }

        let mut issues = Vec::new();
        for ((metric_name, label), values) in label_values {
            if values.len() > self.max_distinct_values_per_label {
                issues.push(MetricCardinalityIssue::new(
                    metric_name,
                    label,
                    values.len(),
                    self.max_distinct_values_per_label,
                    "too_many_values",
                ));
            }
        }
        for ((metric_name, label), values) in blocked_label_values {
            issues.push(MetricCardinalityIssue::new(
                metric_name,
                label,
                values.len(),
                0,
                "blocked_label",
            ));
        }

        Ok(MetricCardinalityLintResult::new(issues))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryDiagnostic {
    pub code: String,
    pub severity: String,
    pub path: String,
    pub message: String,
}

impl TelemetryDiagnostic {
    pub fn error(
        code: impl Into<String>,
        path: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            code: code.into(),
            severity: "error".to_owned(),
            path: path.into(),
            message: message.into(),
        }
    }

    pub fn warning(
        code: impl Into<String>,
        path: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            code: code.into(),
            severity: "warning".to_owned(),
            path: path.into(),
            message: message.into(),
        }
    }

    pub fn diagnostic_contract(&self) -> Value {
        json!({
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryDiagnosticBundleSection {
    pub name: String,
    pub diagnostics: Vec<TelemetryDiagnostic>,
}

impl TelemetryDiagnosticBundleSection {
    pub fn new(
        name: impl Into<String>,
        diagnostics: impl IntoIterator<Item = TelemetryDiagnostic>,
    ) -> Self {
        let mut diagnostics = diagnostics.into_iter().collect::<Vec<_>>();
        diagnostics.sort_by(|left, right| {
            (
                left.severity.as_str(),
                left.code.as_str(),
                left.path.as_str(),
                left.message.as_str(),
            )
                .cmp(&(
                    right.severity.as_str(),
                    right.code.as_str(),
                    right.path.as_str(),
                    right.message.as_str(),
                ))
        });
        Self {
            name: name.into(),
            diagnostics,
        }
    }

    pub fn ok(&self) -> bool {
        !self
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.severity == "error")
    }

    pub fn summary(&self) -> BTreeMap<String, u64> {
        diagnostic_summary(&self.diagnostics)
    }

    pub fn section_contract(&self) -> Value {
        json!({
            "name": self.name,
            "ok": self.ok(),
            "summary": self.summary(),
            "diagnostics": self.diagnostics.iter().map(TelemetryDiagnostic::diagnostic_contract).collect::<Vec<_>>(),
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TelemetryDiagnosticBundle {
    pub bundle_id: String,
    pub sections: Vec<TelemetryDiagnosticBundleSection>,
}

impl TelemetryDiagnosticBundle {
    pub fn new(
        bundle_id: impl Into<String>,
        sections: impl IntoIterator<Item = TelemetryDiagnosticBundleSection>,
    ) -> Self {
        let mut sections = sections.into_iter().collect::<Vec<_>>();
        sections.sort_by(|left, right| left.name.cmp(&right.name));
        Self {
            bundle_id: bundle_id.into(),
            sections,
        }
    }

    pub fn ok(&self) -> bool {
        self.sections
            .iter()
            .all(TelemetryDiagnosticBundleSection::ok)
    }

    pub fn summary(&self) -> BTreeMap<String, u64> {
        let mut summary = empty_diagnostic_summary();
        for section in &self.sections {
            for (severity, count) in section.summary() {
                *summary.entry(severity).or_default() += count;
            }
        }
        summary
    }

    pub fn bundle_contract(&self) -> Value {
        json!({
            "bundle_id": self.bundle_id,
            "ok": self.ok(),
            "summary": self.summary(),
            "sections": self.sections.iter().map(TelemetryDiagnosticBundleSection::section_contract).collect::<Vec<_>>(),
        })
    }
}

pub fn telemetry_diagnostic_bundle<'a>(
    bundle_id: impl Into<String>,
    capture_policy_result: Option<&TelemetryCapturePolicyLintResult>,
    metric_cardinality_result: Option<&MetricCardinalityLintResult>,
    export_results: impl IntoIterator<Item = &'a TelemetryExportResult>,
) -> TelemetryDiagnosticBundle {
    let mut sections = Vec::new();
    if let Some(result) = capture_policy_result {
        sections.push(TelemetryDiagnosticBundleSection::new(
            "capture_policy",
            capture_policy_diagnostics(result),
        ));
    }
    if let Some(result) = metric_cardinality_result {
        sections.push(TelemetryDiagnosticBundleSection::new(
            "metric_cardinality",
            metric_cardinality_diagnostics(result),
        ));
    }
    let export_diagnostics = export_result_diagnostics(export_results);
    if !export_diagnostics.is_empty() {
        sections.push(TelemetryDiagnosticBundleSection::new(
            "exporters",
            export_diagnostics,
        ));
    }
    TelemetryDiagnosticBundle::new(bundle_id, sections)
}

fn capture_policy_diagnostics(
    result: &TelemetryCapturePolicyLintResult,
) -> Vec<TelemetryDiagnostic> {
    result
        .issues
        .iter()
        .map(|issue| {
            TelemetryDiagnostic::error(
                format!("TelemetryCapturePolicy.{}", issue.reason),
                format!("$.capturePolicy.attributes.{}", issue.attribute_key),
                format!(
                    "Telemetry attribute '{}' failed capture-policy lint; required action: {}",
                    issue.attribute_key, issue.required_action
                ),
            )
        })
        .collect()
}

fn metric_cardinality_diagnostics(
    result: &MetricCardinalityLintResult,
) -> Vec<TelemetryDiagnostic> {
    result
        .issues
        .iter()
        .map(|issue| {
            TelemetryDiagnostic::warning(
                format!("TelemetryMetricCardinality.{}", issue.reason),
                format!("$.metrics.{}.labels.{}", issue.metric_name, issue.label),
                format!(
                    "Telemetry metric '{}' label '{}' observed {} distinct value(s); limit: {}",
                    issue.metric_name, issue.label, issue.distinct_values, issue.limit
                ),
            )
        })
        .collect()
}

fn export_result_diagnostics<'a>(
    results: impl IntoIterator<Item = &'a TelemetryExportResult>,
) -> Vec<TelemetryDiagnostic> {
    results
        .into_iter()
        .filter(|result| result.status != "completed")
        .map(|result| {
            TelemetryDiagnostic::warning(
                format!("TelemetryExport.{}", result.status),
                format!("$.exporters.{}", result.exporter),
                format!(
                    "Telemetry exporter '{}' reported status '{}' for {} record(s); retryable: {}; error_type: {}",
                    result.exporter,
                    result.status,
                    result.record_ids.len(),
                    result.retryable,
                    result.error_type.as_deref().unwrap_or("none")
                ),
            )
        })
        .collect()
}

fn empty_diagnostic_summary() -> BTreeMap<String, u64> {
    [
        ("error".to_owned(), 0),
        ("warning".to_owned(), 0),
        ("info".to_owned(), 0),
    ]
    .into_iter()
    .collect()
}

fn diagnostic_summary(diagnostics: &[TelemetryDiagnostic]) -> BTreeMap<String, u64> {
    let mut summary = empty_diagnostic_summary();
    for diagnostic in diagnostics {
        *summary.entry(diagnostic.severity.clone()).or_default() += 1;
    }
    summary
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TelemetryProjectionError {
    ControlCharacter { field: &'static str },
    DeliveredBeyondAccepted { accepted: u64, delivered: u64 },
    EmptyField { field: &'static str },
    ExportAffectsRunCorrectness,
    InvalidAcceptedThroughSequence,
    InvalidLiteral { field: &'static str, value: String },
    InvalidMetricSampleName,
    NonCanonicalField { field: &'static str },
}

impl fmt::Display for TelemetryProjectionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ControlCharacter { field } => {
                write!(
                    formatter,
                    "telemetry field '{field}' must not contain control characters"
                )
            }
            Self::DeliveredBeyondAccepted {
                accepted,
                delivered,
            } => write!(
                formatter,
                "last client-delivered sequence {delivered} exceeds accepted-through sequence {accepted}"
            ),
            Self::EmptyField { field } => {
                write!(formatter, "telemetry field '{field}' must be non-empty")
            }
            Self::ExportAffectsRunCorrectness => {
                write!(
                    formatter,
                    "telemetry export result must not affect run correctness"
                )
            }
            Self::InvalidAcceptedThroughSequence => {
                write!(formatter, "accepted-through sequence must be positive")
            }
            Self::InvalidLiteral { field, value } => {
                write!(
                    formatter,
                    "telemetry field '{field}' has invalid literal '{value}'"
                )
            }
            Self::InvalidMetricSampleName => {
                write!(formatter, "metric sample name must be a non-empty string")
            }
            Self::NonCanonicalField { field } => {
                write!(
                    formatter,
                    "telemetry field '{field}' must not contain surrounding whitespace"
                )
            }
        }
    }
}

impl Error for TelemetryProjectionError {}

fn require_non_empty(field: &'static str, value: &str) -> Result<(), TelemetryProjectionError> {
    if value
        .chars()
        .any(|character| ('\u{0}'..='\u{1f}').contains(&character) || character == '\u{7f}')
    {
        return Err(TelemetryProjectionError::ControlCharacter { field });
    }
    if value.trim().is_empty() {
        return Err(TelemetryProjectionError::EmptyField { field });
    }
    if value != value.trim() {
        return Err(TelemetryProjectionError::NonCanonicalField { field });
    }
    Ok(())
}

fn require_non_empty_keys<'a>(
    field: &'static str,
    values: impl IntoIterator<Item = &'a str>,
) -> Result<(), TelemetryProjectionError> {
    for value in values {
        require_non_empty(field, value)?;
    }
    Ok(())
}

fn require_optional_non_empty(
    field: &'static str,
    value: Option<&str>,
) -> Result<(), TelemetryProjectionError> {
    if let Some(value) = value {
        require_non_empty(field, value)?;
    }
    Ok(())
}

fn require_one_of(
    field: &'static str,
    value: &str,
    valid_values: impl IntoIterator<Item = &'static str>,
) -> Result<(), TelemetryProjectionError> {
    require_non_empty(field, value)?;
    if valid_values.into_iter().any(|valid| valid == value) {
        return Ok(());
    }
    Err(TelemetryProjectionError::InvalidLiteral {
        field,
        value: value.to_owned(),
    })
}

fn require_optional_one_of(
    field: &'static str,
    value: Option<&str>,
    valid_values: impl IntoIterator<Item = &'static str>,
) -> Result<(), TelemetryProjectionError> {
    if let Some(value) = value {
        require_one_of(field, value, valid_values)?;
    }
    Ok(())
}

fn require_tool_status(value: &str) -> Result<(), TelemetryProjectionError> {
    require_non_empty("status", value)?;
    if TOOL_CALL_STATUSES
        .iter()
        .copied()
        .map(ToolCallStatus::as_str)
        .chain(
            TOOL_RESULT_STATUSES
                .iter()
                .copied()
                .map(ToolResultStatus::as_str),
        )
        .any(|valid| valid == value)
    {
        return Ok(());
    }
    Err(TelemetryProjectionError::InvalidLiteral {
        field: "status",
        value: value.to_owned(),
    })
}
