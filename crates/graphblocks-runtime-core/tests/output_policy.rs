use graphblocks_runtime_core::output_policy::{
    DeclarativeOutputPolicyEvaluator, DeclarativeOutputPolicyRule,
    DeclarativeOutputPolicyRuleError, DraftDisposition, DurableResult, FlushBoundary,
    GenerationChunk, GenerationChunkError, OutputCutoff, OutputCutoffError, OutputDeliveryGate,
    OutputDeliveryPolicy, OutputDeliveryPolicyError, OutputDisposition, OutputGateError,
    OutputPolicyDecision, OutputPolicyDecisionError, PendingToolCallsDisposition,
    ProviderCancellation, RedactionInstruction, TerminalReason, ViolationAction,
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
fn output_gate_resumes_pending_holdback_state() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::from_state(
        "stream-1",
        "response-1",
        [GenerationChunk::text("stream-1", "response-1", 2, "held")],
        2,
        1,
        1,
    )?;

    let update = gate.apply_decision(
        OutputPolicyDecision::allow("decision-2", Some(2), "sha256:second"),
        1_010,
    )?;

    assert_eq!(
        update
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(2, "held")]
    );
    assert_eq!(gate.last_generated_sequence(), 2);
    assert_eq!(gate.last_policy_accepted_sequence(), 2);
    assert_eq!(gate.last_client_delivered_sequence(), 2);

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 3, "next"))?;
    assert_eq!(gate.last_generated_sequence(), 3);
    assert_eq!(
        gate.pending_chunks()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(3, "next")]
    );
    Ok(())
}

#[test]
fn sentence_flush_boundary_holds_incomplete_accepted_suffix() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::bounded_holdback(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        )
        .with_holdback_max_tokens(16)
        .flush_on([FlushBoundary::Sentence]),
    )?;

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "Hello "))?;
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        2,
        "world. ",
    ))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 3, "Next"))?;

    let update = gate.apply_decision(
        OutputPolicyDecision::allow("decision-1", Some(3), "sha256:accepted"),
        1_000,
    )?;

    assert_eq!(
        update
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "Hello "), (2, "world. ")]
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 3);
    assert_eq!(gate.last_client_delivered_sequence(), 2);
    assert_eq!(
        gate.commit_accepted_output()
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(3, "Next")]
    );
    Ok(())
}

#[test]
fn paragraph_flush_boundary_waits_for_blank_line() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::bounded_holdback(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        )
        .with_holdback_max_tokens(16)
        .flush_on([FlushBoundary::Paragraph]),
    )?;

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "First"))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 2, "\n\n"))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 3, "Second"))?;

    let update = gate.apply_decision(
        OutputPolicyDecision::allow("decision-1", Some(3), "sha256:accepted"),
        1_000,
    )?;

    assert_eq!(
        update
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "First"), (2, "\n\n")]
    );
    assert_eq!(gate.last_client_delivered_sequence(), 2);
    Ok(())
}

#[test]
fn output_gate_rejects_missing_pending_resume_chunk() {
    assert_eq!(
        OutputDeliveryGate::from_state(
            "stream-1",
            "response-1",
            Vec::<GenerationChunk>::new(),
            2,
            1,
            1,
        ),
        Err(OutputGateError::MissingPendingChunk { sequence: 2 }),
    );
}

#[test]
fn output_gate_rejects_restored_delivery_beyond_policy_acceptance() {
    assert_eq!(
        OutputDeliveryGate::from_state(
            "stream-1",
            "response-1",
            [GenerationChunk::text(
                "stream-1",
                "response-1",
                3,
                "pending"
            )],
            3,
            1,
            2,
        ),
        Err(
            OutputGateError::ClientDeliveredSequenceBeyondPolicyAccepted {
                last_policy_accepted_sequence: 1,
                last_client_delivered_sequence: 2,
            }
        ),
    );
}

#[test]
fn output_gate_resumes_terminal_cutoff_state() -> Result<(), OutputGateError> {
    let cutoff = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: Some("turn-1".to_owned()),
        last_generated_sequence: 2,
        last_policy_accepted_sequence: 1,
        last_client_delivered_sequence: 1,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Retract,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-abort".to_owned()),
        occurred_at_unix_ms: 1_100,
    };

    let mut gate = OutputDeliveryGate::from_cutoff(cutoff.clone())?;

    assert_eq!(gate.cutoff(), Some(&cutoff));
    assert_eq!(gate.last_generated_sequence(), 2);
    assert_eq!(gate.last_policy_accepted_sequence(), 1);
    assert_eq!(gate.last_client_delivered_sequence(), 1);
    assert_eq!(
        gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 3, "late")),
        Err(OutputGateError::PolicyStopped),
    );
    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::allow("decision-late", Some(2), "sha256:late"),
            1_200,
        ),
        Err(OutputGateError::PolicyStopped),
    );
    Ok(())
}

#[test]
fn output_gate_turn_id_update_keeps_restored_cutoff_in_sync() -> Result<(), OutputGateError> {
    let cutoff = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: Some("turn-original".to_owned()),
        last_generated_sequence: 2,
        last_policy_accepted_sequence: 1,
        last_client_delivered_sequence: 1,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Retract,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-abort".to_owned()),
        occurred_at_unix_ms: 1_100,
    };

    let gate = OutputDeliveryGate::from_cutoff(cutoff)?.with_turn_id("turn-updated");

    assert_eq!(
        gate.cutoff().and_then(|cutoff| cutoff.turn_id.as_deref()),
        Some("turn-updated")
    );
    Ok(())
}

#[test]
fn output_gate_rejects_non_contiguous_generation_sequence() {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    assert_eq!(
        gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 2, "late")),
        Err(OutputGateError::NonContiguousSequence {
            last_generated_sequence: 0,
            attempted_sequence: 2,
        }),
    );
    assert_eq!(gate.last_generated_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
}

#[test]
fn bounded_holdback_rejects_pending_output_over_byte_limit() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::bounded_holdback(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        )
        .with_holdback_max_bytes(8),
    )?;

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "safe"))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 2, "text"))?;

    assert_eq!(
        gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 3, "!")),
        Err(OutputGateError::BoundedHoldbackExceeded { max_bytes: 8 }),
    );
    assert_eq!(gate.last_generated_sequence(), 2);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    Ok(())
}

