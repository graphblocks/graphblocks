use graphblocks_runtime_core::application_event::{
    ApplicationEvent, ApplicationEventKind, ApplicationEventMetadata, ApplicationEventStreamState,
    ApplicationEventVisibility,
};
use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::output_policy::{
    DraftDisposition, DurableResult, GenerationChunk, OutputCutoff, OutputPolicyDecision,
    PendingToolCallsDisposition, ProviderCancellation, TerminalReason,
};
use graphblocks_runtime_core::tool_call::{ToolCallDraft, ToolCallStatus};
use graphblocks_runtime_core::tool_result::{
    ArtifactRef, ContentPart, ToolEffectOutcome, ToolResult, ToolResultEvent,
};
use serde_json::{Value, json};

#[test]
fn rust_application_event_stream_matches_shared_tck_cases() -> Result<(), String> {
    let cases =
        serde_json::from_str::<Value>(include_str!("../../../tck/application-events/cases.json"))
            .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "application-events TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let case_name = required_str(case, "name")?;
    let default_response_id = optional_str(case, "responseId").unwrap_or("response-1");
    let mut state = ApplicationEventStreamState::default();
    let operations = case
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("application-events TCK case {case_name} is missing operations"))?;

    for (index, operation) in operations.iter().enumerate() {
        let op = required_str(operation, "op")?;
        let response_id = optional_str(operation, "responseId").unwrap_or(default_response_id);
        let metadata = ApplicationEventMetadata {
            event_id: optional_str(operation, "eventId")
                .map(str::to_owned)
                .unwrap_or_else(|| format!("{case_name}:{}", index + 1)),
            run_id: optional_str(operation, "runId")
                .or_else(|| optional_str(case, "runId"))
                .unwrap_or("run-1")
                .to_owned(),
            response_id: response_id.to_owned(),
            turn_id: optional_str(operation, "turnId")
                .or_else(|| optional_str(case, "turnId"))
                .map(str::to_owned),
            cursor: optional_str(operation, "eventCursor")
                .or_else(|| optional_str(operation, "cursor"))
                .map(str::to_owned),
            graph_id: optional_str(operation, "graphId")
                .or_else(|| optional_str(operation, "graph_id"))
                .map(str::to_owned),
            node_id: optional_str(operation, "nodeId")
                .or_else(|| optional_str(operation, "node_id"))
                .map(str::to_owned),
            operation_id: optional_str(operation, "operationId")
                .or_else(|| optional_str(operation, "operation_id"))
                .map(str::to_owned),
            sequence: optional_u64(operation, "eventSequence").unwrap_or((index + 1) as u64),
            release_id: optional_str(case, "releaseId")
                .unwrap_or("release-1")
                .to_owned(),
            policy_snapshot_id: optional_str(case, "policySnapshotId")
                .unwrap_or("policy-1")
                .to_owned(),
            occurred_at_unix_ms: optional_u64(operation, "occurredAtUnixMs").unwrap_or(1_700_000),
            visibility: optional_str(operation, "visibility")
                .unwrap_or("client")
                .parse::<ApplicationEventVisibility>()
                .map_err(|error| format!("{case_name}: {error}"))?,
        };

        match op {
            "output_policy_evaluation_started" => {
                let chunk = GenerationChunk::text(
                    optional_str(operation, "streamId")
                        .or_else(|| optional_str(case, "streamId"))
                        .unwrap_or("stream-1"),
                    response_id,
                    optional_u64(operation, "sequence")
                        .or_else(|| optional_u64(operation, "chunkSequence"))
                        .unwrap_or(1),
                    optional_str(operation, "text").unwrap_or(""),
                );
                let event = ApplicationEvent::output_policy_evaluation_started(
                    metadata,
                    &chunk,
                    required_str(operation, "inputDigest")?,
                )
                .map_err(|error| format!("{case_name}: {error:?}"))?;
                let accepted = state.accept(event).is_some();
                assert_eq!(
                    accepted,
                    optional_bool(operation, "expectAccepted").unwrap_or(true),
                    "{case_name}",
                );
            }
            "output_policy_decision" => {
                let decision = output_policy_decision(operation)?;
                let event = ApplicationEvent::output_policy_decision(metadata, &decision)
                    .map_err(|error| format!("{case_name}: {error:?}"))?;
                let accepted = state.accept(event).is_some();
                assert_eq!(
                    accepted,
                    optional_bool(operation, "expectAccepted").unwrap_or(true),
                    "{case_name}",
                );
            }
            "output_cutoff" => {
                let cutoff = OutputCutoff {
                    stream_id: optional_str(operation, "streamId")
                        .or_else(|| optional_str(case, "streamId"))
                        .unwrap_or("stream-1")
                        .to_owned(),
                    response_id: response_id.to_owned(),
                    turn_id: optional_str(operation, "turnId")
                        .or_else(|| optional_str(case, "turnId"))
                        .map(str::to_owned),
                    last_generated_sequence: required_u64(operation, "lastGeneratedSequence")?,
                    last_policy_accepted_sequence: required_u64(
                        operation,
                        "lastPolicyAcceptedSequence",
                    )?,
                    last_client_delivered_sequence: required_u64(
                        operation,
                        "lastClientDeliveredSequence",
                    )?,
                    terminal_reason: terminal_reason(required_str(operation, "terminalReason")?)?,
                    draft_disposition: draft_disposition(required_str(
                        operation,
                        "draftDisposition",
                    )?)?,
                    durable_result: durable_result(required_str(operation, "durableResult")?)?,
                    policy_decision_id: optional_str(operation, "policyDecisionId")
                        .map(str::to_owned),
                    occurred_at_unix_ms: required_u64(operation, "occurredAtUnixMs")?,
                };
                for event in ApplicationEvent::output_cutoff(metadata, &cutoff)
                    .map_err(|error| format!("{case_name}: {error:?}"))?
                {
                    assert_eq!(state.accept(event.clone()), Some(event), "{case_name}");
                }
            }
            "run_succeeded" => {
                let event = ApplicationEvent::new(
                    ApplicationEventKind::RunSucceeded,
                    metadata,
                    json!({
                        "status": "succeeded",
                        "outputs": operation.get("outputs").cloned().unwrap_or_else(|| json!({})),
                    }),
                )
                .map_err(|error| format!("{case_name}: {error:?}"))?;
                let accepted = state.accept(event).is_some();
                assert_eq!(
                    accepted,
                    optional_bool(operation, "expectAccepted").unwrap_or(true),
                    "{case_name}",
                );
            }
            "tool_call_state" => {
                let tool_call_id = required_str(operation, "toolCallId")?;
                let tool_name = required_str(operation, "toolName")?;
                let resolved_tool_id = required_str(operation, "resolvedToolId")?;
                let created_at_unix_ms = required_u64(operation, "createdAtUnixMs")?;
                let admitted_at_unix_ms =
                    optional_u64(operation, "admittedAtUnixMs").unwrap_or(created_at_unix_ms + 1);
                let completed_at_unix_ms =
                    optional_u64(operation, "completedAtUnixMs").unwrap_or(admitted_at_unix_ms + 1);
                let mut draft = ToolCallDraft::proposed(response_id, tool_call_id, tool_name);
                let arguments = operation
                    .get("arguments")
                    .cloned()
                    .unwrap_or_else(|| json!({}));
                draft
                    .append_argument_fragment(arguments.to_string())
                    .map_err(|error| format!("{case_name}: {error:?}"))?;
                let base_call = draft
                    .into_completed_tool_call(resolved_tool_id, created_at_unix_ms)
                    .map_err(|error| format!("{case_name}: {error:?}"))?;
                let call = match required_str(operation, "status")? {
                    "validated" => base_call,
                    "policy_pending" => base_call
                        .transition_status(ToolCallStatus::PolicyPending, admitted_at_unix_ms)
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "approval_pending" => base_call
                        .transition_status(ToolCallStatus::ApprovalPending, admitted_at_unix_ms)
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "admitted" => base_call
                        .transition_status(ToolCallStatus::Admitted, admitted_at_unix_ms)
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "running" => base_call
                        .transition_status(ToolCallStatus::Admitted, admitted_at_unix_ms)
                        .and_then(|call| {
                            call.transition_status(ToolCallStatus::Running, admitted_at_unix_ms)
                        })
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "completed" => base_call
                        .transition_status(ToolCallStatus::Admitted, admitted_at_unix_ms)
                        .and_then(|call| {
                            call.transition_status(ToolCallStatus::Running, admitted_at_unix_ms)
                        })
                        .and_then(|call| {
                            call.transition_status(ToolCallStatus::Completed, completed_at_unix_ms)
                        })
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "failed" => base_call
                        .transition_status(ToolCallStatus::Failed, completed_at_unix_ms)
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "denied" => base_call
                        .transition_status(ToolCallStatus::Denied, completed_at_unix_ms)
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "cancelled" => base_call
                        .transition_status(ToolCallStatus::Cancelled, completed_at_unix_ms)
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "policy_stopped" => base_call
                        .transition_status(ToolCallStatus::PolicyStopped, completed_at_unix_ms)
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    "expired" => base_call
                        .transition_status(ToolCallStatus::Expired, completed_at_unix_ms)
                        .map_err(|error| format!("{case_name}: {error:?}"))?,
                    other => {
                        return Err(format!(
                            "application-events TCK case {case_name} has unknown tool call status {other}"
                        ));
                    }
                };
                let accepted = if let Some(event) =
                    ApplicationEvent::tool_call_state(metadata, &call)
                        .map_err(|error| format!("{case_name}: {error:?}"))?
                {
                    state.accept(event).is_some()
                } else {
                    false
                };
                assert_eq!(
                    accepted,
                    optional_bool(operation, "expectAccepted").unwrap_or(true),
                    "{case_name}",
                );
            }
            "tool_result_started"
            | "tool_result_delta"
            | "tool_result_artifact_ready"
            | "tool_result_completed"
            | "tool_result_failed"
            | "tool_result_denied"
            | "tool_result_cancelled"
            | "tool_result_policy_stopped"
            | "tool_result_incomplete" => {
                let tool_call_id = required_str(operation, "toolCallId")?;
                let tool_result_sequence = required_u64(operation, "toolResultSequence")?;
                let result_event = match op {
                    "tool_result_started" => ToolResultEvent::started(
                        tool_call_id,
                        tool_result_sequence,
                        required_u64(operation, "startedAtUnixMs")?,
                    ),
                    "tool_result_artifact_ready" => {
                        let raw_artifact = operation
                            .get("artifact")
                            .and_then(Value::as_object)
                            .ok_or_else(|| {
                                format!(
                                    "application-events TCK case {case_name} tool result artifact must be an object"
                                )
                            })?;
                        let mut artifact = ArtifactRef::new(
                            required_str_object(raw_artifact, "artifactId")?,
                            required_str_object(raw_artifact, "uri")?,
                        );
                        if let Some(checksum) = optional_str_object(raw_artifact, "checksum") {
                            artifact = artifact.with_checksum(checksum);
                        }
                        if let Some(media_type) = optional_str_object(raw_artifact, "mediaType") {
                            artifact = artifact.with_media_type(media_type);
                        }
                        ToolResultEvent::artifact_ready(
                            tool_call_id,
                            tool_result_sequence,
                            artifact,
                        )
                    }
                    "tool_result_failed" | "tool_result_denied" => {
                        let raw_error = operation
                            .get("error")
                            .and_then(Value::as_object)
                            .ok_or_else(|| {
                                format!(
                                    "application-events TCK case {case_name} terminal result error must be an object"
                                )
                            })?;
                        let effect_outcome = tool_effect_outcome(
                            optional_str(operation, "effectOutcome"),
                            case_name,
                        )?;
                        if op == "tool_result_failed" {
                            let result = ToolResult::failed(
                                tool_call_id,
                                BlockError::new(
                                    required_str_object(raw_error, "code")?,
                                    ErrorCategory::Permanent,
                                    required_str_object(raw_error, "message")?,
                                    false,
                                ),
                                required_u64(operation, "startedAtUnixMs")?,
                                required_u64(operation, "completedAtUnixMs")?,
                            )
                            .with_effect_outcome(effect_outcome);
                            ToolResultEvent::failed(tool_call_id, tool_result_sequence, result)
                        } else {
                            let result = ToolResult::denied(
                                tool_call_id,
                                BlockError::new(
                                    required_str_object(raw_error, "code")?,
                                    ErrorCategory::Policy,
                                    required_str_object(raw_error, "message")?,
                                    false,
                                ),
                                required_u64(operation, "completedAtUnixMs")?,
                            )
                            .with_effect_outcome(effect_outcome);
                            ToolResultEvent::denied(tool_call_id, tool_result_sequence, result)
                        }
                    }
                    "tool_result_cancelled"
                    | "tool_result_policy_stopped"
                    | "tool_result_incomplete" => {
                        let effect_outcome = tool_effect_outcome(
                            optional_str(operation, "effectOutcome"),
                            case_name,
                        )?;
                        match op {
                            "tool_result_cancelled" => {
                                let result = ToolResult::cancelled(
                                    tool_call_id,
                                    required_u64(operation, "startedAtUnixMs")?,
                                    required_u64(operation, "completedAtUnixMs")?,
                                )
                                .with_effect_outcome(effect_outcome);
                                ToolResultEvent::cancelled(
                                    tool_call_id,
                                    tool_result_sequence,
                                    result,
                                )
                            }
                            "tool_result_policy_stopped" => {
                                let raw_error = operation
                                    .get("error")
                                    .and_then(Value::as_object)
                                    .ok_or_else(|| {
                                        format!(
                                            "application-events TCK case {case_name} policy stopped error must be an object"
                                        )
                                    })?;
                                let result = ToolResult::policy_stopped(
                                    tool_call_id,
                                    BlockError::new(
                                        required_str_object(raw_error, "code")?,
                                        ErrorCategory::Policy,
                                        required_str_object(raw_error, "message")?,
                                        false,
                                    ),
                                    required_u64(operation, "startedAtUnixMs")?,
                                    required_u64(operation, "completedAtUnixMs")?,
                                )
                                .with_effect_outcome(effect_outcome);
                                ToolResultEvent::policy_stopped(
                                    tool_call_id,
                                    tool_result_sequence,
                                    result,
                                )
                            }
                            "tool_result_incomplete" => {
                                let result = ToolResult::incomplete(
                                    tool_call_id,
                                    required_u64(operation, "startedAtUnixMs")?,
                                    required_u64(operation, "completedAtUnixMs")?,
                                )
                                .with_effect_outcome(effect_outcome);
                                ToolResultEvent::incomplete(
                                    tool_call_id,
                                    tool_result_sequence,
                                    result,
                                )
                            }
                            _ => unreachable!(),
                        }
                    }
                    "tool_result_delta" | "tool_result_completed" => {
                        let raw_output = operation
                            .get("output")
                            .and_then(Value::as_array)
                            .ok_or_else(|| {
                                format!(
                                    "application-events TCK case {case_name} tool result output must be an array"
                                )
                            })?;
                        let mut output = Vec::new();
                        for (part_index, raw_part) in raw_output.iter().enumerate() {
                            let part_kind = optional_str(raw_part, "kind").unwrap_or("text");
                            let mut part = match part_kind {
                                "text" => ContentPart::text(required_str(raw_part, "text")?),
                                "json" => ContentPart::json(raw_part.get("data").cloned().ok_or_else(
                                    || {
                                        format!(
                                            "application-events TCK case {case_name} output part {part_index} missing data"
                                        )
                                    },
                                )?),
                                other => {
                                    return Err(format!(
                                        "application-events TCK case {case_name} has unsupported output part kind {other}"
                                    ));
                                }
                            };
                            if let Some(metadata) = raw_part.get("metadata") {
                                let metadata = metadata.as_object().ok_or_else(|| {
                                    format!(
                                        "application-events TCK case {case_name} output part {part_index} metadata must be an object"
                                    )
                                })?;
                                for (key, value) in metadata {
                                    part = part.with_metadata(key, value.clone());
                                }
                            }
                            output.push(part);
                        }
                        if op == "tool_result_delta" {
                            ToolResultEvent::delta(tool_call_id, tool_result_sequence, output)
                        } else {
                            let effect_outcome = tool_effect_outcome(
                                optional_str(operation, "effectOutcome"),
                                case_name,
                            )?;
                            let result = ToolResult::completed(
                                tool_call_id,
                                output,
                                required_u64(operation, "startedAtUnixMs")?,
                                required_u64(operation, "completedAtUnixMs")?,
                            )
                            .with_effect_outcome(effect_outcome);
                            ToolResultEvent::completed(tool_call_id, tool_result_sequence, result)
                        }
                    }
                    _ => unreachable!(),
                };
                let accepted = if let Some(event) =
                    ApplicationEvent::tool_result_event(metadata, &result_event)
                        .map_err(|error| format!("{case_name}: {error:?}"))?
                {
                    state.accept(event).is_some()
                } else {
                    false
                };
                assert_eq!(
                    accepted,
                    optional_bool(operation, "expectAccepted").unwrap_or(true),
                    "{case_name}",
                );
            }
            other => {
                return Err(format!(
                    "application-events TCK case {case_name} has unknown operation {other}"
                ));
            }
        }
    }

    let actual_kinds = state
        .accepted_events()
        .iter()
        .map(|event| event.kind.as_str())
        .collect::<Vec<_>>();
    let expected_kinds = case
        .get("expectedAcceptedKinds")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            format!("application-events TCK case {case_name} is missing expectedAcceptedKinds")
        })?
        .iter()
        .map(|kind| {
            kind.as_str().ok_or_else(|| {
                format!("application-events TCK case {case_name} expected kind must be a string")
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    assert_eq!(actual_kinds, expected_kinds, "{case_name}");
    Ok(())
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

fn required_str_object<'a>(
    value: &'a serde_json::Map<String, Value>,
    key: &str,
) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing required string field {key}"))
}

fn optional_str_object<'a>(
    value: &'a serde_json::Map<String, Value>,
    key: &str,
) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn required_u64(value: &Value, key: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("missing required u64 field {key}"))
}

