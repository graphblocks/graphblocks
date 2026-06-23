use serde_json::Value;

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
