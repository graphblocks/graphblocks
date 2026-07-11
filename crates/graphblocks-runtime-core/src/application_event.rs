use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::path::Path;
use std::sync::Mutex;

use crate::output_policy::{
    DraftDisposition, DurableResult, GenerationChunk, OutputCutoff, OutputCutoffError,
    OutputDisposition, OutputPolicyDecision, OutputPolicyDecisionError,
    PendingToolCallsDisposition, ProviderCancellation, TerminalReason,
};
use crate::policy::PolicyDecision;
use crate::run_store::RunStatusSnapshot;
use crate::tool_approval::ToolApprovalRequest;
use crate::tool_call::{
    ToolCall, ToolCallDraft, ToolCallDraftStatus, ToolCallError, ToolCallStatus,
};
use crate::tool_result::{
    ContentPart, ContentPartKind, ToolEffectOutcome, ToolResult, ToolResultEvent,
    ToolResultEventError, ToolResultStatus,
};
use rusqlite::{Connection, TransactionBehavior, params};
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ApplicationEventKind {
    RunStarted,
    RunSucceeded,
    RunFailed,
    RunCancelled,
    ToolCallProposed,
    ToolCallArgumentsDelta,
    ToolCallArgumentsCompleted,
    ToolCallValidated,
    ToolCallPolicyEvaluated,
    ToolCallApprovalRequested,
    ToolCallAdmitted,
    ToolCallStarted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallDenied,
    ToolCallCancelled,
    ToolCallPolicyStopped,
    ToolCallIncomplete,
    ToolResultStarted,
    ToolResultDelta,
    ToolResultArtifactReady,
    ToolResultCompleted,
    ToolResultFailed,
    ToolResultDenied,
    ToolResultCancelled,
    ToolResultPolicyStopped,
    ToolResultIncomplete,
    OutputPolicyEvaluationStarted,
    OutputPolicyAllowed,
    OutputPolicyHeld,
    OutputPolicyRedacted,
    OutputPolicyReplaced,
    OutputPolicyViolationDetected,
    OutputCutoff,
    AssistantIncomplete,
    AssistantRetracted,
    RunCompleted,
    RunExpired,
    RunPolicyStopped,
    AsyncOperationStarted,
    AsyncOperationWaitingCallback,
    AsyncOperationPolling,
    AsyncOperationCompleted,
    AsyncOperationFailed,
    AsyncOperationCancelled,
    AsyncOperationExpired,
    ExternalCallbackReceived,
    ExternalCallbackRejected,
    LateExternalCallbackReceived,
    RunResuming,
    RunPausedBudget,
    RunPausedCallbackDelivery,
    RunPausedPolicy,
    RunPausedOperator,
}

impl ApplicationEventKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::RunStarted => "RunStarted",
            Self::RunSucceeded => "RunSucceeded",
            Self::RunFailed => "RunFailed",
            Self::RunCancelled => "RunCancelled",
            Self::ToolCallProposed => "ToolCallProposed",
            Self::ToolCallArgumentsDelta => "ToolCallArgumentsDelta",
            Self::ToolCallArgumentsCompleted => "ToolCallArgumentsCompleted",
            Self::ToolCallValidated => "ToolCallValidated",
            Self::ToolCallPolicyEvaluated => "ToolCallPolicyEvaluated",
            Self::ToolCallApprovalRequested => "ToolCallApprovalRequested",
            Self::ToolCallAdmitted => "ToolCallAdmitted",
            Self::ToolCallStarted => "ToolCallStarted",
            Self::ToolCallCompleted => "ToolCallCompleted",
            Self::ToolCallFailed => "ToolCallFailed",
            Self::ToolCallDenied => "ToolCallDenied",
            Self::ToolCallCancelled => "ToolCallCancelled",
            Self::ToolCallPolicyStopped => "ToolCallPolicyStopped",
            Self::ToolCallIncomplete => "ToolCallIncomplete",
            Self::ToolResultStarted => "ToolResultStarted",
            Self::ToolResultDelta => "ToolResultDelta",
            Self::ToolResultArtifactReady => "ToolResultArtifactReady",
            Self::ToolResultCompleted => "ToolResultCompleted",
            Self::ToolResultFailed => "ToolResultFailed",
            Self::ToolResultDenied => "ToolResultDenied",
            Self::ToolResultCancelled => "ToolResultCancelled",
            Self::ToolResultPolicyStopped => "ToolResultPolicyStopped",
            Self::ToolResultIncomplete => "ToolResultIncomplete",
            Self::OutputPolicyEvaluationStarted => "OutputPolicyEvaluationStarted",
            Self::OutputPolicyAllowed => "OutputPolicyAllowed",
            Self::OutputPolicyHeld => "OutputPolicyHeld",
            Self::OutputPolicyRedacted => "OutputPolicyRedacted",
            Self::OutputPolicyReplaced => "OutputPolicyReplaced",
            Self::OutputPolicyViolationDetected => "OutputPolicyViolationDetected",
            Self::OutputCutoff => "OutputCutoff",
            Self::AssistantIncomplete => "AssistantIncomplete",
            Self::AssistantRetracted => "AssistantRetracted",
            Self::RunCompleted => "RunCompleted",
            Self::RunExpired => "RunExpired",
            Self::RunPolicyStopped => "RunPolicyStopped",
            Self::AsyncOperationStarted => "AsyncOperationStarted",
            Self::AsyncOperationWaitingCallback => "AsyncOperationWaitingCallback",
            Self::AsyncOperationPolling => "AsyncOperationPolling",
            Self::AsyncOperationCompleted => "AsyncOperationCompleted",
            Self::AsyncOperationFailed => "AsyncOperationFailed",
            Self::AsyncOperationCancelled => "AsyncOperationCancelled",
            Self::AsyncOperationExpired => "AsyncOperationExpired",
            Self::ExternalCallbackReceived => "ExternalCallbackReceived",
            Self::ExternalCallbackRejected => "ExternalCallbackRejected",
            Self::LateExternalCallbackReceived => "LateExternalCallbackReceived",
            Self::RunResuming => "RunResuming",
            Self::RunPausedBudget => "RunPausedBudget",
            Self::RunPausedCallbackDelivery => "RunPausedCallbackDelivery",
            Self::RunPausedPolicy => "RunPausedPolicy",
            Self::RunPausedOperator => "RunPausedOperator",
        }
    }

    pub fn is_tool_event(&self) -> bool {
        matches!(
            self,
            Self::ToolCallProposed
                | Self::ToolCallArgumentsDelta
                | Self::ToolCallArgumentsCompleted
                | Self::ToolCallValidated
                | Self::ToolCallPolicyEvaluated
                | Self::ToolCallApprovalRequested
                | Self::ToolCallAdmitted
                | Self::ToolCallStarted
                | Self::ToolCallCompleted
                | Self::ToolCallFailed
                | Self::ToolCallDenied
                | Self::ToolCallCancelled
                | Self::ToolCallPolicyStopped
                | Self::ToolCallIncomplete
                | Self::ToolResultStarted
                | Self::ToolResultDelta
                | Self::ToolResultArtifactReady
                | Self::ToolResultCompleted
                | Self::ToolResultFailed
                | Self::ToolResultDenied
                | Self::ToolResultCancelled
                | Self::ToolResultPolicyStopped
                | Self::ToolResultIncomplete
        )
    }

    fn is_allowed_after_output_cutoff(&self) -> bool {
        matches!(
            self,
            Self::ToolCallDenied
                | Self::ToolCallCancelled
                | Self::ToolCallPolicyStopped
                | Self::ToolCallIncomplete
                | Self::ToolResultDenied
                | Self::ToolResultCancelled
                | Self::ToolResultPolicyStopped
                | Self::ToolResultIncomplete
        )
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ApplicationEventVisibility {
    Client,
    Operator,
    Internal,
    AuditOnly,
}

impl ApplicationEventVisibility {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Client => "client",
            Self::Operator => "operator",
            Self::Internal => "internal",
            Self::AuditOnly => "audit_only",
        }
    }
}

impl fmt::Display for ApplicationEventVisibility {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl std::str::FromStr for ApplicationEventVisibility {
    type Err = ApplicationEventVisibilityParseError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "client" => Ok(Self::Client),
            "operator" => Ok(Self::Operator),
            "internal" => Ok(Self::Internal),
            "audit_only" => Ok(Self::AuditOnly),
            other => Err(ApplicationEventVisibilityParseError {
                value: other.to_owned(),
            }),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ApplicationEventVisibilityParseError {
    pub value: String,
}

impl fmt::Display for ApplicationEventVisibilityParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "unknown application event visibility {:?}",
            self.value
        )
    }
}

