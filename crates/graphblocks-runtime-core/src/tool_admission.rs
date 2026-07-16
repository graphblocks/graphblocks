use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::policy::{
    EnforcementPoint, PolicyDecision, PolicyEffect, PolicyRequest, PrincipalRef, ResourceRef,
    parse_policy_datetime_millis,
};
use crate::tool::{
    ResolvedTool, ToolApproval, ToolIdempotency, ToolResolutionError, canonical_effect_names,
};
use crate::tool_approval::ToolApprovalRecord;
use crate::tool_call::{ToolCall, ToolCallError, ToolCallStatus};
use crate::tool_schema::{ToolSchemaRegistry, ToolSchemaValidationError};

#[derive(Clone, Debug, PartialEq)]
pub struct ToolAdmissionRequest<'a> {
    pub call: ToolCall,
    pub resolved_tool: &'a ResolvedTool,
    pub schema_registry: &'a ToolSchemaRegistry,
    pub policy_decision: &'a PolicyDecision,
    pub expected_policy_input_digest: &'a str,
    pub output_policy_state: Option<&'a Value>,
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

#[derive(Clone, Debug, PartialEq)]
pub struct ToolPolicyRequestContext<'a> {
    pub request_id: &'a str,
    pub call: &'a ToolCall,
    pub resolved_tool: &'a ResolvedTool,
    pub principal: PrincipalRef,
    pub occurred_at: &'a str,
    pub run_id: Option<&'a str>,
    pub output_policy_state: Option<Value>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolAdmissionError {
    InvalidToolCall {
        source: ToolCallError,
    },
    InvalidResolvedTool {
        source: ToolResolutionError,
    },
    InvalidOutputPolicyState,
    EmptyPrincipalId,
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
    EmptyIdempotencyKey {
        tool_call_id: String,
    },
    InputSchemaMissing {
        schema_id: String,
    },
    ArgumentsSchemaInvalid {
        tool_call_id: String,
        schema_id: String,
        path: String,
        expected: String,
    },
    RequiredArgumentMissing {
        tool_call_id: String,
        schema_id: String,
        path: String,
        property: String,
    },
    ArgumentsDigestMismatch {
        tool_call_id: String,
    },
    ResolvedToolNotAllowed {
        resolved_tool_id: String,
        principal_id: String,
    },
    ResolvedToolExpired {
        resolved_tool_id: String,
        valid_until_unix_ms: u64,
        admitted_at_unix_ms: u64,
    },
    PolicyDecisionMissingInputDigest {
        decision_id: String,
    },
    PolicyInputDigestMismatch {
        decision_id: String,
        expected: String,
        actual: String,
    },
    PolicyDecisionExpired {
        decision_id: String,
        valid_until: String,
        admitted_at_unix_ms: u64,
    },
    PolicyDenied {
        decision_id: String,
        reason_codes: Vec<String>,
    },
    PolicyDeferred {
        decision_id: String,
        reason_codes: Vec<String>,
    },
    ResponsePolicyStopped {
        response_id: String,
    },
}

pub struct ToolAdmission;