fn optional_u64(value: &Value, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}

fn optional_bool(value: &Value, key: &str) -> Option<bool> {
    value.get(key).and_then(Value::as_bool)
}

fn tool_effect_outcome(value: Option<&str>, case_name: &str) -> Result<ToolEffectOutcome, String> {
    match value.unwrap_or("unknown") {
        "no_external_effect" => Ok(ToolEffectOutcome::NoExternalEffect),
        "committed" => Ok(ToolEffectOutcome::Committed),
        "not_committed" => Ok(ToolEffectOutcome::NotCommitted),
        "unknown" => Ok(ToolEffectOutcome::Unknown),
        other => Err(format!(
            "application-events TCK case {case_name} has unknown effect outcome {other}"
        )),
    }
}

fn output_policy_decision(operation: &Value) -> Result<OutputPolicyDecision, String> {
    let decision_id = required_str(operation, "decisionId")?;
    let input_digest = required_str(operation, "inputDigest")?;
    let accepted_through_sequence = optional_u64(operation, "acceptedThrough")
        .or_else(|| optional_u64(operation, "acceptedThroughSequence"));
    let mut decision = match required_str(operation, "disposition")? {
        "allow" => {
            OutputPolicyDecision::allow(decision_id, accepted_through_sequence, input_digest)
        }
        "hold" => OutputPolicyDecision::hold(decision_id, input_digest),
        "redact" => OutputPolicyDecision::redact(
            decision_id,
            accepted_through_sequence,
            Vec::new(),
            input_digest,
        ),
        "replace" => {
            let replacement_chunks = operation
                .get("replacementChunks")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[])
                .iter()
                .map(|chunk| {
                    Ok(GenerationChunk::text(
                        optional_str(chunk, "streamId").unwrap_or("stream-1"),
                        optional_str(chunk, "responseId").unwrap_or("response-1"),
                        required_u64(chunk, "sequence")?,
                        required_str(chunk, "text")?,
                    ))
                })
                .collect::<Result<Vec<_>, String>>()?;
            OutputPolicyDecision::replace(
                decision_id,
                accepted_through_sequence,
                replacement_chunks,
                input_digest,
            )
        }
        "abort_response" => OutputPolicyDecision::abort_response(decision_id, input_digest),
        "abort_turn" => OutputPolicyDecision::abort_turn(decision_id, input_digest),
        "deny_commit" => OutputPolicyDecision::deny_commit(decision_id, input_digest),
        other => return Err(format!("unknown output policy disposition {other}")),
    };
    if let Some(reason_codes) = string_array(operation, "reasonCodes")? {
        decision = decision.with_reason_codes(reason_codes);
    }
    if let Some(policy_refs) = string_array(operation, "policyRefs")? {
        decision = decision.with_policy_refs(policy_refs);
    }
    if let Some(provider_cancellation_value) = optional_str(operation, "providerCancellation") {
        decision = decision
            .with_provider_cancellation(provider_cancellation(provider_cancellation_value)?);
    }
    if let Some(draft_disposition_value) = optional_str(operation, "draftDisposition") {
        decision = decision.with_draft_disposition(draft_disposition(draft_disposition_value)?);
    }
    if let Some(pending_tool_calls_value) = optional_str(operation, "pendingToolCalls") {
        decision = decision.with_pending_tool_calls(pending_tool_calls(pending_tool_calls_value)?);
    }
    if let Some(evaluated_at_unix_ms) = optional_u64(operation, "evaluatedAtUnixMs") {
        decision = decision.evaluated_at_unix_ms(evaluated_at_unix_ms);
    }
    Ok(decision)
}

