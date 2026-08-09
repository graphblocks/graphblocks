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
use serde_json::{Map, Value, json};

pub(crate) fn evaluate_case(case: &Value) -> Result<Value, String> {
    let case_object = case
        .as_object()
        .ok_or_else(|| "tool-lifecycle TCK case must be an object".to_owned())?;
    let case_name = required_str(case_object, "name")?;
    let kind = required_str(case_object, "kind")?;
    let expected = case_object
        .get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} requires expected"))?;
    let observed = match kind {
        "incremental_arguments" => evaluate_incremental(case_object, case_name)?,
        "approval_argument_mutation" => evaluate_approval_mutation(case_object, case_name)?,
        kind if kind.starts_with("admission_") => evaluate_admission(case_object, case_name, kind)?,
        _ => {
            return Err(format!(
                "tool-lifecycle TCK case {case_name} has unknown kind {kind}"
            ));
        }
    };

    let mut contract = Map::new();
    for key in expected
        .keys()
        .filter(|key| key.as_str() != "errorContains")
    {
        contract.insert(
            key.clone(),
            observed.get(key).cloned().ok_or_else(|| {
                format!("tool-lifecycle TCK case {case_name} did not observe {key}")
            })?,
        );
    }
    Ok(Value::Object(contract))
}

fn evaluate_incremental(
    case: &Map<String, Value>,
    case_name: &str,
) -> Result<Map<String, Value>, String> {
    let mut draft = ToolCallDraft::proposed(
        required_str(case, "responseId")?,
        required_str(case, "toolCallId")?,
        required_str(case, "toolName")?,
    );
    let mut statuses = vec![json!(draft_status_name(draft.status))];
    let fragments = case
        .get("fragments")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} requires fragments"))?;
    for (index, fragment) in fragments.iter().enumerate() {
        let fragment = fragment.as_str().ok_or_else(|| {
            format!("tool-lifecycle TCK case {case_name} fragment {index} must be a string")
        })?;
        draft.append_argument_fragment(fragment).map_err(|error| {
            format!("tool-lifecycle TCK case {case_name} fragment {index}: {error:?}")
        })?;
        statuses.push(json!(draft_status_name(draft.status)));
    }
    let resolved_tool_id = required_str(case, "resolvedToolId")?;
    let created_at_unix_ms = required_u64(case, "createdAtUnixMs")?;
    let finalized_before_complete = draft
        .clone()
        .into_tool_call(resolved_tool_id, created_at_unix_ms)
        .is_ok();
    draft
        .complete_arguments()
        .map_err(|error| format!("tool-lifecycle TCK case {case_name}: {error:?}"))?;
    statuses.push(json!(draft_status_name(draft.status)));
    let call = draft
        .into_tool_call(resolved_tool_id, created_at_unix_ms)
        .map_err(|error| format!("tool-lifecycle TCK case {case_name}: {error:?}"))?;
    Ok(json!({
        "statuses": statuses,
        "finalizedBeforeComplete": finalized_before_complete,
        "finalizedAfterComplete": true,
        "callStatus": super::tool_call_status_name(call.status),
        "arguments": call.arguments,
    })
    .as_object()
    .cloned()
    .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} result must be an object"))?)
}

