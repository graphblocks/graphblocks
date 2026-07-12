use graphblocks_runtime_core::output_policy::PendingToolCallsDisposition;
use graphblocks_runtime_core::tool::{ToolCancellation, ToolEffect};
use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft};
use graphblocks_runtime_core::tool_execution::{
    ToolExecutionCancellationPolicy, ToolExecutionFailurePolicy, ToolExecutionPlan,
    ToolExecutionPlanError, ToolExecutionState, ToolPlanCall,
};
use serde_json::{Map, Value};

#[test]
fn rust_tool_execution_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("fixtures/tool-execution-cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "tool-execution TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let case_name = required_str(case, "name")?;
    if required_str(case, "kind")? != "execution_plan" {
        return Err(format!(
            "tool-execution TCK case {case_name} has unsupported kind"
        ));
    }
    let response_id = required_str(case, "responseId")?;
    let effect_key_template = optional_str(case, "effectKeyTemplate");
    let planned_calls = case
        .get("calls")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("tool-execution TCK case {case_name} missing calls"))?
        .iter()
        .map(|raw_call| planned_call(raw_call, response_id, effect_key_template))
        .collect::<Result<Vec<_>, _>>()?;

    let expected_creation_error = optional_str(case, "expectedCreationError");
    let mut plan = match ToolExecutionPlan::new(
        required_str(case, "planId")?,
        response_id,
        planned_calls,
        required_u64(case, "maximumParallelism")? as usize,
    ) {
        Ok(plan) => {
            if expected_creation_error.is_some() {
                return Err(format!(
                    "tool-execution TCK case {case_name} expected creation error"
                ));
            }
            plan
        }
        Err(error) => {
            let actual = execution_error_code(&error);
            if Some(actual) != expected_creation_error {
                return Err(format!(
                    "tool-execution TCK case {case_name} creation error mismatch: {error:?}"
                ));
            }
            return Ok(());
        }
    };
    if let Some(failure_policy) = optional_str(case, "failurePolicy") {
        plan = plan.with_failure_policy(failure_policy_kind(failure_policy)?);
    }
    if let Some(cancellation_policy) = optional_str(case, "cancellationPolicy") {
        plan = plan.with_cancellation_policy(cancellation_policy_kind(cancellation_policy)?);
    }

    let operations = case
        .get("operations")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    for (operation_index, operation) in operations.iter().enumerate() {
        match required_str(operation, "op")? {
            "ready" => {
                let ready = plan.ready_call_ids();
                let expected = string_array(
                    operation
                        .as_object()
                        .ok_or_else(|| "tool-execution operation must be an object".to_owned())?,
                    "expect",
                )?;
                assert_eq!(ready, expected, "{case_name} operation {operation_index}");
            }
            "start" => {
                let actual_error = plan
                    .record_started(required_str(operation, "toolCallId")?)
                    .err()
                    .map(|error| execution_error_code(&error));
                assert_operation_error(case_name, operation_index, operation, actual_error)?;
            }
            "complete" => {
                let actual_error = plan
                    .record_completed(required_str(operation, "toolCallId")?)
                    .err()
                    .map(|error| execution_error_code(&error));
                assert_operation_error(case_name, operation_index, operation, actual_error)?;
            }
            "fail" => {
                let actual_error = plan
                    .record_failed(required_str(operation, "toolCallId")?)
                    .err()
                    .map(|error| execution_error_code(&error));
                assert_operation_error(case_name, operation_index, operation, actual_error)?;
            }
            "deny" => {
                let actual_error = plan
                    .record_denied(required_str(operation, "toolCallId")?)
                    .err()
                    .map(|error| execution_error_code(&error));
                assert_operation_error(case_name, operation_index, operation, actual_error)?;
            }
            "expire" => {
                let actual_error = plan
                    .record_expired(required_str(operation, "toolCallId")?)
                    .err()
                    .map(|error| execution_error_code(&error));
                assert_operation_error(case_name, operation_index, operation, actual_error)?;
            }
            "cancel" => {
                let actual_error = plan
                    .record_cancelled(required_str(operation, "toolCallId")?)
                    .err()
                    .map(|error| execution_error_code(&error));
                assert_operation_error(case_name, operation_index, operation, actual_error)?;
            }
            "policy_stopped" => {
                let actual_error = plan
                    .record_policy_stopped(required_str(operation, "toolCallId")?)
                    .err()
                    .map(|error| execution_error_code(&error));
                assert_operation_error(case_name, operation_index, operation, actual_error)?;
            }
            "policy_stop" => {
                let affected = plan.apply_policy_stop(pending_tool_calls_disposition(
                    required_str(operation, "pendingToolCalls")?,
                )?);
                let expected = string_array(
                    operation
                        .as_object()
                        .ok_or_else(|| "tool-execution operation must be an object".to_owned())?,
                    "expectAffected",
                )?;
                assert_eq!(
                    affected, expected,
                    "{case_name} operation {operation_index}"
                );
            }
            other => {
                return Err(format!(
                    "tool-execution TCK case {case_name} operation {operation_index} has unknown op {other}"
                ));
            }
        }
    }

    if let Some(expected_states) = case.get("expectedStates").and_then(Value::as_object) {
        for (tool_call_id, expected_state) in expected_states {
            let expected_state = expected_state.as_str().ok_or_else(|| {
                format!("tool-execution TCK case {case_name} expected state must be a string")
            })?;
            assert_eq!(
                plan.state(tool_call_id).map(execution_state_str),
                Some(expected_state),
                "{case_name} state {tool_call_id}",
            );
        }
    }
    Ok(())
}

