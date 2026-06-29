use graphblocks_runtime_core::application_event::{
    ApplicationEvent, ApplicationEventKind, ApplicationEventMetadata, ApplicationEventStreamState,
};
use graphblocks_runtime_core::output_policy::{
    DraftDisposition, DurableResult, OutputCutoff, TerminalReason,
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
            event_id: format!("{case_name}:{}", index + 1),
            run_id: optional_str(case, "runId").unwrap_or("run-1").to_owned(),
            response_id: response_id.to_owned(),
            turn_id: optional_str(operation, "turnId")
                .or_else(|| optional_str(case, "turnId"))
                .map(str::to_owned),
            sequence: (index + 1) as u64,
            release_id: optional_str(case, "releaseId")
                .unwrap_or("release-1")
                .to_owned(),
            policy_snapshot_id: optional_str(case, "policySnapshotId")
                .unwrap_or("policy-1")
                .to_owned(),
            occurred_at_unix_ms: optional_u64(operation, "occurredAtUnixMs").unwrap_or(1_700_000),
        };

        match op {
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