#[test]
fn bounded_holdback_rejects_pending_output_over_token_limit() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::bounded_holdback(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        )
        .with_holdback_max_tokens(3),
    )?;

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "safe text",
    ))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 2, "still"))?;

    assert_eq!(
        gate.record_chunk(GenerationChunk::text(
            "stream-1",
            "response-1",
            3,
            "blocked",
        )),
        Err(OutputGateError::BoundedHoldbackTokensExceeded { max_tokens: 3 }),
    );
    assert_eq!(gate.last_generated_sequence(), 2);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    Ok(())
}

#[test]
fn generation_chunk_requires_stream_and_response_ids() {
    let empty_stream = GenerationChunk::text(" ", "response-1", 1, "late");
    assert_eq!(
        empty_stream.validate(),
        Err(GenerationChunkError::EmptyIdentityField { field: "stream_id" })
    );

    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    assert_eq!(
        gate.record_chunk(empty_stream),
        Err(OutputGateError::InvalidGenerationChunk {
            source: GenerationChunkError::EmptyIdentityField { field: "stream_id" },
        })
    );

    assert_eq!(
        GenerationChunk::text("stream-1", "", 1, "late").validate(),
        Err(GenerationChunkError::EmptyIdentityField {
            field: "response_id",
        })
    );

    assert_eq!(
        GenerationChunk::text("stream-1", "response-1", 0, "late").validate(),
        Err(GenerationChunkError::InvalidSequence { sequence: 0 })
    );
}

#[test]
fn output_gate_rejects_empty_identity_fields() {
    let mut empty_stream_gate = OutputDeliveryGate::new(" ", "response-1");
    assert_eq!(
        empty_stream_gate.record_chunk(GenerationChunk::text(" ", "response-1", 1, "late")),
        Err(OutputGateError::EmptyIdentityField { field: "stream_id" })
    );

    let mut empty_response_gate = OutputDeliveryGate::new("stream-1", "");
    assert_eq!(
        empty_response_gate.apply_decision(
            OutputPolicyDecision::hold("decision-1", "sha256:input"),
            1_000,
        ),
        Err(OutputGateError::EmptyIdentityField {
            field: "response_id",
        })
    );

    let mut empty_turn_gate = OutputDeliveryGate::new("stream-1", "response-1").with_turn_id(" ");
    assert_eq!(
        empty_turn_gate.apply_decision(
            OutputPolicyDecision::abort_response("decision-1", "sha256:input"),
            1_000,
        ),
        Err(OutputGateError::EmptyIdentityField { field: "turn_id" })
    );
}

#[test]
fn policy_decision_cannot_accept_future_generation_sequences() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "hello"))?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::allow("decision-1", Some(2), "sha256:future"),
            1_000,
        ),
        Err(OutputGateError::AcceptedSequenceBeyondGenerated {
            last_generated_sequence: 1,
            accepted_through_sequence: 2,
        }),
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    Ok(())
}

#[test]
fn output_policy_decision_requires_a_decision_id() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    let decision = OutputPolicyDecision::allow(" ", Some(1), "sha256:input");

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "hello"))?;

    assert_eq!(
        decision.validate(),
        Err(OutputPolicyDecisionError::MissingDecisionId)
    );
    assert_eq!(
        gate.apply_decision(decision, 1_000),
        Err(OutputGateError::MissingDecisionId)
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    Ok(())
}

#[test]
fn output_policy_decision_rejects_zero_accepted_sequence() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    let decision = OutputPolicyDecision::allow("decision-1", Some(0), "sha256:input");

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "hello"))?;

    assert_eq!(
        decision.validate(),
        Err(OutputPolicyDecisionError::InvalidAcceptedThroughSequence {
            accepted_through_sequence: 0,
        })
    );
    assert_eq!(
        gate.apply_decision(decision, 1_000),
        Err(OutputGateError::InvalidAcceptedThroughSequence {
            accepted_through_sequence: 0,
        })
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    Ok(())
}

#[test]
fn output_policy_decision_rejects_zero_evaluated_at_unix_ms() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    let decision =
        OutputPolicyDecision::allow("decision-1", Some(1), "sha256:input").evaluated_at_unix_ms(0);

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "hello"))?;

    assert_eq!(
        decision.validate(),
        Err(OutputPolicyDecisionError::InvalidEvaluatedAtUnixMs {
            evaluated_at_unix_ms: 0,
        })
    );
    assert_eq!(
        gate.apply_decision(decision, 1_000),
        Err(OutputGateError::InvalidEvaluatedAtUnixMs {
            evaluated_at_unix_ms: 0,
        })
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    Ok(())
}

#[test]
fn output_cutoff_rejects_sequences_beyond_generated() {
    let policy_accepted_after_generated = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: None,
        last_generated_sequence: 1,
        last_policy_accepted_sequence: 2,
        last_client_delivered_sequence: 1,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Retract,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-1".to_owned()),
        occurred_at_unix_ms: 1_000,
    };

    assert_eq!(
        policy_accepted_after_generated.validate(),
        Err(OutputCutoffError::PolicyAcceptedSequenceBeyondGenerated {
            last_generated_sequence: 1,
            last_policy_accepted_sequence: 2,
        })
    );

    let client_delivered_after_generated = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: None,
        last_generated_sequence: 1,
        last_policy_accepted_sequence: 1,
        last_client_delivered_sequence: 2,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Retract,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-1".to_owned()),
        occurred_at_unix_ms: 1_000,
    };

    assert_eq!(
        client_delivered_after_generated.validate(),
        Err(OutputCutoffError::ClientDeliveredSequenceBeyondGenerated {
            last_generated_sequence: 1,
            last_client_delivered_sequence: 2,
        })
    );
}

#[test]
fn output_cutoff_rejects_kept_draft_delivered_beyond_policy_acceptance() {
    let cutoff = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: None,
        last_generated_sequence: 3,
        last_policy_accepted_sequence: 1,
        last_client_delivered_sequence: 2,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Keep,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-1".to_owned()),
        occurred_at_unix_ms: 1_000,
    };

    assert_eq!(
        cutoff.validate(),
        Err(
            OutputCutoffError::DeliveredDraftBeyondPolicyAcceptanceKept {
                last_policy_accepted_sequence: 1,
                last_client_delivered_sequence: 2,
            }
        )
    );

    let retract_cutoff = OutputCutoff {
        draft_disposition: DraftDisposition::Retract,
        ..cutoff
    };
    assert_eq!(retract_cutoff.validate(), Ok(()));
}

