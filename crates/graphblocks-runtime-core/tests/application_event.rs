use graphblocks_runtime_core::application_event::{
    ApplicationCommand, ApplicationCommandKind, ApplicationCommandMetadata, ApplicationEvent,
    ApplicationEventError, ApplicationEventKind, ApplicationEventMetadata,
    ApplicationProtocolCapabilities, ApplicationProtocolEvent, ApplicationProtocolEventKind,
    ApplicationProtocolEventMetadata, ApplicationProtocolLog,
};
use graphblocks_runtime_core::output_policy::{GenerationChunk, OutputPolicyDecision};
use serde_json::json;

fn metadata() -> ApplicationEventMetadata {
    ApplicationEventMetadata {
        event_id: "event-1".to_string(),
        run_id: "run-1".to_string(),
        response_id: "response-1".to_string(),
        turn_id: Some("turn-1".to_string()),
        sequence: 7,
        release_id: "release-1".to_string(),
        policy_snapshot_id: "policy-1".to_string(),
        occurred_at_unix_ms: 1_700_000,
    }
}

#[test]
fn standard_event_names_match_the_tool_and_output_policy_contract() {
    let names = [
        ApplicationEventKind::ToolCallProposed.as_str(),
        ApplicationEventKind::ToolCallArgumentsDelta.as_str(),
        ApplicationEventKind::ToolCallArgumentsCompleted.as_str(),
        ApplicationEventKind::ToolCallValidated.as_str(),
        ApplicationEventKind::ToolCallPolicyEvaluated.as_str(),
        ApplicationEventKind::ToolCallApprovalRequested.as_str(),
        ApplicationEventKind::ToolCallAdmitted.as_str(),
        ApplicationEventKind::ToolCallStarted.as_str(),
        ApplicationEventKind::ToolCallCompleted.as_str(),
        ApplicationEventKind::ToolCallFailed.as_str(),
        ApplicationEventKind::ToolCallDenied.as_str(),
        ApplicationEventKind::ToolCallCancelled.as_str(),
        ApplicationEventKind::ToolCallPolicyStopped.as_str(),
        ApplicationEventKind::OutputPolicyEvaluationStarted.as_str(),
        ApplicationEventKind::OutputPolicyAllowed.as_str(),
        ApplicationEventKind::OutputPolicyHeld.as_str(),
        ApplicationEventKind::OutputPolicyRedacted.as_str(),
        ApplicationEventKind::OutputPolicyReplaced.as_str(),
        ApplicationEventKind::OutputPolicyViolationDetected.as_str(),
        ApplicationEventKind::OutputCutoff.as_str(),
        ApplicationEventKind::AssistantIncomplete.as_str(),
        ApplicationEventKind::AssistantRetracted.as_str(),
    ];

    assert_eq!(
        names,
        [
            "ToolCallProposed",
            "ToolCallArgumentsDelta",
            "ToolCallArgumentsCompleted",
            "ToolCallValidated",
            "ToolCallPolicyEvaluated",
            "ToolCallApprovalRequested",
            "ToolCallAdmitted",
            "ToolCallStarted",
            "ToolCallCompleted",
            "ToolCallFailed",
            "ToolCallDenied",
            "ToolCallCancelled",
            "ToolCallPolicyStopped",
            "OutputPolicyEvaluationStarted",
            "OutputPolicyAllowed",
            "OutputPolicyHeld",
            "OutputPolicyRedacted",
            "OutputPolicyReplaced",
            "OutputPolicyViolationDetected",
            "OutputCutoff",
            "AssistantIncomplete",
            "AssistantRetracted"
        ]
    );
}

#[test]
fn tool_events_carry_tool_call_id_and_required_envelope_fields() {
    let event = ApplicationEvent::tool(
        ApplicationEventKind::ToolCallCompleted,
        metadata(),
        "tool-call-1",
        json!({"status": "completed"}),
    )
    .expect("tool event is valid");

    assert_eq!(event.kind, ApplicationEventKind::ToolCallCompleted);
    assert_eq!(event.tool_call_id.as_deref(), Some("tool-call-1"));
    assert_eq!(event.metadata.event_id, "event-1");
    assert_eq!(event.metadata.run_id, "run-1");
    assert_eq!(event.metadata.response_id, "response-1");
    assert_eq!(event.metadata.turn_id.as_deref(), Some("turn-1"));
    assert_eq!(event.metadata.sequence, 7);
    assert_eq!(event.metadata.release_id, "release-1");
    assert_eq!(event.metadata.policy_snapshot_id, "policy-1");
    assert_eq!(event.metadata.occurred_at_unix_ms, 1_700_000);
    assert_eq!(event.payload, json!({"status": "completed"}));
}

