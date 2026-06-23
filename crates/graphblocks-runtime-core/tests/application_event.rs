use graphblocks_runtime_core::application_event::{
    ApplicationCommand, ApplicationCommandKind, ApplicationCommandMetadata, ApplicationEvent,
    ApplicationEventError, ApplicationEventKind, ApplicationEventMetadata,
    ApplicationProtocolCapabilities, ApplicationProtocolEvent, ApplicationProtocolEventKind,
    ApplicationProtocolEventMetadata, ApplicationProtocolLog,
};
use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::output_policy::{
    DraftDisposition, DurableResult, GenerationChunk, OutputCutoff, OutputPolicyDecision,
    TerminalReason,
};
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ToolBinding, ToolCatalog, ToolDefinition, ToolImplementation,
    ToolResolutionScope,
};
use graphblocks_runtime_core::tool_approval::ToolApprovalRequest;
use graphblocks_runtime_core::tool_call::{ToolCallDraft, ToolCallStatus};
use graphblocks_runtime_core::tool_result::{ContentPart, ToolResult, ToolResultEvent};
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
fn tool_call_drafts_map_to_argument_lifecycle_application_events() {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "knowledge.search");
    let proposed = ApplicationEvent::tool_call_draft(metadata(), &draft)
        .expect("proposed draft event should be valid");

    draft
        .append_argument_fragment("{\"query\"")
        .expect("argument fragment should append");
    let delta = ApplicationEvent::tool_call_draft(
        ApplicationEventMetadata {
            event_id: "event-2".to_string(),
            sequence: 8,
            ..metadata()
        },
        &draft,
    )
    .expect("argument delta event should be valid");

    draft
        .append_argument_fragment(":\"runtime\"}")
        .expect("argument fragment should append");
    draft
        .complete_arguments()
        .expect("arguments should complete");
    let completed = ApplicationEvent::tool_call_draft(
        ApplicationEventMetadata {
            event_id: "event-3".to_string(),
            sequence: 9,
            ..metadata()
        },
        &draft,
    )
    .expect("completed arguments event should be valid");

    assert_eq!(proposed.kind, ApplicationEventKind::ToolCallProposed);
    assert_eq!(proposed.tool_call_id.as_deref(), Some("call-1"));
    assert_eq!(
        proposed.payload,
        json!({
            "tool_name": "knowledge.search",
            "status": "proposed",
            "draft_sequence": 0,
            "fragment_count": 0,
        })
    );
    assert_eq!(delta.kind, ApplicationEventKind::ToolCallArgumentsDelta);
    assert_eq!(
        delta.payload,
        json!({
            "tool_name": "knowledge.search",
            "status": "arguments_streaming",
            "draft_sequence": 1,
            "fragment_count": 1,
            "argument_fragment": "{\"query\"",
        })
    );
    assert_eq!(
        completed.kind,
        ApplicationEventKind::ToolCallArgumentsCompleted
    );
    assert_eq!(
        completed.payload,
        json!({
            "tool_name": "knowledge.search",
            "status": "arguments_complete",
            "draft_sequence": 2,
            "fragment_count": 2,
        })
    );
}

#[test]
fn final_tool_calls_map_to_validated_and_admitted_application_events() {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "knowledge.search");
    draft
        .append_argument_fragment("{\"query\":\"runtime\"}")
        .expect("argument fragment should append");
    let call = draft
        .into_completed_tool_call("resolved-tool-1", 1_000)
        .expect("arguments should parse");
    let validated = ApplicationEvent::tool_call_state(metadata(), &call)
        .expect("validated call state should be valid")
        .expect("validated calls should emit an event");

    let mut admitted_call = call.clone();
    admitted_call.status = ToolCallStatus::Admitted;
    admitted_call.admitted_at_unix_ms = Some(1_100);
    let admitted = ApplicationEvent::tool_call_state(
        ApplicationEventMetadata {
            event_id: "event-2".to_string(),
            sequence: 8,
            ..metadata()
        },
        &admitted_call,
    )
    .expect("admitted call state should be valid")
    .expect("admitted calls should emit an event");

    assert_eq!(validated.kind, ApplicationEventKind::ToolCallValidated);
    assert_eq!(validated.tool_call_id.as_deref(), Some("call-1"));
    assert_eq!(
        validated.payload,
        json!({
            "tool_name": "knowledge.search",
            "resolved_tool_id": "resolved-tool-1",
            "status": "validated",
            "arguments_digest": call.arguments_digest,
            "revision": 1,
            "depends_on": [],
            "created_at_unix_ms": 1_000,
            "admitted_at_unix_ms": null,
            "completed_at_unix_ms": null,
        })
    );
    assert_eq!(admitted.kind, ApplicationEventKind::ToolCallAdmitted);
    assert_eq!(
        admitted.payload,
        json!({
            "tool_name": "knowledge.search",
            "resolved_tool_id": "resolved-tool-1",
            "status": "admitted",
            "arguments_digest": admitted_call.arguments_digest,
            "revision": 1,
            "depends_on": [],
            "created_at_unix_ms": 1_000,
            "admitted_at_unix_ms": 1_100,
            "completed_at_unix_ms": null,
        })
    );
}