#[test]
fn output_cutoff_requires_stream_and_response_ids() {
    let valid = OutputCutoff {
        stream_id: "stream-1".to_owned(),
        response_id: "response-1".to_owned(),
        turn_id: None,
        last_generated_sequence: 1,
        last_policy_accepted_sequence: 1,
        last_client_delivered_sequence: 1,
        terminal_reason: TerminalReason::PolicyDenied,
        draft_disposition: DraftDisposition::Retract,
        durable_result: DurableResult::None,
        policy_decision_id: Some("decision-1".to_owned()),
        occurred_at_unix_ms: 1_000,
    };

    assert_eq!(
        OutputCutoff {
            stream_id: " ".to_owned(),
            ..valid.clone()
        }
        .validate(),
        Err(OutputCutoffError::EmptyIdentityField { field: "stream_id" })
    );
    assert_eq!(
        OutputCutoff {
            response_id: "".to_owned(),
            ..valid
        }
        .validate(),
        Err(OutputCutoffError::EmptyIdentityField {
            field: "response_id",
        })
    );
}

#[test]
fn output_cutoff_rejects_empty_policy_decision_id() {
    assert_eq!(
        OutputCutoff {
            stream_id: "stream-1".to_owned(),
            response_id: "response-1".to_owned(),
            turn_id: None,
            last_generated_sequence: 1,
            last_policy_accepted_sequence: 1,
            last_client_delivered_sequence: 1,
            terminal_reason: TerminalReason::PolicyDenied,
            draft_disposition: DraftDisposition::Retract,
            durable_result: DurableResult::None,
            policy_decision_id: Some(" ".to_owned()),
            occurred_at_unix_ms: 1_000,
        }
        .validate(),
        Err(OutputCutoffError::EmptyIdentityField {
            field: "policy_decision_id",
        })
    );
}

#[test]
fn output_cutoff_rejects_empty_turn_id_when_present() {
    assert_eq!(
        OutputCutoff {
            stream_id: "stream-1".to_owned(),
            response_id: "response-1".to_owned(),
            turn_id: Some(" ".to_owned()),
            last_generated_sequence: 1,
            last_policy_accepted_sequence: 1,
            last_client_delivered_sequence: 1,
            terminal_reason: TerminalReason::PolicyDenied,
            draft_disposition: DraftDisposition::Retract,
            durable_result: DurableResult::None,
            policy_decision_id: Some("decision-1".to_owned()),
            occurred_at_unix_ms: 1_000,
        }
        .validate(),
        Err(OutputCutoffError::EmptyIdentityField { field: "turn_id" })
    );
}

#[test]
fn output_cutoff_requires_positive_occurred_at_unix_ms() {
    assert_eq!(
        OutputCutoff {
            stream_id: "stream-1".to_owned(),
            response_id: "response-1".to_owned(),
            turn_id: None,
            last_generated_sequence: 1,
            last_policy_accepted_sequence: 1,
            last_client_delivered_sequence: 1,
            terminal_reason: TerminalReason::PolicyDenied,
            draft_disposition: DraftDisposition::Retract,
            durable_result: DurableResult::None,
            policy_decision_id: Some("decision-1".to_owned()),
            occurred_at_unix_ms: 0,
        }
        .validate(),
        Err(OutputCutoffError::MissingOccurredAtUnixMs)
    );
}

#[test]
fn output_gate_rejects_policy_decision_without_input_digest() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "hello"))?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::allow("decision-1", Some(1), ""),
            1_000
        ),
        Err(OutputGateError::MissingInputDigest {
            decision_id: "decision-1".to_owned(),
        }),
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    Ok(())
}

#[test]
fn output_gate_terminal_decision_requires_occurred_at_unix_ms() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "blocked",
    ))?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::abort_response("decision-abort", "sha256:blocked"),
            0,
        ),
        Err(OutputGateError::MissingOccurredAtUnixMs)
    );
    assert_eq!(gate.cutoff(), None);
    assert_eq!(gate.last_generated_sequence(), 1);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    assert_eq!(
        gate.pending_chunks()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "blocked")]
    );
    Ok(())
}

#[test]
fn output_policy_decision_rejects_invalid_metadata_values() {
    let blank_reason = OutputPolicyDecision::hold("decision-hold", "sha256:hold")
        .with_reason_codes(["secret.detected", " "]);
    assert_eq!(
        blank_reason.validate(),
        Err(OutputPolicyDecisionError::InvalidReasonCode {
            reason_code: " ".to_owned(),
        }),
    );

    let blank_policy_ref = OutputPolicyDecision::hold("decision-hold", "sha256:hold")
        .with_policy_refs(["policy/output-standard", ""]);
    assert_eq!(
        blank_policy_ref.validate(),
        Err(OutputPolicyDecisionError::InvalidPolicyRef {
            policy_ref: "".to_owned(),
        }),
    );

    let zero_evaluated_at =
        OutputPolicyDecision::hold("decision-hold", "sha256:hold").evaluated_at_unix_ms(0);
    assert_eq!(
        zero_evaluated_at.validate(),
        Err(OutputPolicyDecisionError::InvalidEvaluatedAtUnixMs {
            evaluated_at_unix_ms: 0,
        }),
    );
}

