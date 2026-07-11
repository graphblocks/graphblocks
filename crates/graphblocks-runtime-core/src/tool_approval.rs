use crate::tool::ResolvedTool;
use crate::tool_call::{ToolCall, ToolCallError};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolApprovalError {
    EmptyField {
        field: &'static str,
    },
    MissingField {
        field: &'static str,
    },
    ApprovalIdMismatch {
        expected: String,
        actual: String,
    },
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
    InvalidDecisionTime {
        requested_at_unix_ms: u64,
        decided_at_unix_ms: u64,
        expires_at_unix_ms: u64,
    },
    InvalidInvalidationTime {
        requested_at_unix_ms: u64,
        invalidated_at_unix_ms: u64,
    },
    InvalidRevision {
        revision: u32,
    },
    ArgumentsNotObject {
        tool_call_id: String,
    },
    InvalidToolCall {
        source: ToolCallError,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolApprovalRequest {
    pub approval_id: String,
    pub tool_call_id: String,
    pub tool_name: String,
    pub revision: u32,
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
        let approval_id = approval_id.into();
        if approval_id.trim().is_empty() {
            return Err(ToolApprovalError::EmptyField {
                field: "approval_id",
            });
        }
        let principal_id = principal_id.into();
        if principal_id.trim().is_empty() {
            return Err(ToolApprovalError::EmptyField {
                field: "principal_id",
            });
        }
        if expires_at_unix_ms <= requested_at_unix_ms {
            return Err(ToolApprovalError::InvalidExpiration {
                requested_at_unix_ms,
                expires_at_unix_ms,
            });
        }
        call.validate()
            .map_err(|source| ToolApprovalError::InvalidToolCall { source })?;
        if !call.arguments.is_object() {
            return Err(ToolApprovalError::ArgumentsNotObject {
                tool_call_id: call.tool_call_id.clone(),
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

        let request = Self {
            approval_id,
            tool_call_id: call.tool_call_id.clone(),
            tool_name: call.name.clone(),
            revision: call.revision,
            definition_digest: resolved_tool.definition_digest.clone(),
            binding_digest: resolved_tool.binding_digest.clone(),
            arguments_digest: call.arguments_digest.clone(),
            policy_snapshot_id: resolved_tool.effective_policy_snapshot_id.clone(),
            principal_id,
            requested_at_unix_ms,
            expires_at_unix_ms,
        };
        request.validate()?;
        Ok(request)
    }

    pub fn validate(&self) -> Result<(), ToolApprovalError> {
        for (field, value) in [
            ("approval_id", self.approval_id.as_str()),
            ("tool_call_id", self.tool_call_id.as_str()),
            ("tool_name", self.tool_name.as_str()),
            ("definition_digest", self.definition_digest.as_str()),
            ("binding_digest", self.binding_digest.as_str()),
            ("arguments_digest", self.arguments_digest.as_str()),
            ("policy_snapshot_id", self.policy_snapshot_id.as_str()),
            ("principal_id", self.principal_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(ToolApprovalError::EmptyField { field });
            }
        }
        if self.revision == 0 {
            return Err(ToolApprovalError::InvalidRevision {
                revision: self.revision,
            });
        }
        if self.expires_at_unix_ms <= self.requested_at_unix_ms {
            return Err(ToolApprovalError::InvalidExpiration {
                requested_at_unix_ms: self.requested_at_unix_ms,
                expires_at_unix_ms: self.expires_at_unix_ms,
            });
        }
        Ok(())
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

    pub fn validate(&self) -> Result<(), ToolApprovalError> {
        self.request.validate()?;
        if self.approval_id != self.request.approval_id {
            return Err(ToolApprovalError::ApprovalIdMismatch {
                expected: self.request.approval_id.clone(),
                actual: self.approval_id.clone(),
            });
        }
        if matches!(
            self.status,
            ToolApprovalStatus::Approved | ToolApprovalStatus::Denied
        ) {
            if self
                .approver_id
                .as_deref()
                .is_none_or(|approver_id| approver_id.trim().is_empty())
            {
                return Err(ToolApprovalError::EmptyField {
                    field: "approver_id",
                });
            }
            if self.decided_at_unix_ms.is_none() {
                return Err(ToolApprovalError::MissingField {
                    field: "decided_at_unix_ms",
                });
            }
            if let Some(decided_at_unix_ms) = self.decided_at_unix_ms
                && (decided_at_unix_ms < self.request.requested_at_unix_ms
                    || decided_at_unix_ms >= self.request.expires_at_unix_ms)
            {
                return Err(ToolApprovalError::InvalidDecisionTime {
                    requested_at_unix_ms: self.request.requested_at_unix_ms,
                    decided_at_unix_ms,
                    expires_at_unix_ms: self.request.expires_at_unix_ms,
                });
            }
        }
        if self.status == ToolApprovalStatus::Denied {
            match self.reason.as_deref() {
                None => return Err(ToolApprovalError::MissingField { field: "reason" }),
                Some(reason) if reason.trim().is_empty() => {
                    return Err(ToolApprovalError::EmptyField { field: "reason" });
                }
                Some(_) => {}
            }
        }
        if self.status == ToolApprovalStatus::Invalidated {
            let Some(invalidated_at_unix_ms) = self.invalidated_at_unix_ms else {
                return Err(ToolApprovalError::MissingField {
                    field: "invalidated_at_unix_ms",
                });
            };
            if invalidated_at_unix_ms < self.request.requested_at_unix_ms {
                return Err(ToolApprovalError::InvalidInvalidationTime {
                    requested_at_unix_ms: self.request.requested_at_unix_ms,
                    invalidated_at_unix_ms,
                });
            }
        }
        Ok(())
    }

    pub fn is_valid_for(
        &self,
        resolved_tool: &ResolvedTool,
        call: &ToolCall,
        principal_id: impl AsRef<str>,
        now_unix_ms: u64,
    ) -> bool {
        self.status == ToolApprovalStatus::Approved
            && self.validate().is_ok()
            && self.approval_id == self.request.approval_id
            && self.invalidated_at_unix_ms.is_none()
            && self
                .decided_at_unix_ms
                .is_some_and(|decided_at_unix_ms| now_unix_ms >= decided_at_unix_ms)
            && now_unix_ms < self.request.expires_at_unix_ms
            && self.request.tool_call_id == call.tool_call_id
            && self.request.tool_name == call.name
            && self.request.revision == call.revision
            && self.request.definition_digest == resolved_tool.definition_digest
            && self.request.binding_digest == resolved_tool.binding_digest
            && self.request.arguments_digest == call.arguments_digest
            && self.request.policy_snapshot_id == resolved_tool.effective_policy_snapshot_id
            && self.request.principal_id == principal_id.as_ref()
    }
}