impl ToolAdmission {
    pub fn before_tool_or_effect_policy_request(
        context: ToolPolicyRequestContext<'_>,
    ) -> Result<PolicyRequest, ToolAdmissionError> {
        context
            .call
            .validate()
            .map_err(|source| ToolAdmissionError::InvalidToolCall { source })?;
        context
            .resolved_tool
            .validate()
            .map_err(|source| ToolAdmissionError::InvalidResolvedTool { source })?;
        if context.principal.principal_id.trim().is_empty() {
            return Err(ToolAdmissionError::EmptyPrincipalId);
        }
        if context.call.resolved_tool_id != context.resolved_tool.resolved_tool_id {
            return Err(ToolAdmissionError::ResolvedToolMismatch {
                expected: context.resolved_tool.resolved_tool_id.clone(),
                actual: context.call.resolved_tool_id.clone(),
            });
        }
        if context.call.name != context.resolved_tool.definition.name {
            return Err(ToolAdmissionError::ToolNameMismatch {
                expected: context.resolved_tool.definition.name.clone(),
                actual: context.call.name.clone(),
            });
        }
        if !output_policy_state_is_valid_for_response(
            context.output_policy_state.as_ref(),
            &context.call.response_id,
        ) {
            return Err(ToolAdmissionError::InvalidOutputPolicyState);
        }

        let mut request = PolicyRequest::new(
            context.request_id,
            EnforcementPoint::BeforeToolOrEffect,
            "tool.run",
            ResourceRef::new(format!("tool:{}", context.resolved_tool.definition.name))
                .with_resource_kind("tool"),
            context.occurred_at,
        )
        .with_principal(context.principal)
        .with_policy_snapshot_id(context.resolved_tool.effective_policy_snapshot_id.clone())
        .with_attribute("tool_call_id", json!(&context.call.tool_call_id))
        .with_attribute("response_id", json!(&context.call.response_id))
        .with_attribute(
            "resolved_tool_id",
            json!(&context.resolved_tool.resolved_tool_id),
        )
        .with_attribute("tool_name", json!(&context.resolved_tool.definition.name))
        .with_attribute("arguments", context.call.arguments.clone())
        .with_attribute("arguments_digest", json!(&context.call.arguments_digest))
        .with_attribute(
            "definition_digest",
            json!(&context.resolved_tool.definition_digest),
        )
        .with_attribute(
            "binding_digest",
            json!(&context.resolved_tool.binding_digest),
        )
        .with_attribute(
            "effects",
            json!(canonical_effect_names(
                &context.resolved_tool.binding.effects
            )),
        );

        if let Some(run_id) = context.run_id {
            request = request.with_run_id(run_id);
        }
        if let Some(output_policy_state) = context.output_policy_state {
            request = request.with_attribute("output_policy_state", output_policy_state);
        }
        Ok(request)
    }

