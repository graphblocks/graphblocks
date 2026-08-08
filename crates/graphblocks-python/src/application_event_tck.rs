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
use serde_json::{Map, Value, json};

pub(crate) fn evaluate_case(case: &Value) -> Result<Value, String> {
    let case_name = required_str(case, "name")?;
    let operations = case
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("application-events TCK case {case_name} is missing operations"))?;
    let expected_kinds = string_array(case, "expectedAcceptedKinds")?.ok_or_else(|| {
        format!("application-events TCK case {case_name} is missing expectedAcceptedKinds")
    })?;
    let mut state = ApplicationEventStreamState::default();
    let mut diagnostics = Vec::new();
    let mut operation_results = (0..operations.len())
        .map(|operation_index| {
            json!({
                "operationIndex": operation_index,
                "emissions": [{
                    "emissionIndex": null,
                    "emission": "none",
                    "admission": "not_applicable",
                    "event": null,
                }],
            })
        })
        .collect::<Vec<_>>();

    for (index, operation) in operations.iter().enumerate() {
        let op = required_str(operation, "op")?;
        let response_id = optional_str(operation, "responseId").unwrap_or("response-1");
        let metadata = ApplicationEventMetadata {
            event_id: optional_str(operation, "eventId")
                .map(str::to_owned)
                .unwrap_or_else(|| format!("{case_name}:{}", index + 1)),
            run_id: optional_str(operation, "runId")
                .unwrap_or("run-1")
                .to_owned(),
            response_id: response_id.to_owned(),
            turn_id: optional_str(operation, "turnId").map(str::to_owned),
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
            release_id: optional_str(operation, "releaseId")
                .unwrap_or("release-1")
                .to_owned(),
            policy_snapshot_id: optional_str(operation, "policySnapshotId")
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
                let sequence = optional_u64(operation, "sequence")
                    .or_else(|| optional_u64(operation, "chunkSequence"));
                let Some(sequence) = sequence else {
                    diagnostics.push(diagnostic(
                        "ApplicationEventGenerationSequenceInvalid",
                        "generation chunk sequence must be an integer",
                        &format!("$.operations[{index}].sequence"),
                    ));
                    continue;
                };
                let chunk = GenerationChunk::text(
                    optional_str(operation, "streamId").unwrap_or("stream-1"),
                    response_id,
                    sequence,
                    optional_str(operation, "text").unwrap_or(""),
                );
                let event = ApplicationEvent::output_policy_evaluation_started(
                    metadata,
                    &chunk,
                    required_str(operation, "inputDigest")?,
                )
                .map_err(|error| format!("{case_name}: {error:?}"))?;
                let accepted = accept_event(&mut state, event, index, 0, &mut operation_results)?;
                record_acceptance_mismatch(operation, index, accepted.is_some(), &mut diagnostics);
            }
            "output_policy_decision" => {
                let decision = output_policy_decision(operation)?;
                let event = ApplicationEvent::output_policy_decision(metadata, &decision)
                    .map_err(|error| format!("{case_name}: {error:?}"))?;
                let accepted = accept_event(&mut state, event, index, 0, &mut operation_results)?;
                record_acceptance_mismatch(operation, index, accepted.is_some(), &mut diagnostics);
            }
            "output_cutoff" => {
                let cutoff = OutputCutoff {
                    stream_id: optional_str(operation, "streamId")
                        .unwrap_or("stream-1")
                        .to_owned(),
                    response_id: response_id.to_owned(),
                    turn_id: optional_str(operation, "turnId").map(str::to_owned),
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
                let events = ApplicationEvent::output_cutoff(metadata, &cutoff)
                    .map_err(|error| format!("{case_name}: {error:?}"))?;
                for (emission_index, event) in events.into_iter().enumerate() {
                    if accept_event(
                        &mut state,
                        event,
                        index,
                        emission_index,
                        &mut operation_results,
                    )?
                    .is_none()
                    {
                        diagnostics.push(diagnostic(
                            "ApplicationEventUnexpectedRejection",
                            "application event TCK output cutoff event was rejected",
                            &format!("$.operations[{index}]"),
                        ));
                    }
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
                let accepted = accept_event(&mut state, event, index, 0, &mut operation_results)?;
                record_acceptance_mismatch(operation, index, accepted.is_some(), &mut diagnostics);
            }
            "tool_call_state" => {
                let event = tool_call_state_event(operation, metadata, response_id, case_name)?;
                let accepted = match event {
                    Some(event) => {
                        accept_event(&mut state, event, index, 0, &mut operation_results)?
                    }
                    None => None,
                };
                record_acceptance_mismatch(operation, index, accepted.is_some(), &mut diagnostics);
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
                let result_event = tool_result_event(operation, op, case_name)?;
                let event = ApplicationEvent::tool_result_event(metadata, &result_event)
                    .map_err(|error| format!("{case_name}: {error:?}"))?;
                let accepted = match event {
                    Some(event) => {
                        accept_event(&mut state, event, index, 0, &mut operation_results)?
                    }
                    None => None,
                };
                record_acceptance_mismatch(operation, index, accepted.is_some(), &mut diagnostics);
            }
            other => {
                diagnostics.push(diagnostic(
                    "ApplicationEventOperationUnknown",
                    &format!("application event TCK operation '{other}' is not supported"),
                    &format!("$.operations[{index}].op"),
                ));
            }
        }
    }

    let accepted_kinds = state
        .accepted_events()
        .iter()
        .map(|event| event.kind.as_str())
        .collect::<Vec<_>>();
    if accepted_kinds != expected_kinds {
        diagnostics.push(diagnostic(
            "ApplicationEventAcceptedKindsMismatch",
            "accepted application event kinds did not match expected kinds",
            "$.expectedAcceptedKinds",
        ));
    }
    let accepted_metadata = state
        .accepted_events()
        .iter()
        .map(|event| {
            let metadata = &event.metadata;
            json!({
                "event_id": metadata.event_id,
                "run_id": metadata.run_id,
                "response_id": metadata.response_id,
                "turn_id": metadata.turn_id,
                "sequence": metadata.sequence,
                "cursor": metadata.cursor,
                "release_id": metadata.release_id,
                "policy_snapshot_id": metadata.policy_snapshot_id,
                "occurred_at_unix_ms": metadata.occurred_at_unix_ms,
                "graph_id": metadata.graph_id,
                "node_id": metadata.node_id,
                "operation_id": metadata.operation_id,
                "visibility": metadata.visibility.as_str(),
            })
        })
        .collect::<Vec<_>>();
    let accepted_events = state
        .accepted_events()
        .iter()
        .map(serialize_application_event)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(json!({
        "ok": diagnostics.is_empty(),
        "diagnostics": diagnostics,
        "observed": {
            "accepted_kinds": accepted_kinds,
            "accepted_metadata": accepted_metadata,
            "accepted_events": accepted_events,
            "operation_results": operation_results,
        },
    }))
}

