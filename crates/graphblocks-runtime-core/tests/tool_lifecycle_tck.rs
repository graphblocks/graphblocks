use graphblocks_runtime_core::policy::{PolicyDecision, PolicyEffect};
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ResolvedTool, ToolApproval, ToolBinding, ToolCatalog, ToolDefinition,
    ToolEffect, ToolIdempotency, ToolImplementation, ToolResolutionScope,
};
use graphblocks_runtime_core::tool_admission::{
    ToolAdmission, ToolAdmissionError, ToolAdmissionRequest,
};
use graphblocks_runtime_core::tool_approval::{ToolApprovalRecord, ToolApprovalRequest};
use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft, ToolCallDraftStatus};
use graphblocks_runtime_core::tool_schema::{JsonSchema, JsonSchemaNode, ToolSchemaRegistry};
use serde_json::{Map, Value};

#[test]
fn rust_tool_lifecycle_matches_shared_tck_cases() -> Result<(), String> {
    let cases =
        serde_json::from_str::<Value>(include_str!("../../../tck/tool-lifecycle/cases.json"))
            .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "tool-lifecycle TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let case_name = required_str(case, "name")?;
    match required_str(case, "kind")? {
        "incremental_arguments" => run_incremental_arguments_case(case_name, case),
        "admission_invalid_arguments" => run_invalid_admission_case(case_name, case),
        "admission_policy_stopped_response" => run_policy_stopped_admission_case(case_name, case),
        "admission_expired_policy_decision" => run_expired_policy_decision_case(case_name, case),
        "admission_expired_resolved_tool" => run_expired_resolved_tool_case(case_name, case),
        "admission_policy_input_digest_mismatch" => {
            run_policy_input_digest_mismatch_case(case_name, case)
        }
        "admission_policy_input_digest_missing" => {
            run_policy_input_digest_missing_case(case_name, case)
        }
        "admission_policy_denied" => run_policy_denied_case(case_name, case),
        "admission_policy_deferred" => run_policy_deferred_case(case_name, case),
        "admission_missing_required_idempotency_key" => {
            run_missing_required_idempotency_key_case(case_name, case)
        }
        "admission_blank_idempotency_key" => run_blank_idempotency_key_case(case_name, case),
        "approval_argument_mutation" => run_approval_mutation_case(case_name, case),
        other => Err(format!(
            "tool-lifecycle TCK case {case_name} has unknown kind {other}"
        )),
    }
}

fn run_incremental_arguments_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let mut draft = ToolCallDraft::proposed(
        required_str(case, "responseId")?,
        required_str(case, "toolCallId")?,
        required_str(case, "toolName")?,
    );
    let mut statuses = vec![draft_status_str(draft.status).to_owned()];
    for fragment in case
        .get("fragments")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing fragments"))?
    {
        draft
            .append_argument_fragment(fragment.as_str().ok_or_else(|| {
                format!("tool-lifecycle TCK case {case_name} has non-string fragment")
            })?)
            .map_err(|error| format!("tool-lifecycle TCK case {case_name} failed: {error:?}"))?;
        statuses.push(draft_status_str(draft.status).to_owned());
    }

    let finalized_before_complete = draft
        .clone()
        .into_tool_call(
            required_str(case, "resolvedToolId")?,
            required_u64(case, "createdAtUnixMs")?,
        )
        .is_ok();
    draft
        .complete_arguments()
        .map_err(|error| format!("tool-lifecycle TCK case {case_name} failed: {error:?}"))?;
    statuses.push(draft_status_str(draft.status).to_owned());
    let call = draft
        .into_tool_call(
            required_str(case, "resolvedToolId")?,
            required_u64(case, "createdAtUnixMs")?,
        )
        .map_err(|error| format!("tool-lifecycle TCK case {case_name} failed: {error:?}"))?;
    let finalized_after_complete = true;

    assert_eq!(statuses, string_array(expected, "statuses")?, "{case_name}",);
    assert_eq!(
        finalized_before_complete,
        required_bool(expected, "finalizedBeforeComplete")?,
        "{case_name}",
    );
    assert_eq!(
        finalized_after_complete,
        required_bool(expected, "finalizedAfterComplete")?,
        "{case_name}",
    );
    assert_eq!(
        tool_call_status_str(call.status),
        required_map_str(expected, "callStatus")?,
        "{case_name}",
    );
    assert_eq!(
        call.arguments,
        expected.get("arguments").cloned().ok_or_else(|| format!(
            "tool-lifecycle TCK case {case_name} missing expected arguments"
        ))?,
        "{case_name}",
    );
    Ok(())
}