#[test]
fn tool_events_cannot_be_created_without_tool_call_id() {
    let error = ApplicationEvent::new(
        ApplicationEventKind::ToolCallStarted,
        metadata(),
        json!({"status": "running"}),
    )
    .expect_err("tool event requires a tool_call_id");

    assert_eq!(
        error,
        ApplicationEventError::ToolEventRequiresToolCallId {
            kind: ApplicationEventKind::ToolCallStarted
        }
    );
}

#[test]
fn non_tool_events_reject_tool_event_constructor() {
    let error = ApplicationEvent::tool(
        ApplicationEventKind::OutputCutoff,
        metadata(),
        "tool-call-1",
        json!({"terminal_reason": "policy_denied"}),
    )
    .expect_err("non-tool event cannot carry tool_call_id through tool constructor");

    assert_eq!(
        error,
        ApplicationEventError::NotToolEvent {
            kind: ApplicationEventKind::OutputCutoff
        }
    );
}

#[test]
fn output_policy_decision_event_maps_disposition_and_metadata_payload() {
    let decision = OutputPolicyDecision::redact(
        "decision-redact",
        Some(4),
        [GenerationChunk::text(
            "stream-1",
            "response-1",
            4,
            "[redacted]",
        )],
        "sha256:redact",
    )
    .with_reason_codes(["pii.detected"])
    .with_policy_refs(["policy/output-standard"])
    .evaluated_at_unix_ms(1_699_995);

    let event = ApplicationEvent::output_policy_decision(metadata(), &decision)
        .expect("output policy decision event is valid");

    assert_eq!(event.kind, ApplicationEventKind::OutputPolicyRedacted);
    assert_eq!(event.tool_call_id, None);
    assert_eq!(
        event.payload,
        json!({
            "decision_id": "decision-redact",
            "disposition": "redact",
            "accepted_through_sequence": 4,
            "reason_codes": ["pii.detected"],
            "policy_refs": ["policy/output-standard"],
            "evaluated_at_unix_ms": 1_699_995,
            "input_digest": "sha256:redact",
            "replacement_chunk_count": 1,
            "redaction_count": 0,
        })
    );
}

#[test]
fn output_policy_termination_decision_maps_to_violation_event() {
    let decision = OutputPolicyDecision::abort_response("decision-abort", "sha256:abort")
        .with_reason_codes(["policy.denied"]);

    let event = ApplicationEvent::output_policy_decision(metadata(), &decision)
        .expect("output policy violation event is valid");

    assert_eq!(
        event.kind,
        ApplicationEventKind::OutputPolicyViolationDetected
    );
    assert_eq!(
        event.payload.get("disposition"),
        Some(&json!("abort_response"))
    );
    assert_eq!(
        event.payload.get("reason_codes"),
        Some(&json!(["policy.denied"]))
    );
}

fn command_metadata() -> ApplicationCommandMetadata {
    ApplicationCommandMetadata {
        command_id: "command-1".to_owned(),
        protocol_version: "graphblocks.app.v1".to_owned(),
        run_id: "run-1".to_owned(),
        turn_id: Some("turn-1".to_owned()),
        sequence: 3,
        idempotency_key: Some("idem-1".to_owned()),
        issued_at_unix_ms: 1_700_100,
    }
}

fn protocol_event_metadata(
    event_id: &str,
    sequence: u64,
    cursor: &str,
) -> ApplicationProtocolEventMetadata {
    ApplicationProtocolEventMetadata {
        event_id: event_id.to_owned(),
        protocol_version: "graphblocks.app.v1".to_owned(),
        run_id: "run-1".to_owned(),
        turn_id: Some("turn-1".to_owned()),
        sequence,
        cursor: Some(cursor.to_owned()),
        occurred_at_unix_ms: 1_700_200 + sequence,
    }
}

#[test]
fn application_command_names_match_the_client_protocol() {
    let names = [
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
    ];

    assert_eq!(
        names,
        [
            "InvokeGraph",
            "CancelRun",
            "SubmitInput",
            "ApproveEffect",
            "DenyEffect",
            "SubmitReview",
            "RequestBudgetExtension",
            "ApplyPolicyOverride",
            "ResumeInterrupt",
            "SelectCandidate",
            "OpenArtifact",
            "SetBreakpoint",
            "RequestSnapshot"
        ]
    );
}

#[test]
fn application_command_preserves_common_envelope_and_payload() {
    let command = ApplicationCommand::new(
        ApplicationCommandKind::ApproveEffect,
        command_metadata(),
        json!({"tool_call_id": "tool-1"}),
    )
    .expect("command is valid");

    assert_eq!(command.kind, ApplicationCommandKind::ApproveEffect);
    assert_eq!(command.metadata.command_id, "command-1");
    assert_eq!(command.metadata.protocol_version, "graphblocks.app.v1");
    assert_eq!(command.metadata.run_id, "run-1");
    assert_eq!(command.metadata.turn_id.as_deref(), Some("turn-1"));
    assert_eq!(command.metadata.idempotency_key.as_deref(), Some("idem-1"));
    assert_eq!(command.payload, json!({"tool_call_id": "tool-1"}));
}