fn serialize_application_event(event: &ApplicationEvent) -> Result<Value, String> {
    let metadata = &event.metadata;
    let mut payload = event.payload.clone();
    let payload_object = payload
        .as_object_mut()
        .ok_or_else(|| "application event payload must be an object".to_owned())?;
    if let Some(replacement_chunk_count) = payload_object.remove("replacement_chunk_count")
        && payload_object.get("replacement_part_count") != Some(&replacement_chunk_count)
    {
        return Err("application event replacement part and chunk counts must match".to_owned());
    }
    Ok(json!({
        "kind": event.kind.as_str(),
        "metadata": {
            "eventId": metadata.event_id,
            "runId": metadata.run_id,
            "responseId": metadata.response_id,
            "turnId": metadata.turn_id,
            "cursor": metadata.cursor,
            "graphId": metadata.graph_id,
            "nodeId": metadata.node_id,
            "operationId": metadata.operation_id,
            "sequence": metadata.sequence,
            "releaseId": metadata.release_id,
            "policySnapshotId": metadata.policy_snapshot_id,
            "occurredAtUnixMs": metadata.occurred_at_unix_ms,
            "visibility": metadata.visibility.as_str(),
        },
        "toolCallId": event.tool_call_id,
        "payload": payload,
    }))
}

fn accept_event(
    state: &mut ApplicationEventStreamState,
    event: ApplicationEvent,
    operation_index: usize,
    emission_index: usize,
    operation_results: &mut [Value],
) -> Result<Option<ApplicationEvent>, String> {
    let attempted_event = serialize_application_event(&event)?;
    let accepted = state.accept(event);
    let event_contract = match &accepted {
        Some(event) => serialize_application_event(event)?,
        None => attempted_event,
    };
    let emissions = operation_results
        .get_mut(operation_index)
        .and_then(Value::as_object_mut)
        .and_then(|operation| operation.get_mut("emissions"))
        .and_then(Value::as_array_mut)
        .ok_or_else(|| "application event operation result must contain emissions".to_owned())?;
    if emission_index == 0 {
        emissions.clear();
    }
    emissions.push(json!({
        "emissionIndex": emission_index,
        "emission": "event",
        "admission": if accepted.is_some() { "accepted" } else { "dropped" },
        "event": event_contract,
    }));
    Ok(accepted)
}