fn planned_call(
    raw_call: &Value,
    response_id: &str,
    effect_key_template: Option<&str>,
) -> Result<ToolPlanCall, String> {
    let call_object = raw_call
        .as_object()
        .ok_or_else(|| "tool-execution call must be an object".to_owned())?;
    let mut call = tool_call_from_arguments(
        response_id,
        required_str(raw_call, "toolCallId")?,
        required_str(raw_call, "toolName")?,
        raw_call
            .get("arguments")
            .cloned()
            .ok_or_else(|| "tool-execution call missing arguments".to_owned())?,
    )?;
    if let Some(depends_on) = raw_call.get("dependsOn") {
        call.depends_on = depends_on
            .as_array()
            .ok_or_else(|| "tool-execution dependsOn must be an array".to_owned())?
            .iter()
            .map(|dependency| {
                dependency
                    .as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| "tool-execution dependsOn values must be strings".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()?;
    }
    let mut planned_call = ToolPlanCall::new(call).with_effects(effects(raw_call)?);
    if let Some(cancellation) = optional_str(raw_call, "cancellation") {
        planned_call = planned_call.with_cancellation(cancellation_kind(cancellation)?);
    }
    if let Some(effect_key) = call_object.get("effectKey").and_then(Value::as_str) {
        planned_call = planned_call.with_effect_key(effect_key);
    } else if let Some(template) = effect_key_template {
        planned_call = planned_call
            .with_effect_key_template(template)
            .map_err(|error| format!("effect key template failed: {error:?}"))?;
    }
    Ok(planned_call)
}

fn tool_call_from_arguments(
    response_id: &str,
    tool_call_id: &str,
    tool_name: &str,
    arguments: Value,
) -> Result<ToolCall, String> {
    let mut draft = ToolCallDraft::proposed(response_id, tool_call_id, tool_name);
    draft
        .append_argument_fragment(arguments.to_string())
        .map_err(|error| format!("tool call draft failed: {error:?}"))?;
    draft
        .into_completed_tool_call("resolved-tool-1", 1_000)
        .map_err(|error| format!("tool call completion failed: {error:?}"))
}

fn effects(raw_call: &Value) -> Result<Vec<ToolEffect>, String> {
    let effects = raw_call
        .get("effects")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    effects
        .iter()
        .map(|effect| {
            effect
                .as_str()
                .ok_or_else(|| "tool-execution effect must be a string".to_owned())
                .and_then(effect_kind)
        })
        .collect()
}

fn effect_kind(effect: &str) -> Result<ToolEffect, String> {
    match effect {
        "none" => Ok(ToolEffect::None),
        "external_read" => Ok(ToolEffect::ExternalRead),
        "external_write" => Ok(ToolEffect::ExternalWrite),
        "filesystem_read" => Ok(ToolEffect::FilesystemRead),
        "filesystem_write" => Ok(ToolEffect::FilesystemWrite),
        "process" => Ok(ToolEffect::Process),
        "network" => Ok(ToolEffect::Network),
        "destructive" => Ok(ToolEffect::Destructive),
        other => Err(format!("unknown tool effect {other}")),
    }
}

fn cancellation_kind(cancellation: &str) -> Result<ToolCancellation, String> {
    match cancellation {
        "unsupported" => Ok(ToolCancellation::Unsupported),
        "cooperative" => Ok(ToolCancellation::Cooperative),
        "force_terminable" => Ok(ToolCancellation::ForceTerminable),
        other => Err(format!("unknown tool cancellation {other}")),
    }
}

fn failure_policy_kind(policy: &str) -> Result<ToolExecutionFailurePolicy, String> {
    match policy {
        "fail_fast" => Ok(ToolExecutionFailurePolicy::FailFast),
        "collect" => Ok(ToolExecutionFailurePolicy::Collect),
        "return_failures_to_model" => Ok(ToolExecutionFailurePolicy::ReturnFailuresToModel),
        other => Err(format!("unknown tool execution failure policy {other}")),
    }
}

fn cancellation_policy_kind(policy: &str) -> Result<ToolExecutionCancellationPolicy, String> {
    match policy {
        "cancel_dependents" => Ok(ToolExecutionCancellationPolicy::CancelDependents),
        "cancel_all" => Ok(ToolExecutionCancellationPolicy::CancelAll),
        "allow_independent_calls" => Ok(ToolExecutionCancellationPolicy::AllowIndependentCalls),
        other => Err(format!(
            "unknown tool execution cancellation policy {other}"
        )),
    }
}

fn execution_error_code(error: &ToolExecutionPlanError) -> &'static str {
    match error {
        ToolExecutionPlanError::UnsafeParallelEffects { .. } => "unsafe_parallel_effects",
        ToolExecutionPlanError::EffectConflict { .. } => "effect_conflict",
        ToolExecutionPlanError::DuplicateDependency { .. } => "duplicate_dependency",
        ToolExecutionPlanError::ParallelismExhausted => "parallelism_exhausted",
        ToolExecutionPlanError::DependenciesNotReady { .. } => "dependencies_not_ready",
        ToolExecutionPlanError::ToolCallNotPending { .. } => "tool_call_not_pending",
        ToolExecutionPlanError::ToolCallNotRunning { .. } => "tool_call_not_running",
        _ => "tool_execution_plan_error",
    }
}

fn pending_tool_calls_disposition(
    disposition: &str,
) -> Result<PendingToolCallsDisposition, String> {
    match disposition {
        "keep" => Ok(PendingToolCallsDisposition::Keep),
        "deny" => Ok(PendingToolCallsDisposition::Deny),
        "cancel_admitted" => Ok(PendingToolCallsDisposition::CancelAdmitted),
        other => Err(format!("unknown pending tool calls disposition {other}")),
    }
}

fn assert_operation_error(
    case_name: &str,
    operation_index: usize,
    operation: &Value,
    actual_error: Option<&str>,
) -> Result<(), String> {
    let expected_error = optional_str(operation, "expectError");
    if actual_error != expected_error {
        return Err(format!(
            "tool-execution TCK case {case_name} operation {operation_index} error mismatch: expected {expected_error:?}, actual {actual_error:?}"
        ));
    }
    Ok(())
}

fn execution_state_str(state: ToolExecutionState) -> &'static str {
    match state {
        ToolExecutionState::Pending => "pending",
        ToolExecutionState::Running => "running",
        ToolExecutionState::Completed => "completed",
        ToolExecutionState::Failed => "failed",
        ToolExecutionState::Denied => "denied",
        ToolExecutionState::Cancelled => "cancelled",
        ToolExecutionState::PolicyStopped => "policy_stopped",
        ToolExecutionState::Expired => "expired",
        ToolExecutionState::Skipped => "skipped",
    }
}

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing required string field {key}"))
}

fn optional_str<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
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
