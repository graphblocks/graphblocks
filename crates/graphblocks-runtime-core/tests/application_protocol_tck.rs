use graphblocks_runtime_core::application_event::{
    ApplicationCommand, ApplicationCommandKind, ApplicationCommandMetadata,
    ApplicationProtocolCapabilities, ApplicationProtocolEvent, ApplicationProtocolEventKind,
    ApplicationProtocolEventMetadata,
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
        "CancelRun" => Ok(ApplicationCommandKind::CancelRun),
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
            ],
        })),
        "command_envelope" => {
            let metadata = case
                .get("metadata")
                .and_then(Value::as_object)
                .ok_or_else(|| "command_envelope case missing metadata".to_owned())?;
            let command = ApplicationCommand::new(
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
            )
            .map_err(|error| error.to_string())?;
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
        "event_envelope" => {
            let metadata = case
                .get("metadata")
                .and_then(Value::as_object)
                .ok_or_else(|| "event_envelope case missing metadata".to_owned())?;
            let event = ApplicationProtocolEvent::new(
                event_kind(required_str(case, "eventKind")?)?,
                ApplicationProtocolEventMetadata {
                    event_id: required_str(&case["metadata"], "eventId")?.to_owned(),
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
            )
            .map_err(|error| error.to_string())?;
            Ok(json!({
                "kind": event.kind.as_str(),
                "eventId": event.metadata.event_id,
                "protocolVersion": event.metadata.protocol_version,
                "runId": event.metadata.run_id,
                "turnId": event.metadata.turn_id,
                "sequence": event.metadata.sequence,
                "cursor": event.metadata.cursor,
                "payload": event.payload,
            }))
        }
        "capability_negotiation" => {
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
            let negotiated =
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
                    )
                    .map_err(|error| error.to_string())?;
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
