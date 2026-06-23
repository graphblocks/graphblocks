use graphblocks_runtime_core::application_event::{
    ApplicationEvent, ApplicationEventError, ApplicationEventKind, ApplicationEventMetadata,
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
