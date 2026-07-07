use graphblocks_runtime_core::application_event::{
    ApplicationCommand, ApplicationCommandKind, ApplicationCommandMetadata,
    ApplicationProtocolCapabilities, ApplicationProtocolError, ApplicationProtocolEvent,
    ApplicationProtocolEventKind, ApplicationProtocolEventMetadata, ApplicationProtocolLog,
    ApplicationProtocolStreamState,
};
use serde_json::{Value, json};

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("application-protocol TCK case missing string {key}"))
}

fn command_kind(value: &str) -> Result<ApplicationCommandKind, String> {
    match value {
        "InvokeGraph" => Ok(ApplicationCommandKind::InvokeGraph),
        "GetRunStatus" => Ok(ApplicationCommandKind::GetRunStatus),
        "ListRuns" => Ok(ApplicationCommandKind::ListRuns),
        "AttachToRun" => Ok(ApplicationCommandKind::AttachToRun),
        "DetachFromRun" => Ok(ApplicationCommandKind::DetachFromRun),
        "SubscribeEvents" => Ok(ApplicationCommandKind::SubscribeEvents),
        "UnsubscribeEvents" => Ok(ApplicationCommandKind::UnsubscribeEvents),
        "AckEvent" => Ok(ApplicationCommandKind::AckEvent),
        "RegisterCallback" => Ok(ApplicationCommandKind::RegisterCallback),
        "RevokeCallback" => Ok(ApplicationCommandKind::RevokeCallback),
        "SubmitAsyncCallback" => Ok(ApplicationCommandKind::SubmitAsyncCallback),
        "PauseRun" => Ok(ApplicationCommandKind::PauseRun),
        "ResumeRun" => Ok(ApplicationCommandKind::ResumeRun),
        "CancelRun" => Ok(ApplicationCommandKind::CancelRun),
        "ExpireRun" => Ok(ApplicationCommandKind::ExpireRun),
        "SubmitInput" => Ok(ApplicationCommandKind::SubmitInput),
        "ApproveEffect" => Ok(ApplicationCommandKind::ApproveEffect),
        "DenyEffect" => Ok(ApplicationCommandKind::DenyEffect),
        "SubmitReview" => Ok(ApplicationCommandKind::SubmitReview),
        "RequestBudgetExtension" => Ok(ApplicationCommandKind::RequestBudgetExtension),
        "ApplyPolicyOverride" => Ok(ApplicationCommandKind::ApplyPolicyOverride),
        "ResumeInterrupt" => Ok(ApplicationCommandKind::ResumeInterrupt),
        "SelectCandidate" => Ok(ApplicationCommandKind::SelectCandidate),
        "OpenArtifact" => Ok(ApplicationCommandKind::OpenArtifact),
        "SetBreakpoint" => Ok(ApplicationCommandKind::SetBreakpoint),
        "RequestSnapshot" => Ok(ApplicationCommandKind::RequestSnapshot),
        "RedriveCallbackDelivery" => Ok(ApplicationCommandKind::RedriveCallbackDelivery),
        "MoveCallbackToDeadLetter" => Ok(ApplicationCommandKind::MoveCallbackToDeadLetter),
        other => Err(format!("unsupported application command kind {other:?}")),
    }
}