fn record_acceptance_mismatch(
    operation: &Value,
    index: usize,
    accepted: bool,
    diagnostics: &mut Vec<Value>,
) {
    if accepted != optional_bool(operation, "expectAccepted").unwrap_or(true) {
        diagnostics.push(diagnostic(
            "ApplicationEventAcceptanceMismatch",
            "application event acceptance did not match expected result",
            &format!("$.operations[{index}].expectAccepted"),
        ));
    }
}

fn diagnostic(code: &str, message: &str, path: &str) -> Value {
    json!({"code": code, "message": message, "path": path})
}

fn tool_call_state_event(
    operation: &Value,
    metadata: ApplicationEventMetadata,
    response_id: &str,
    case_name: &str,
) -> Result<Option<ApplicationEvent>, String> {
    let tool_call_id = required_str(operation, "toolCallId")?;
    let tool_name = required_str(operation, "toolName")?;
    let resolved_tool_id = required_str(operation, "resolvedToolId")?;
    let created_at_unix_ms = required_u64(operation, "createdAtUnixMs")?;
    let admitted_at_unix_ms = match optional_u64(operation, "admittedAtUnixMs") {
        Some(value) => value,
        None => created_at_unix_ms
            .checked_add(1)
            .ok_or_else(|| format!("{case_name}: admitted timestamp exceeds u64"))?,
    };
    let completed_at_unix_ms = match optional_u64(operation, "completedAtUnixMs") {
        Some(value) => value,
        None => admitted_at_unix_ms
            .checked_add(1)
            .ok_or_else(|| format!("{case_name}: completed timestamp exceeds u64"))?,
    };
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
            .and_then(|call| call.transition_status(ToolCallStatus::Running, admitted_at_unix_ms))
            .map_err(|error| format!("{case_name}: {error:?}"))?,
        "completed" => base_call
            .transition_status(ToolCallStatus::Admitted, admitted_at_unix_ms)
            .and_then(|call| call.transition_status(ToolCallStatus::Running, admitted_at_unix_ms))
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
        other => return Err(format!("{case_name}: unknown tool call status {other}")),
    };
    ApplicationEvent::tool_call_state(metadata, &call)
        .map_err(|error| format!("{case_name}: {error:?}"))
}

fn tool_result_event(
    operation: &Value,
    op: &str,
    case_name: &str,
) -> Result<ToolResultEvent, String> {
    let tool_call_id = required_str(operation, "toolCallId")?;
    let sequence = required_u64(operation, "toolResultSequence")?;
    match op {
        "tool_result_started" => Ok(ToolResultEvent::started(
            tool_call_id,
            sequence,
            required_u64(operation, "startedAtUnixMs")?,
        )),
        "tool_result_artifact_ready" => {
            let raw_artifact = operation
                .get("artifact")
                .and_then(Value::as_object)
                .ok_or_else(|| format!("{case_name}: tool result artifact must be an object"))?;
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
            Ok(ToolResultEvent::artifact_ready(
                tool_call_id,
                sequence,
                artifact,
            ))
        }
        "tool_result_failed" | "tool_result_denied" => {
            let raw_error = operation
                .get("error")
                .and_then(Value::as_object)
                .ok_or_else(|| format!("{case_name}: terminal result error must be an object"))?;
            let effect_outcome =
                tool_effect_outcome(optional_str(operation, "effectOutcome"), case_name)?;
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
                Ok(ToolResultEvent::failed(tool_call_id, sequence, result))
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
                Ok(ToolResultEvent::denied(tool_call_id, sequence, result))
            }
        }
        "tool_result_cancelled" | "tool_result_policy_stopped" | "tool_result_incomplete" => {
            let effect_outcome =
                tool_effect_outcome(optional_str(operation, "effectOutcome"), case_name)?;
            let started_at = required_u64(operation, "startedAtUnixMs")?;
            let completed_at = required_u64(operation, "completedAtUnixMs")?;
            match op {
                "tool_result_cancelled" => {
                    let result = ToolResult::cancelled(tool_call_id, started_at, completed_at)
                        .with_effect_outcome(effect_outcome);
                    Ok(ToolResultEvent::cancelled(tool_call_id, sequence, result))
                }
                "tool_result_policy_stopped" => {
                    let raw_error = operation
                        .get("error")
                        .and_then(Value::as_object)
                        .ok_or_else(|| {
                            format!("{case_name}: policy stopped error must be an object")
                        })?;
                    let result = ToolResult::policy_stopped(
                        tool_call_id,
                        BlockError::new(
                            required_str_object(raw_error, "code")?,
                            ErrorCategory::Policy,
                            required_str_object(raw_error, "message")?,
                            false,
                        ),
                        started_at,
                        completed_at,
                    )
                    .with_effect_outcome(effect_outcome);
                    Ok(ToolResultEvent::policy_stopped(
                        tool_call_id,
                        sequence,
                        result,
                    ))
                }
                "tool_result_incomplete" => {
                    let result = ToolResult::incomplete(tool_call_id, started_at, completed_at)
                        .with_effect_outcome(effect_outcome);
                    Ok(ToolResultEvent::incomplete(tool_call_id, sequence, result))
                }
                _ => unreachable!(),
            }
        }
        "tool_result_delta" | "tool_result_completed" => {
            let output = parse_content_parts(operation, case_name)?;
            if op == "tool_result_delta" {
                Ok(ToolResultEvent::delta(tool_call_id, sequence, output))
            } else {
                let effect_outcome =
                    tool_effect_outcome(optional_str(operation, "effectOutcome"), case_name)?;
                let result = ToolResult::completed(
                    tool_call_id,
                    output,
                    required_u64(operation, "startedAtUnixMs")?,
                    required_u64(operation, "completedAtUnixMs")?,
                )
                .map_err(|error| format!("{case_name}: {error:?}"))?
                .with_effect_outcome(effect_outcome);
                Ok(ToolResultEvent::completed(tool_call_id, sequence, result))
            }
        }
        _ => Err(format!("{case_name}: unknown tool result operation {op}")),
    }
}