fn evaluate_admission(
    case: &Map<String, Value>,
    case_name: &str,
    kind: &str,
) -> Result<Map<String, Value>, String> {
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(
        tool_name,
        schema_id,
        if kind == "admission_expired_resolved_tool" {
            Some(required_u64(case, "resolvedToolValidUntilUnixMs")?)
        } else {
            None
        },
    )?;
    let schemas = if kind == "admission_missing_schema" {
        ToolSchemaRegistry::default()
    } else {
        process_schema_registry(schema_id)?
    };
    let call_tool_name = optional_str(case, "callToolName").unwrap_or(tool_name);
    let call_resolved_tool_id =
        optional_str(case, "callResolvedToolId").unwrap_or(&resolved_tool.resolved_tool_id);
    let arguments = case
        .get("arguments")
        .cloned()
        .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} requires arguments"))?;
    let mut call = tool_call_from_arguments(call_tool_name, call_resolved_tool_id, arguments)?;
    if kind == "admission_arguments_digest_mismatch" {
        call.arguments_digest = required_str(case, "argumentsDigest")?.to_owned();
    }

    let mut policy_decision = allow_tool_policy_decision();
    match kind {
        "admission_expired_policy_decision" => {
            policy_decision.valid_until = Some(required_str(case, "policyValidUntil")?.to_owned());
        }
        "admission_policy_input_digest_mismatch" | "admission_policy_input_digest_missing" => {
            policy_decision.input_digest =
                required_str(case, "actualPolicyInputDigest")?.to_owned();
        }
        "admission_policy_denied" | "admission_policy_deferred" => {
            policy_decision.decision_id = required_str(case, "decisionId")?.to_owned();
            policy_decision.effect = if kind == "admission_policy_denied" {
                PolicyEffect::Deny
            } else {
                PolicyEffect::Defer
            };
            policy_decision.reason_codes = required_string_array(case, "reasonCodes")?;
        }
        _ => {}
    }

    let approval = if matches!(
        kind,
        "admission_missing_required_idempotency_key"
            | "admission_blank_idempotency_key"
            | "admission_expired_approval"
    ) {
        let request = ToolApprovalRequest::for_call(
            optional_str(case, "approvalId").unwrap_or("approval-1"),
            &resolved_tool,
            &call,
            "user-1",
            optional_u64(case, "requestedAtUnixMs").unwrap_or(1_000),
            optional_u64(case, "expiresAtUnixMs").unwrap_or(2_000),
        )
        .map_err(|error| format!("tool-lifecycle TCK case {case_name}: {error:?}"))?;
        Some(ToolApprovalRecord::approve(
            request,
            "admin-1",
            optional_u64(case, "decidedAtUnixMs").unwrap_or(1_100),
        ))
    } else {
        None
    };
    let idempotency_key = match kind {
        "admission_missing_required_idempotency_key" => None,
        "admission_blank_idempotency_key"
        | "admission_missing_approval"
        | "admission_expired_approval" => Some(
            optional_str(case, "idempotencyKey")
                .unwrap_or(" ")
                .to_owned(),
        ),
        _ => Some("idem-1".to_owned()),
    };
    let expected_policy_input_digest =
        optional_str(case, "expectedPolicyInputDigest").unwrap_or(&policy_decision.input_digest);
    let output_policy_state = if kind == "admission_policy_stopped_response" {
        case.get("outputPolicyState")
    } else {
        None
    };
    let result = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        policy_decision: &policy_decision,
        expected_policy_input_digest,
        output_policy_state,
        approval: approval.as_ref(),
        principal_id: "user-1",
        idempotency_key,
        admitted_at_unix_ms: optional_u64(case, "admittedAtUnixMs").unwrap_or(1_200),
    });

    Ok(json!({
        "admitted": result.is_ok(),
        "schemaRejectedBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::ArgumentsSchemaInvalid { .. })
                | Err(ToolAdmissionError::RequiredArgumentMissing { .. })
        ),
        "schemaMissingBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::InputSchemaMissing { .. })
        ),
        "resolvedToolMismatchBeforeSchema": matches!(
            &result,
            Err(ToolAdmissionError::ResolvedToolMismatch { .. })
        ),
        "toolNameMismatchBeforeSchema": matches!(
            &result,
            Err(ToolAdmissionError::ToolNameMismatch { .. })
        ),
        "argumentsDigestRejectedBeforeSchema": matches!(
            &result,
            Err(ToolAdmissionError::ArgumentsDigestMismatch { .. })
        ),
        "policyStoppedBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::ResponsePolicyStopped { .. })
        ),
        "policyExpiredBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::PolicyDecisionExpired { .. })
        ),
        "resolvedToolExpiredBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::ResolvedToolExpired { .. })
        ),
        "policyDigestRejectedBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::PolicyInputDigestMismatch { .. })
        ),
        "policyDigestMissingBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::PolicyDecisionMissingInputDigest { .. })
        ),
        "policyDeniedBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::PolicyDenied { .. })
        ),
        "policyDeferredBeforeApproval": matches!(
            &result,
            Err(ToolAdmissionError::PolicyDeferred { .. })
        ),
        "approvalRequiredBeforeIdempotency": matches!(
            &result,
            Err(ToolAdmissionError::ApprovalRequired { .. })
        ),
        "expiredApprovalRejectedBeforeIdempotency": matches!(
            &result,
            Err(ToolAdmissionError::ApprovalInvalid { .. })
        ),
        "idempotencyRejectedAfterApproval": matches!(
            &result,
            Err(ToolAdmissionError::IdempotencyKeyRequired { .. })
        ),
        "blankIdempotencyRejectedAfterApproval": matches!(
            &result,
            Err(ToolAdmissionError::EmptyIdempotencyKey { .. })
        ),
    })
    .as_object()
    .cloned()
    .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} result must be an object"))?)
}