fn event_kind(value: &str) -> Result<ApplicationProtocolEventKind, String> {
    match value {
        "RunStarted" => Ok(ApplicationProtocolEventKind::RunStarted),
        "TurnStarted" => Ok(ApplicationProtocolEventKind::TurnStarted),
        "ContextReady" => Ok(ApplicationProtocolEventKind::ContextReady),
        "AssistantDraftStarted" => Ok(ApplicationProtocolEventKind::AssistantDraftStarted),
        "AssistantDraftDelta" => Ok(ApplicationProtocolEventKind::AssistantDraftDelta),
        "AssistantCommitted" => Ok(ApplicationProtocolEventKind::AssistantCommitted),
        "AssistantIncomplete" => Ok(ApplicationProtocolEventKind::AssistantIncomplete),
        "AssistantRetracted" => Ok(ApplicationProtocolEventKind::AssistantRetracted),
        "ToolStarted" => Ok(ApplicationProtocolEventKind::ToolStarted),
        "ToolCompleted" => Ok(ApplicationProtocolEventKind::ToolCompleted),
        "ToolCallApprovalRequested" => Ok(ApplicationProtocolEventKind::ToolCallApprovalRequested),
        "ApprovalRequested" => Ok(ApplicationProtocolEventKind::ApprovalRequested),
        "ReviewRequested" => Ok(ApplicationProtocolEventKind::ReviewRequested),
        "BudgetConstrained" => Ok(ApplicationProtocolEventKind::BudgetConstrained),
        "BudgetExhausted" => Ok(ApplicationProtocolEventKind::BudgetExhausted),
        "BudgetExtensionRequested" => Ok(ApplicationProtocolEventKind::BudgetExtensionRequested),
        "BudgetExtensionGranted" => Ok(ApplicationProtocolEventKind::BudgetExtensionGranted),
        "PolicyDecisionRequired" => Ok(ApplicationProtocolEventKind::PolicyDecisionRequired),
        "ExecutionDegraded" => Ok(ApplicationProtocolEventKind::ExecutionDegraded),
        "OutputCutoff" => Ok(ApplicationProtocolEventKind::OutputCutoff),
        "FilePatchPreview" => Ok(ApplicationProtocolEventKind::FilePatchPreview),
        "JobProgress" => Ok(ApplicationProtocolEventKind::JobProgress),
        "ArtifactReady" => Ok(ApplicationProtocolEventKind::ArtifactReady),
        "StateSnapshot" => Ok(ApplicationProtocolEventKind::StateSnapshot),
        "RunCompleted" => Ok(ApplicationProtocolEventKind::RunCompleted),
        "RunFailed" => Ok(ApplicationProtocolEventKind::RunFailed),
        "RunCancelled" => Ok(ApplicationProtocolEventKind::RunCancelled),
        "RunPolicyStopped" => Ok(ApplicationProtocolEventKind::RunPolicyStopped),
        "RunExpired" => Ok(ApplicationProtocolEventKind::RunExpired),
        "AsyncOperationStarted" => Ok(ApplicationProtocolEventKind::AsyncOperationStarted),
        "AsyncOperationWaitingCallback" => {
            Ok(ApplicationProtocolEventKind::AsyncOperationWaitingCallback)
        }
        "AsyncOperationPolling" => Ok(ApplicationProtocolEventKind::AsyncOperationPolling),
        "AsyncOperationCompleted" => Ok(ApplicationProtocolEventKind::AsyncOperationCompleted),
        "AsyncOperationFailed" => Ok(ApplicationProtocolEventKind::AsyncOperationFailed),
        "AsyncOperationCancelled" => Ok(ApplicationProtocolEventKind::AsyncOperationCancelled),
        "AsyncOperationExpired" => Ok(ApplicationProtocolEventKind::AsyncOperationExpired),
        "ExternalCallbackReceived" => Ok(ApplicationProtocolEventKind::ExternalCallbackReceived),
        "ExternalCallbackRejected" => Ok(ApplicationProtocolEventKind::ExternalCallbackRejected),
        "LateExternalCallbackReceived" => {
            Ok(ApplicationProtocolEventKind::LateExternalCallbackReceived)
        }
        "RunResuming" => Ok(ApplicationProtocolEventKind::RunResuming),
        "RunPausedBudget" => Ok(ApplicationProtocolEventKind::RunPausedBudget),
        "RunPausedPolicy" => Ok(ApplicationProtocolEventKind::RunPausedPolicy),
        "RunPausedOperator" => Ok(ApplicationProtocolEventKind::RunPausedOperator),
        other => Err(format!(
            "unsupported application protocol event kind {other:?}"
        )),
    }
}

fn strings(value: &Value, key: &str) -> Result<Vec<String>, String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("application-protocol TCK case missing array {key}"))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(ToOwned::to_owned)
                .ok_or_else(|| format!("application-protocol TCK {key} entry must be a string"))
        })
        .collect()
}

