use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use crate::output_policy::{
    DraftDisposition, DurableResult, GenerationChunk, OutputCutoff, OutputDisposition,
    OutputPolicyDecision, PendingToolCallsDisposition, ProviderCancellation, TerminalReason,
};
use crate::policy::PolicyDecision;
use crate::tool_approval::ToolApprovalRequest;
use crate::tool_call::{ToolCall, ToolCallDraft, ToolCallDraftStatus, ToolCallStatus};
use crate::tool_result::{
    ContentPartKind, ToolEffectOutcome, ToolResult, ToolResultEvent, ToolResultStatus,
};
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ApplicationEventKind {
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
    OutputPolicyEvaluationStarted,
    OutputPolicyAllowed,
    OutputPolicyHeld,
    OutputPolicyRedacted,
    OutputPolicyReplaced,
    OutputPolicyViolationDetected,
    OutputCutoff,
    AssistantIncomplete,
    AssistantRetracted,
}

impl ApplicationEventKind {
    pub fn as_str(&self) -> &'static str {
        match self {
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
            Self::OutputPolicyEvaluationStarted => "OutputPolicyEvaluationStarted",
            Self::OutputPolicyAllowed => "OutputPolicyAllowed",
            Self::OutputPolicyHeld => "OutputPolicyHeld",
            Self::OutputPolicyRedacted => "OutputPolicyRedacted",
            Self::OutputPolicyReplaced => "OutputPolicyReplaced",
            Self::OutputPolicyViolationDetected => "OutputPolicyViolationDetected",
            Self::OutputCutoff => "OutputCutoff",
            Self::AssistantIncomplete => "AssistantIncomplete",
            Self::AssistantRetracted => "AssistantRetracted",
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
        )
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ApplicationEventMetadata {
    pub event_id: String,
    pub run_id: String,
    pub response_id: String,
    pub turn_id: Option<String>,
    pub sequence: u64,
    pub release_id: String,
    pub policy_snapshot_id: String,
    pub occurred_at_unix_ms: u64,
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
    EmptyToolCallId,
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
        let kind = match call.status {
            ToolCallStatus::Validated => ApplicationEventKind::ToolCallValidated,
            ToolCallStatus::Admitted => ApplicationEventKind::ToolCallAdmitted,
            ToolCallStatus::PolicyPending
            | ToolCallStatus::ApprovalPending
            | ToolCallStatus::Running
            | ToolCallStatus::Completed
            | ToolCallStatus::Failed
            | ToolCallStatus::Denied
            | ToolCallStatus::Cancelled
            | ToolCallStatus::PolicyStopped
            | ToolCallStatus::Expired => return Ok(None),
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
        match event {
            ToolResultEvent::Started {
                tool_call_id,
                sequence,
                started_at_unix_ms,
            } => Self::tool(
                ApplicationEventKind::ToolCallStarted,
                metadata,
                tool_call_id,
                json!({
                    "status": "running",
                    "tool_result_sequence": sequence,
                    "started_at_unix_ms": started_at_unix_ms,
                }),
            )
            .map(Some),
            ToolResultEvent::Completed {
                tool_call_id,
                sequence,
                result,
            } => Self::tool(
                ApplicationEventKind::ToolCallCompleted,
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
                ApplicationEventKind::ToolCallFailed,
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
                ApplicationEventKind::ToolCallDenied,
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
                ApplicationEventKind::ToolCallCancelled,
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
                ApplicationEventKind::ToolCallPolicyStopped,
                metadata,
                tool_call_id,
                Self::tool_result_payload(*sequence, result),
            )
            .map(Some),
            ToolResultEvent::Delta { .. }
            | ToolResultEvent::ArtifactReady { .. }
            | ToolResultEvent::Incomplete { .. } => Ok(None),
        }
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
        Self::new(
            ApplicationEventKind::OutputPolicyEvaluationStarted,
            metadata,
            json!({
                "stream_id": &chunk.stream_id,
                "response_id": &chunk.response_id,
                "chunk_sequence": chunk.sequence,
                "input_digest": input_digest.as_ref(),
                "chunk_text_bytes": chunk.text.len(),
            }),
        )
    }

    pub fn output_cutoff(
        metadata: ApplicationEventMetadata,
        cutoff: &OutputCutoff,
    ) -> Result<Vec<Self>, ApplicationEventError> {
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

    pub fn new(
        kind: ApplicationEventKind,
        metadata: ApplicationEventMetadata,
        payload: Value,
    ) -> Result<Self, ApplicationEventError> {
        if kind.is_tool_event() {
            return Err(ApplicationEventError::ToolEventRequiresToolCallId { kind });
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

        let tool_call_id = tool_call_id.into();
        if tool_call_id.trim().is_empty() {
            return Err(ApplicationEventError::EmptyToolCallId);
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
}

impl ApplicationEventStreamState {
    pub fn accept(&mut self, event: ApplicationEvent) -> Option<ApplicationEvent> {
        if event.kind == ApplicationEventKind::OutputCutoff {
            let cutoff = {
                let payload = &event.payload;
                let response_id = payload
                    .get("response_id")
                    .and_then(Value::as_str)
                    .unwrap_or(&event.metadata.response_id)
                    .to_owned();
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
                OutputCutoff {
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
                }
            };
            self.cutoffs.insert(cutoff.response_id.clone(), cutoff);
            self.accepted_events.push(event.clone());
            return Some(event);
        }

        let response_id = event
            .payload
            .get("response_id")
            .and_then(Value::as_str)
            .unwrap_or(&event.metadata.response_id);
        if self.cutoffs.contains_key(response_id) {
            if matches!(
                event.kind,
                ApplicationEventKind::AssistantRetracted
                    | ApplicationEventKind::AssistantIncomplete
            ) {
                self.accepted_events.push(event.clone());
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
            if matches!(
                event.kind,
                ApplicationEventKind::ToolCallProposed
                    | ApplicationEventKind::ToolCallArgumentsDelta
                    | ApplicationEventKind::ToolCallArgumentsCompleted
            ) {
                return None;
            }
        }

        self.accepted_events.push(event.clone());
        Some(event)
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
    CancelRun,
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
}

impl ApplicationCommandKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::InvokeGraph => "InvokeGraph",
            Self::CancelRun => "CancelRun",
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
    AssistantRetracted,
    ToolStarted,
    ToolCompleted,
    ApprovalRequested,
    ReviewRequested,
    BudgetConstrained,
    BudgetExhausted,
    BudgetExtensionRequested,
    BudgetExtensionGranted,
    PolicyDecisionRequired,
    ExecutionDegraded,
    FilePatchPreview,
    JobProgress,
    ArtifactReady,
    StateSnapshot,
    RunCompleted,
    RunFailed,
    RunCancelled,
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
            Self::AssistantRetracted => "AssistantRetracted",
            Self::ToolStarted => "ToolStarted",
            Self::ToolCompleted => "ToolCompleted",
            Self::ApprovalRequested => "ApprovalRequested",
            Self::ReviewRequested => "ReviewRequested",
            Self::BudgetConstrained => "BudgetConstrained",
            Self::BudgetExhausted => "BudgetExhausted",
            Self::BudgetExtensionRequested => "BudgetExtensionRequested",
            Self::BudgetExtensionGranted => "BudgetExtensionGranted",
            Self::PolicyDecisionRequired => "PolicyDecisionRequired",
            Self::ExecutionDegraded => "ExecutionDegraded",
            Self::FilePatchPreview => "FilePatchPreview",
            Self::JobProgress => "JobProgress",
            Self::ArtifactReady => "ArtifactReady",
            Self::StateSnapshot => "StateSnapshot",
            Self::RunCompleted => "RunCompleted",
            Self::RunFailed => "RunFailed",
            Self::RunCancelled => "RunCancelled",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ApplicationProtocolEventMetadata {
    pub event_id: String,
    pub protocol_version: String,
    pub run_id: String,
    pub turn_id: Option<String>,
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
    NonMonotonicSequence { previous: u64, next: u64 },
    ProtocolVersionMismatch { left: String, right: String },
}

impl fmt::Display for ApplicationProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyCommandId => write!(formatter, "application command id must not be empty"),
            Self::EmptyEventId => write!(formatter, "application event id must not be empty"),
            Self::NonMonotonicSequence { previous, next } => write!(
                formatter,
                "application event sequence {next} must be greater than previous sequence {previous}"
            ),
            Self::ProtocolVersionMismatch { left, right } => {
                write!(formatter, "protocol versions differ: {left:?} vs {right:?}")
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

#[derive(Clone, Debug, Default)]
pub struct ApplicationProtocolLog {
    events: Vec<ApplicationProtocolEvent>,
    event_ids: BTreeSet<String>,
    last_sequence: Option<u64>,
}

impl ApplicationProtocolLog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn append(
        &mut self,
        event: ApplicationProtocolEvent,
    ) -> Result<bool, ApplicationProtocolError> {
        if self.event_ids.contains(&event.metadata.event_id) {
            return Ok(false);
        }
        if let Some(previous) = self.last_sequence {
            if event.metadata.sequence <= previous {
                return Err(ApplicationProtocolError::NonMonotonicSequence {
                    previous,
                    next: event.metadata.sequence,
                });
            }
        }
        self.last_sequence = Some(event.metadata.sequence);
        self.event_ids.insert(event.metadata.event_id.clone());
        self.events.push(event);
        Ok(true)
    }

    pub fn replay_after(
        &self,
        cursor: Option<&str>,
        limit: usize,
    ) -> Vec<ApplicationProtocolEvent> {
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

    pub fn len(&self) -> usize {
        self.events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }
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