fn run_invalid_admission_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let policy_decision = allow_tool_policy_decision();
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: None,
        approval: None,
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(
            result,
            Err(ToolAdmissionError::ArgumentsSchemaInvalid { .. })
                | Err(ToolAdmissionError::RequiredArgumentMissing { .. })
        ),
        required_bool(expected, "schemaRejectedBeforeApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_policy_stopped_admission_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let output_policy_state = case
        .get("outputPolicyState")
        .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing outputPolicyState"))?;
    let policy_decision = allow_tool_policy_decision();
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: Some(output_policy_state),
        approval: None,
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(
            result,
            Err(ToolAdmissionError::ResponsePolicyStopped { .. })
        ),
        required_bool(expected, "policyStoppedBeforeApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_expired_policy_decision_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.valid_until = Some(required_str(case, "policyValidUntil")?.to_owned());
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: None,
        approval: None,
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: required_u64(case, "admittedAtUnixMs")?,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(
            result,
            Err(ToolAdmissionError::PolicyDecisionExpired { .. })
        ),
        required_bool(expected, "policyExpiredBeforeApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_expired_resolved_tool_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool_with_valid_until(
        tool_name,
        schema_id,
        Some(required_u64(case, "resolvedToolValidUntilUnixMs")?),
    )?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let policy_decision = allow_tool_policy_decision();
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: None,
        approval: None,
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: required_u64(case, "admittedAtUnixMs")?,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(result, Err(ToolAdmissionError::ResolvedToolExpired { .. })),
        required_bool(expected, "resolvedToolExpiredBeforeApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_policy_input_digest_mismatch_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.input_digest = required_str(case, "actualPolicyInputDigest")?.to_owned();
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: required_str(case, "expectedPolicyInputDigest")?,
        output_policy_state: None,
        approval: None,
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(
            result,
            Err(ToolAdmissionError::PolicyInputDigestMismatch { .. })
        ),
        required_bool(expected, "policyDigestRejectedBeforeApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_policy_input_digest_missing_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.input_digest = required_str(case, "actualPolicyInputDigest")?.to_owned();
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: "sha256:before-tool",
        output_policy_state: None,
        approval: None,
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(
            result,
            Err(ToolAdmissionError::PolicyDecisionMissingInputDigest { .. })
        ),
        required_bool(expected, "policyDigestMissingBeforeApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_policy_denied_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.decision_id = required_str(case, "decisionId")?.to_owned();
    policy_decision.effect = PolicyEffect::Deny;
    policy_decision.reason_codes = case
        .get("reasonCodes")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing reasonCodes"))?
        .iter()
        .map(|item| {
            item.as_str().map(str::to_owned).ok_or_else(|| {
                format!("tool-lifecycle TCK case {case_name} has non-string reasonCode")
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: None,
        approval: None,
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(result, Err(ToolAdmissionError::PolicyDenied { .. })),
        required_bool(expected, "policyDeniedBeforeApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_policy_deferred_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.decision_id = required_str(case, "decisionId")?.to_owned();
    policy_decision.effect = PolicyEffect::Defer;
    policy_decision.reason_codes = case
        .get("reasonCodes")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing reasonCodes"))?
        .iter()
        .map(|item| {
            item.as_str().map(str::to_owned).ok_or_else(|| {
                format!("tool-lifecycle TCK case {case_name} has non-string reasonCode")
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: None,
        approval: None,
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(result, Err(ToolAdmissionError::PolicyDeferred { .. })),
        required_bool(expected, "policyDeferredBeforeApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_missing_required_idempotency_key_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let approval_request = ToolApprovalRequest::for_call(
        required_str(case, "approvalId")?,
        &resolved_tool,
        &call,
        "user-1",
        1_000,
        2_000,
    )
    .map_err(|error| format!("tool-lifecycle TCK case {case_name} failed: {error:?}"))?;
    let approval = ToolApprovalRecord::approve(approval_request, "admin-1", 1_100);
    let policy_decision = allow_tool_policy_decision();
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: None,
        approval: Some(&approval),
        principal_id: "user-1",
        idempotency_key: None,
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(
            result,
            Err(ToolAdmissionError::IdempotencyKeyRequired { .. })
        ),
        required_bool(expected, "idempotencyRejectedAfterApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_blank_idempotency_key_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("arguments")
            .cloned()
            .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing arguments"))?,
    )?;
    let approval_request = ToolApprovalRequest::for_call(
        required_str(case, "approvalId")?,
        &resolved_tool,
        &call,
        "user-1",
        1_000,
        2_000,
    )
    .map_err(|error| format!("tool-lifecycle TCK case {case_name} failed: {error:?}"))?;
    let approval = ToolApprovalRecord::approve(approval_request, "admin-1", 1_100);
    let policy_decision = allow_tool_policy_decision();
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: None,
        approval: Some(&approval),
        principal_id: "user-1",
        idempotency_key: Some(required_str(case, "idempotencyKey")?.to_owned()),
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admitted")?,
        "{case_name}",
    );
    assert_eq!(
        matches!(result, Err(ToolAdmissionError::EmptyIdempotencyKey { .. })),
        required_bool(expected, "blankIdempotencyRejectedAfterApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn run_approval_mutation_case(case_name: &str, case: &Value) -> Result<(), String> {
    let expected = expected(case, case_name)?;
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("initialArguments").cloned().ok_or_else(|| {
            format!("tool-lifecycle TCK case {case_name} missing initialArguments")
        })?,
    )?;
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved_tool, &call, "user-1", 1_000, 2_000)
            .map_err(|error| format!("tool-lifecycle TCK case {case_name} failed: {error:?}"))?;
    let approval = ToolApprovalRecord::approve(request, "admin-1", 1_100);
    let revised = call
        .revise_arguments(case.get("mutatedArguments").cloned().ok_or_else(|| {
            format!("tool-lifecycle TCK case {case_name} missing mutatedArguments")
        })?)
        .map_err(|error| format!("tool-lifecycle TCK case {case_name} failed: {error:?}"))?;
    let initial_valid = approval.is_valid_for(&resolved_tool, &call, "user-1", 1_500);
    let revised_valid = approval.is_valid_for(&resolved_tool, &revised, "user-1", 1_500);
    let policy_decision = allow_tool_policy_decision();
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call: revised.clone(),
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        output_policy_state: None,
        approval: Some(&approval),
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    });
    let error_text = result
        .as_ref()
        .err()
        .map(admission_error_text)
        .unwrap_or_default();

    assert_eq!(
        initial_valid,
        required_bool(expected, "initialApprovalValid")?,
        "{case_name}",
    );
    assert_eq!(
        revised_valid,
        required_bool(expected, "mutatedApprovalValid")?,
        "{case_name}",
    );
    assert_eq!(
        revised.arguments_digest != call.arguments_digest,
        required_bool(expected, "digestChanged")?,
        "{case_name}",
    );
    assert_eq!(
        u64::from(revised.revision),
        required_map_u64(expected, "revisedRevision")?,
        "{case_name}",
    );
    assert_eq!(
        result.is_ok(),
        required_bool(expected, "admissionWithStaleApproval")?,
        "{case_name}",
    );
    assert!(
        error_text.contains(required_map_str(expected, "errorContains")?),
        "{case_name}: expected {error_text:?} to contain configured text",
    );
    Ok(())
}

fn resolved_process_tool(tool_name: &str, schema_id: &str) -> Result<ResolvedTool, String> {
    resolved_process_tool_with_valid_until(tool_name, schema_id, None)
}

fn resolved_process_tool_with_valid_until(
    tool_name: &str,
    schema_id: &str,
    valid_until_unix_ms: Option<u64>,
) -> Result<ResolvedTool, String> {
    let catalog = ToolCatalog::new(
        [ToolDefinition::new(
            tool_name,
            "Run an approved process.",
            schema_id,
        )],
        [ToolBinding::new(
            "binding-process",
            tool_name,
            ToolImplementation::Block(BlockToolImplementation::new("blocks.process")),
        )
        .with_effects([ToolEffect::Process])
        .with_approval(ToolApproval::Always)
        .with_idempotency(ToolIdempotency::Required)],
    )
    .map_err(|error| format!("tool catalog failed: {error:?}"))?;
    let mut resolved = catalog
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")
        .map_err(|error| format!("tool resolution failed: {error:?}"))?;
    let resolved = resolved.remove(0);
    ResolvedTool::from_definition_and_binding(
        resolved.resolved_tool_id,
        resolved.definition,
        resolved.binding,
        resolved.effective_policy_snapshot_id,
        resolved.allowed_for_principal,
        valid_until_unix_ms,
    )
    .map_err(|error| format!("tool resolution failed: {error:?}"))
}

fn process_schema_registry(schema_id: &str) -> Result<ToolSchemaRegistry, String> {
    ToolSchemaRegistry::new([JsonSchema::new(
        schema_id,
        JsonSchemaNode::object()
            .required_property("cmd", JsonSchemaNode::array(JsonSchemaNode::string())),
    )])
    .map_err(|error| format!("schema registry failed: {error:?}"))
}

fn tool_call_from_arguments(
    tool_name: &str,
    resolved_tool_id: &str,
    arguments: Value,
) -> Result<ToolCall, String> {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", tool_name);
    draft
        .append_argument_fragment(arguments.to_string())
        .map_err(|error| format!("tool call draft failed: {error:?}"))?;
    draft
        .into_completed_tool_call(resolved_tool_id, 1_000)
        .map_err(|error| format!("tool call completion failed: {error:?}"))
}

fn allow_tool_policy_decision() -> PolicyDecision {
    PolicyDecision {
        decision_id: "decision-allow-tool".to_owned(),
        effect: PolicyEffect::Allow,
        reason_codes: vec!["allow-process".to_owned()],
        policy_refs: vec!["allow-process".to_owned()],
        obligations: Vec::new(),
        advice: Vec::new(),
        evaluated_at: "2026-06-23T00:00:01Z".to_owned(),
        valid_until: None,
        input_digest: "sha256:before-tool".to_owned(),
    }
}

fn admission_error_text(error: &ToolAdmissionError) -> &'static str {
    match error {
        ToolAdmissionError::ArgumentsSchemaInvalid { .. }
        | ToolAdmissionError::RequiredArgumentMissing { .. } => "arguments invalid",
        ToolAdmissionError::ApprovalInvalid { .. } => "not valid",
        ToolAdmissionError::ApprovalRequired { .. } => "requires approval",
        ToolAdmissionError::EmptyIdempotencyKey { .. } => "idempotency",
        ToolAdmissionError::IdempotencyKeyRequired { .. } => "idempotency",
        ToolAdmissionError::PolicyDecisionExpired { .. } => "expired",
        ToolAdmissionError::PolicyDecisionMissingInputDigest { .. } => "input digest",
        ToolAdmissionError::PolicyInputDigestMismatch { .. } => "input digest",
        ToolAdmissionError::PolicyDenied { .. } => "denied",
        ToolAdmissionError::PolicyDeferred { .. } => "deferred",
        ToolAdmissionError::ResolvedToolExpired { .. } => "expired",
        ToolAdmissionError::ResponsePolicyStopped { .. } => "policy stopped",
        _ => "admission failed",
    }
}

fn expected<'a>(case: &'a Value, case_name: &str) -> Result<&'a Map<String, Value>, String> {
    case.get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} missing expected"))
}

fn draft_status_str(status: ToolCallDraftStatus) -> &'static str {
    match status {
        ToolCallDraftStatus::Proposed => "proposed",
        ToolCallDraftStatus::ArgumentsStreaming => "arguments_streaming",
        ToolCallDraftStatus::ArgumentsComplete => "arguments_complete",
    }
}

fn tool_call_status_str(
    status: graphblocks_runtime_core::tool_call::ToolCallStatus,
) -> &'static str {
    match status {
        graphblocks_runtime_core::tool_call::ToolCallStatus::Validated => "validated",
        graphblocks_runtime_core::tool_call::ToolCallStatus::PolicyPending => "policy_pending",
        graphblocks_runtime_core::tool_call::ToolCallStatus::ApprovalPending => "approval_pending",
        graphblocks_runtime_core::tool_call::ToolCallStatus::Admitted => "admitted",
        graphblocks_runtime_core::tool_call::ToolCallStatus::Running => "running",
        graphblocks_runtime_core::tool_call::ToolCallStatus::Completed => "completed",
        graphblocks_runtime_core::tool_call::ToolCallStatus::Failed => "failed",
        graphblocks_runtime_core::tool_call::ToolCallStatus::Denied => "denied",
        graphblocks_runtime_core::tool_call::ToolCallStatus::Cancelled => "cancelled",
        graphblocks_runtime_core::tool_call::ToolCallStatus::PolicyStopped => "policy_stopped",
        graphblocks_runtime_core::tool_call::ToolCallStatus::Expired => "expired",
    }
}

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing required string field {key}"))
}

fn required_bool(value: &Map<String, Value>, key: &str) -> Result<bool, String> {
    value
        .get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| format!("missing required bool field {key}"))
}

fn required_map_str<'a>(value: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing required string field {key}"))
}

fn required_map_u64(value: &Map<String, Value>, key: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("missing required u64 field {key}"))
}

fn required_u64(value: &Value, key: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("missing required u64 field {key}"))
}

fn string_array(value: &Map<String, Value>, key: &str) -> Result<Vec<String>, String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("missing string array {key}"))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| format!("string array {key} contains non-string value"))
        })
        .collect()
}