#[test]
fn output_policy_decision_rejects_content_incompatible_with_disposition() {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "draft"))
        .expect("chunk records");
    let mut allow_with_replacement =
        OutputPolicyDecision::allow("decision-allow", Some(1), "sha256:allow");
    allow_with_replacement.replacement_chunks = vec![GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "replacement",
    )];
    assert_eq!(
        allow_with_replacement.validate(),
        Err(OutputPolicyDecisionError::InvalidDispositionContent {
            decision_id: "decision-allow".to_owned(),
            disposition: OutputDisposition::Allow,
            field: "replacement_chunks",
        }),
    );
    assert_eq!(
        gate.apply_decision(allow_with_replacement, 1_000),
        Err(OutputGateError::InvalidDispositionContent {
            decision_id: "decision-allow".to_owned(),
            disposition: OutputDisposition::Allow,
            field: "replacement_chunks",
        }),
    );

    let mut replace_with_redaction = OutputPolicyDecision::replace(
        "decision-replace",
        Some(1),
        [GenerationChunk::text(
            "stream-1",
            "response-1",
            1,
            "replacement",
        )],
        "sha256:replace",
    );
    replace_with_redaction.redactions = vec![RedactionInstruction::text_range(
        "/chunks/1/text",
        0,
        3,
        "[redacted]",
    )];
    assert_eq!(
        replace_with_redaction.validate(),
        Err(OutputPolicyDecisionError::InvalidDispositionContent {
            decision_id: "decision-replace".to_owned(),
            disposition: OutputDisposition::Replace,
            field: "redactions",
        }),
    );
}

#[test]
fn replace_decision_requires_policy_approved_replacement_content() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    let decision = OutputPolicyDecision::replace(
        "decision-replace",
        Some(1),
        Vec::<GenerationChunk>::new(),
        "sha256:replace",
    );

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "blocked draft",
    ))?;

    assert_eq!(
        decision.validate(),
        Err(OutputPolicyDecisionError::ReplacementContentMissing {
            decision_id: "decision-replace".to_owned(),
        }),
    );

    let invalid_replacement = OutputPolicyDecision::replace(
        "decision-replace",
        Some(1),
        [GenerationChunk::text(
            " ",
            "response-1",
            1,
            "policy-approved replacement",
        )],
        "sha256:replace",
    );
    assert_eq!(
        invalid_replacement.validate(),
        Err(OutputPolicyDecisionError::InvalidReplacementChunk {
            source: GenerationChunkError::EmptyIdentityField { field: "stream_id" },
        }),
    );

    assert_eq!(
        gate.apply_decision(decision, 1_000),
        Err(OutputGateError::ReplacementContentMissing {
            decision_id: "decision-replace".to_owned(),
        }),
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
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
            .with_provider_cancellation(ProviderCancellation::RequiredIfSupported)
            .with_draft_disposition(DraftDisposition::Retract)
            .with_pending_tool_calls(PendingToolCallsDisposition::Deny),
        1_100,
    )?;
    assert!(stopped.deliverable.is_empty());
    assert_eq!(
        stopped.provider_cancellation,
        Some(ProviderCancellation::RequiredIfSupported)
    );
    assert_eq!(
        stopped.pending_tool_calls,
        Some(PendingToolCallsDisposition::Deny)
    );

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

#[test]
fn terminal_decision_records_accepted_prefix_in_cutoff() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "safe "))?;
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        2,
        "blocked",
    ))?;

    let stopped = gate.apply_decision(
        OutputPolicyDecision::abort_response("decision-abort", "sha256:blocked")
            .with_accepted_through_sequence(1),
        1_100,
    )?;

    let cutoff = stopped.cutoff.expect("policy abort records cutoff");
    assert_eq!(cutoff.last_generated_sequence, 2);
    assert_eq!(cutoff.last_policy_accepted_sequence, 1);
    assert_eq!(cutoff.last_client_delivered_sequence, 0);
    assert_eq!(gate.last_policy_accepted_sequence(), 1);
    Ok(())
}

#[test]
fn policy_abort_forces_kept_pending_tool_calls_to_denied_cleanup() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "blocked",
    ))?;

    let stopped = gate.apply_decision(
        OutputPolicyDecision::abort_response("decision-abort", "sha256:abort")
            .with_pending_tool_calls(PendingToolCallsDisposition::Keep),
        1_000,
    )?;

    assert_eq!(
        stopped.pending_tool_calls,
        Some(PendingToolCallsDisposition::Deny)
    );
    Ok(())
}

#[test]
fn deny_commit_preserves_kept_pending_tool_calls() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "blocked",
    ))?;

    let stopped = gate.apply_decision(
        OutputPolicyDecision::deny_commit("decision-deny-commit", "sha256:abort")
            .with_pending_tool_calls(PendingToolCallsDisposition::Keep),
        1_000,
    )?;

    assert_eq!(
        stopped.pending_tool_calls,
        Some(PendingToolCallsDisposition::Keep)
    );
    Ok(())
}

#[test]
fn bounded_holdback_policy_requires_a_bound() {
    let policy = OutputDeliveryPolicy::bounded_holdback(
        ViolationAction::AbortResponse,
        DraftDisposition::Retract,
    );

    assert_eq!(
        policy.validate(),
        Err(OutputDeliveryPolicyError::UnboundedPolicyHoldback),
    );
}

#[test]
fn bounded_holdback_policy_accepts_time_or_size_bounds() -> Result<(), OutputDeliveryPolicyError> {
    OutputDeliveryPolicy::bounded_holdback(
        ViolationAction::AbortResponse,
        DraftDisposition::Retract,
    )
    .with_holdback_max_duration_ms(250)
    .validate()?;
    OutputDeliveryPolicy::bounded_holdback(
        ViolationAction::AbortResponse,
        DraftDisposition::Retract,
    )
    .with_holdback_max_bytes(4_096)
    .validate()?;
    Ok(())
}

#[test]
fn buffer_until_commit_policy_rejects_flush_boundaries() {
    assert_eq!(
        OutputDeliveryPolicy::buffer_until_commit(ViolationAction::AbortResponse)
            .flush_on([FlushBoundary::Sentence])
            .validate(),
        Err(OutputDeliveryPolicyError::FlushBoundaryWithoutStreaming),
    );
}

#[test]
fn buffer_until_commit_policy_rejects_holdback_limits() {
    assert_eq!(
        OutputDeliveryPolicy::buffer_until_commit(ViolationAction::AbortResponse)
            .with_holdback_max_tokens(48)
            .validate(),
        Err(OutputDeliveryPolicyError::HoldbackLimitWithoutHoldback),
    );
}