    pub fn admit(
        request: ToolAdmissionRequest<'_>,
    ) -> Result<AdmittedToolCall, ToolAdmissionError> {
        if let Err(source) = request.call.validate() {
            return match source {
                ToolCallError::ArgumentsDigestMismatch { tool_call_id } => {
                    Err(ToolAdmissionError::ArgumentsDigestMismatch { tool_call_id })
                }
                source => Err(ToolAdmissionError::InvalidToolCall { source }),
            };
        }
        if request.principal_id.trim().is_empty() {
            return Err(ToolAdmissionError::EmptyPrincipalId);
        }
        if request.call.status != ToolCallStatus::Validated {
            return Err(ToolAdmissionError::ToolCallNotValidated {
                tool_call_id: request.call.tool_call_id,
                status: request.call.status,
            });
        }
        if !output_policy_state_is_valid_for_response(
            request.output_policy_state,
            &request.call.response_id,
        ) {
            return Err(ToolAdmissionError::InvalidOutputPolicyState);
        }
        if output_policy_state_is_stopped(request.output_policy_state) {
            return Err(ToolAdmissionError::ResponsePolicyStopped {
                response_id: request.call.response_id,
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
        if canonical_hash(&request.call.arguments) != request.call.arguments_digest {
            return Err(ToolAdmissionError::ArgumentsDigestMismatch {
                tool_call_id: request.call.tool_call_id,
            });
        }

        if let Err(error) = request.schema_registry.validate(
            &request.resolved_tool.definition.input_schema,
            &request.call.arguments,
        ) {
            return match error {
                ToolSchemaValidationError::SchemaMissing { schema_id } => {
                    Err(ToolAdmissionError::InputSchemaMissing { schema_id })
                }
                ToolSchemaValidationError::TypeMismatch {
                    schema_id,
                    path,
                    expected,
                } => Err(ToolAdmissionError::ArgumentsSchemaInvalid {
                    tool_call_id: request.call.tool_call_id,
                    schema_id,
                    path,
                    expected,
                }),
                ToolSchemaValidationError::RequiredPropertyMissing {
                    schema_id,
                    path,
                    property,
                } => Err(ToolAdmissionError::RequiredArgumentMissing {
                    tool_call_id: request.call.tool_call_id,
                    schema_id,
                    path,
                    property,
                }),
            };
        }

        if !request.resolved_tool.allowed_for_principal {
            return Err(ToolAdmissionError::ResolvedToolNotAllowed {
                resolved_tool_id: request.resolved_tool.resolved_tool_id.clone(),
                principal_id: request.principal_id.to_owned(),
            });
        }

        if let Some(valid_until_unix_ms) = request.resolved_tool.valid_until_unix_ms
            && request.admitted_at_unix_ms >= valid_until_unix_ms
        {
            return Err(ToolAdmissionError::ResolvedToolExpired {
                resolved_tool_id: request.resolved_tool.resolved_tool_id.clone(),
                valid_until_unix_ms,
                admitted_at_unix_ms: request.admitted_at_unix_ms,
            });
        }

        if request.policy_decision.input_digest.trim().is_empty() {
            return Err(ToolAdmissionError::PolicyDecisionMissingInputDigest {
                decision_id: request.policy_decision.decision_id.clone(),
            });
        }
        if request.policy_decision.input_digest != request.expected_policy_input_digest {
            return Err(ToolAdmissionError::PolicyInputDigestMismatch {
                decision_id: request.policy_decision.decision_id.clone(),
                expected: request.expected_policy_input_digest.to_owned(),
                actual: request.policy_decision.input_digest.clone(),
            });
        }
        if let Some(valid_until) = &request.policy_decision.valid_until
            && policy_decision_expired(valid_until, request.admitted_at_unix_ms)
        {
            return Err(ToolAdmissionError::PolicyDecisionExpired {
                decision_id: request.policy_decision.decision_id.clone(),
                valid_until: valid_until.clone(),
                admitted_at_unix_ms: request.admitted_at_unix_ms,
            });
        }
        match request.policy_decision.effect {
            PolicyEffect::Allow | PolicyEffect::AllowWithObligations => {}
            PolicyEffect::Deny => {
                return Err(ToolAdmissionError::PolicyDenied {
                    decision_id: request.policy_decision.decision_id.clone(),
                    reason_codes: request.policy_decision.reason_codes.clone(),
                });
            }
            PolicyEffect::Defer => {
                return Err(ToolAdmissionError::PolicyDeferred {
                    decision_id: request.policy_decision.decision_id.clone(),
                    reason_codes: request.policy_decision.reason_codes.clone(),
                });
            }
        }

        let policy_requires_approval = request.resolved_tool.binding.approval
            == ToolApproval::Policy
            && request
                .policy_decision
                .obligations
                .iter()
                .any(|obligation| obligation.obligation_type == "require_tool_approval");

        if request.resolved_tool.binding.approval == ToolApproval::Always
            || policy_requires_approval
        {
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
        match request.idempotency_key.as_deref() {
            Some(idempotency_key) if idempotency_key.trim().is_empty() => {
                return Err(ToolAdmissionError::EmptyIdempotencyKey {
                    tool_call_id: request.call.tool_call_id,
                });
            }
            None if request.resolved_tool.binding.idempotency == ToolIdempotency::Required => {
                return Err(ToolAdmissionError::IdempotencyKeyRequired {
                    tool_call_id: request.call.tool_call_id,
                });
            }
            _ => {}
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

fn policy_decision_expired(valid_until: &str, admitted_at_unix_ms: u64) -> bool {
    parse_policy_datetime_millis(valid_until)
        .is_none_or(|valid_until| valid_until <= i128::from(admitted_at_unix_ms))
}

fn output_policy_state_is_stopped(output_policy_state: Option<&Value>) -> bool {
    let Some(Value::Object(state)) = output_policy_state else {
        return false;
    };
    ["response_status", "status", "terminal_state"]
        .iter()
        .any(|field| state.get(*field).and_then(Value::as_str) == Some("policy_stopped"))
}

fn output_policy_state_is_valid_for_response(
    output_policy_state: Option<&Value>,
    response_id: &str,
) -> bool {
    let Some(Value::Object(state)) = output_policy_state else {
        return output_policy_state.is_none();
    };
    state
        .get("response_id")
        .and_then(Value::as_str)
        .is_none_or(|state_response_id| state_response_id == response_id)
}