impl Error for ApplicationEventVisibilityParseError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ApplicationEventMetadata {
    pub event_id: String,
    pub run_id: String,
    pub response_id: String,
    pub turn_id: Option<String>,
    pub cursor: Option<String>,
    pub graph_id: Option<String>,
    pub node_id: Option<String>,
    pub operation_id: Option<String>,
    pub sequence: u64,
    pub release_id: String,
    pub policy_snapshot_id: String,
    pub occurred_at_unix_ms: u64,
    pub visibility: ApplicationEventVisibility,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ApplicationEvent {
    pub kind: ApplicationEventKind,
    pub metadata: ApplicationEventMetadata,
    pub tool_call_id: Option<String>,
    pub payload: Value,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ApplicationEventError {
    ToolEventRequiresToolCallId { kind: ApplicationEventKind },
    NotToolEvent { kind: ApplicationEventKind },
    EmptyMetadataField { field: &'static str },
    EmptyPayloadField { field: &'static str },
    InvalidPayload { field: &'static str },
    InvalidPayloadKey { field: String },
    EmptyToolCallId,
    InvalidToolCall { source: ToolCallError },
    InvalidToolResultEvent { source: ToolResultEventError },
    InvalidOutputCutoff { source: OutputCutoffError },
    InvalidOutputPolicyDecision { source: OutputPolicyDecisionError },
}

impl ApplicationEvent {
    pub fn tool_call_draft(
        metadata: ApplicationEventMetadata,
        draft: &ToolCallDraft,
    ) -> Result<Self, ApplicationEventError> {
        match draft.status {
            ToolCallDraftStatus::Proposed => Self::tool(
                ApplicationEventKind::ToolCallProposed,
                metadata,
                &draft.tool_call_id,
                json!({
                    "tool_name": &draft.tool_name,
                    "status": "proposed",
                    "draft_sequence": draft.sequence,
                    "fragment_count": draft.argument_fragments.len(),
                }),
            ),
            ToolCallDraftStatus::ArgumentsStreaming => Self::tool(
                ApplicationEventKind::ToolCallArgumentsDelta,
                metadata,
                &draft.tool_call_id,
                json!({
                    "tool_name": &draft.tool_name,
                    "status": "arguments_streaming",
                    "draft_sequence": draft.sequence,
                    "fragment_count": draft.argument_fragments.len(),
                    "argument_fragment": draft.argument_fragments.last().map(String::as_str),
                }),
            ),
            ToolCallDraftStatus::ArgumentsComplete => Self::tool(
                ApplicationEventKind::ToolCallArgumentsCompleted,
                metadata,
                &draft.tool_call_id,
                json!({
                    "tool_name": &draft.tool_name,
                    "status": "arguments_complete",
                    "draft_sequence": draft.sequence,
                    "fragment_count": draft.argument_fragments.len(),
                }),
            ),
        }
    }

    pub fn tool_call_state(
        metadata: ApplicationEventMetadata,
        call: &ToolCall,
    ) -> Result<Option<Self>, ApplicationEventError> {
        call.validate()
            .map_err(|source| ApplicationEventError::InvalidToolCall { source })?;
        let kind = match call.status {
            ToolCallStatus::Validated => ApplicationEventKind::ToolCallValidated,
            ToolCallStatus::Admitted => ApplicationEventKind::ToolCallAdmitted,
            ToolCallStatus::Running => ApplicationEventKind::ToolCallStarted,
            ToolCallStatus::Completed => ApplicationEventKind::ToolCallCompleted,
            ToolCallStatus::Failed => ApplicationEventKind::ToolCallFailed,
            ToolCallStatus::Denied => ApplicationEventKind::ToolCallDenied,
            ToolCallStatus::Cancelled => ApplicationEventKind::ToolCallCancelled,
            ToolCallStatus::PolicyStopped => ApplicationEventKind::ToolCallPolicyStopped,
            ToolCallStatus::Expired => ApplicationEventKind::ToolCallIncomplete,
            ToolCallStatus::PolicyPending | ToolCallStatus::ApprovalPending => return Ok(None),
        };

        Self::tool(
            kind,
            metadata,
            &call.tool_call_id,
            json!({
                "tool_name": &call.name,
                "resolved_tool_id": &call.resolved_tool_id,
                "status": Self::tool_call_status(call.status),
                "arguments_digest": &call.arguments_digest,
                "revision": call.revision,
                "depends_on": &call.depends_on,
                "created_at_unix_ms": call.created_at_unix_ms,
                "admitted_at_unix_ms": call.admitted_at_unix_ms,
                "completed_at_unix_ms": call.completed_at_unix_ms,
            }),
        )
        .map(Some)
    }

    fn tool_call_status(status: ToolCallStatus) -> &'static str {
        match status {
            ToolCallStatus::Validated => "validated",
            ToolCallStatus::PolicyPending => "policy_pending",
            ToolCallStatus::ApprovalPending => "approval_pending",
            ToolCallStatus::Admitted => "admitted",
            ToolCallStatus::Running => "running",
            ToolCallStatus::Completed => "completed",
            ToolCallStatus::Failed => "failed",
            ToolCallStatus::Denied => "denied",
            ToolCallStatus::Cancelled => "cancelled",
            ToolCallStatus::PolicyStopped => "policy_stopped",
            ToolCallStatus::Expired => "expired",
        }
    }

    pub fn tool_call_policy_evaluated(
        metadata: ApplicationEventMetadata,
        call: &ToolCall,
        decision: &PolicyDecision,
    ) -> Result<Self, ApplicationEventError> {
        Self::tool(
            ApplicationEventKind::ToolCallPolicyEvaluated,
            metadata,
            &call.tool_call_id,
            json!({
                "tool_name": &call.name,
                "resolved_tool_id": &call.resolved_tool_id,
                "status": Self::tool_call_status(call.status),
                "arguments_digest": &call.arguments_digest,
                "revision": call.revision,
                "decision_id": &decision.decision_id,
                "effect": decision.effect.as_str(),
                "reason_codes": &decision.reason_codes,
                "policy_refs": &decision.policy_refs,
                "obligation_count": decision.obligations.len(),
                "advice_count": decision.advice.len(),
                "evaluated_at": &decision.evaluated_at,
                "valid_until": &decision.valid_until,
                "input_digest": &decision.input_digest,
            }),
        )
    }

    pub fn tool_approval_requested(
        metadata: ApplicationEventMetadata,
        request: &ToolApprovalRequest,
    ) -> Result<Self, ApplicationEventError> {
        Self::tool(
            ApplicationEventKind::ToolCallApprovalRequested,
            metadata,
            &request.tool_call_id,
            json!({
                "approval_id": &request.approval_id,
                "tool_name": &request.tool_name,
                "revision": request.revision,
                "definition_digest": &request.definition_digest,
                "binding_digest": &request.binding_digest,
                "arguments_digest": &request.arguments_digest,
                "policy_snapshot_id": &request.policy_snapshot_id,
                "principal_id": &request.principal_id,
                "requested_at_unix_ms": request.requested_at_unix_ms,
                "expires_at_unix_ms": request.expires_at_unix_ms,
            }),
        )
    }

    pub fn tool_result_event(
        metadata: ApplicationEventMetadata,
        event: &ToolResultEvent,
    ) -> Result<Option<Self>, ApplicationEventError> {
        event
            .validate()
            .map_err(|source| ApplicationEventError::InvalidToolResultEvent { source })?;
        match event {
            ToolResultEvent::Started {
                tool_call_id,
                sequence,
                started_at_unix_ms,
            } => Self::tool(
                ApplicationEventKind::ToolResultStarted,
                metadata,
                tool_call_id,
                json!({
                    "status": "running",
                    "tool_result_sequence": sequence,
                    "started_at_unix_ms": started_at_unix_ms,
                }),
            )
            .map(Some),
            ToolResultEvent::Delta {
                tool_call_id,
                sequence,
                output,
            } => Self::tool(
                ApplicationEventKind::ToolResultDelta,
                metadata,
                tool_call_id,
                json!({
                    "status": "incremental",
                    "tool_result_sequence": sequence,
                    "output": output
                        .iter()
                        .map(Self::content_part_payload)
                        .collect::<Vec<_>>(),
                }),
            )
            .map(Some),
            ToolResultEvent::ArtifactReady {
                tool_call_id,
                sequence,
                artifact,
            } => Self::tool(
                ApplicationEventKind::ToolResultArtifactReady,
                metadata,
                tool_call_id,
                json!({
                    "status": "artifact_ready",
                    "tool_result_sequence": sequence,
                    "artifact": {
                        "artifact_id": &artifact.artifact_id,
                        "uri": &artifact.uri,
                        "checksum": &artifact.checksum,
                        "media_type": &artifact.media_type,
                    },
                }),
            )
            .map(Some),
            ToolResultEvent::Completed {
                tool_call_id,
                sequence,
                result,
            } => Self::tool(
                ApplicationEventKind::ToolResultCompleted,
                metadata,
                tool_call_id,
                Self::tool_result_payload(*sequence, result),
            )
            .map(Some),
            ToolResultEvent::Failed {
                tool_call_id,
                sequence,
                result,
            } => Self::tool(
                ApplicationEventKind::ToolResultFailed,
                metadata,
                tool_call_id,
                Self::tool_result_payload(*sequence, result),
            )
            .map(Some),
            ToolResultEvent::Denied {
                tool_call_id,
                sequence,
                result,
            } => Self::tool(
                ApplicationEventKind::ToolResultDenied,
                metadata,
                tool_call_id,
                Self::tool_result_payload(*sequence, result),
            )
            .map(Some),
            ToolResultEvent::Cancelled {
                tool_call_id,
                sequence,
                result,
            } => Self::tool(
                ApplicationEventKind::ToolResultCancelled,
                metadata,
                tool_call_id,
                Self::tool_result_payload(*sequence, result),
            )
            .map(Some),
            ToolResultEvent::PolicyStopped {
                tool_call_id,
                sequence,
                result,
            } => Self::tool(
                ApplicationEventKind::ToolResultPolicyStopped,
                metadata,
                tool_call_id,
                Self::tool_result_payload(*sequence, result),
            )
            .map(Some),
            ToolResultEvent::Incomplete {
                tool_call_id,
                sequence,
                result,
            } => Self::tool(
                ApplicationEventKind::ToolResultIncomplete,
                metadata,
                tool_call_id,
                Self::tool_result_payload(*sequence, result),
            )
            .map(Some),
        }
    }

    fn content_part_payload(part: &ContentPart) -> Value {
        let kind = match part.kind {
            ContentPartKind::Text => "text",
            ContentPartKind::Json => "json",
            ContentPartKind::ArtifactRef => "artifact_ref",
        };
        json!({
            "kind": kind,
            "text": &part.text,
            "data": &part.data,
            "metadata": &part.metadata,
        })
    }

    fn tool_result_payload(sequence: u64, result: &ToolResult) -> Value {
        json!({
            "status": Self::tool_result_status(result.status),
            "tool_result_sequence": sequence,
            "started_at_unix_ms": result.started_at_unix_ms,
            "completed_at_unix_ms": result.completed_at_unix_ms,
            "output_digest": result.output_digest,
            "effect_outcome": Self::tool_effect_outcome(result.effect_outcome),
            "error_code": result.error.as_ref().map(|error| error.code.as_str()),
        })
    }

    fn tool_result_status(status: ToolResultStatus) -> &'static str {
        match status {
            ToolResultStatus::Completed => "completed",
            ToolResultStatus::Failed => "failed",
            ToolResultStatus::Denied => "denied",
            ToolResultStatus::Cancelled => "cancelled",
            ToolResultStatus::PolicyStopped => "policy_stopped",
            ToolResultStatus::Incomplete => "incomplete",
        }
    }

    fn tool_effect_outcome(effect_outcome: ToolEffectOutcome) -> &'static str {
        match effect_outcome {
            ToolEffectOutcome::NoExternalEffect => "no_external_effect",
            ToolEffectOutcome::Committed => "committed",
            ToolEffectOutcome::NotCommitted => "not_committed",
            ToolEffectOutcome::Unknown => "unknown",
        }
    }

    pub fn output_policy_evaluation_started(
        metadata: ApplicationEventMetadata,
        chunk: &GenerationChunk,
        input_digest: impl AsRef<str>,
    ) -> Result<Self, ApplicationEventError> {
        let input_digest = input_digest.as_ref();
        if input_digest.trim().is_empty() {
            return Err(ApplicationEventError::EmptyPayloadField {
                field: "input_digest",
            });
        }
        Self::new(
            ApplicationEventKind::OutputPolicyEvaluationStarted,
            metadata,
            json!({
                "stream_id": &chunk.stream_id,
                "response_id": &chunk.response_id,
                "chunk_sequence": chunk.sequence,
                "input_digest": input_digest,
                "chunk_text_bytes": chunk.text.len(),
            }),
        )
    }

    pub fn output_cutoff(
        metadata: ApplicationEventMetadata,
        cutoff: &OutputCutoff,
    ) -> Result<Vec<Self>, ApplicationEventError> {
        cutoff
            .validate()
            .map_err(|source| ApplicationEventError::InvalidOutputCutoff { source })?;
        let terminal_reason = match cutoff.terminal_reason {
            TerminalReason::PolicyDenied => "policy_denied",
            TerminalReason::BudgetExhausted => "budget_exhausted",
            TerminalReason::Cancelled => "cancelled",
            TerminalReason::ClientDisconnected => "client_disconnected",
        };
        let draft_disposition = Self::draft_disposition(cutoff.draft_disposition);
        let durable_result = match cutoff.durable_result {
            DurableResult::None => "none",
            DurableResult::Incomplete => "incomplete",
            DurableResult::Partial => "partial",
        };
        let mut events = vec![Self::new(
            ApplicationEventKind::OutputCutoff,
            metadata.clone(),
            json!({
                "stream_id": cutoff.stream_id,
                "response_id": cutoff.response_id,
                "turn_id": cutoff.turn_id,
                "last_generated_sequence": cutoff.last_generated_sequence,
                "last_policy_accepted_sequence": cutoff.last_policy_accepted_sequence,
                "last_client_delivered_sequence": cutoff.last_client_delivered_sequence,
                "terminal_reason": terminal_reason,
                "draft_disposition": draft_disposition,
                "durable_result": durable_result,
                "policy_decision_id": cutoff.policy_decision_id,
                "occurred_at_unix_ms": cutoff.occurred_at_unix_ms,
            }),
        )?];

        let draft_event_kind = match cutoff.draft_disposition {
            DraftDisposition::Keep => None,
            DraftDisposition::MarkIncomplete => Some(ApplicationEventKind::AssistantIncomplete),
            DraftDisposition::Retract => Some(ApplicationEventKind::AssistantRetracted),
        };
        if let Some(kind) = draft_event_kind {
            let mut draft_metadata = metadata;
            draft_metadata.event_id = format!("{}:draft", draft_metadata.event_id);
            draft_metadata.sequence += 1;
            events.push(Self::new(
                kind,
                draft_metadata,
                json!({
                    "response_id": cutoff.response_id,
                    "last_client_delivered_sequence": cutoff.last_client_delivered_sequence,
                    "terminal_reason": terminal_reason,
                    "draft_disposition": draft_disposition,
                    "policy_decision_id": cutoff.policy_decision_id,
                }),
            )?);
        }

        Ok(events)
    }

    pub fn output_policy_decision(
        metadata: ApplicationEventMetadata,
        decision: &OutputPolicyDecision,
    ) -> Result<Self, ApplicationEventError> {
        decision
            .validate()
            .map_err(|source| ApplicationEventError::InvalidOutputPolicyDecision { source })?;
        let (kind, disposition) = match decision.disposition {
            OutputDisposition::Allow => (ApplicationEventKind::OutputPolicyAllowed, "allow"),
            OutputDisposition::Hold => (ApplicationEventKind::OutputPolicyHeld, "hold"),
            OutputDisposition::Redact => (ApplicationEventKind::OutputPolicyRedacted, "redact"),
            OutputDisposition::Replace => (ApplicationEventKind::OutputPolicyReplaced, "replace"),
            OutputDisposition::AbortResponse => (
                ApplicationEventKind::OutputPolicyViolationDetected,
                "abort_response",
            ),
            OutputDisposition::AbortTurn => (
                ApplicationEventKind::OutputPolicyViolationDetected,
                "abort_turn",
            ),
            OutputDisposition::DenyCommit => (
                ApplicationEventKind::OutputPolicyViolationDetected,
                "deny_commit",
            ),
        };
        let provider_cancellation = match decision.provider_cancellation {
            ProviderCancellation::None => "none",
            ProviderCancellation::Request => "request",
            ProviderCancellation::RequiredIfSupported => "required_if_supported",
        };
        let pending_tool_calls = match decision.pending_tool_calls {
            PendingToolCallsDisposition::Keep => "keep",
            PendingToolCallsDisposition::Deny => "deny",
            PendingToolCallsDisposition::CancelAdmitted => "cancel_admitted",
        };
        Self::new(
            kind,
            metadata,
            json!({
                "decision_id": decision.decision_id,
                "disposition": disposition,
                "accepted_through_sequence": decision.accepted_through_sequence,
                "reason_codes": decision.reason_codes,
                "policy_refs": decision.policy_refs,
                "evaluated_at_unix_ms": decision.evaluated_at_unix_ms,
                "input_digest": decision.input_digest,
                "replacement_part_count": decision.replacement_chunks.len(),
                "replacement_chunk_count": decision.replacement_chunks.len(),
                "redaction_count": decision.redactions.len(),
                "provider_cancellation": provider_cancellation,
                "draft_disposition": Self::draft_disposition(decision.draft_disposition),
                "pending_tool_calls": pending_tool_calls,
            }),
        )
    }

    fn draft_disposition(disposition: DraftDisposition) -> &'static str {
        match disposition {
            DraftDisposition::Keep => "keep",
            DraftDisposition::MarkIncomplete => "mark_incomplete",
            DraftDisposition::Retract => "retract",
        }
    }

    fn validate_metadata(metadata: &ApplicationEventMetadata) -> Result<(), ApplicationEventError> {
        for (field, value) in [
            ("event_id", metadata.event_id.as_str()),
            ("run_id", metadata.run_id.as_str()),
            ("response_id", metadata.response_id.as_str()),
            ("release_id", metadata.release_id.as_str()),
            ("policy_snapshot_id", metadata.policy_snapshot_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ApplicationEventError::EmptyMetadataField { field });
            }
        }
        for (field, value) in [
            ("turn_id", &metadata.turn_id),
            ("cursor", &metadata.cursor),
            ("graph_id", &metadata.graph_id),
            ("node_id", &metadata.node_id),
            ("operation_id", &metadata.operation_id),
        ] {
            if value.as_ref().is_some_and(|item| item.trim().is_empty()) {
                return Err(ApplicationEventError::EmptyMetadataField { field });
            }
        }
        Ok(())
    }