#[test]
fn application_protocol_event_names_and_envelope_match_client_protocol() {
    let names = [
        ApplicationProtocolEventKind::RunStarted.as_str(),
        ApplicationProtocolEventKind::TurnStarted.as_str(),
        ApplicationProtocolEventKind::ContextReady.as_str(),
        ApplicationProtocolEventKind::AssistantDraftStarted.as_str(),
        ApplicationProtocolEventKind::AssistantDraftDelta.as_str(),
        ApplicationProtocolEventKind::AssistantCommitted.as_str(),
        ApplicationProtocolEventKind::AssistantRetracted.as_str(),
        ApplicationProtocolEventKind::ToolStarted.as_str(),
        ApplicationProtocolEventKind::ToolCompleted.as_str(),
        ApplicationProtocolEventKind::ApprovalRequested.as_str(),
        ApplicationProtocolEventKind::ReviewRequested.as_str(),
        ApplicationProtocolEventKind::BudgetConstrained.as_str(),
        ApplicationProtocolEventKind::BudgetExhausted.as_str(),
        ApplicationProtocolEventKind::BudgetExtensionRequested.as_str(),
        ApplicationProtocolEventKind::BudgetExtensionGranted.as_str(),
        ApplicationProtocolEventKind::PolicyDecisionRequired.as_str(),
        ApplicationProtocolEventKind::ExecutionDegraded.as_str(),
        ApplicationProtocolEventKind::FilePatchPreview.as_str(),
        ApplicationProtocolEventKind::JobProgress.as_str(),
        ApplicationProtocolEventKind::ArtifactReady.as_str(),
        ApplicationProtocolEventKind::StateSnapshot.as_str(),
        ApplicationProtocolEventKind::RunCompleted.as_str(),
        ApplicationProtocolEventKind::RunFailed.as_str(),
        ApplicationProtocolEventKind::RunCancelled.as_str(),
    ];

    let event = ApplicationProtocolEvent::new(
        ApplicationProtocolEventKind::StateSnapshot,
        protocol_event_metadata("event-1", 5, "cursor-5"),
        json!({"revision": 2}),
    )
    .expect("event is valid");

    assert_eq!(names[0], "RunStarted");
    assert_eq!(names[23], "RunCancelled");
    assert_eq!(event.metadata.event_id, "event-1");
    assert_eq!(event.metadata.protocol_version, "graphblocks.app.v1");
    assert_eq!(event.metadata.cursor.as_deref(), Some("cursor-5"));
    assert_eq!(event.payload, json!({"revision": 2}));
}

#[test]
fn protocol_log_suppresses_duplicate_event_ids_and_replays_after_cursor() {
    let mut log = ApplicationProtocolLog::new();
    let first = ApplicationProtocolEvent::new(
        ApplicationProtocolEventKind::RunStarted,
        protocol_event_metadata("event-1", 1, "cursor-1"),
        json!({}),
    )
    .expect("event is valid");
    let second = ApplicationProtocolEvent::new(
        ApplicationProtocolEventKind::JobProgress,
        protocol_event_metadata("event-2", 2, "cursor-2"),
        json!({"done": 1, "total": 2}),
    )
    .expect("event is valid");

    assert!(log.append(first.clone()).expect("append succeeds"));
    assert!(!log.append(first).expect("duplicate is suppressed"));
    assert!(log.append(second).expect("append succeeds"));

    let replay = log.replay_after(Some("cursor-1"), 10);

    assert_eq!(log.len(), 2);
    assert_eq!(replay.len(), 1);
    assert_eq!(replay[0].metadata.event_id, "event-2");
}

#[test]
fn protocol_capability_negotiation_intersects_commands_and_events() {
    let server = ApplicationProtocolCapabilities::new("graphblocks.app.v1")
        .with_commands([
            ApplicationCommandKind::InvokeGraph,
            ApplicationCommandKind::CancelRun,
        ])
        .with_events([
            ApplicationProtocolEventKind::RunStarted,
            ApplicationProtocolEventKind::RunCompleted,
        ]);
    let client = ApplicationProtocolCapabilities::new("graphblocks.app.v1")
        .with_commands([
            ApplicationCommandKind::CancelRun,
            ApplicationCommandKind::OpenArtifact,
        ])
        .with_events([
            ApplicationProtocolEventKind::RunCompleted,
            ApplicationProtocolEventKind::ArtifactReady,
        ]);

    let negotiated = server.negotiate(&client).expect("versions match");

    assert_eq!(
        negotiated.commands,
        [ApplicationCommandKind::CancelRun].into()
    );
    assert_eq!(
        negotiated.events,
        [ApplicationProtocolEventKind::RunCompleted].into()
    );
}