#[test]
fn tool_approval_request_maps_to_standard_application_event() {
    let catalog = ToolCatalog::new(
        [ToolDefinition::new(
            "ticket.create",
            "Create a support ticket.",
            "schemas/TicketCreate@1",
        )],
        [ToolBinding::new(
            "binding-ticket",
            "ticket.create",
            ToolImplementation::Block(BlockToolImplementation::new("blocks.ticket.create")),
        )],
    )
    .expect("catalog should be valid");
    let resolved = catalog
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")
        .expect("tool should resolve")
        .remove(0);
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "ticket.create");
    draft
        .append_argument_fragment("{\"title\":\"Need help\"}")
        .expect("argument fragment should append");
    let call = draft
        .into_completed_tool_call(resolved.resolved_tool_id.clone(), 1_000)
        .expect("arguments should parse");
    let approval =
        ToolApprovalRequest::for_call("approval-1", &resolved, &call, "user-1", 1_100, 2_000)
            .expect("approval request should be valid");

    let event = ApplicationEvent::tool_approval_requested(metadata(), &approval)
        .expect("approval request event should be valid");

    assert_eq!(event.kind, ApplicationEventKind::ToolCallApprovalRequested);
    assert_eq!(event.tool_call_id.as_deref(), Some("call-1"));
    assert_eq!(
        event.payload,
        json!({
            "approval_id": "approval-1",
            "tool_name": "ticket.create",
            "revision": 1,
            "definition_digest": resolved.definition_digest,
            "binding_digest": resolved.binding_digest,
            "arguments_digest": call.arguments_digest,
            "policy_snapshot_id": "policy-snapshot-1",
            "principal_id": "user-1",
            "requested_at_unix_ms": 1_100,
            "expires_at_unix_ms": 2_000,
        })
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

#[test]
fn output_cutoff_events_include_cutoff_and_retraction_semantics() {
    let cutoff = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: Some("turn-1".to_owned()),
        last_generated_sequence: 4,
        last_policy_accepted_sequence: 2,
        last_client_delivered_sequence: 2,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Retract,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-abort".to_owned()),
        occurred_at_unix_ms: 1_700_000,
    };

    let events = ApplicationEvent::output_cutoff(metadata(), &cutoff)
        .expect("output cutoff events are valid");

    assert_eq!(
        events.iter().map(|event| event.kind).collect::<Vec<_>>(),
        vec![
            ApplicationEventKind::OutputCutoff,
            ApplicationEventKind::AssistantRetracted
        ]
    );
    assert_eq!(events[0].metadata.event_id, "event-1");
    assert_eq!(events[1].metadata.event_id, "event-1:draft");
    assert_eq!(events[1].metadata.sequence, events[0].metadata.sequence + 1);
    assert_eq!(
        events[0].payload,
        json!({
            "stream_id": "stream-1",
            "response_id": "response-1",
            "turn_id": "turn-1",
            "last_generated_sequence": 4,
            "last_policy_accepted_sequence": 2,
            "last_client_delivered_sequence": 2,
            "terminal_reason": "policy_denied",
            "draft_disposition": "retract",
            "durable_result": "none",
            "policy_decision_id": "decision-abort",
            "occurred_at_unix_ms": 1_700_000,
        })
    );
    assert_eq!(
        events[1].payload,
        json!({
            "response_id": "response-1",
            "last_client_delivered_sequence": 2,
            "policy_decision_id": "decision-abort",
        })
    );
}