    pub fn new(
        kind: ApplicationEventKind,
        metadata: ApplicationEventMetadata,
        payload: Value,
    ) -> Result<Self, ApplicationEventError> {
        if kind.is_tool_event() {
            return Err(ApplicationEventError::ToolEventRequiresToolCallId { kind });
        }
        Self::validate_metadata(&metadata)?;
        if !payload.is_object() {
            return Err(ApplicationEventError::InvalidPayload { field: "payload" });
        }
        if let Some(field) = invalid_payload_key_path(&payload, "payload") {
            return Err(ApplicationEventError::InvalidPayloadKey { field });
        }

        Ok(Self {
            kind,
            metadata,
            tool_call_id: None,
            payload,
        })
    }

    pub fn tool(
        kind: ApplicationEventKind,
        metadata: ApplicationEventMetadata,
        tool_call_id: impl Into<String>,
        payload: Value,
    ) -> Result<Self, ApplicationEventError> {
        if !kind.is_tool_event() {
            return Err(ApplicationEventError::NotToolEvent { kind });
        }
        Self::validate_metadata(&metadata)?;

        let tool_call_id = tool_call_id.into();
        if tool_call_id.trim().is_empty() {
            return Err(ApplicationEventError::EmptyToolCallId);
        }
        if !payload.is_object() {
            return Err(ApplicationEventError::InvalidPayload { field: "payload" });
        }
        if let Some(field) = invalid_payload_key_path(&payload, "payload") {
            return Err(ApplicationEventError::InvalidPayloadKey { field });
        }

        Ok(Self {
            kind,
            metadata,
            tool_call_id: Some(tool_call_id),
            payload,
        })
    }
}

