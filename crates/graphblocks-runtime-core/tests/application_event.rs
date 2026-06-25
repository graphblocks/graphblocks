use graphblocks_runtime_core::application_event::{
    ApplicationCommand, ApplicationCommandKind, ApplicationCommandMetadata, ApplicationEvent,
    ApplicationEventError, ApplicationEventKind, ApplicationEventMetadata,
    ApplicationEventStreamState, ApplicationProtocolCapabilities, ApplicationProtocolEvent,
    ApplicationProtocolEventKind, ApplicationProtocolEventMetadata, ApplicationProtocolLog,
};
use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::output_policy::{
    DraftDisposition, DurableResult, GenerationChunk, OutputCutoff, OutputCutoffError,
    OutputPolicyDecision, TerminalReason,
};
use graphblocks_runtime_core::policy::{PolicyDecision, PolicyEffect};
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ToolBinding, ToolCatalog, ToolDefinition, ToolImplementation,
    ToolResolutionScope,
};
use graphblocks_runtime_core::tool_approval::ToolApprovalRequest;
use graphblocks_runtime_core::tool_call::{ToolCallDraft, ToolCallStatus};
use graphblocks_runtime_core::tool_result::{
    ArtifactRef, ContentPart, ToolResult, ToolResultEvent,
};
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
        ApplicationEventKind::RunStarted.as_str(),
        ApplicationEventKind::RunSucceeded.as_str(),
        ApplicationEventKind::RunFailed.as_str(),
        ApplicationEventKind::RunCancelled.as_str(),
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
            "RunStarted",
            "RunSucceeded",
            "RunFailed",
            "RunCancelled",
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
fn run_events_use_the_common_application_event_envelope() {
    let event = ApplicationEvent::new(
        ApplicationEventKind::RunStarted,
        metadata(),
        json!({"status": "running"}),
    )
    .expect("run event is valid");

    assert_eq!(event.kind, ApplicationEventKind::RunStarted);
    assert_eq!(event.tool_call_id, None);
    assert_eq!(event.metadata.run_id, "run-1");
    assert_eq!(event.metadata.release_id, "release-1");
    assert_eq!(event.metadata.policy_snapshot_id, "policy-1");
    assert_eq!(event.payload, json!({"status": "running"}));
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
fn tool_policy_decisions_map_to_policy_evaluated_application_events() {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "knowledge.search");
    draft
        .append_argument_fragment("{\"query\":\"runtime\"}")
        .expect("argument fragment should append");
    let call = draft
        .into_completed_tool_call("resolved-tool-1", 1_000)
        .expect("arguments should parse");
    let decision = PolicyDecision {
        decision_id: "decision-1".to_string(),
        effect: PolicyEffect::Deny,
        reason_codes: vec!["tool.denied".to_string()],
        policy_refs: vec!["policy/tool-safety".to_string()],
        obligations: Vec::new(),
        advice: vec![json!({"message": "tool denied"})],
        evaluated_at: "2026-06-23T00:00:01Z".to_string(),
        valid_until: Some("2026-06-23T00:05:01Z".to_string()),
        input_digest: "sha256:policy-input".to_string(),
    };

    let event = ApplicationEvent::tool_call_policy_evaluated(metadata(), &call, &decision)
        .expect("policy evaluation event should be valid");

    assert_eq!(event.kind, ApplicationEventKind::ToolCallPolicyEvaluated);
    assert_eq!(event.tool_call_id.as_deref(), Some("call-1"));
    assert_eq!(
        event.payload,
        json!({
            "tool_name": "knowledge.search",
            "resolved_tool_id": "resolved-tool-1",
            "status": "validated",
            "arguments_digest": call.arguments_digest,
            "revision": 1,
            "decision_id": "decision-1",
            "effect": "deny",
            "reason_codes": ["tool.denied"],
            "policy_refs": ["policy/tool-safety"],
            "obligation_count": 0,
            "advice_count": 1,
            "evaluated_at": "2026-06-23T00:00:01Z",
            "valid_until": "2026-06-23T00:05:01Z",
            "input_digest": "sha256:policy-input",
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
fn output_policy_evaluation_start_event_identifies_chunk_without_text_payload() {
    let chunk = GenerationChunk::text("stream-1", "response-1", 4, "sensitive text");

    let event = ApplicationEvent::output_policy_evaluation_started(
        metadata(),
        &chunk,
        "sha256:pending-window",
    )
    .expect("output policy evaluation started event is valid");

    assert_eq!(
        event.kind,
        ApplicationEventKind::OutputPolicyEvaluationStarted
    );
    assert_eq!(event.tool_call_id, None);
    assert_eq!(
        event.payload,
        json!({
            "stream_id": "stream-1",
            "response_id": "response-1",
            "chunk_sequence": 4,
            "input_digest": "sha256:pending-window",
            "chunk_text_bytes": 14,
        })
    );
    assert_eq!(event.payload.get("text"), None);
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
            "replacement_part_count": 1,
            "replacement_chunk_count": 1,
            "redaction_count": 0,
            "provider_cancellation": "request",
            "draft_disposition": "keep",
            "pending_tool_calls": "keep",
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
    assert_eq!(
        event.payload.get("provider_cancellation"),
        Some(&json!("request"))
    );
    assert_eq!(
        event.payload.get("draft_disposition"),
        Some(&json!("retract"))
    );
    assert_eq!(
        event.payload.get("pending_tool_calls"),
        Some(&json!("deny"))
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
fn output_cutoff_events_reject_invalid_sequence_order() {
    let cutoff = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: None,
        last_generated_sequence: 1,
        last_policy_accepted_sequence: 1,
        last_client_delivered_sequence: 2,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Retract,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-abort".to_owned()),
        occurred_at_unix_ms: 1_700_010,
    };

    assert_eq!(
        ApplicationEvent::output_cutoff(metadata(), &cutoff),
        Err(ApplicationEventError::InvalidOutputCutoff {
            source: OutputCutoffError::ClientDeliveredSequenceBeyondGenerated {
                last_generated_sequence: 1,
                last_client_delivered_sequence: 2,
            }
        })
    );
}

#[test]
fn application_event_stream_state_rejects_invalid_output_cutoff_payload() {
    let mut state = ApplicationEventStreamState::default();
    let invalid_cutoff = ApplicationEvent::new(
        ApplicationEventKind::OutputCutoff,
        metadata(),
        json!({
            "stream_id": "stream-1",
            "response_id": "response-1",
            "turn_id": "turn-1",
            "last_generated_sequence": 1,
            "last_policy_accepted_sequence": 1,
            "last_client_delivered_sequence": 2,
            "terminal_reason": "policy_denied",
            "draft_disposition": "retract",
            "durable_result": "none",
            "policy_decision_id": "decision-abort",
            "occurred_at_unix_ms": 1_700_020,
        }),
    )
    .expect("raw output cutoff event envelope is valid");

    assert_eq!(state.accept(invalid_cutoff), None);
    assert_eq!(state.cutoff_for_response("response-1"), None);
    assert!(state.accepted_events().is_empty());
}

#[test]
fn application_event_stream_state_discards_late_output_after_cutoff() {
    let mut state = ApplicationEventStreamState::default();
    let cutoff = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: Some("turn-1".to_owned()),
        last_generated_sequence: 3,
        last_policy_accepted_sequence: 1,
        last_client_delivered_sequence: 1,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Retract,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-abort".to_owned()),
        occurred_at_unix_ms: 1_700_020,
    };
    let cutoff_events = ApplicationEvent::output_cutoff(metadata(), &cutoff)
        .expect("output cutoff events are valid");
    let late_output = ApplicationEvent::output_policy_evaluation_started(
        metadata(),
        &GenerationChunk::text("stream-1", "response-1", 2, "blocked"),
        "sha256:late",
    )
    .expect("output policy evaluation event is valid");
    let replacement_response = ApplicationEvent::output_policy_evaluation_started(
        metadata(),
        &GenerationChunk::text("stream-1", "response-2", 1, "replacement"),
        "sha256:replacement",
    )
    .expect("replacement response event is valid");
    let late_tool_draft = ApplicationEvent::tool_call_draft(
        metadata(),
        &ToolCallDraft::proposed("response-1", "call-draft", "ticket.create"),
    )
    .expect("tool draft event is valid");
    let validated_tool = ApplicationEvent::tool(
        ApplicationEventKind::ToolCallValidated,
        metadata(),
        "call-validated",
        json!({"status": "validated"}),
    )
    .expect("tool event is valid");
    let admitted_tool = ApplicationEvent::tool(
        ApplicationEventKind::ToolCallAdmitted,
        metadata(),
        "call-admitted",
        json!({"status": "admitted"}),
    )
    .expect("tool event is valid");
    let started_tool = ApplicationEvent::tool(
        ApplicationEventKind::ToolCallStarted,
        metadata(),
        "call-started",
        json!({"status": "running"}),
    )
    .expect("tool event is valid");
    let completed_tool = ApplicationEvent::tool(
        ApplicationEventKind::ToolCallCompleted,
        metadata(),
        "call-completed",
        json!({"status": "completed"}),
    )
    .expect("tool event is valid");
    let replacement_tool_draft = ApplicationEvent::tool_call_draft(
        ApplicationEventMetadata {
            event_id: "event-replacement-tool".to_owned(),
            response_id: "response-2".to_owned(),
            sequence: 8,
            ..metadata()
        },
        &ToolCallDraft::proposed("response-2", "call-replacement", "knowledge.search"),
    )
    .expect("replacement tool draft event is valid");
    let denied_tool = ApplicationEvent::tool(
        ApplicationEventKind::ToolCallDenied,
        metadata(),
        "call-1",
        json!({"status": "denied"}),
    )
    .expect("tool event is valid");
    let cancelled_tool = ApplicationEvent::tool(
        ApplicationEventKind::ToolCallCancelled,
        metadata(),
        "call-2",
        json!({"status": "cancelled"}),
    )
    .expect("tool event is valid");
    let policy_stopped_tool = ApplicationEvent::tool(
        ApplicationEventKind::ToolCallPolicyStopped,
        metadata(),
        "call-3",
        json!({"status": "policy_stopped"}),
    )
    .expect("tool event is valid");

    assert_eq!(
        state.accept(cutoff_events[0].clone()),
        Some(cutoff_events[0].clone())
    );
    assert_eq!(state.cutoff_for_response("response-1"), Some(&cutoff));
    assert_eq!(
        state.accept(cutoff_events[1].clone()),
        Some(cutoff_events[1].clone())
    );
    assert_eq!(state.accept(late_output), None);
    assert_eq!(state.accept(late_tool_draft), None);
    assert_eq!(state.accept(validated_tool), None);
    assert_eq!(state.accept(admitted_tool), None);
    assert_eq!(state.accept(started_tool), None);
    assert_eq!(state.accept(completed_tool), None);
    assert_eq!(
        state.accept(replacement_response.clone()),
        Some(replacement_response)
    );
    assert_eq!(
        state.accept(replacement_tool_draft.clone()),
        Some(replacement_tool_draft)
    );
    assert_eq!(state.accept(denied_tool.clone()), Some(denied_tool));
    assert_eq!(state.accept(cancelled_tool.clone()), Some(cancelled_tool));
    assert_eq!(
        state.accept(policy_stopped_tool.clone()),
        Some(policy_stopped_tool)
    );
    assert_eq!(
        state
            .accepted_events()
            .iter()
            .map(|event| event.kind)
            .collect::<Vec<_>>(),
        vec![
            ApplicationEventKind::OutputCutoff,
            ApplicationEventKind::AssistantRetracted,
            ApplicationEventKind::OutputPolicyEvaluationStarted,
            ApplicationEventKind::ToolCallProposed,
            ApplicationEventKind::ToolCallDenied,
            ApplicationEventKind::ToolCallCancelled,
            ApplicationEventKind::ToolCallPolicyStopped,
        ]
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
fn protocol_events_represent_streaming_tool_result_deltas_and_artifacts() {
    let delta = ToolResultEvent::delta(
        "call-1",
        7,
        [
            ContentPart::text("draft chunk")
                .with_metadata("trust_designation", json!("untrusted_external")),
            ContentPart::json(json!({"items": 2})),
        ],
    );
    let artifact = ToolResultEvent::artifact_ready(
        "call-1",
        8,
        ArtifactRef::new("artifact-1", "file:///tmp/result.json")
            .with_checksum("sha256:artifact")
            .with_media_type("application/json"),
    );

    let delta_event = ApplicationProtocolEvent::tool_result_stream(
        protocol_event_metadata("event-delta", 7, "cursor-7"),
        &delta,
    )
    .expect("delta protocol event should be valid")
    .expect("delta should map to a protocol event");
    let artifact_event = ApplicationProtocolEvent::tool_result_stream(
        protocol_event_metadata("event-artifact", 8, "cursor-8"),
        &artifact,
    )
    .expect("artifact protocol event should be valid")
    .expect("artifact should map to a protocol event");

    assert_eq!(delta_event.kind, ApplicationProtocolEventKind::JobProgress);
    assert_eq!(
        delta_event.payload,
        json!({
            "tool_call_id": "call-1",
            "tool_result_sequence": 7,
            "output": [
                {
                    "kind": "text",
                    "text": "draft chunk",
                    "data": null,
                    "metadata": {"trust_designation": "untrusted_external"}
                },
                {
                    "kind": "json",
                    "text": null,
                    "data": {"items": 2},
                    "metadata": {}
                }
            ]
        })
    );
    assert_eq!(
        artifact_event.kind,
        ApplicationProtocolEventKind::ArtifactReady
    );
    assert_eq!(
        artifact_event.payload,
        json!({
            "tool_call_id": "call-1",
            "tool_result_sequence": 8,
            "artifact": {
                "artifact_id": "artifact-1",
                "uri": "file:///tmp/result.json",
                "checksum": "sha256:artifact",
                "media_type": "application/json"
            }
        })
    );

    let completed = ToolResultEvent::completed(
        "call-1",
        9,
        ToolResult::completed("call-1", [ContentPart::text("done")], 1_000, 1_050),
    );
    assert_eq!(
        ApplicationProtocolEvent::tool_result_stream(
            protocol_event_metadata("event-complete", 9, "cursor-9"),
            &completed,
        )
        .expect("completed conversion should be valid"),
        None
    );
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
