use crate::tool::ResolvedTool;
use crate::tool_call::ToolCall;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolApprovalError {
    ResolvedToolMismatch {
        expected: String,
        actual: String,
    },
    ToolNameMismatch {
        expected: String,
        actual: String,
    },
    InvalidExpiration {
        requested_at_unix_ms: u64,
        expires_at_unix_ms: u64,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolApprovalRequest {
    pub approval_id: String,
    pub tool_call_id: String,
    pub tool_name: String,
    pub definition_digest: String,
    pub binding_digest: String,
    pub arguments_digest: String,
    pub policy_snapshot_id: String,
    pub principal_id: String,
    pub requested_at_unix_ms: u64,
    pub expires_at_unix_ms: u64,
}

impl ToolApprovalRequest {
    pub fn for_call(
        approval_id: impl Into<String>,
        resolved_tool: &ResolvedTool,
        call: &ToolCall,
        principal_id: impl Into<String>,
        requested_at_unix_ms: u64,
        expires_at_unix_ms: u64,
    ) -> Result<Self, ToolApprovalError> {
        if expires_at_unix_ms <= requested_at_unix_ms {
            return Err(ToolApprovalError::InvalidExpiration {
                requested_at_unix_ms,
                expires_at_unix_ms,
            });
        }
        if call.resolved_tool_id != resolved_tool.resolved_tool_id {
            return Err(ToolApprovalError::ResolvedToolMismatch {
                expected: resolved_tool.resolved_tool_id.clone(),
                actual: call.resolved_tool_id.clone(),
            });
        }
        if call.name != resolved_tool.definition.name {
            return Err(ToolApprovalError::ToolNameMismatch {
                expected: resolved_tool.definition.name.clone(),
                actual: call.name.clone(),
            });
        }

        Ok(Self {
            approval_id: approval_id.into(),
            tool_call_id: call.tool_call_id.clone(),
            tool_name: call.name.clone(),
            definition_digest: resolved_tool.definition_digest.clone(),
            binding_digest: resolved_tool.binding_digest.clone(),
            arguments_digest: call.arguments_digest.clone(),
            policy_snapshot_id: resolved_tool.effective_policy_snapshot_id.clone(),
            principal_id: principal_id.into(),
            requested_at_unix_ms,
            expires_at_unix_ms,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ToolApprovalStatus {
    Requested,
    Approved,
    Denied,
    Invalidated,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolApprovalRecord {
    pub approval_id: String,
    pub request: ToolApprovalRequest,
    pub status: ToolApprovalStatus,
    pub approver_id: Option<String>,
    pub decided_at_unix_ms: Option<u64>,
    pub invalidated_at_unix_ms: Option<u64>,
    pub reason: Option<String>,
}

impl ToolApprovalRecord {
    pub fn requested(request: ToolApprovalRequest) -> Self {
        Self {
            approval_id: request.approval_id.clone(),
            request,
            status: ToolApprovalStatus::Requested,
            approver_id: None,
            decided_at_unix_ms: None,
            invalidated_at_unix_ms: None,
            reason: None,
        }
    }

    pub fn approve(
        request: ToolApprovalRequest,
        approver_id: impl Into<String>,
        decided_at_unix_ms: u64,
    ) -> Self {
        Self {
            approval_id: request.approval_id.clone(),
            request,
            status: ToolApprovalStatus::Approved,
            approver_id: Some(approver_id.into()),
            decided_at_unix_ms: Some(decided_at_unix_ms),
            invalidated_at_unix_ms: None,
            reason: None,
        }
    }

    pub fn deny(
        request: ToolApprovalRequest,
        approver_id: impl Into<String>,
        decided_at_unix_ms: u64,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            approval_id: request.approval_id.clone(),
            request,
            status: ToolApprovalStatus::Denied,
            approver_id: Some(approver_id.into()),
            decided_at_unix_ms: Some(decided_at_unix_ms),
            invalidated_at_unix_ms: None,
            reason: Some(reason.into()),
        }
    }

    pub fn invalidate(mut self, invalidated_at_unix_ms: u64) -> Self {
        self.status = ToolApprovalStatus::Invalidated;
        self.invalidated_at_unix_ms = Some(invalidated_at_unix_ms);
        self
    }

    pub fn is_valid_for(
        &self,
        resolved_tool: &ResolvedTool,
        call: &ToolCall,
        principal_id: impl AsRef<str>,
        now_unix_ms: u64,
    ) -> bool {
        self.status == ToolApprovalStatus::Approved
            && now_unix_ms <= self.request.expires_at_unix_ms
            && self.request.tool_call_id == call.tool_call_id
            && self.request.tool_name == call.name
            && self.request.definition_digest == resolved_tool.definition_digest
            && self.request.binding_digest == resolved_tool.binding_digest
            && self.request.arguments_digest == call.arguments_digest
            && self.request.policy_snapshot_id == resolved_tool.effective_policy_snapshot_id
            && self.request.principal_id == principal_id.as_ref()
    }
}