#[derive(Clone, Debug, Default)]
pub struct ApplicationEventStreamState {
    cutoffs: BTreeMap<String, OutputCutoff>,
    accepted_events: Vec<ApplicationEvent>,
    accepted_events_by_id: BTreeMap<String, ApplicationEvent>,
    last_sequence_by_run_id: BTreeMap<String, u64>,
}

impl ApplicationEventStreamState {
    pub fn accept(&mut self, event: ApplicationEvent) -> Option<ApplicationEvent> {
        if let Some(existing_event) = self.accepted_events_by_id.get(&event.metadata.event_id) {
            if existing_event == &event {
                return Some(existing_event.clone());
            }
            return None;
        }
        if let Some(last_sequence) = self.last_sequence_by_run_id.get(&event.metadata.run_id)
            && event.metadata.sequence <= *last_sequence
        {
            return None;
        }
        if event.kind == ApplicationEventKind::OutputCutoff {
            let cutoff = {
                let payload = &event.payload;
                if let Some(payload_response_id) =
                    payload.get("response_id").and_then(Value::as_str)
                    && payload_response_id != event.metadata.response_id
                {
                    return None;
                }
                let response_id = event.metadata.response_id.clone();
                if self.cutoffs.contains_key(&response_id) {
                    return None;
                }
                let terminal_reason = match payload.get("terminal_reason")?.as_str()? {
                    "policy_denied" => TerminalReason::PolicyDenied,
                    "budget_exhausted" => TerminalReason::BudgetExhausted,
                    "cancelled" => TerminalReason::Cancelled,
                    "client_disconnected" => TerminalReason::ClientDisconnected,
                    _ => return None,
                };
                let draft_disposition = match payload.get("draft_disposition")?.as_str()? {
                    "keep" => DraftDisposition::Keep,
                    "mark_incomplete" => DraftDisposition::MarkIncomplete,
                    "retract" => DraftDisposition::Retract,
                    _ => return None,
                };
                let durable_result = match payload.get("durable_result")?.as_str()? {
                    "none" => DurableResult::None,
                    "incomplete" => DurableResult::Incomplete,
                    "partial" => DurableResult::Partial,
                    _ => return None,
                };
                let cutoff = OutputCutoff {
                    stream_id: payload.get("stream_id")?.as_str()?.to_owned(),
                    response_id,
                    turn_id: payload
                        .get("turn_id")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                    last_generated_sequence: payload.get("last_generated_sequence")?.as_u64()?,
                    last_policy_accepted_sequence: payload
                        .get("last_policy_accepted_sequence")?
                        .as_u64()?,
                    last_client_delivered_sequence: payload
                        .get("last_client_delivered_sequence")?
                        .as_u64()?,
                    terminal_reason,
                    draft_disposition,
                    durable_result,
                    policy_decision_id: payload
                        .get("policy_decision_id")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                    occurred_at_unix_ms: payload.get("occurred_at_unix_ms")?.as_u64()?,
                };
                cutoff.validate().ok()?;
                cutoff
            };
            self.cutoffs.insert(cutoff.response_id.clone(), cutoff);
            self.record(event.clone());
            return Some(event);
        }

        if let Some(payload_response_id) = event.payload.get("response_id").and_then(Value::as_str)
            && payload_response_id != event.metadata.response_id
        {
            return None;
        }
        let response_id = event.metadata.response_id.as_str();
        if let Some(cutoff) = self.cutoffs.get(response_id) {
            if matches!(
                event.kind,
                ApplicationEventKind::AssistantRetracted
                    | ApplicationEventKind::AssistantIncomplete
            ) {
                if !draft_terminal_event_matches_cutoff(&event, cutoff) {
                    return None;
                }
                self.record(event.clone());
                return Some(event);
            }
            if event
                .payload
                .get("chunk_sequence")
                .and_then(Value::as_u64)
                .is_some()
            {
                return None;
            }
            if matches!(
                event.kind,
                ApplicationEventKind::OutputPolicyEvaluationStarted
                    | ApplicationEventKind::OutputPolicyAllowed
                    | ApplicationEventKind::OutputPolicyHeld
                    | ApplicationEventKind::OutputPolicyRedacted
                    | ApplicationEventKind::OutputPolicyReplaced
                    | ApplicationEventKind::OutputPolicyViolationDetected
            ) {
                return None;
            }
            if event.kind == ApplicationEventKind::RunSucceeded {
                return None;
            }
            if matches!(
                event.kind,
                ApplicationEventKind::ToolCallProposed
                    | ApplicationEventKind::ToolCallArgumentsDelta
                    | ApplicationEventKind::ToolCallArgumentsCompleted
            ) {
                return None;
            }
            if event.kind.is_tool_event() && !event.kind.is_allowed_after_output_cutoff() {
                return None;
            }
        }

        self.record(event.clone());
        Some(event)
    }

    fn record(&mut self, event: ApplicationEvent) {
        self.accepted_events_by_id
            .insert(event.metadata.event_id.clone(), event.clone());
        self.last_sequence_by_run_id
            .insert(event.metadata.run_id.clone(), event.metadata.sequence);
        self.accepted_events.push(event);
    }

    pub fn accepted_events(&self) -> &[ApplicationEvent] {
        &self.accepted_events
    }