fn string_array(value: &Value, key: &str) -> Result<Option<Vec<String>>, String> {
    let Some(values) = value.get(key) else {
        return Ok(None);
    };
    let values = values
        .as_array()
        .ok_or_else(|| format!("{key} must be an array"))?;
    values
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| format!("{key} values must be strings"))
        })
        .collect::<Result<Vec<_>, _>>()
        .map(Some)
}

fn provider_cancellation(value: &str) -> Result<ProviderCancellation, String> {
    match value {
        "none" => Ok(ProviderCancellation::None),
        "request" => Ok(ProviderCancellation::Request),
        "required_if_supported" => Ok(ProviderCancellation::RequiredIfSupported),
        other => Err(format!("unknown provider cancellation {other}")),
    }
}

fn pending_tool_calls(value: &str) -> Result<PendingToolCallsDisposition, String> {
    match value {
        "keep" => Ok(PendingToolCallsDisposition::Keep),
        "deny" => Ok(PendingToolCallsDisposition::Deny),
        "cancel_admitted" => Ok(PendingToolCallsDisposition::CancelAdmitted),
        other => Err(format!("unknown pending tool calls disposition {other}")),
    }
}

fn terminal_reason(value: &str) -> Result<TerminalReason, String> {
    match value {
        "policy_denied" => Ok(TerminalReason::PolicyDenied),
        "budget_exhausted" => Ok(TerminalReason::BudgetExhausted),
        "cancelled" => Ok(TerminalReason::Cancelled),
        "client_disconnected" => Ok(TerminalReason::ClientDisconnected),
        other => Err(format!("unknown terminal reason {other}")),
    }
}

fn draft_disposition(value: &str) -> Result<DraftDisposition, String> {
    match value {
        "keep" => Ok(DraftDisposition::Keep),
        "mark_incomplete" => Ok(DraftDisposition::MarkIncomplete),
        "retract" => Ok(DraftDisposition::Retract),
        other => Err(format!("unknown draft disposition {other}")),
    }
}

fn durable_result(value: &str) -> Result<DurableResult, String> {
    match value {
        "none" => Ok(DurableResult::None),
        "incomplete" => Ok(DurableResult::Incomplete),
        "partial" => Ok(DurableResult::Partial),
        other => Err(format!("unknown durable result {other}")),
    }
}
