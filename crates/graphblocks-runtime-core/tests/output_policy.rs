use graphblocks_runtime_core::output_policy::{
    DraftDisposition, DurableResult, GenerationChunk, OutputDeliveryGate, OutputGateError,
    OutputPolicyDecision, PendingToolCallsDisposition, TerminalReason,
};

#[test]
fn bounded_holdback_releases_only_policy_accepted_chunks() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "hello "))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 2, "world"))?;

    let first = gate.apply_decision(
        OutputPolicyDecision::allow("decision-1", Some(1), "sha256:first"),
        1_000,
    )?;
    assert_eq!(
        first
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "hello ")]
    );
    assert_eq!(first.cutoff, None);
    assert_eq!(gate.last_policy_accepted_sequence(), 1);
    assert_eq!(gate.last_client_delivered_sequence(), 1);

    let second = gate.apply_decision(
        OutputPolicyDecision::allow("decision-2", Some(2), "sha256:second"),
        1_010,
    )?;
    assert_eq!(
        second
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(2, "world")]
    );
    assert_eq!(second.cutoff, None);
    assert_eq!(gate.last_policy_accepted_sequence(), 2);
    assert_eq!(gate.last_client_delivered_sequence(), 2);
    Ok(())
}

#[test]
fn policy_abort_cuts_off_delivery_and_rejects_late_chunks() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "safe "))?;
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        2,
        "blocked",
    ))?;
    let delivered = gate.apply_decision(
        OutputPolicyDecision::allow("decision-1", Some(1), "sha256:first"),
        1_000,
    )?;
    assert_eq!(delivered.deliverable.len(), 1);

    let stopped = gate.apply_decision(
        OutputPolicyDecision::abort_response("decision-abort", "sha256:abort")
            .with_draft_disposition(DraftDisposition::Retract)
            .with_pending_tool_calls(PendingToolCallsDisposition::Deny),
        1_100,
    )?;
    assert!(stopped.deliverable.is_empty());

    let cutoff = stopped.cutoff.expect("policy abort records cutoff");
    assert_eq!(cutoff.stream_id, "stream-1");
    assert_eq!(cutoff.response_id, "response-1");
    assert_eq!(cutoff.last_generated_sequence, 2);
    assert_eq!(cutoff.last_policy_accepted_sequence, 1);
    assert_eq!(cutoff.last_client_delivered_sequence, 1);
    assert_eq!(cutoff.terminal_reason, TerminalReason::PolicyDenied);
    assert_eq!(cutoff.draft_disposition, DraftDisposition::Retract);
    assert_eq!(cutoff.durable_result, DurableResult::None);
    assert_eq!(cutoff.policy_decision_id.as_deref(), Some("decision-abort"));
    assert_eq!(cutoff.occurred_at_unix_ms, 1_100);

    assert_eq!(
        gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 3, "late")),
        Err(OutputGateError::PolicyStopped),
    );
    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::allow("decision-late", Some(3), "sha256:late"),
            1_200,
        ),
        Err(OutputGateError::PolicyStopped),
    );
    Ok(())
}