#[test]
fn output_policy_decision_preserves_metadata_and_redaction_instructions() {
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
    .with_reason_codes(["pii.detected", "secret.detected"])
    .with_policy_refs(["policy/output-standard", "rule/pii"])
    .with_redactions([RedactionInstruction::text_range(
        "/chunks/4/text",
        5,
        17,
        "[redacted]",
    )]);

    assert_eq!(
        decision.reason_codes,
        vec!["pii.detected", "secret.detected"]
    );
    assert_eq!(
        decision.policy_refs,
        vec!["policy/output-standard", "rule/pii"]
    );
    assert_eq!(
        decision.redactions,
        vec![RedactionInstruction::text_range(
            "/chunks/4/text",
            5,
            17,
            "[redacted]",
        )]
    );
}

#[test]
fn output_policy_decision_requires_input_digest() {
    let decision = OutputPolicyDecision::allow("decision-1", Some(1), " ");

    assert_eq!(
        decision.validate(),
        Err(OutputPolicyDecisionError::MissingInputDigest {
            decision_id: "decision-1".to_owned(),
        })
    );
}

#[test]
fn output_policy_decision_rejects_invalid_redaction_instructions() {
    let blank_path = OutputPolicyDecision::redact(
        "decision-redact",
        Some(1),
        Vec::<GenerationChunk>::new(),
        "sha256:redact",
    )
    .with_redactions([RedactionInstruction::text_range(" ", 0, 6, "[redacted]")]);
    assert_eq!(
        blank_path.validate(),
        Err(OutputPolicyDecisionError::InvalidRedactionInstruction {
            path: " ".to_owned(),
        })
    );

    let reversed_range = OutputPolicyDecision::redact(
        "decision-redact",
        Some(1),
        Vec::<GenerationChunk>::new(),
        "sha256:redact",
    )
    .with_redactions([RedactionInstruction::text_range(
        "/chunks/1/text",
        6,
        5,
        "[redacted]",
    )]);
    assert_eq!(
        reversed_range.validate(),
        Err(OutputPolicyDecisionError::InvalidRedactionInstruction {
            path: "/chunks/1/text".to_owned(),
        })
    );
}

#[test]
fn declarative_output_policy_rules_reject_invalid_contracts() {
    let empty_rule_id =
        DeclarativeOutputPolicyRule::new(" ", "secret", OutputDisposition::AbortResponse);
    assert_eq!(
        empty_rule_id.validate(),
        Err(DeclarativeOutputPolicyRuleError::EmptyRuleId)
    );

    let empty_literal =
        DeclarativeOutputPolicyRule::new("redact-empty-literal", "", OutputDisposition::Redact)
            .with_replacement("[redacted]");
    assert_eq!(
        empty_literal.validate(),
        Err(DeclarativeOutputPolicyRuleError::EmptyLiteral {
            rule_id: "redact-empty-literal".to_owned(),
        })
    );

    let missing_replacement =
        DeclarativeOutputPolicyRule::new("redact-secret", "secret", OutputDisposition::Redact);
    assert_eq!(
        missing_replacement.validate(),
        Err(DeclarativeOutputPolicyRuleError::ReplacementRequired {
            rule_id: "redact-secret".to_owned(),
            disposition: OutputDisposition::Redact,
        })
    );

    let blank_reason = DeclarativeOutputPolicyRule::new(
        "blocked-secret",
        "secret",
        OutputDisposition::AbortResponse,
    )
    .with_reason_codes(["secret.detected", " "]);
    assert_eq!(
        blank_reason.validate(),
        Err(DeclarativeOutputPolicyRuleError::InvalidReasonCode {
            rule_id: "blocked-secret".to_owned(),
            reason_code: " ".to_owned(),
        })
    );

    let blank_policy_ref = DeclarativeOutputPolicyRule::new(
        "blocked-secret",
        "secret",
        OutputDisposition::AbortResponse,
    )
    .with_policy_refs(["policy/output-standard", ""]);
    assert_eq!(
        blank_policy_ref.validate(),
        Err(DeclarativeOutputPolicyRuleError::InvalidPolicyRef {
            rule_id: "blocked-secret".to_owned(),
            policy_ref: "".to_owned(),
        })
    );

    let evaluator = DeclarativeOutputPolicyEvaluator::new([empty_literal]);
    assert_eq!(
        evaluator.evaluate_chunk_checked(
            &GenerationChunk::text("stream-1", "response-1", 1, "secret"),
            1_000,
        ),
        Err(DeclarativeOutputPolicyRuleError::EmptyLiteral {
            rule_id: "redact-empty-literal".to_owned(),
        })
    );
}

#[test]
fn declarative_output_policy_evaluator_allows_unmatched_chunk() {
    let evaluator = DeclarativeOutputPolicyEvaluator::new([DeclarativeOutputPolicyRule::new(
        "blocked-secret",
        "secret",
        OutputDisposition::AbortResponse,
    )]);
    let decision = evaluator.evaluate_chunk(
        &GenerationChunk::text("stream-1", "response-1", 3, "safe response"),
        1_000,
    );

    assert_eq!(decision.disposition, OutputDisposition::Allow);
    assert_eq!(decision.accepted_through_sequence, Some(3));
    assert!(decision.input_digest.starts_with("sha256:"));
    assert_eq!(decision.evaluated_at_unix_ms, Some(1_000));
    assert!(decision.reason_codes.is_empty());
    assert!(decision.policy_refs.is_empty());
}

#[test]
fn declarative_output_policy_evaluator_rejects_zero_evaluation_timestamp() {
    let evaluator = DeclarativeOutputPolicyEvaluator::new([DeclarativeOutputPolicyRule::new(
        "blocked-secret",
        "secret",
        OutputDisposition::AbortResponse,
    )]);

    assert_eq!(
        evaluator.evaluate_chunk_checked(
            &GenerationChunk::text("stream-1", "response-1", 4, "unsafe secret"),
            0,
        ),
        Err(DeclarativeOutputPolicyRuleError::InvalidEvaluationTimestamp {
            evaluated_at_unix_ms: 0,
        })
    );
}