    pub fn cutoff_for_response(&self, response_id: &str) -> Option<&OutputCutoff> {
        self.cutoffs.get(response_id)
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ApplicationCommandKind {
    InvokeGraph,
    GetRunStatus,
    ListRuns,
    AttachToRun,
    DetachFromRun,
    SubscribeEvents,
    UnsubscribeEvents,
    AckEvent,
    RegisterCallback,
    RevokeCallback,
    SubmitAsyncCallback,
    PauseRun,
    ResumeRun,
    CancelRun,
    ExpireRun,
    SubmitInput,
    ApproveEffect,
    DenyEffect,
    SubmitReview,
    RequestBudgetExtension,
    ApplyPolicyOverride,
    ResumeInterrupt,
    SelectCandidate,
    OpenArtifact,
    SetBreakpoint,
    RequestSnapshot,
    RedriveCallbackDelivery,
    MoveCallbackToDeadLetter,
}

impl ApplicationCommandKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::InvokeGraph => "InvokeGraph",
            Self::GetRunStatus => "GetRunStatus",
            Self::ListRuns => "ListRuns",
            Self::AttachToRun => "AttachToRun",
            Self::DetachFromRun => "DetachFromRun",
            Self::SubscribeEvents => "SubscribeEvents",
            Self::UnsubscribeEvents => "UnsubscribeEvents",
            Self::AckEvent => "AckEvent",
            Self::RegisterCallback => "RegisterCallback",
            Self::RevokeCallback => "RevokeCallback",
            Self::SubmitAsyncCallback => "SubmitAsyncCallback",
            Self::PauseRun => "PauseRun",
            Self::ResumeRun => "ResumeRun",
            Self::CancelRun => "CancelRun",
            Self::ExpireRun => "ExpireRun",
            Self::SubmitInput => "SubmitInput",
            Self::ApproveEffect => "ApproveEffect",
            Self::DenyEffect => "DenyEffect",
            Self::SubmitReview => "SubmitReview",
            Self::RequestBudgetExtension => "RequestBudgetExtension",
            Self::ApplyPolicyOverride => "ApplyPolicyOverride",
            Self::ResumeInterrupt => "ResumeInterrupt",
            Self::SelectCandidate => "SelectCandidate",
            Self::OpenArtifact => "OpenArtifact",
            Self::SetBreakpoint => "SetBreakpoint",
            Self::RequestSnapshot => "RequestSnapshot",
            Self::RedriveCallbackDelivery => "RedriveCallbackDelivery",
            Self::MoveCallbackToDeadLetter => "MoveCallbackToDeadLetter",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ApplicationCommandMetadata {
    pub command_id: String,
    pub protocol_version: String,
    pub run_id: String,
    pub turn_id: Option<String>,
    pub sequence: u64,
    pub idempotency_key: Option<String>,
    pub issued_at_unix_ms: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ApplicationCommand {
    pub kind: ApplicationCommandKind,
    pub metadata: ApplicationCommandMetadata,
    pub payload: Value,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ApplicationProtocolEventKind {
    RunStarted,
    TurnStarted,
    ContextReady,
    AssistantDraftStarted,
    AssistantDraftDelta,
    AssistantCommitted,
    AssistantIncomplete,
    AssistantRetracted,
    ToolStarted,
    ToolCompleted,
    ToolCallApprovalRequested,
    ApprovalRequested,
    ReviewRequested,
    BudgetConstrained,
    BudgetExhausted,
    BudgetExtensionRequested,
    BudgetExtensionGranted,
    PolicyDecisionRequired,
    ExecutionDegraded,
    OutputCutoff,
    FilePatchPreview,
    JobProgress,
    ArtifactReady,
    StateSnapshot,
    RunCompleted,
    RunFailed,
    RunCancelled,
    RunPolicyStopped,
    RunExpired,
    AsyncOperationStarted,
    AsyncOperationWaitingCallback,
    AsyncOperationPolling,
    AsyncOperationCompleted,
    AsyncOperationFailed,
    AsyncOperationCancelled,
    AsyncOperationExpired,
    ExternalCallbackReceived,
    ExternalCallbackRejected,
    LateExternalCallbackReceived,
    RunResuming,
    RunPausedBudget,
    RunPausedCallbackDelivery,
    RunPausedPolicy,
    RunPausedOperator,
}

impl ApplicationProtocolEventKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::RunStarted => "RunStarted",
            Self::TurnStarted => "TurnStarted",
            Self::ContextReady => "ContextReady",
            Self::AssistantDraftStarted => "AssistantDraftStarted",
            Self::AssistantDraftDelta => "AssistantDraftDelta",
            Self::AssistantCommitted => "AssistantCommitted",
            Self::AssistantIncomplete => "AssistantIncomplete",
            Self::AssistantRetracted => "AssistantRetracted",
            Self::ToolStarted => "ToolStarted",
            Self::ToolCompleted => "ToolCompleted",
            Self::ToolCallApprovalRequested => "ToolCallApprovalRequested",
            Self::ApprovalRequested => "ApprovalRequested",
            Self::ReviewRequested => "ReviewRequested",
            Self::BudgetConstrained => "BudgetConstrained",
            Self::BudgetExhausted => "BudgetExhausted",
            Self::BudgetExtensionRequested => "BudgetExtensionRequested",
            Self::BudgetExtensionGranted => "BudgetExtensionGranted",
            Self::PolicyDecisionRequired => "PolicyDecisionRequired",
            Self::ExecutionDegraded => "ExecutionDegraded",
            Self::OutputCutoff => "OutputCutoff",
            Self::FilePatchPreview => "FilePatchPreview",
            Self::JobProgress => "JobProgress",
            Self::ArtifactReady => "ArtifactReady",
            Self::StateSnapshot => "StateSnapshot",
            Self::RunCompleted => "RunCompleted",
            Self::RunFailed => "RunFailed",
            Self::RunCancelled => "RunCancelled",
            Self::RunPolicyStopped => "RunPolicyStopped",
            Self::RunExpired => "RunExpired",
            Self::AsyncOperationStarted => "AsyncOperationStarted",
            Self::AsyncOperationWaitingCallback => "AsyncOperationWaitingCallback",
            Self::AsyncOperationPolling => "AsyncOperationPolling",
            Self::AsyncOperationCompleted => "AsyncOperationCompleted",
            Self::AsyncOperationFailed => "AsyncOperationFailed",
            Self::AsyncOperationCancelled => "AsyncOperationCancelled",
            Self::AsyncOperationExpired => "AsyncOperationExpired",
            Self::ExternalCallbackReceived => "ExternalCallbackReceived",
            Self::ExternalCallbackRejected => "ExternalCallbackRejected",
            Self::LateExternalCallbackReceived => "LateExternalCallbackReceived",
            Self::RunResuming => "RunResuming",
            Self::RunPausedBudget => "RunPausedBudget",
            Self::RunPausedCallbackDelivery => "RunPausedCallbackDelivery",
            Self::RunPausedPolicy => "RunPausedPolicy",
            Self::RunPausedOperator => "RunPausedOperator",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ApplicationProtocolEventMetadata {
    pub event_id: String,
    pub protocol_version: String,
    pub run_id: String,
    pub release_id: String,
    pub turn_id: Option<String>,
    pub operation_id: Option<String>,
    pub sequence: u64,
    pub cursor: Option<String>,
    pub occurred_at_unix_ms: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ApplicationProtocolEvent {
    pub kind: ApplicationProtocolEventKind,
    pub metadata: ApplicationProtocolEventMetadata,
    pub payload: Value,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ApplicationProtocolError {
    EmptyCommandId,
    EmptyEventId,
    EmptyMetadataField {
        field: &'static str,
    },
    InvalidPayload {
        field: &'static str,
    },
    InvalidPayloadKey {
        field: String,
    },
    InvalidToolResultEvent {
        source: ToolResultEventError,
    },
    DuplicateEventIdConflict {
        event_id: String,
    },
    DuplicateCursorConflict {
        cursor: String,
    },
    NonMonotonicSequence {
        previous: u64,
        next: u64,
    },
    ProtocolVersionMismatch {
        left: String,
        right: String,
    },
    RunMismatch {
        expected_run_id: String,
        actual_run_id: String,
    },
    Storage {
        message: String,
    },
}

impl fmt::Display for ApplicationProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyCommandId => write!(formatter, "application command id must not be empty"),
            Self::EmptyEventId => write!(formatter, "application event id must not be empty"),
            Self::EmptyMetadataField { field } => {
                write!(
                    formatter,
                    "application protocol metadata field {field} must not be empty"
                )
            }
            Self::InvalidPayload { field } => {
                write!(
                    formatter,
                    "application protocol {field} must be a JSON object"
                )
            }
            Self::InvalidPayloadKey { field } => {
                write!(
                    formatter,
                    "application protocol {field} keys must be non-empty strings"
                )
            }
            Self::InvalidToolResultEvent { source } => {
                write!(formatter, "tool result event is invalid: {source:?}")
            }
            Self::DuplicateEventIdConflict { event_id } => write!(
                formatter,
                "application event id {event_id:?} was replayed with different event content"
            ),
            Self::DuplicateCursorConflict { cursor } => write!(
                formatter,
                "application event cursor {cursor:?} was assigned to more than one event"
            ),
            Self::NonMonotonicSequence { previous, next } => write!(
                formatter,
                "application event sequence {next} must be greater than previous sequence {previous}"
            ),
            Self::ProtocolVersionMismatch { left, right } => {
                write!(formatter, "protocol versions differ: {left:?} vs {right:?}")
            }
            Self::RunMismatch {
                expected_run_id,
                actual_run_id,
            } => write!(
                formatter,
                "application event run {actual_run_id:?} does not match log run {expected_run_id:?}"
            ),
            Self::Storage { message } => {
                write!(
                    formatter,
                    "application protocol log storage error: {message}"
                )
            }
        }
    }
}

impl Error for ApplicationProtocolError {}

impl ApplicationCommand {
    pub fn new(
        kind: ApplicationCommandKind,
        metadata: ApplicationCommandMetadata,
        payload: Value,
    ) -> Result<Self, ApplicationProtocolError> {
        if metadata.command_id.trim().is_empty() {
            return Err(ApplicationProtocolError::EmptyCommandId);
        }
        for (field, value) in [
            ("protocol_version", metadata.protocol_version.as_str()),
            ("run_id", metadata.run_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ApplicationProtocolError::EmptyMetadataField { field });
            }
        }
        for (field, value) in [
            ("turn_id", metadata.turn_id.as_deref()),
            ("idempotency_key", metadata.idempotency_key.as_deref()),
        ] {
            if value.is_some_and(|value| value.trim().is_empty()) {
                return Err(ApplicationProtocolError::EmptyMetadataField { field });
            }
        }
        if !payload.is_object() {
            return Err(ApplicationProtocolError::InvalidPayload { field: "payload" });
        }
        if let Some(field) = invalid_payload_key_path(&payload, "payload") {
            return Err(ApplicationProtocolError::InvalidPayloadKey { field });
        }
        Ok(Self {
            kind,
            metadata,
            payload,
        })
    }
}

impl ApplicationProtocolEvent {
    pub fn new(
        kind: ApplicationProtocolEventKind,
        metadata: ApplicationProtocolEventMetadata,
        payload: Value,
    ) -> Result<Self, ApplicationProtocolError> {
        if metadata.event_id.trim().is_empty() {
            return Err(ApplicationProtocolError::EmptyEventId);
        }
        for (field, value) in [
            ("protocol_version", metadata.protocol_version.as_str()),
            ("run_id", metadata.run_id.as_str()),
            ("release_id", metadata.release_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ApplicationProtocolError::EmptyMetadataField { field });
            }
        }
        for (field, value) in [
            ("turn_id", metadata.turn_id.as_deref()),
            ("operation_id", metadata.operation_id.as_deref()),
            ("cursor", metadata.cursor.as_deref()),
        ] {
            if value.is_some_and(|value| value.trim().is_empty()) {
                return Err(ApplicationProtocolError::EmptyMetadataField { field });
            }
        }
        if metadata.cursor.is_none() {
            return Err(ApplicationProtocolError::EmptyMetadataField { field: "cursor" });
        }
        if !payload.is_object() {
            return Err(ApplicationProtocolError::InvalidPayload { field: "payload" });
        }
        if let Some(field) = invalid_payload_key_path(&payload, "payload") {
            return Err(ApplicationProtocolError::InvalidPayloadKey { field });
        }
        Ok(Self {
            kind,
            metadata,
            payload,
        })
    }

    pub fn tool_result_stream(
        metadata: ApplicationProtocolEventMetadata,
        event: &ToolResultEvent,
    ) -> Result<Option<Self>, ApplicationProtocolError> {
        event
            .validate()
            .map_err(|source| ApplicationProtocolError::InvalidToolResultEvent { source })?;

        match event {
            ToolResultEvent::Delta {
                tool_call_id,
                sequence,
                output,
            } => Self::new(
                ApplicationProtocolEventKind::JobProgress,
                metadata,
                json!({
                    "tool_call_id": tool_call_id,
                    "tool_result_sequence": sequence,
                    "output": output
                        .iter()
                        .map(|part| {
                            let kind = match part.kind {
                                ContentPartKind::Text => "text",
                                ContentPartKind::Json => "json",
                                ContentPartKind::ArtifactRef => "artifact_ref",
                            };
                            json!({
                                "kind": kind,
                                "text": part.text,
                                "data": part.data,
                                "metadata": part.metadata,
                            })
                        })
                        .collect::<Vec<_>>(),
                }),
            )
            .map(Some),
            ToolResultEvent::ArtifactReady {
                tool_call_id,
                sequence,
                artifact,
            } => Self::new(
                ApplicationProtocolEventKind::ArtifactReady,
                metadata,
                json!({
                    "tool_call_id": tool_call_id,
                    "tool_result_sequence": sequence,
                    "artifact": {
                        "artifact_id": &artifact.artifact_id,
                        "uri": &artifact.uri,
                        "checksum": &artifact.checksum,
                        "media_type": &artifact.media_type,
                    },
                }),
            )
            .map(Some),
            ToolResultEvent::Started { .. }
            | ToolResultEvent::Completed { .. }
            | ToolResultEvent::Failed { .. }
            | ToolResultEvent::Denied { .. }
            | ToolResultEvent::Cancelled { .. }
            | ToolResultEvent::PolicyStopped { .. }
            | ToolResultEvent::Incomplete { .. } => Ok(None),
        }
    }
}

fn invalid_payload_key_path(value: &Value, field: &str) -> Option<String> {
    match value {
        Value::Object(object) => {
            for (key, value) in object {
                if key.trim().is_empty() {
                    return Some(field.to_owned());
                }
                let nested_field = format!("{field}.{key}");
                if let Some(invalid_field) = invalid_payload_key_path(value, &nested_field) {
                    return Some(invalid_field);
                }
            }
            None
        }
        Value::Array(items) => {
            for item in items {
                if let Some(invalid_field) = invalid_payload_key_path(item, field) {
                    return Some(invalid_field);
                }
            }
            None
        }
        _ => None,
    }
}

fn draft_terminal_event_matches_cutoff(event: &ApplicationEvent, cutoff: &OutputCutoff) -> bool {
    let terminal_reason = match cutoff.terminal_reason {
        TerminalReason::PolicyDenied => "policy_denied",
        TerminalReason::BudgetExhausted => "budget_exhausted",
        TerminalReason::Cancelled => "cancelled",
        TerminalReason::ClientDisconnected => "client_disconnected",
    };
    if event.payload.get("terminal_reason").and_then(Value::as_str) != Some(terminal_reason) {
        return false;
    }

    let draft_disposition = match event.kind {
        ApplicationEventKind::AssistantIncomplete => "mark_incomplete",
        ApplicationEventKind::AssistantRetracted => "retract",
        _ => return false,
    };
    if event
        .payload
        .get("draft_disposition")
        .and_then(Value::as_str)
        != Some(draft_disposition)
    {
        return false;
    }

    event
        .payload
        .get("last_client_delivered_sequence")
        .and_then(Value::as_u64)
        == Some(cutoff.last_client_delivered_sequence)
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ApplicationProtocolStreamState {
    cutoffs: BTreeMap<String, OutputCutoffBoundary>,
    accepted_events: Vec<ApplicationProtocolEvent>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct OutputCutoffBoundary {
    last_client_delivered_sequence: u64,
    terminal_reason: String,
}

impl ApplicationProtocolStreamState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn accept(&mut self, event: ApplicationProtocolEvent) -> Option<ApplicationProtocolEvent> {
        if event.kind == ApplicationProtocolEventKind::OutputCutoff {
            let response_id = event
                .payload
                .get("response_id")
                .and_then(Value::as_str)?
                .to_owned();
            if response_id.trim().is_empty() || self.cutoffs.contains_key(&response_id) {
                return None;
            }
            let terminal_reason = event
                .payload
                .get("terminal_reason")
                .and_then(Value::as_str)?;
            if !matches!(
                terminal_reason,
                "policy_denied" | "budget_exhausted" | "cancelled" | "client_disconnected"
            ) {
                return None;
            }
            let draft_disposition = event
                .payload
                .get("draft_disposition")
                .and_then(Value::as_str)?;
            if !matches!(draft_disposition, "keep" | "mark_incomplete" | "retract") {
                return None;
            }
            if !matches!(
                event
                    .payload
                    .get("durable_result")
                    .and_then(Value::as_str)?,
                "none" | "incomplete" | "partial"
            ) {
                return None;
            }
            if event
                .payload
                .get("occurred_at_unix_ms")
                .and_then(Value::as_u64)?
                == 0
            {
                return None;
            }
            let last_generated_sequence = event
                .payload
                .get("last_generated_sequence")
                .and_then(Value::as_u64)?;
            let last_policy_accepted_sequence = event
                .payload
                .get("last_policy_accepted_sequence")
                .and_then(Value::as_u64)?;
            let last_client_delivered_sequence = event
                .payload
                .get("last_client_delivered_sequence")
                .and_then(Value::as_u64)?;
            if last_policy_accepted_sequence > last_generated_sequence
                || last_client_delivered_sequence > last_generated_sequence
            {
                return None;
            }
            if last_client_delivered_sequence > last_policy_accepted_sequence
                && draft_disposition == "keep"
            {
                return None;
            }
            self.cutoffs.insert(
                response_id,
                OutputCutoffBoundary {
                    last_client_delivered_sequence,
                    terminal_reason: terminal_reason.to_owned(),
                },
            );
            self.accepted_events.push(event.clone());
            return Some(event);
        }

        let response_id = event.payload.get("response_id").and_then(Value::as_str);
        if let Some(response_id) = response_id
            && let Some(cutoff_boundary) = self.cutoffs.get(response_id)
        {
            if matches!(
                event.kind,
                ApplicationProtocolEventKind::AssistantIncomplete
                    | ApplicationProtocolEventKind::AssistantRetracted
            ) {
                if event.payload.get("terminal_reason").and_then(Value::as_str)
                    != Some(cutoff_boundary.terminal_reason.as_str())
                {
                    return None;
                }
                let draft_disposition = event
                    .payload
                    .get("draft_disposition")
                    .and_then(Value::as_str);
                if !matches!(
                    (event.kind, draft_disposition),
                    (
                        ApplicationProtocolEventKind::AssistantIncomplete,
                        Some("mark_incomplete")
                    ) | (
                        ApplicationProtocolEventKind::AssistantRetracted,
                        Some("retract")
                    )
                ) {
                    return None;
                }
                if event
                    .payload
                    .get("last_client_delivered_sequence")
                    .and_then(Value::as_u64)
                    != Some(cutoff_boundary.last_client_delivered_sequence)
                {
                    return None;
                }
                self.accepted_events.push(event.clone());
                return Some(event);
            }
            return None;
        }

        self.accepted_events.push(event.clone());
        Some(event)
    }

    pub fn accepted_events(&self) -> &[ApplicationProtocolEvent] {
        &self.accepted_events
    }

    pub fn cutoff_for_response(&self, response_id: &str) -> Option<u64> {
        self.cutoffs
            .get(response_id)
            .map(|boundary| boundary.last_client_delivered_sequence)
    }
}

#[derive(Clone, Debug, Default)]
pub struct ApplicationProtocolLog {
    events: Vec<ApplicationProtocolEvent>,
    events_by_id: BTreeMap<String, ApplicationProtocolEvent>,
    event_ids_by_cursor: BTreeMap<String, String>,
    run_id: Option<String>,
    last_sequence: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ApplicationProtocolReplayError {
    CursorExpired {
        requested_cursor: String,
        earliest_available_cursor: Option<String>,
        last_cursor: Option<String>,
        last_sequence: Option<u64>,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub enum AttachToRunReplay {
    Attached {
        replayed_events: Vec<ApplicationProtocolEvent>,
        live_cursor: Option<String>,
    },
    CursorExpired {
        requested_cursor: String,
        earliest_available_cursor: Option<String>,
        last_cursor: Option<String>,
        last_sequence: Option<u64>,
        run_status: Option<RunStatusSnapshot>,
    },
}

impl ApplicationProtocolLog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn append(
        &mut self,
        event: ApplicationProtocolEvent,
    ) -> Result<bool, ApplicationProtocolError> {
        if let Some(existing) = self.events_by_id.get(&event.metadata.event_id) {
            if existing == &event {
                return Ok(false);
            }
            return Err(ApplicationProtocolError::DuplicateEventIdConflict {
                event_id: event.metadata.event_id,
            });
        }
        if let Some(run_id) = &self.run_id {
            if run_id != &event.metadata.run_id {
                return Err(ApplicationProtocolError::RunMismatch {
                    expected_run_id: run_id.clone(),
                    actual_run_id: event.metadata.run_id,
                });
            }
        } else {
            self.run_id = Some(event.metadata.run_id.clone());
        }
        if let Some(cursor) = event.metadata.cursor.as_ref()
            && self
                .event_ids_by_cursor
                .get(cursor)
                .is_some_and(|event_id| event_id != &event.metadata.event_id)
        {
            return Err(ApplicationProtocolError::DuplicateCursorConflict {
                cursor: cursor.clone(),
            });
        }
        if let Some(previous) = self.last_sequence
            && event.metadata.sequence <= previous
        {
            return Err(ApplicationProtocolError::NonMonotonicSequence {
                previous,
                next: event.metadata.sequence,
            });
        }
        self.last_sequence = Some(event.metadata.sequence);
        self.events_by_id
            .insert(event.metadata.event_id.clone(), event.clone());
        if let Some(cursor) = event.metadata.cursor.as_ref() {
            self.event_ids_by_cursor
                .insert(cursor.clone(), event.metadata.event_id.clone());
        }
        self.events.push(event);
        Ok(true)
    }

    pub fn replay_after(
        &self,
        cursor: Option<&str>,
        limit: usize,
    ) -> Vec<ApplicationProtocolEvent> {
        if cursor.is_some_and(|cursor| cursor.trim().is_empty()) {
            return Vec::new();
        }
        let start_index = cursor
            .and_then(|cursor| {
                self.events.iter().position(|event| {
                    event.metadata.cursor.as_deref() == Some(cursor)
                        || event.metadata.sequence.to_string() == cursor
                })
            })
            .map_or(0, |index| index + 1);
        self.events
            .iter()
            .skip(start_index)
            .take(limit)
            .cloned()
            .collect()
    }

    pub fn replay_after_retained(
        &self,
        cursor: Option<&str>,
        limit: usize,
        retained_event_count: usize,
    ) -> Result<Vec<ApplicationProtocolEvent>, ApplicationProtocolReplayError> {
        let retained_start = self.events.len().saturating_sub(retained_event_count);
        let retained = &self.events[retained_start..];

        if let Some(cursor) = cursor {
            let full_index = self
                .events
                .iter()
                .position(|event| event_matches_cursor(event, cursor));
            match full_index {
                Some(index) if index >= retained_start => {
                    let retained_index = index - retained_start + 1;
                    return Ok(retained
                        .iter()
                        .skip(retained_index)
                        .take(limit)
                        .cloned()
                        .collect());
                }
                Some(_) | None => {
                    return Err(ApplicationProtocolReplayError::CursorExpired {
                        requested_cursor: cursor.to_owned(),
                        earliest_available_cursor: retained.first().and_then(event_cursor),
                        last_cursor: self.events.last().and_then(event_cursor),
                        last_sequence: self.events.last().map(|event| event.metadata.sequence),
                    });
                }
            }
        }

        Ok(retained.iter().take(limit).cloned().collect())
    }

    pub fn attach_to_run(
        &self,
        last_cursor: Option<&str>,
        replay_limit: usize,
        retained_event_count: usize,
    ) -> AttachToRunReplay {
        self.attach_to_run_with_optional_status(
            last_cursor,
            replay_limit,
            retained_event_count,
            None,
        )
    }

    pub fn attach_to_run_with_status(
        &self,
        last_cursor: Option<&str>,
        replay_limit: usize,
        retained_event_count: usize,
        run_status: RunStatusSnapshot,
    ) -> AttachToRunReplay {
        self.attach_to_run_with_optional_status(
            last_cursor,
            replay_limit,
            retained_event_count,
            Some(run_status),
        )
    }

    fn attach_to_run_with_optional_status(
        &self,
        last_cursor: Option<&str>,
        replay_limit: usize,
        retained_event_count: usize,
        run_status: Option<RunStatusSnapshot>,
    ) -> AttachToRunReplay {
        match self.replay_after_retained(last_cursor, replay_limit, retained_event_count) {
            Ok(replayed_events) => AttachToRunReplay::Attached {
                replayed_events,
                live_cursor: self.events.last().and_then(event_cursor),
            },
            Err(ApplicationProtocolReplayError::CursorExpired {
                requested_cursor,
                earliest_available_cursor,
                last_cursor,
                last_sequence,
            }) => AttachToRunReplay::CursorExpired {
                requested_cursor,
                earliest_available_cursor,
                last_cursor,
                last_sequence,
                run_status,
            },
        }
    }

    pub fn len(&self) -> usize {
        self.events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }
}

pub struct SqliteApplicationProtocolLog {
    connection: Mutex<Connection>,
}

impl SqliteApplicationProtocolLog {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, ApplicationProtocolError> {
        let connection = Connection::open(path).map_err(application_protocol_storage_error)?;
        initialize_sqlite_application_protocol_log(&connection)?;
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }

    pub fn open_in_memory() -> Result<Self, ApplicationProtocolError> {
        let connection =
            Connection::open_in_memory().map_err(application_protocol_storage_error)?;
        initialize_sqlite_application_protocol_log(&connection)?;
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }

    pub fn append(
        &self,
        event: ApplicationProtocolEvent,
    ) -> Result<bool, ApplicationProtocolError> {
        let event = ApplicationProtocolEvent::new(
            event.kind,
            event.metadata.clone(),
            event.payload.clone(),
        )?;
        let cursor = event
            .metadata
            .cursor
            .as_deref()
            .ok_or(ApplicationProtocolError::EmptyMetadataField { field: "cursor" })?;
        let sequence = sqlite_i64_from_u64("sequence", event.metadata.sequence)?;
        let event_json = application_protocol_event_to_value(&event).to_string();
        let mut connection =
            self.connection
                .lock()
                .map_err(|_| ApplicationProtocolError::Storage {
                    message: "application protocol log mutex was poisoned".to_owned(),
                })?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(application_protocol_storage_error)?;
        let mut log = sqlite_load_application_protocol_log(&transaction)?;
        let appended = log.append(event.clone())?;
        if !appended {
            transaction
                .commit()
                .map_err(application_protocol_storage_error)?;
            return Ok(false);
        }

        transaction
            .execute(
                "INSERT INTO application_protocol_events (
                    event_id,
                    run_id,
                    sequence,
                    cursor,
                    event_json
                ) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![
                    &event.metadata.event_id,
                    &event.metadata.run_id,
                    sequence,
                    cursor,
                    event_json,
                ],
            )
            .map_err(application_protocol_storage_error)?;
        transaction
            .commit()
            .map_err(application_protocol_storage_error)?;
        Ok(true)
    }

    pub fn replay_after(
        &self,
        cursor: Option<&str>,
        limit: usize,
    ) -> Result<Vec<ApplicationProtocolEvent>, ApplicationProtocolError> {
        Ok(self.to_protocol_log()?.replay_after(cursor, limit))
    }

    pub fn attach_to_run(
        &self,
        last_cursor: Option<&str>,
        replay_limit: usize,
        retained_event_count: usize,
    ) -> Result<AttachToRunReplay, ApplicationProtocolError> {
        Ok(self
            .to_protocol_log()?
            .attach_to_run(last_cursor, replay_limit, retained_event_count))
    }

    pub fn attach_to_run_with_status(
        &self,
        last_cursor: Option<&str>,
        replay_limit: usize,
        retained_event_count: usize,
        run_status: RunStatusSnapshot,
    ) -> Result<AttachToRunReplay, ApplicationProtocolError> {
        Ok(self.to_protocol_log()?.attach_to_run_with_status(
            last_cursor,
            replay_limit,
            retained_event_count,
            run_status,
        ))
    }

    pub fn to_protocol_log(&self) -> Result<ApplicationProtocolLog, ApplicationProtocolError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| ApplicationProtocolError::Storage {
                message: "application protocol log mutex was poisoned".to_owned(),
            })?;
        sqlite_load_application_protocol_log(&connection)
    }

    pub fn len(&self) -> Result<usize, ApplicationProtocolError> {
        Ok(self.to_protocol_log()?.len())
    }

    pub fn is_empty(&self) -> Result<bool, ApplicationProtocolError> {
        Ok(self.to_protocol_log()?.is_empty())
    }
}

fn initialize_sqlite_application_protocol_log(
    connection: &Connection,
) -> Result<(), ApplicationProtocolError> {
    connection
        .execute_batch(
            "
            CREATE TABLE IF NOT EXISTS application_protocol_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                cursor TEXT NOT NULL,
                event_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS application_protocol_events_cursor_idx
                ON application_protocol_events(cursor);
            CREATE INDEX IF NOT EXISTS application_protocol_events_sequence_idx
                ON application_protocol_events(sequence, event_id);
            ",
        )
        .map_err(application_protocol_storage_error)
}

fn sqlite_load_application_protocol_log(
    connection: &Connection,
) -> Result<ApplicationProtocolLog, ApplicationProtocolError> {
    let mut statement = connection
        .prepare(
            "SELECT event_id, run_id, sequence, cursor, event_json
             FROM application_protocol_events
             ORDER BY sequence ASC, event_id ASC",
        )
        .map_err(application_protocol_storage_error)?;
    let mut rows = statement
        .query([])
        .map_err(application_protocol_storage_error)?;
    let mut log = ApplicationProtocolLog::new();

    while let Some(row) = rows.next().map_err(application_protocol_storage_error)? {
        let event_id: String = row.get(0).map_err(application_protocol_storage_error)?;
        let run_id: String = row.get(1).map_err(application_protocol_storage_error)?;
        let sequence = sqlite_u64_from_i64(
            "sequence",
            row.get(2).map_err(application_protocol_storage_error)?,
        )?;
        let cursor: String = row.get(3).map_err(application_protocol_storage_error)?;
        let event_json: String = row.get(4).map_err(application_protocol_storage_error)?;
        let event_value: Value = serde_json::from_str(&event_json).map_err(|error| {
            ApplicationProtocolError::Storage {
                message: format!(
                    "stored application protocol event {event_id:?} is invalid JSON: {error}"
                ),
            }
        })?;
        let event = application_protocol_event_from_value(event_value)?;

        if event.metadata.event_id != event_id
            || event.metadata.run_id != run_id
            || event.metadata.sequence != sequence
            || event.metadata.cursor.as_deref() != Some(cursor.as_str())
        {
            return Err(ApplicationProtocolError::Storage {
                message: format!(
                    "stored application protocol event {event_id:?} row metadata does not match decoded event"
                ),
            });
        }

        log.append(event)?;
    }

    Ok(log)
}

fn application_protocol_event_to_value(event: &ApplicationProtocolEvent) -> Value {
    json!({
        "kind": event.kind.as_str(),
        "metadata": {
            "event_id": event.metadata.event_id.clone(),
            "protocol_version": event.metadata.protocol_version.clone(),
            "run_id": event.metadata.run_id.clone(),
            "release_id": event.metadata.release_id.clone(),
            "turn_id": event.metadata.turn_id.clone(),
            "operation_id": event.metadata.operation_id.clone(),
            "sequence": event.metadata.sequence,
            "cursor": event.metadata.cursor.clone(),
            "occurred_at_unix_ms": event.metadata.occurred_at_unix_ms,
        },
        "payload": event.payload.clone(),
    })
}

fn application_protocol_event_from_value(
    value: Value,
) -> Result<ApplicationProtocolEvent, ApplicationProtocolError> {
    let object = value
        .as_object()
        .ok_or_else(|| ApplicationProtocolError::Storage {
            message: "stored application protocol event must be a JSON object".to_owned(),
        })?;
    let kind = application_protocol_event_kind_from_str(required_json_string(object, "kind")?)?;
    let metadata = required_json_object(object, "metadata")?;
    let payload =
        object
            .get("payload")
            .cloned()
            .ok_or_else(|| ApplicationProtocolError::Storage {
                message: "stored application protocol event missing payload".to_owned(),
            })?;

    ApplicationProtocolEvent::new(
        kind,
        ApplicationProtocolEventMetadata {
            event_id: required_json_string(metadata, "event_id")?.to_owned(),
            protocol_version: required_json_string(metadata, "protocol_version")?.to_owned(),
            run_id: required_json_string(metadata, "run_id")?.to_owned(),
            release_id: required_json_string(metadata, "release_id")?.to_owned(),
            turn_id: optional_json_string(metadata, "turn_id")?,
            operation_id: optional_json_string(metadata, "operation_id")?,
            sequence: required_json_u64(metadata, "sequence")?,
            cursor: optional_json_string(metadata, "cursor")?,
            occurred_at_unix_ms: required_json_u64(metadata, "occurred_at_unix_ms")?,
        },
        payload,
    )
}

fn application_protocol_event_kind_from_str(
    value: &str,
) -> Result<ApplicationProtocolEventKind, ApplicationProtocolError> {
    match value {
        "RunStarted" => Ok(ApplicationProtocolEventKind::RunStarted),
        "TurnStarted" => Ok(ApplicationProtocolEventKind::TurnStarted),
        "ContextReady" => Ok(ApplicationProtocolEventKind::ContextReady),
        "AssistantDraftStarted" => Ok(ApplicationProtocolEventKind::AssistantDraftStarted),
        "AssistantDraftDelta" => Ok(ApplicationProtocolEventKind::AssistantDraftDelta),
        "AssistantCommitted" => Ok(ApplicationProtocolEventKind::AssistantCommitted),
        "AssistantIncomplete" => Ok(ApplicationProtocolEventKind::AssistantIncomplete),
        "AssistantRetracted" => Ok(ApplicationProtocolEventKind::AssistantRetracted),
        "ToolStarted" => Ok(ApplicationProtocolEventKind::ToolStarted),
        "ToolCompleted" => Ok(ApplicationProtocolEventKind::ToolCompleted),
        "ToolCallApprovalRequested" => Ok(ApplicationProtocolEventKind::ToolCallApprovalRequested),
        "ApprovalRequested" => Ok(ApplicationProtocolEventKind::ApprovalRequested),
        "ReviewRequested" => Ok(ApplicationProtocolEventKind::ReviewRequested),
        "BudgetConstrained" => Ok(ApplicationProtocolEventKind::BudgetConstrained),
        "BudgetExhausted" => Ok(ApplicationProtocolEventKind::BudgetExhausted),
        "BudgetExtensionRequested" => Ok(ApplicationProtocolEventKind::BudgetExtensionRequested),
        "BudgetExtensionGranted" => Ok(ApplicationProtocolEventKind::BudgetExtensionGranted),
        "PolicyDecisionRequired" => Ok(ApplicationProtocolEventKind::PolicyDecisionRequired),
        "ExecutionDegraded" => Ok(ApplicationProtocolEventKind::ExecutionDegraded),
        "OutputCutoff" => Ok(ApplicationProtocolEventKind::OutputCutoff),
        "FilePatchPreview" => Ok(ApplicationProtocolEventKind::FilePatchPreview),
        "JobProgress" => Ok(ApplicationProtocolEventKind::JobProgress),
        "ArtifactReady" => Ok(ApplicationProtocolEventKind::ArtifactReady),
        "StateSnapshot" => Ok(ApplicationProtocolEventKind::StateSnapshot),
        "RunCompleted" => Ok(ApplicationProtocolEventKind::RunCompleted),
        "RunFailed" => Ok(ApplicationProtocolEventKind::RunFailed),
        "RunCancelled" => Ok(ApplicationProtocolEventKind::RunCancelled),
        "RunPolicyStopped" => Ok(ApplicationProtocolEventKind::RunPolicyStopped),
        "RunExpired" => Ok(ApplicationProtocolEventKind::RunExpired),
        "AsyncOperationStarted" => Ok(ApplicationProtocolEventKind::AsyncOperationStarted),
        "AsyncOperationWaitingCallback" => {
            Ok(ApplicationProtocolEventKind::AsyncOperationWaitingCallback)
        }
        "AsyncOperationPolling" => Ok(ApplicationProtocolEventKind::AsyncOperationPolling),
        "AsyncOperationCompleted" => Ok(ApplicationProtocolEventKind::AsyncOperationCompleted),
        "AsyncOperationFailed" => Ok(ApplicationProtocolEventKind::AsyncOperationFailed),
        "AsyncOperationCancelled" => Ok(ApplicationProtocolEventKind::AsyncOperationCancelled),
        "AsyncOperationExpired" => Ok(ApplicationProtocolEventKind::AsyncOperationExpired),
        "ExternalCallbackReceived" => Ok(ApplicationProtocolEventKind::ExternalCallbackReceived),
        "ExternalCallbackRejected" => Ok(ApplicationProtocolEventKind::ExternalCallbackRejected),
        "LateExternalCallbackReceived" => {
            Ok(ApplicationProtocolEventKind::LateExternalCallbackReceived)
        }
        "RunResuming" => Ok(ApplicationProtocolEventKind::RunResuming),
        "RunPausedBudget" => Ok(ApplicationProtocolEventKind::RunPausedBudget),
        "RunPausedCallbackDelivery" => Ok(ApplicationProtocolEventKind::RunPausedCallbackDelivery),
        "RunPausedPolicy" => Ok(ApplicationProtocolEventKind::RunPausedPolicy),
        "RunPausedOperator" => Ok(ApplicationProtocolEventKind::RunPausedOperator),
        _ => Err(ApplicationProtocolError::Storage {
            message: format!("stored application protocol event kind {value:?} is unknown"),
        }),
    }
}

fn required_json_object<'a>(
    object: &'a serde_json::Map<String, Value>,
    field: &'static str,
) -> Result<&'a serde_json::Map<String, Value>, ApplicationProtocolError> {
    object
        .get(field)
        .and_then(Value::as_object)
        .ok_or_else(|| ApplicationProtocolError::Storage {
            message: format!("stored application protocol event {field} must be a JSON object"),
        })
}