fn evaluate_approval_mutation(
    case: &Map<String, Value>,
    case_name: &str,
) -> Result<Map<String, Value>, String> {
    let schema_id = required_str(case, "schemaId")?;
    let tool_name = required_str(case, "toolName")?;
    let resolved_tool = resolved_process_tool(tool_name, schema_id, None)?;
    let schemas = process_schema_registry(schema_id)?;
    let call = tool_call_from_arguments(
        tool_name,
        &resolved_tool.resolved_tool_id,
        case.get("initialArguments").cloned().ok_or_else(|| {
            format!("tool-lifecycle TCK case {case_name} requires initialArguments")
        })?,
    )?;
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved_tool, &call, "user-1", 1_000, 2_000)
            .map_err(|error| format!("tool-lifecycle TCK case {case_name}: {error:?}"))?;
    let approval = ToolApprovalRecord::approve(request, "admin-1", 1_100);
    let revised = call
        .revise_arguments(case.get("mutatedArguments").cloned().ok_or_else(|| {
            format!("tool-lifecycle TCK case {case_name} requires mutatedArguments")
        })?)
        .map_err(|error| format!("tool-lifecycle TCK case {case_name}: {error:?}"))?;
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
    Ok(json!({
        "initialApprovalValid": initial_valid,
        "mutatedApprovalValid": revised_valid,
        "digestChanged": revised.arguments_digest != call.arguments_digest,
        "revisedRevision": revised.revision,
        "admissionWithStaleApproval": result.is_ok(),
    })
    .as_object()
    .cloned()
    .ok_or_else(|| format!("tool-lifecycle TCK case {case_name} result must be an object"))?)
}

fn resolved_process_tool(
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
    let resolved = catalog
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")
        .map_err(|error| format!("tool resolution failed: {error:?}"))?;
    let resolved = resolved
        .into_iter()
        .next()
        .ok_or_else(|| "tool resolution returned no process tool".to_owned())?;
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

fn draft_status_name(status: ToolCallDraftStatus) -> &'static str {
    match status {
        ToolCallDraftStatus::Proposed => "proposed",
        ToolCallDraftStatus::ArgumentsStreaming => "arguments_streaming",
        ToolCallDraftStatus::ArgumentsComplete => "arguments_complete",
    }
}

fn required_str<'a>(value: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("tool-lifecycle TCK case requires string {key}"))
}

fn optional_str<'a>(value: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn required_u64(value: &Map<String, Value>, key: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("tool-lifecycle TCK case requires unsigned integer {key}"))
}

fn optional_u64(value: &Map<String, Value>, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}

fn required_string_array(value: &Map<String, Value>, key: &str) -> Result<Vec<String>, String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("tool-lifecycle TCK case requires array {key}"))?
        .iter()
        .enumerate()
        .map(|(index, item)| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| format!("tool-lifecycle TCK case {key}[{index}] must be a string"))
        })
        .collect()
}