#[test]
fn output_cutoff_events_mark_incomplete_when_retraction_is_not_required() {
    let cutoff = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: None,
        last_generated_sequence: 3,
        last_policy_accepted_sequence: 1,
        last_client_delivered_sequence: 1,
        terminal_reason: TerminalReason::Cancelled,
        draft_disposition: DraftDisposition::MarkIncomplete,
        durable_result: DurableResult::Incomplete,
        policy_decision_id: None,
        occurred_at_unix_ms: 1_700_010,
    };

    let events = ApplicationEvent::output_cutoff(metadata(), &cutoff)
        .expect("output cutoff events are valid");

    assert_eq!(events.len(), 2);
    assert_eq!(events[0].kind, ApplicationEventKind::OutputCutoff);
    assert_eq!(events[1].kind, ApplicationEventKind::AssistantIncomplete);
    assert_eq!(
        events[1].payload.get("last_client_delivered_sequence"),
        Some(&json!(1))
    );
}

#[test]
fn tool_result_events_map_to_standard_tool_application_events() {
    let completed = ToolResult::completed("call-1", [ContentPart::text("done")], 1_000, 1_050);
    let failed = ToolResult::failed(
        "call-2",
        BlockError::new(
            "tool.failed",
            ErrorCategory::Permanent,
            "tool execution failed",
            true,
        ),
        1_100,
        1_120,
    );
    let denied = ToolResult::denied(
        "call-3",
        BlockError::new(
            "tool.denied",
            ErrorCategory::Policy,
            "tool execution was denied",
            false,
        ),
        1_200,
    );
    let cancelled = ToolResult::cancelled("call-4", 1_300, 1_330);
    let policy_stopped = ToolResult::policy_stopped(
        "call-5",
        BlockError::new(
            "policy.denied",
            ErrorCategory::Policy,
            "tool result was stopped by policy",
            false,
        ),
        1_400,
        1_430,
    );

    let events = [
        ToolResultEvent::started("call-0", 1, 990),
        ToolResultEvent::completed("call-1", 2, completed),
        ToolResultEvent::failed("call-2", 3, failed),
        ToolResultEvent::denied("call-3", 4, denied),
        ToolResultEvent::cancelled("call-4", 5, cancelled),
        ToolResultEvent::policy_stopped("call-5", 6, policy_stopped),
    ]
    .into_iter()
    .map(|event| {
        ApplicationEvent::tool_result_event(metadata(), &event)
            .expect("tool result event can be converted")
            .expect("lifecycle event maps to application event")
    })
    .collect::<Vec<_>>();

    assert_eq!(
        events.iter().map(|event| event.kind).collect::<Vec<_>>(),
        vec![
            ApplicationEventKind::ToolCallStarted,
            ApplicationEventKind::ToolCallCompleted,
            ApplicationEventKind::ToolCallFailed,
            ApplicationEventKind::ToolCallDenied,
            ApplicationEventKind::ToolCallCancelled,
            ApplicationEventKind::ToolCallPolicyStopped,
        ]
    );
    assert_eq!(events[0].tool_call_id.as_deref(), Some("call-0"));
    assert_eq!(events[1].payload.get("status"), Some(&json!("completed")));
    assert_eq!(events[2].payload.get("status"), Some(&json!("failed")));
    assert_eq!(events[3].payload.get("status"), Some(&json!("denied")));
    assert_eq!(events[4].payload.get("status"), Some(&json!("cancelled")));
    assert_eq!(
        events[5].payload.get("status"),
        Some(&json!("policy_stopped"))
    );
}

#[test]
fn tool_result_delta_does_not_become_application_event() {
    let delta = ToolResultEvent::delta("call-1", 7, [ContentPart::text("draft")]);

    assert_eq!(
        ApplicationEvent::tool_result_event(metadata(), &delta).expect("delta conversion is valid"),
        None
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