fn run_case(case: &Value) -> Result<Value, String> {
    let kind = required_str(case, "kind")?;
    let protocol_error_code = |error: &ApplicationProtocolError| match error {
        ApplicationProtocolError::InvalidPayload { .. } => "invalid_payload",
        ApplicationProtocolError::InvalidPayloadKey { .. } => "invalid_payload_key",
        ApplicationProtocolError::EmptyMetadataField {
            field: "protocol_version",
        } => "empty_protocol_version",
        ApplicationProtocolError::ProtocolVersionMismatch { .. } => "protocol_version_mismatch",
        ApplicationProtocolError::EmptyCommandId => "empty_command_id",
        ApplicationProtocolError::EmptyEventId => "empty_event_id",
        ApplicationProtocolError::EmptyMetadataField { .. } => "empty_metadata_field",
        ApplicationProtocolError::InvalidToolResultEvent { .. } => "invalid_tool_result_event",
        ApplicationProtocolError::DuplicateEventIdConflict { .. } => "duplicate_event_id_conflict",
        ApplicationProtocolError::DuplicateCursorConflict { .. } => "duplicate_cursor_conflict",
        ApplicationProtocolError::NonMonotonicSequence { .. } => "non_monotonic_sequence",
        ApplicationProtocolError::RunMismatch { .. } => "run_mismatch",
    };
    match kind {
        "kind_sets" => Ok(json!({
            "commands": [
                ApplicationCommandKind::InvokeGraph.as_str(),
                ApplicationCommandKind::CancelRun.as_str(),
                ApplicationCommandKind::SubmitInput.as_str(),
                ApplicationCommandKind::ApproveEffect.as_str(),
                ApplicationCommandKind::DenyEffect.as_str(),
                ApplicationCommandKind::SubmitReview.as_str(),
                ApplicationCommandKind::RequestBudgetExtension.as_str(),
                ApplicationCommandKind::ApplyPolicyOverride.as_str(),
                ApplicationCommandKind::ResumeInterrupt.as_str(),
                ApplicationCommandKind::SelectCandidate.as_str(),
                ApplicationCommandKind::OpenArtifact.as_str(),
                ApplicationCommandKind::SetBreakpoint.as_str(),
                ApplicationCommandKind::RequestSnapshot.as_str(),
                ApplicationCommandKind::GetRunStatus.as_str(),
                ApplicationCommandKind::ListRuns.as_str(),
                ApplicationCommandKind::AttachToRun.as_str(),
                ApplicationCommandKind::DetachFromRun.as_str(),
                ApplicationCommandKind::SubscribeEvents.as_str(),
                ApplicationCommandKind::UnsubscribeEvents.as_str(),
                ApplicationCommandKind::AckEvent.as_str(),
                ApplicationCommandKind::RegisterCallback.as_str(),
                ApplicationCommandKind::RevokeCallback.as_str(),
                ApplicationCommandKind::SubmitAsyncCallback.as_str(),
                ApplicationCommandKind::PauseRun.as_str(),
                ApplicationCommandKind::ResumeRun.as_str(),
                ApplicationCommandKind::ExpireRun.as_str(),
                ApplicationCommandKind::RedriveCallbackDelivery.as_str(),
                ApplicationCommandKind::MoveCallbackToDeadLetter.as_str(),
            ],
            "events": [
                ApplicationProtocolEventKind::RunStarted.as_str(),
                ApplicationProtocolEventKind::TurnStarted.as_str(),
                ApplicationProtocolEventKind::ContextReady.as_str(),
                ApplicationProtocolEventKind::AssistantDraftStarted.as_str(),
                ApplicationProtocolEventKind::AssistantDraftDelta.as_str(),
                ApplicationProtocolEventKind::AssistantCommitted.as_str(),
                ApplicationProtocolEventKind::AssistantIncomplete.as_str(),
                ApplicationProtocolEventKind::AssistantRetracted.as_str(),
                ApplicationProtocolEventKind::ToolStarted.as_str(),
                ApplicationProtocolEventKind::ToolCompleted.as_str(),
                ApplicationProtocolEventKind::ToolCallApprovalRequested.as_str(),
                ApplicationProtocolEventKind::ApprovalRequested.as_str(),
                ApplicationProtocolEventKind::ReviewRequested.as_str(),
                ApplicationProtocolEventKind::BudgetConstrained.as_str(),
                ApplicationProtocolEventKind::BudgetExhausted.as_str(),
                ApplicationProtocolEventKind::BudgetExtensionRequested.as_str(),
                ApplicationProtocolEventKind::BudgetExtensionGranted.as_str(),
                ApplicationProtocolEventKind::PolicyDecisionRequired.as_str(),
                ApplicationProtocolEventKind::ExecutionDegraded.as_str(),
                ApplicationProtocolEventKind::OutputCutoff.as_str(),
                ApplicationProtocolEventKind::FilePatchPreview.as_str(),
                ApplicationProtocolEventKind::JobProgress.as_str(),
                ApplicationProtocolEventKind::ArtifactReady.as_str(),
                ApplicationProtocolEventKind::StateSnapshot.as_str(),
                ApplicationProtocolEventKind::RunCompleted.as_str(),
                ApplicationProtocolEventKind::RunFailed.as_str(),
                ApplicationProtocolEventKind::RunCancelled.as_str(),
                ApplicationProtocolEventKind::RunPolicyStopped.as_str(),
                ApplicationProtocolEventKind::RunExpired.as_str(),
                ApplicationProtocolEventKind::AsyncOperationStarted.as_str(),
                ApplicationProtocolEventKind::AsyncOperationWaitingCallback.as_str(),
                ApplicationProtocolEventKind::AsyncOperationPolling.as_str(),
                ApplicationProtocolEventKind::AsyncOperationCompleted.as_str(),
                ApplicationProtocolEventKind::AsyncOperationFailed.as_str(),
                ApplicationProtocolEventKind::AsyncOperationCancelled.as_str(),
                ApplicationProtocolEventKind::AsyncOperationExpired.as_str(),
                ApplicationProtocolEventKind::ExternalCallbackReceived.as_str(),
                ApplicationProtocolEventKind::ExternalCallbackRejected.as_str(),
                ApplicationProtocolEventKind::LateExternalCallbackReceived.as_str(),
                ApplicationProtocolEventKind::RunResuming.as_str(),
                ApplicationProtocolEventKind::RunPausedBudget.as_str(),
                ApplicationProtocolEventKind::RunPausedPolicy.as_str(),
                ApplicationProtocolEventKind::RunPausedOperator.as_str(),
            ],
        })),
        "command_envelope" | "command_envelope_error" => {
            let metadata = case
                .get("metadata")
                .and_then(Value::as_object)
                .ok_or_else(|| "command_envelope case missing metadata".to_owned())?;
            let command_result = ApplicationCommand::new(
                command_kind(required_str(case, "commandKind")?)?,
                ApplicationCommandMetadata {
                    command_id: required_str(&case["metadata"], "commandId")?.to_owned(),
                    protocol_version: required_str(&case["metadata"], "protocolVersion")?
                        .to_owned(),
                    run_id: required_str(&case["metadata"], "runId")?.to_owned(),
                    turn_id: metadata
                        .get("turnId")
                        .and_then(Value::as_str)
                        .map(ToOwned::to_owned),
                    sequence: metadata
                        .get("sequence")
                        .and_then(Value::as_u64)
                        .unwrap_or(0),
                    idempotency_key: metadata
                        .get("idempotencyKey")
                        .and_then(Value::as_str)
                        .map(ToOwned::to_owned),
                    issued_at_unix_ms: metadata
                        .get("issuedAtUnixMs")
                        .and_then(Value::as_u64)
                        .unwrap_or(0),
                },
                case.get("payload").cloned().unwrap_or_else(|| json!({})),
            );
            if kind == "command_envelope_error" {
                return Ok(json!({
                    "error": command_result
                        .as_ref()
                        .err()
                        .map(protocol_error_code)
                        .unwrap_or("none"),
                }));
            }
            let command = command_result.map_err(|error| error.to_string())?;
            Ok(json!({
                "kind": command.kind.as_str(),
                "commandId": command.metadata.command_id,
                "protocolVersion": command.metadata.protocol_version,
                "runId": command.metadata.run_id,
                "turnId": command.metadata.turn_id,
                "sequence": command.metadata.sequence,
                "idempotencyKey": command.metadata.idempotency_key,
                "payload": command.payload,
            }))
        }
        "event_envelope" | "event_envelope_error" => {
            let metadata = case
                .get("metadata")
                .and_then(Value::as_object)
                .ok_or_else(|| "event_envelope case missing metadata".to_owned())?;
            let event_result = ApplicationProtocolEvent::new(
                event_kind(required_str(case, "eventKind")?)?,
                ApplicationProtocolEventMetadata {
                    event_id: required_str(&case["metadata"], "eventId")?.to_owned(),
                    protocol_version: required_str(&case["metadata"], "protocolVersion")?
                        .to_owned(),
                    run_id: required_str(&case["metadata"], "runId")?.to_owned(),
                    release_id: required_str(&case["metadata"], "releaseId")?.to_owned(),
                    turn_id: metadata
                        .get("turnId")
                        .and_then(Value::as_str)
                        .map(ToOwned::to_owned),
                    operation_id: metadata
                        .get("operationId")
                        .and_then(Value::as_str)
                        .map(ToOwned::to_owned),
                    sequence: metadata
                        .get("sequence")
                        .and_then(Value::as_u64)
                        .unwrap_or(0),
                    cursor: metadata
                        .get("cursor")
                        .and_then(Value::as_str)
                        .map(ToOwned::to_owned),
                    occurred_at_unix_ms: metadata
                        .get("occurredAtUnixMs")
                        .and_then(Value::as_u64)
                        .unwrap_or(0),
                },
                case.get("payload").cloned().unwrap_or_else(|| json!({})),
            );
            if kind == "event_envelope_error" {
                return Ok(json!({
                    "error": event_result
                        .as_ref()
                        .err()
                        .map(protocol_error_code)
                        .unwrap_or("none"),
                }));
            }
            let event = event_result.map_err(|error| error.to_string())?;
            let mut observed = json!({
                "kind": event.kind.as_str(),
                "eventId": event.metadata.event_id,
                "protocolVersion": event.metadata.protocol_version,
                "runId": event.metadata.run_id,
                "releaseId": event.metadata.release_id,
                "turnId": event.metadata.turn_id,
                "operationId": event.metadata.operation_id,
                "sequence": event.metadata.sequence,
                "cursor": event.metadata.cursor,
                "payload": event.payload,
            });
            if event.metadata.operation_id.is_none() {
                observed
                    .as_object_mut()
                    .expect("observed event envelope is an object")
                    .remove("operationId");
            }
            Ok(observed)
        }
        "protocol_log" => {
            let operations = case
                .get("operations")
                .and_then(Value::as_array)
                .ok_or_else(|| "protocol_log case missing operations".to_owned())?;
            let mut log = ApplicationProtocolLog::new();
            let mut append_results = Vec::new();
            let mut append_errors = Vec::new();
            for (operation_index, operation) in operations.iter().enumerate() {
                let metadata = operation
                    .get("metadata")
                    .and_then(Value::as_object)
                    .ok_or_else(|| {
                        format!("protocol_log operation {operation_index} missing metadata")
                    })?;
                let event = ApplicationProtocolEvent::new(
                    event_kind(required_str(operation, "eventKind")?)?,
                    ApplicationProtocolEventMetadata {
                        event_id: required_str(&operation["metadata"], "eventId")?.to_owned(),
                        protocol_version: required_str(&operation["metadata"], "protocolVersion")?
                            .to_owned(),
                        run_id: required_str(&operation["metadata"], "runId")?.to_owned(),
                        release_id: required_str(&operation["metadata"], "releaseId")?.to_owned(),
                        turn_id: metadata
                            .get("turnId")
                            .and_then(Value::as_str)
                            .map(ToOwned::to_owned),
                        operation_id: metadata
                            .get("operationId")
                            .and_then(Value::as_str)
                            .map(ToOwned::to_owned),
                        sequence: metadata
                            .get("sequence")
                            .and_then(Value::as_u64)
                            .unwrap_or(0),
                        cursor: metadata
                            .get("cursor")
                            .and_then(Value::as_str)
                            .map(ToOwned::to_owned),
                        occurred_at_unix_ms: metadata
                            .get("occurredAtUnixMs")
                            .and_then(Value::as_u64)
                            .unwrap_or(0),
                    },
                    operation
                        .get("payload")
                        .cloned()
                        .unwrap_or_else(|| json!({})),
                )
                .map_err(|error| error.to_string())?;
                let append_result = log.append(event);
                if let Some(expected_error) = operation.get("expectError").and_then(Value::as_str) {
                    match append_result {
                        Ok(_) => {
                            return Err(format!(
                                "protocol_log operation {operation_index} expected error {expected_error}"
                            ));
                        }
                        Err(error) => {
                            let observed_error = protocol_error_code(&error);
                            if observed_error != expected_error {
                                return Err(format!(
                                    "protocol_log operation {operation_index} error mismatch: expected {expected_error}, observed {observed_error}"
                                ));
                            }
                            append_results.push(false);
                            append_errors.push(observed_error);
                            continue;
                        }
                    }
                }
                let appended = append_result.map_err(|error| error.to_string())?;
                let expected_appended = operation
                    .get("expectAppended")
                    .and_then(Value::as_bool)
                    .unwrap_or(true);
                if appended != expected_appended {
                    return Err(format!(
                        "protocol_log operation {operation_index} append result mismatch"
                    ));
                }
                append_results.push(appended);
            }
            let replay_cursor = case
                .get("replayAfter")
                .or_else(|| case.get("replay_after"))
                .and_then(Value::as_str);
            let replay_limit = case
                .get("replayLimit")
                .or_else(|| case.get("replay_limit"))
                .and_then(Value::as_u64)
                .unwrap_or(100) as usize;
            let replay = log.replay_after(replay_cursor, replay_limit);
            Ok(json!({
                "eventIds": log
                    .replay_after(None, usize::MAX)
                    .iter()
                    .map(|event| event.metadata.event_id.as_str())
                    .collect::<Vec<_>>(),
                "appendResults": append_results,
                "appendErrors": append_errors,
                "replayEventIds": replay
                    .iter()
                    .map(|event| event.metadata.event_id.as_str())
                    .collect::<Vec<_>>(),
                "length": log.len(),
            }))
        }
        "stream_cutoff" => {
            let operations = case
                .get("operations")
                .and_then(Value::as_array)
                .ok_or_else(|| "stream_cutoff case missing operations".to_owned())?;
            let mut state = ApplicationProtocolStreamState::new();
            for (operation_index, operation) in operations.iter().enumerate() {
                let metadata = operation
                    .get("metadata")
                    .and_then(Value::as_object)
                    .ok_or_else(|| {
                        format!("stream_cutoff operation {operation_index} missing metadata")
                    })?;
                let event = ApplicationProtocolEvent::new(
                    event_kind(required_str(operation, "eventKind")?)?,
                    ApplicationProtocolEventMetadata {
                        event_id: required_str(&operation["metadata"], "eventId")?.to_owned(),
                        protocol_version: required_str(&operation["metadata"], "protocolVersion")?
                            .to_owned(),
                        run_id: required_str(&operation["metadata"], "runId")?.to_owned(),
                        release_id: required_str(&operation["metadata"], "releaseId")?.to_owned(),
                        turn_id: metadata
                            .get("turnId")
                            .and_then(Value::as_str)
                            .map(ToOwned::to_owned),
                        operation_id: metadata
                            .get("operationId")
                            .and_then(Value::as_str)
                            .map(ToOwned::to_owned),
                        sequence: metadata
                            .get("sequence")
                            .and_then(Value::as_u64)
                            .unwrap_or(0),
                        cursor: metadata
                            .get("cursor")
                            .and_then(Value::as_str)
                            .map(ToOwned::to_owned),
                        occurred_at_unix_ms: metadata
                            .get("occurredAtUnixMs")
                            .and_then(Value::as_u64)
                            .unwrap_or(0),
                    },
                    operation
                        .get("payload")
                        .cloned()
                        .unwrap_or_else(|| json!({})),
                )
                .map_err(|error| error.to_string())?;
                let expected_accepted = operation
                    .get("expectAccepted")
                    .and_then(Value::as_bool)
                    .unwrap_or(true);
                let accepted = state.accept(event).is_some();
                if accepted != expected_accepted {
                    return Err(format!(
                        "stream_cutoff operation {operation_index} acceptance mismatch"
                    ));
                }
            }
            let cutoff_response_id = state
                .accepted_events()
                .iter()
                .find(|event| event.kind == ApplicationProtocolEventKind::OutputCutoff)
                .and_then(|event| event.payload.get("response_id"))
                .and_then(Value::as_str);
            Ok(json!({
                "acceptedKinds": state
                    .accepted_events()
                    .iter()
                    .map(|event| event.kind.as_str())
                    .collect::<Vec<_>>(),
                "cutoffResponseId": cutoff_response_id,
                "cutoffLastClientDeliveredSequence": cutoff_response_id
                    .and_then(|response_id| state.cutoff_for_response(response_id)),
            }))
        }
        "capability_negotiation" | "capability_negotiation_error" => {
            let server = case
                .get("server")
                .ok_or_else(|| "capability_negotiation case missing server".to_owned())?;
            let client = case
                .get("client")
                .ok_or_else(|| "capability_negotiation case missing client".to_owned())?;
            let server_commands = strings(server, "commands")?
                .iter()
                .map(|item| command_kind(item))
                .collect::<Result<Vec<_>, _>>()?;
            let client_commands = strings(client, "commands")?
                .iter()
                .map(|item| command_kind(item))
                .collect::<Result<Vec<_>, _>>()?;
            let server_events = strings(server, "events")?
                .iter()
                .map(|item| event_kind(item))
                .collect::<Result<Vec<_>, _>>()?;
            let client_events = strings(client, "events")?
                .iter()
                .map(|item| event_kind(item))
                .collect::<Result<Vec<_>, _>>()?;
            let negotiated_result =
                ApplicationProtocolCapabilities::new(required_str(server, "protocolVersion")?)
                    .with_commands(server_commands)
                    .with_events(server_events)
                    .negotiate(
                        &ApplicationProtocolCapabilities::new(required_str(
                            client,
                            "protocolVersion",
                        )?)
                        .with_commands(client_commands)
                        .with_events(client_events),
                    );
            if kind == "capability_negotiation_error" {
                return Ok(json!({
                    "error": negotiated_result
                        .as_ref()
                        .err()
                        .map(protocol_error_code)
                        .unwrap_or("none"),
                }));
            }
            let negotiated = negotiated_result.map_err(|error| error.to_string())?;
            let mut commands = negotiated
                .commands
                .iter()
                .map(ApplicationCommandKind::as_str)
                .collect::<Vec<_>>();
            commands.sort_unstable();
            let mut events = negotiated
                .events
                .iter()
                .map(ApplicationProtocolEventKind::as_str)
                .collect::<Vec<_>>();
            events.sort_unstable();
            Ok(json!({
                "protocolVersion": negotiated.protocol_version,
                "commands": commands,
                "events": events,
            }))
        }
        other => Err(format!(
            "unsupported application-protocol TCK kind {other:?}"
        )),
    }
}

#[test]
fn rust_application_protocol_matches_shared_tck_cases() -> Result<(), String> {
    let cases: Value =
        serde_json::from_str(include_str!("../../../tck/application-protocol/cases.json"))
            .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "application-protocol TCK root must be an array".to_owned())?;

    for case in cases {
        let case_name = required_str(case, "name")?;
        let observed = run_case(case).map_err(|error| format!("{case_name}: {error}"))?;
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("application-protocol TCK case {case_name} missing expected"))?;
        for (key, expected_value) in expected {
            assert_eq!(
                observed.get(key).unwrap_or(&Value::Null),
                expected_value,
                "application-protocol TCK case {case_name} expected {key}"
            );
        }
    }

    Ok(())
}