#[test]
fn declarative_output_policy_evaluator_redacts_literal_match() {
    let evaluator = DeclarativeOutputPolicyEvaluator::new([DeclarativeOutputPolicyRule::new(
        "redact-secret",
        "secret",
        OutputDisposition::Redact,
    )
    .with_replacement("[redacted]")
    .with_reason_codes(["secret.detected"])
    .with_policy_refs(["policy/output-standard#redact-secret"])]);
    let decision = evaluator.evaluate_chunk(
        &GenerationChunk::text("stream-1", "response-1", 4, "safe secret suffix"),
        1_010,
    );

    assert_eq!(decision.disposition, OutputDisposition::Redact);
    assert_eq!(decision.accepted_through_sequence, Some(4));
    assert_eq!(
        decision.redactions,
        vec![RedactionInstruction::text_range(
            "/chunks/4/text",
            5,
            11,
            "[redacted]",
        )]
    );
    assert_eq!(decision.reason_codes, vec!["secret.detected"]);
    assert_eq!(
        decision.policy_refs,
        vec!["policy/output-standard#redact-secret"]
    );
    assert_eq!(decision.evaluated_at_unix_ms, Some(1_010));
}

#[test]
fn output_redaction_offsets_are_character_positions() -> Result<(), OutputGateError> {
    let evaluator = DeclarativeOutputPolicyEvaluator::new([DeclarativeOutputPolicyRule::new(
        "redact-secret",
        "secret",
        OutputDisposition::Redact,
    )
    .with_replacement("[redacted]")]);
    let decision = evaluator.evaluate_chunk(
        &GenerationChunk::text("stream-1", "response-1", 1, "safe 🔐 secret suffix"),
        1_010,
    );

    assert_eq!(
        decision.redactions,
        vec![RedactionInstruction::text_range(
            "/chunks/1/text",
            7,
            13,
            "[redacted]",
        )]
    );

    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "safe 🔐 secret suffix",
    ))?;

    let update = gate.apply_decision(decision, 1_020)?;

    assert_eq!(
        update
            .deliverable
            .iter()
            .map(|chunk| chunk.text.as_str())
            .collect::<Vec<_>>(),
        vec!["safe 🔐 [redacted] suffix"],
    );
    Ok(())
}

#[test]
fn declarative_output_policy_evaluator_aborts_on_blocked_literal() {
    let evaluator = DeclarativeOutputPolicyEvaluator::new([DeclarativeOutputPolicyRule::new(
        "blocked-secret",
        "secret",
        OutputDisposition::AbortResponse,
    )
    .with_reason_codes(["secret.detected"])]);
    let decision = evaluator.evaluate_chunk(
        &GenerationChunk::text("stream-1", "response-1", 5, "unsafe secret"),
        1_020,
    );

    assert_eq!(decision.disposition, OutputDisposition::AbortResponse);
    assert_eq!(decision.accepted_through_sequence, None);
    assert_eq!(
        decision.pending_tool_calls,
        PendingToolCallsDisposition::Deny
    );
    assert_eq!(decision.draft_disposition, DraftDisposition::Retract);
    assert_eq!(decision.reason_codes, vec!["secret.detected"]);
    assert_eq!(decision.policy_refs, vec!["blocked-secret"]);
    assert_eq!(decision.evaluated_at_unix_ms, Some(1_020));
}

#[test]
fn immediate_draft_requires_incomplete_or_retraction_semantics() {
    let policy = OutputDeliveryPolicy::immediate_draft(
        ViolationAction::AbortResponse,
        DraftDisposition::Keep,
    );

    assert_eq!(
        policy.validate(),
        Err(OutputDeliveryPolicyError::ImmediateDraftWithoutRetractionSupport),
    );
}

#[test]
fn immediate_draft_policy_rejects_flush_boundaries() {
    assert_eq!(
        OutputDeliveryPolicy::immediate_draft(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        )
        .flush_on([FlushBoundary::Sentence])
        .validate(),
        Err(OutputDeliveryPolicyError::FlushBoundaryWithoutStreaming),
    );
}

#[test]
fn immediate_draft_delivers_before_policy_and_retracts_on_abort() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::immediate_draft(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        ),
    )?;

    let delivered = gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "provisional draft",
    ))?;

    assert_eq!(
        delivered
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "provisional draft")]
    );
    assert_eq!(gate.last_generated_sequence(), 1);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 1);

    let stopped = gate.apply_decision(
        OutputPolicyDecision::abort_response("decision-abort", "sha256:blocked")
            .with_draft_disposition(DraftDisposition::Retract),
        1_050,
    )?;
    let cutoff = stopped.cutoff.expect("policy abort records cutoff");

    assert_eq!(cutoff.last_generated_sequence, 1);
    assert_eq!(cutoff.last_policy_accepted_sequence, 0);
    assert_eq!(cutoff.last_client_delivered_sequence, 1);
    assert_eq!(cutoff.draft_disposition, DraftDisposition::Retract);
    assert_eq!(
        gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 2, "late",)),
        Err(OutputGateError::PolicyStopped),
    );
    Ok(())
}

#[test]
fn terminal_decision_rejects_invalid_output_cutoff_semantics() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::immediate_draft(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        ),
    )?;

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "provisional draft",
    ))?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::abort_response("decision-abort", "sha256:blocked")
                .with_draft_disposition(DraftDisposition::Keep),
            1_050,
        ),
        Err(OutputGateError::InvalidCutoff {
            source: OutputCutoffError::DeliveredDraftBeyondPolicyAcceptanceKept {
                last_policy_accepted_sequence: 0,
                last_client_delivered_sequence: 1,
            }
        })
    );
    assert_eq!(gate.cutoff(), None);
    assert_eq!(gate.last_generated_sequence(), 1);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 1);
    Ok(())
}

#[test]
fn output_cutoff_accepts_only_already_delivered_output_for_its_response() {
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
        policy_decision_id: Some("decision-1".to_owned()),
        occurred_at_unix_ms: 1_000,
    };

    let accepted = GenerationChunk::text("stream-1", "response-1", 1, "safe");
    let delayed = GenerationChunk::text("stream-1", "response-1", 2, "blocked");
    let other_response = GenerationChunk::text("stream-1", "response-2", 1, "replacement");

    assert!(cutoff.accepts(&accepted));
    assert!(!cutoff.accepts(&delayed));
    assert!(!cutoff.accepts(&other_response));
    assert!(!cutoff.accepts_sequence(0));
}