fn required_json_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    field: &'static str,
) -> Result<&'a str, ApplicationProtocolError> {
    object
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| ApplicationProtocolError::Storage {
            message: format!("stored application protocol event {field} must be a string"),
        })
}

fn optional_json_string(
    object: &serde_json::Map<String, Value>,
    field: &'static str,
) -> Result<Option<String>, ApplicationProtocolError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        Some(_) => Err(ApplicationProtocolError::Storage {
            message: format!(
                "stored application protocol event optional field {field} must be a string or null"
            ),
        }),
    }
}

fn required_json_u64(
    object: &serde_json::Map<String, Value>,
    field: &'static str,
) -> Result<u64, ApplicationProtocolError> {
    object
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| ApplicationProtocolError::Storage {
            message: format!(
                "stored application protocol event field {field} must be an unsigned integer"
            ),
        })
}

fn sqlite_i64_from_u64(field: &'static str, value: u64) -> Result<i64, ApplicationProtocolError> {
    i64::try_from(value).map_err(|_| ApplicationProtocolError::Storage {
        message: format!("application protocol event {field} {value} exceeds SQLite integer range"),
    })
}

fn sqlite_u64_from_i64(field: &'static str, value: i64) -> Result<u64, ApplicationProtocolError> {
    u64::try_from(value).map_err(|_| ApplicationProtocolError::Storage {
        message: format!("stored application protocol event {field} {value} is negative"),
    })
}

