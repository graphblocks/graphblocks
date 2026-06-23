use crate::tool::{ResolvedTool, ToolApproval, ToolIdempotency};
use crate::tool_approval::ToolApprovalRecord;
use crate::tool_call::{ToolCall, ToolCallStatus};

#[derive(Clone, Debug, PartialEq)]
pub struct ToolAdmissionRequest<'a> {
    pub call: ToolCall,
    pub resolved_tool: &'a ResolvedTool,
    pub approval: Option<&'a ToolApprovalRecord>,
    pub principal_id: &'a str,
    pub idempotency_key: Option<String>,
    pub admitted_at_unix_ms: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct AdmittedToolCall {
    pub call: ToolCall,
    pub idempotency_key: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolAdmissionError {
    ToolCallNotValidated {
        tool_call_id: String,
        status: ToolCallStatus,
    },
    ResolvedToolMismatch {
        expected: String,
        actual: String,
    },
    ToolNameMismatch {
        expected: String,
        actual: String,
    },
    ApprovalRequired {
        tool_call_id: String,
    },
    ApprovalInvalid {
        approval_id: String,
        tool_call_id: String,
    },
    IdempotencyKeyRequired {
        tool_call_id: String,
    },
}

pub struct ToolAdmission;

impl ToolAdmission {
    pub fn admit(
        request: ToolAdmissionRequest<'_>,
    ) -> Result<AdmittedToolCall, ToolAdmissionError> {
        if request.call.status != ToolCallStatus::Validated {
            return Err(ToolAdmissionError::ToolCallNotValidated {
                tool_call_id: request.call.tool_call_id,
                status: request.call.status,
            });
        }
        if request.call.resolved_tool_id != request.resolved_tool.resolved_tool_id {
            return Err(ToolAdmissionError::ResolvedToolMismatch {
                expected: request.resolved_tool.resolved_tool_id.clone(),
                actual: request.call.resolved_tool_id,
            });
        }
        if request.call.name != request.resolved_tool.definition.name {
            return Err(ToolAdmissionError::ToolNameMismatch {
                expected: request.resolved_tool.definition.name.clone(),
                actual: request.call.name,
            });
        }
        if request.resolved_tool.binding.approval == ToolApproval::Always {
            let Some(approval) = request.approval else {
                return Err(ToolAdmissionError::ApprovalRequired {
                    tool_call_id: request.call.tool_call_id,
                });
            };
            if !approval.is_valid_for(
                request.resolved_tool,
                &request.call,
                request.principal_id,
                request.admitted_at_unix_ms,
            ) {
                return Err(ToolAdmissionError::ApprovalInvalid {
                    approval_id: approval.approval_id.clone(),
                    tool_call_id: request.call.tool_call_id,
                });
            }
        } else if let Some(approval) = request.approval
            && !approval.is_valid_for(
                request.resolved_tool,
                &request.call,
                request.principal_id,
                request.admitted_at_unix_ms,
            )
        {
            return Err(ToolAdmissionError::ApprovalInvalid {
                approval_id: approval.approval_id.clone(),
                tool_call_id: request.call.tool_call_id,
            });
        }
        if request.resolved_tool.binding.idempotency == ToolIdempotency::Required
            && request.idempotency_key.is_none()
        {
            return Err(ToolAdmissionError::IdempotencyKeyRequired {
                tool_call_id: request.call.tool_call_id,
            });
        }

        let mut call = request.call;
        call.status = ToolCallStatus::Admitted;
        call.admitted_at_unix_ms = Some(request.admitted_at_unix_ms);
        Ok(AdmittedToolCall {
            call,
            idempotency_key: request.idempotency_key,
        })
    }
}
