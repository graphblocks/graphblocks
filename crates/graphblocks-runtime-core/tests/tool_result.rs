use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::tool_result::{
    ArtifactRef, ContentPart, Diagnostic, ToolEffectOutcome, ToolResult, ToolResultEvent,
    ToolResultStatus,
};
use serde_json::json;

#[test]
fn completed_tool_result_computes_stable_output_digest() {
    let left = ToolResult::completed(
        "call-1",
        [
            ContentPart::text("policy summary"),
            ContentPart::json(json!({"b": 2, "a": 1})),
        ],
        1_000,
        1_050,
    );
    let right = ToolResult::completed(
        "call-1",
        [
            ContentPart::text("policy summary"),
            ContentPart::json(json!({"a": 1, "b": 2})),
        ],
        1_000,
        1_050,
    );

    assert_eq!(left.status, ToolResultStatus::Completed);
    assert_eq!(left.output_digest, right.output_digest);
    assert!(
        left.output_digest
            .as_deref()
            .is_some_and(|digest| digest.starts_with("sha256:"))
    );
    assert_eq!(left.started_at_unix_ms, Some(1_000));
    assert_eq!(left.completed_at_unix_ms, Some(1_050));
}

#[test]
fn streaming_tool_result_delta_is_not_a_durable_result() {
    let delta = ToolResultEvent::delta("call-1", 3, [ContentPart::text("draft chunk")]);

    assert_eq!(delta.tool_call_id(), "call-1");
    assert!(!delta.is_final_durable_result());
    assert_eq!(delta.into_result(), None);
}

#[test]
fn completed_event_carries_the_final_durable_result() {
    let result = ToolResult::completed("call-1", [ContentPart::text("done")], 1_000, 1_050)
        .with_artifacts([
            ArtifactRef::new("artifact-1", "file:///tmp/out.txt").with_checksum("sha256:out")
        ])
        .with_diagnostics([Diagnostic::warning("tool.redacted", "output was redacted")]);
    let event = ToolResultEvent::completed("call-1", 7, result.clone());

    assert!(event.is_final_durable_result());
    assert_eq!(event.into_result(), Some(result));
}

#[test]
fn terminal_tool_result_events_preserve_partial_terminal_kind() {
    let policy_stopped = ToolResult::policy_stopped(
        "call-1",
        BlockError::new(
            "policy.denied",
            ErrorCategory::Policy,
            "tool output was stopped by policy",
            false,
        ),
        1_000,
        1_020,
    );
    let cancelled = ToolResult::cancelled("call-2", 1_100, 1_120);
    let incomplete = ToolResult::incomplete("call-3", 1_200, 1_230);

    let policy_event = ToolResultEvent::policy_stopped("call-1", 8, policy_stopped.clone());
    let cancelled_event = ToolResultEvent::cancelled("call-2", 9, cancelled.clone());
    let incomplete_event = ToolResultEvent::incomplete("call-3", 10, incomplete.clone());

    assert!(policy_event.is_final_durable_result());
    assert!(cancelled_event.is_final_durable_result());
    assert!(incomplete_event.is_final_durable_result());
    assert_eq!(policy_event.into_result(), Some(policy_stopped));
    assert_eq!(cancelled_event.into_result(), Some(cancelled));
    assert_eq!(incomplete_event.into_result(), Some(incomplete));
}

#[test]
fn failed_and_denied_tool_result_events_are_final_results() {
    let failed = ToolResult::failed(
        "call-1",
        BlockError::new(
            "tool.failed",
            ErrorCategory::Permanent,
            "tool execution failed",
            true,
        ),
        1_000,
        1_020,
    );
    let denied = ToolResult::denied(
        "call-2",
        BlockError::new(
            "tool.denied",
            ErrorCategory::Policy,
            "tool execution was denied",
            false,
        ),
        1_100,
    );

    let failed_event = ToolResultEvent::failed("call-1", 11, failed.clone());
    let denied_event = ToolResultEvent::denied("call-2", 12, denied.clone());

    assert!(failed_event.is_final_durable_result());
    assert!(denied_event.is_final_durable_result());
    assert_eq!(failed_event.into_result(), Some(failed));
    assert_eq!(denied_event.into_result(), Some(denied));
}

#[test]
fn policy_stopped_result_is_final_but_incomplete() {
    let result = ToolResult::policy_stopped(
        "call-1",
        BlockError::new(
            "policy.denied",
            ErrorCategory::Policy,
            "tool output was stopped by policy",
            false,
        ),
        1_000,
        1_020,
    );

    assert_eq!(result.status, ToolResultStatus::PolicyStopped);
    assert_eq!(result.output_digest, None);
    assert_eq!(
        result.error.as_ref().map(|error| error.code.as_str()),
        Some("policy.denied")
    );
    assert_eq!(result.started_at_unix_ms, Some(1_000));
    assert_eq!(result.completed_at_unix_ms, Some(1_020));
}

#[test]
fn denied_tool_result_records_pre_execution_denial() {
    let result = ToolResult::denied(
        "call-1",
        BlockError::new(
            "tool.denied",
            ErrorCategory::Policy,
            "tool was denied before execution",
            false,
        ),
        1_000,
    );

    assert_eq!(result.status, ToolResultStatus::Denied);
    assert_eq!(result.output_digest, None);
    assert_eq!(result.started_at_unix_ms, None);
    assert_eq!(result.completed_at_unix_ms, Some(1_000));
    assert_eq!(
        result.error.as_ref().map(|error| error.code.as_str()),
        Some("tool.denied")
    );
}

#[test]
fn policy_stopped_result_can_report_committed_effect_outcome() {
    let result = ToolResult::policy_stopped(
        "call-1",
        BlockError::new(
            "policy.denied",
            ErrorCategory::Policy,
            "tool output was stopped after a write committed",
            false,
        ),
        1_000,
        1_020,
    )
    .with_effect_outcome(ToolEffectOutcome::Committed);

    assert_eq!(result.status, ToolResultStatus::PolicyStopped);
    assert_eq!(result.effect_outcome, ToolEffectOutcome::Committed);
    assert!(result.effect_was_committed());
}