fn application_protocol_storage_error(error: rusqlite::Error) -> ApplicationProtocolError {
    ApplicationProtocolError::Storage {
        message: error.to_string(),
    }
}

fn event_matches_cursor(event: &ApplicationProtocolEvent, cursor: &str) -> bool {
    event.metadata.cursor.as_deref() == Some(cursor)
        || event.metadata.sequence.to_string() == cursor
}

fn event_cursor(event: &ApplicationProtocolEvent) -> Option<String> {
    event
        .metadata
        .cursor
        .clone()
        .or_else(|| Some(event.metadata.sequence.to_string()))
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ApplicationProtocolCapabilities {
    pub protocol_version: String,
    pub commands: BTreeSet<ApplicationCommandKind>,
    pub events: BTreeSet<ApplicationProtocolEventKind>,
}

impl ApplicationProtocolCapabilities {
    pub fn new(protocol_version: impl Into<String>) -> Self {
        Self {
            protocol_version: protocol_version.into(),
            commands: BTreeSet::new(),
            events: BTreeSet::new(),
        }
    }

    pub fn with_commands<I>(mut self, commands: I) -> Self
    where
        I: IntoIterator<Item = ApplicationCommandKind>,
    {
        self.commands = commands.into_iter().collect();
        self
    }

    pub fn with_events<I>(mut self, events: I) -> Self
    where
        I: IntoIterator<Item = ApplicationProtocolEventKind>,
    {
        self.events = events.into_iter().collect();
        self
    }

    pub fn negotiate(
        &self,
        peer: &ApplicationProtocolCapabilities,
    ) -> Result<ApplicationProtocolCapabilities, ApplicationProtocolError> {
        if self.protocol_version.trim().is_empty() || peer.protocol_version.trim().is_empty() {
            return Err(ApplicationProtocolError::EmptyMetadataField {
                field: "protocol_version",
            });
        }
        if self.protocol_version != peer.protocol_version {
            return Err(ApplicationProtocolError::ProtocolVersionMismatch {
                left: self.protocol_version.clone(),
                right: peer.protocol_version.clone(),
            });
        }
        Ok(ApplicationProtocolCapabilities {
            protocol_version: self.protocol_version.clone(),
            commands: self
                .commands
                .intersection(&peer.commands)
                .copied()
                .collect(),
            events: self.events.intersection(&peer.events).copied().collect(),
        })
    }
}