#[test]
fn buffer_until_commit_holds_accepted_chunks_until_output_commit() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::buffer_until_commit(ViolationAction::AbortResponse),
    )?;

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "hello "))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 2, "world"))?;

    let held = gate.apply_decision(
        OutputPolicyDecision::allow("decision-1", Some(2), "sha256:accepted"),
        1_000,
    )?;

    assert!(held.deliverable.is_empty());
    assert_eq!(gate.last_policy_accepted_sequence(), 2);
    assert_eq!(gate.last_client_delivered_sequence(), 0);

    let committed = gate.commit_accepted_output();

    assert_eq!(
        committed
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "hello "), (2, "world")]
    );
    assert_eq!(gate.last_client_delivered_sequence(), 2);
    Ok(())
}

#[test]
fn buffer_until_commit_exposes_no_rejected_content() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::buffer_until_commit(ViolationAction::AbortResponse),
    )?;

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "safe draft",
    ))?;
    let held = gate.apply_decision(
        OutputPolicyDecision::allow("decision-1", Some(1), "sha256:accepted"),
        1_000,
    )?;
    assert!(held.deliverable.is_empty());

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        2,
        "blocked draft",
    ))?;
    let stopped = gate.apply_decision(
        OutputPolicyDecision::abort_response("decision-abort", "sha256:blocked"),
        1_050,
    )?;

    assert!(stopped.deliverable.is_empty());
    let cutoff = stopped.cutoff.expect("policy abort records cutoff");
    assert_eq!(cutoff.last_generated_sequence, 2);
    assert_eq!(cutoff.last_policy_accepted_sequence, 1);
    assert_eq!(cutoff.last_client_delivered_sequence, 0);
    assert!(gate.commit_accepted_output().is_empty());
    Ok(())
}

#[test]
fn replace_decision_delivers_policy_approved_replacement() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "blocked draft",
    ))?;

    let replaced = gate.apply_decision(
        OutputPolicyDecision::replace(
            "decision-replace",
            Some(1),
            [GenerationChunk::text(
                "stream-1",
                "response-1",
                1,
                "policy-approved replacement",
            )],
            "sha256:replace",
        ),
        1_000,
    )?;

    assert_eq!(
        replaced
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "policy-approved replacement")]
    );
    assert_eq!(replaced.cutoff, None);
    assert_eq!(gate.last_policy_accepted_sequence(), 1);
    assert_eq!(gate.last_client_delivered_sequence(), 1);
    assert!(gate.commit_accepted_output().is_empty());
    Ok(())
}

#[test]
fn replace_decision_delivers_all_policy_approved_replacements() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "blocked draft",
    ))?;

    let replaced = gate.apply_decision(
        OutputPolicyDecision::replace(
            "decision-replace",
            Some(1),
            [
                GenerationChunk::text("stream-1", "response-1", 1, "policy-approved "),
                GenerationChunk::text("stream-1", "response-1", 2, "replacement"),
            ],
            "sha256:replace",
        ),
        1_000,
    )?;

    assert_eq!(
        replaced
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "policy-approved "), (2, "replacement")]
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 2);
    assert_eq!(gate.last_client_delivered_sequence(), 2);
    assert!(gate.commit_accepted_output().is_empty());
    Ok(())
}

#[test]
fn replace_decision_rejects_non_contiguous_replacement_chunks() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "blocked draft",
    ))?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::replace(
                "decision-replace",
                Some(1),
                [
                    GenerationChunk::text("stream-1", "response-1", 1, "policy-approved "),
                    GenerationChunk::text("stream-1", "response-1", 3, "replacement"),
                ],
                "sha256:replace",
            ),
            1_000,
        ),
        Err(OutputGateError::NonContiguousSequence {
            last_generated_sequence: 1,
            attempted_sequence: 3,
        }),
    );
    assert_eq!(gate.last_generated_sequence(), 1);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    assert_eq!(
        gate.pending_chunks()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "blocked draft")]
    );
    Ok(())
}

#[test]
fn replace_decision_rejects_zero_sequence_replacement_chunk() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::replace(
                "decision-replace",
                None,
                [GenerationChunk::text(
                    "stream-1",
                    "response-1",
                    0,
                    "replacement",
                )],
                "sha256:replace",
            ),
            1_000,
        ),
        Err(OutputGateError::InvalidGenerationChunk {
            source: GenerationChunkError::InvalidSequence { sequence: 0 },
        }),
    );

    assert_eq!(gate.last_generated_sequence(), 0);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    assert!(gate.pending_chunks().next().is_none());
    Ok(())
}

#[test]
fn redact_decision_rejects_non_contiguous_replacement_chunks() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "sensitive draft",
    ))?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::redact(
                "decision-redact",
                Some(1),
                [
                    GenerationChunk::text("stream-1", "response-1", 1, "safe "),
                    GenerationChunk::text("stream-1", "response-1", 3, "replacement"),
                ],
                "sha256:redact",
            ),
            1_000,
        ),
        Err(OutputGateError::NonContiguousSequence {
            last_generated_sequence: 1,
            attempted_sequence: 3,
        }),
    );
    assert_eq!(gate.last_generated_sequence(), 1);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    assert_eq!(
        gate.pending_chunks()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "sensitive draft")]
    );
    Ok(())
}

#[test]
fn replace_decision_preserves_earlier_pending_chunks() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "safe "))?;
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        2,
        "context ",
    ))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 3, "secret"))?;

    let replaced = gate.apply_decision(
        OutputPolicyDecision::replace(
            "decision-replace",
            Some(3),
            [GenerationChunk::text(
                "stream-1",
                "response-1",
                3,
                "[redacted]",
            )],
            "sha256:replace",
        ),
        1_000,
    )?;

    assert_eq!(
        replaced
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "safe "), (2, "context "), (3, "[redacted]")]
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 3);
    assert_eq!(gate.last_client_delivered_sequence(), 3);
    Ok(())
}