fn parse_content_parts(operation: &Value, case_name: &str) -> Result<Vec<ContentPart>, String> {
    let raw_output = operation
        .get("output")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{case_name}: tool result output must be an array"))?;
    raw_output
        .iter()
        .enumerate()
        .map(|(index, raw_part)| {
            let mut part = match optional_str(raw_part, "kind").unwrap_or("text") {
                "text" => ContentPart::text(required_str(raw_part, "text")?),
                "json" => ContentPart::json(
                    raw_part
                        .get("data")
                        .cloned()
                        .ok_or_else(|| format!("{case_name}: output part {index} missing data"))?,
                ),
                other => {
                    return Err(format!("{case_name}: unsupported output part kind {other}"));
                }
            };
            if let Some(metadata) = raw_part.get("metadata") {
                for (key, value) in metadata
                    .as_object()
                    .ok_or_else(|| format!("{case_name}: output metadata must be an object"))?
                {
                    part = part.with_metadata(key, value.clone());
                }
            }
            Ok(part)
        })
        .collect()
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
        "replace" => OutputPolicyDecision::replace(
            decision_id,
            accepted_through_sequence,
            Vec::new(),
            input_digest,
        ),
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
    if let Some(value) = optional_str(operation, "providerCancellation") {
        decision = decision.with_provider_cancellation(provider_cancellation(value)?);
    }
    if let Some(value) = optional_str(operation, "draftDisposition") {
        decision = decision.with_draft_disposition(draft_disposition(value)?);
    }
    if let Some(value) = optional_str(operation, "pendingToolCalls") {
        decision = decision.with_pending_tool_calls(pending_tool_calls(value)?);
    }
    if let Some(value) = optional_u64(operation, "evaluatedAtUnixMs") {
        decision = decision.evaluated_at_unix_ms(value);
    }
    Ok(decision)
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

fn optional_u64(value: &Value, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}

fn optional_bool(value: &Value, key: &str) -> Option<bool> {
    value.get(key).and_then(Value::as_bool)
}

fn required_str_object<'a>(value: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing required string field {key}"))
}

fn optional_str_object<'a>(value: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn string_array(value: &Value, key: &str) -> Result<Option<Vec<String>>, String> {
    let Some(values) = value.get(key) else {
        return Ok(None);
    };
    values
        .as_array()
        .ok_or_else(|| format!("{key} must be an array"))?
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

fn tool_effect_outcome(value: Option<&str>, case_name: &str) -> Result<ToolEffectOutcome, String> {
    match value.unwrap_or("unknown") {
        "no_external_effect" => Ok(ToolEffectOutcome::NoExternalEffect),
        "committed" => Ok(ToolEffectOutcome::Committed),
        "not_committed" => Ok(ToolEffectOutcome::NotCommitted),
        "unknown" => Ok(ToolEffectOutcome::Unknown),
        other => Err(format!("{case_name}: unknown effect outcome {other}")),
    }
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