#[test]
fn redact_decision_rewrites_pending_chunk_before_delivery() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 1, "safe "))?;
    let delivered = gate.apply_decision(
        OutputPolicyDecision::allow("decision-allow", Some(1), "sha256:allow"),
        1_000,
    )?;
    assert_eq!(delivered.deliverable.len(), 1);

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        2,
        "secret value",
    ))?;
    let redacted = gate.apply_decision(
        OutputPolicyDecision::redact(
            "decision-redact",
            Some(2),
            [GenerationChunk::text(
                "stream-1",
                "response-1",
                2,
                "[redacted]",
            )],
            "sha256:redact",
        ),
        1_010,
    )?;

    assert_eq!(
        redacted
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(2, "[redacted]")]
    );
    assert_eq!(redacted.cutoff, None);
    assert_eq!(gate.last_policy_accepted_sequence(), 2);
    assert_eq!(gate.last_client_delivered_sequence(), 2);
    Ok(())
}

#[test]
fn redact_decision_applies_typed_redaction_instruction_before_delivery()
-> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "hello secret world",
    ))?;

    let redacted = gate.apply_decision(
        OutputPolicyDecision::redact(
            "decision-redact",
            Some(1),
            Vec::<GenerationChunk>::new(),
            "sha256:redact",
        )
        .with_redactions([RedactionInstruction::text_range(
            "/chunks/1/text",
            6,
            12,
            "[redacted]",
        )]),
        1_000,
    )?;

    assert_eq!(
        redacted
            .deliverable
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "hello [redacted] world")]
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 1);
    assert_eq!(gate.last_client_delivered_sequence(), 1);
    Ok(())
}

#[test]
fn redact_decision_rejects_invalid_range_without_mutating_pending() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "hello secret world",
    ))?;
    gate.record_chunk(GenerationChunk::text("stream-1", "response-1", 2, "short"))?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::redact(
                "decision-redact",
                Some(2),
                Vec::<GenerationChunk>::new(),
                "sha256:redact",
            )
            .with_redactions([
                RedactionInstruction::text_range("/chunks/1/text", 6, 12, "[redacted]"),
                RedactionInstruction::text_range("/chunks/2/text", 0, 99, "[redacted]"),
            ]),
            1_000,
        ),
        Err(OutputGateError::InvalidRedactionInstruction {
            path: "/chunks/2/text".to_owned(),
        }),
    );
    assert_eq!(
        gate.pending_chunks()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "hello secret world"), (2, "short")]
    );
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    Ok(())
}

#[test]
fn redact_decision_rejects_noncanonical_redaction_target_sequence() -> Result<(), OutputGateError> {
    for path in ["/chunks/+1/text", "/chunks/01/text"] {
        let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
        gate.record_chunk(GenerationChunk::text(
            "stream-1",
            "response-1",
            1,
            "hello secret world",
        ))?;

        assert_eq!(
            gate.apply_decision(
                OutputPolicyDecision::redact(
                    "decision-redact",
                    Some(1),
                    Vec::<GenerationChunk>::new(),
                    "sha256:redact",
                )
                .with_redactions([RedactionInstruction::text_range(
                    path,
                    6,
                    12,
                    "[redacted]",
                )]),
                1_000,
            ),
            Err(OutputGateError::InvalidRedactionInstruction {
                path: path.to_owned(),
            }),
        );
    }
    Ok(())
}

#[test]
fn redact_decision_rejects_already_delivered_redaction_target() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "hello secret world",
    ))?;
    gate.apply_decision(
        OutputPolicyDecision::allow("decision-allow", Some(1), "sha256:allow"),
        1_000,
    )?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::redact(
                "decision-redact",
                Some(1),
                Vec::<GenerationChunk>::new(),
                "sha256:redact",
            )
            .with_redactions([RedactionInstruction::text_range(
                "/chunks/1/text",
                6,
                12,
                "[redacted]",
            )]),
            1_010,
        ),
        Err(OutputGateError::PendingChunkAlreadyDelivered {
            sequence: 1,
            last_client_delivered_sequence: 1,
        }),
    );
    Ok(())
}

#[test]
fn redact_decision_rejects_future_redaction_target() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");
    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "hello secret world",
    ))?;

    assert_eq!(
        gate.apply_decision(
            OutputPolicyDecision::redact(
                "decision-redact",
                Some(1),
                Vec::<GenerationChunk>::new(),
                "sha256:redact",
            )
            .with_redactions([RedactionInstruction::text_range(
                "/chunks/2/text",
                0,
                6,
                "[redacted]",
            )]),
            1_000,
        ),
        Err(OutputGateError::PendingChunkBeyondGenerated {
            sequence: 2,
            last_generated_sequence: 1,
        }),
    );
    Ok(())
}

#[test]
fn redact_decision_without_replacement_holds_original_pending_chunk() -> Result<(), OutputGateError>
{
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1");

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "secret value",
    ))?;

    let redacted = gate.apply_decision(
        OutputPolicyDecision::redact(
            "decision-redact",
            Some(1),
            Vec::<GenerationChunk>::new(),
            "sha256:redact",
        ),
        1_000,
    )?;

    assert!(redacted.deliverable.is_empty());
    assert_eq!(redacted.cutoff, None);
    assert_eq!(gate.last_policy_accepted_sequence(), 0);
    assert_eq!(gate.last_client_delivered_sequence(), 0);
    assert!(gate.commit_accepted_output().is_empty());
    Ok(())
}

#[test]
fn buffer_until_commit_holds_replacement_until_commit() -> Result<(), OutputGateError> {
    let mut gate = OutputDeliveryGate::new("stream-1", "response-1").with_delivery_policy(
        OutputDeliveryPolicy::buffer_until_commit(ViolationAction::AbortResponse),
    )?;

    gate.record_chunk(GenerationChunk::text(
        "stream-1",
        "response-1",
        1,
        "blocked draft",
    ))?;

    let held = gate.apply_decision(
        OutputPolicyDecision::replace(
            "decision-replace",
            Some(1),
            [GenerationChunk::text(
                "stream-1",
                "response-1",
                1,
                "policy-approved replacement",
            )],
            "sha256:replace",
        ),
        1_000,
    )?;

    assert!(held.deliverable.is_empty());
    assert_eq!(gate.last_policy_accepted_sequence(), 1);
    assert_eq!(gate.last_client_delivered_sequence(), 0);

    let committed = gate.commit_accepted_output();
    assert_eq!(
        committed
            .iter()
            .map(|chunk| (chunk.sequence, chunk.text.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "policy-approved replacement")]
    );
    assert_eq!(gate.last_client_delivered_sequence(), 1);
    Ok(())
}
