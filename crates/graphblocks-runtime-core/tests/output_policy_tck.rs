use graphblocks_runtime_core::output_policy::{
    DraftDisposition, GenerationChunk, OutputDeliveryGate, OutputDeliveryPolicy, OutputGateError,
    OutputGateUpdate, OutputPolicyDecision, PendingToolCallsDisposition, ProviderCancellation,
    RedactionInstruction, ViolationAction,
};
use serde_json::Value;

#[test]
fn rust_output_policy_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("../../../tck/policy/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "policy TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let name = required_str(case, "name", "policy TCK case")?;
    let stream_id = optional_str(case, "streamId").unwrap_or("stream-1");
    let response_id = optional_str(case, "responseId").unwrap_or("response-1");
    let mut gate = OutputDeliveryGate::new(stream_id, response_id);

    if let Some(delivery) = case.get("delivery") {
        gate = gate
            .with_delivery_policy(delivery_policy(delivery, name)?)
            .map_err(|error| {
                format!("policy TCK case {name} has invalid delivery policy: {error:?}")
            })?;
    }

    let operations = case
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("policy TCK case {name} is missing operations"))?;
    for operation in operations {
        let op = required_str(operation, "op", name)?;
        match op {
            "chunk" => {
                let sequence = required_u64(operation, "sequence", name)?;
                let text = required_str(operation, "text", name)?;
                let result = gate.record_chunk(GenerationChunk::text(
                    stream_id,
                    response_id,
                    sequence,
                    text,
                ));
                assert_chunk_result(name, operation, result)?;
            }
            "allow" => {
                let decision = OutputPolicyDecision::allow(
                    required_str(operation, "decisionId", name)?,
                    optional_u64(operation, "acceptedThrough"),
                    required_str(operation, "inputDigest", name)?,
                );
                let result =
                    gate.apply_decision(decision, required_u64(operation, "occurredAt", name)?);
                assert_update_result(name, operation, result)?;
            }
            "redact" | "replace" => {
                let mut replacement_chunks = Vec::new();
                if let Some(chunks) = operation.get("replacementChunks").and_then(Value::as_array) {
                    for chunk in chunks {
                        replacement_chunks.push(GenerationChunk::text(
                            optional_str(chunk, "streamId").unwrap_or(stream_id),
                            optional_str(chunk, "responseId").unwrap_or(response_id),
                            required_u64(chunk, "sequence", name)?,
                            required_str(chunk, "text", name)?,
                        ));
                    }
                }
                let mut decision = if op == "redact" {
                    OutputPolicyDecision::redact(
                        required_str(operation, "decisionId", name)?,
                        optional_u64(operation, "acceptedThrough"),
                        replacement_chunks,
                        required_str(operation, "inputDigest", name)?,
                    )
                } else {
                    OutputPolicyDecision::replace(
                        required_str(operation, "decisionId", name)?,
                        optional_u64(operation, "acceptedThrough"),
                        replacement_chunks,
                        required_str(operation, "inputDigest", name)?,
                    )
                };
                if let Some(redactions) = operation.get("redactions").and_then(Value::as_array) {
                    let mut parsed_redactions = Vec::new();
                    for redaction in redactions {
                        parsed_redactions.push(RedactionInstruction::text_range(
                            required_str(redaction, "path", name)?,
                            required_u64(redaction, "start", name)?,
                            required_u64(redaction, "end", name)?,
                            required_str(redaction, "replacement", name)?,
                        ));
                    }
                    decision = decision.with_redactions(parsed_redactions);
                }
                let result =
                    gate.apply_decision(decision, required_u64(operation, "occurredAt", name)?);
                assert_update_result(name, operation, result)?;
            }
            "abort_response" | "abort_turn" | "deny_commit" => {
                let mut decision = match op {
                    "abort_turn" => OutputPolicyDecision::abort_turn(
                        required_str(operation, "decisionId", name)?,
                        required_str(operation, "inputDigest", name)?,
                    ),
                    "deny_commit" => OutputPolicyDecision::deny_commit(
                        required_str(operation, "decisionId", name)?,
                        required_str(operation, "inputDigest", name)?,
                    ),
                    _ => OutputPolicyDecision::abort_response(
                        required_str(operation, "decisionId", name)?,
                        required_str(operation, "inputDigest", name)?,
                    ),
                };
                if let Some(accepted_through) = optional_u64(operation, "acceptedThrough") {
                    decision = decision.with_accepted_through_sequence(accepted_through);
                }
                if let Some(cancellation) = optional_str(operation, "providerCancellation") {
                    decision = decision
                        .with_provider_cancellation(provider_cancellation(cancellation, name)?);
                }
                if let Some(disposition) = optional_str(operation, "draftDisposition") {
                    decision =
                        decision.with_draft_disposition(draft_disposition(disposition, name)?);
                }
                if let Some(disposition) = optional_str(operation, "pendingToolCalls") {
                    decision =
                        decision.with_pending_tool_calls(pending_tool_calls(disposition, name)?);
                }
                let result =
                    gate.apply_decision(decision, required_u64(operation, "occurredAt", name)?);
                assert_update_result(name, operation, result)?;
            }
            "commit" => {
                let deliverable = gate.commit_accepted_output();
                assert_eq!(
                    chunk_pairs(&deliverable),
                    expected_chunks(operation, "deliver", name)?,
                    "{name}"
                );
            }
            other => {
                return Err(format!(
                    "policy TCK case {name} has unknown operation {other}"
                ));
            }
        }
    }

    if let Some(expected) = case.get("expected") {
        assert_gate_state(name, expected, &gate)?;
    }
    Ok(())
}

fn delivery_policy(value: &Value, name: &str) -> Result<OutputDeliveryPolicy, String> {
    let mode = required_str(value, "mode", name)?;
    let mut policy = match mode {
        "buffer_until_commit" => {
            OutputDeliveryPolicy::buffer_until_commit(ViolationAction::AbortResponse)
        }
        "bounded_holdback" => OutputDeliveryPolicy::bounded_holdback(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        ),
        "immediate_draft" => OutputDeliveryPolicy::immediate_draft(
            ViolationAction::AbortResponse,
            DraftDisposition::Retract,
        ),
        other => {
            return Err(format!(
                "policy TCK case {name} has unknown delivery mode {other}"
            ));
        }
    };
    if let Some(tokens) = optional_u64(value, "holdbackMaxTokens") {
        policy = policy.with_holdback_max_tokens(tokens);
    }
    if let Some(bytes) = optional_u64(value, "holdbackMaxBytes") {
        policy = policy.with_holdback_max_bytes(bytes);
    }
    if let Some(duration_ms) = optional_u64(value, "holdbackMaxDurationMs") {
        policy = policy.with_holdback_max_duration_ms(duration_ms);
    }
    Ok(policy)
}

fn assert_chunk_result(
    name: &str,
    operation: &Value,
    result: Result<Vec<GenerationChunk>, OutputGateError>,
) -> Result<(), String> {
    if let Some(expected_error) = optional_str(operation, "expectError") {
        match result {
            Ok(_) => {
                return Err(format!(
                    "policy TCK case {name} expected gate error {expected_error}, but operation succeeded"
                ));
            }
            Err(error) => assert_eq!(gate_error_name(&error), expected_error, "{name}"),
        }
        return Ok(());
    }
    let deliverable =
        result.map_err(|error| format!("policy TCK case {name} operation failed: {error:?}"))?;
    assert_eq!(
        chunk_pairs(&deliverable),
        expected_chunks(operation, "deliver", name)?,
        "{name}"
    );
    Ok(())
}

fn assert_update_result(
    name: &str,
    operation: &Value,
    result: Result<OutputGateUpdate, OutputGateError>,
) -> Result<(), String> {
    if let Some(expected_error) = optional_str(operation, "expectError") {
        match result {
            Ok(_) => {
                return Err(format!(
                    "policy TCK case {name} expected gate error {expected_error}, but decision succeeded"
                ));
            }
            Err(error) => assert_eq!(gate_error_name(&error), expected_error, "{name}"),
        }
        return Ok(());
    }

    let update =
        result.map_err(|error| format!("policy TCK case {name} decision failed: {error:?}"))?;
    assert_eq!(
        chunk_pairs(&update.deliverable),
        expected_chunks(operation, "deliver", name)?,
        "{name}"
    );

    if let Some(expected) = operation
        .get("providerCancellation")
        .and_then(Value::as_str)
    {
        assert_eq!(
            update
                .provider_cancellation
                .as_ref()
                .map(provider_cancellation_name),
            Some(expected),
            "{name}"
        );
    }
    if let Some(expected) = operation.get("pendingToolCalls").and_then(Value::as_str) {
        assert_eq!(
            update
                .pending_tool_calls
                .as_ref()
                .map(pending_tool_calls_name),
            Some(expected),
            "{name}"
        );
    }
    if let Some(expected_cutoff) = operation.get("cutoff") {
        let cutoff = update
            .cutoff
            .as_ref()
            .ok_or_else(|| format!("policy TCK case {name} expected cutoff"))?;
        if let Some(sequence) = optional_u64(expected_cutoff, "lastGeneratedSequence") {
            assert_eq!(cutoff.last_generated_sequence, sequence, "{name}");
        }
        if let Some(sequence) = optional_u64(expected_cutoff, "lastPolicyAcceptedSequence") {
            assert_eq!(cutoff.last_policy_accepted_sequence, sequence, "{name}");
        }
        if let Some(sequence) = optional_u64(expected_cutoff, "lastClientDeliveredSequence") {
            assert_eq!(cutoff.last_client_delivered_sequence, sequence, "{name}");
        }
        if let Some(disposition) = optional_str(expected_cutoff, "draftDisposition") {
            assert_eq!(
                draft_disposition_name(&cutoff.draft_disposition),
                disposition,
                "{name}"
            );
        }
        if let Some(decision_id) = optional_str(expected_cutoff, "policyDecisionId") {
            assert_eq!(
                cutoff.policy_decision_id.as_deref(),
                Some(decision_id),
                "{name}"
            );
        }
    } else {
        assert_eq!(update.cutoff, None, "{name}");
    }
    Ok(())
}

fn assert_gate_state(
    name: &str,
    expected: &Value,
    gate: &OutputDeliveryGate,
) -> Result<(), String> {
    if let Some(sequence) = optional_u64(expected, "lastGeneratedSequence") {
        assert_eq!(gate.last_generated_sequence(), sequence, "{name}");
    }
    if let Some(sequence) = optional_u64(expected, "lastPolicyAcceptedSequence") {
        assert_eq!(gate.last_policy_accepted_sequence(), sequence, "{name}");
    }
    if let Some(sequence) = optional_u64(expected, "lastClientDeliveredSequence") {
        assert_eq!(gate.last_client_delivered_sequence(), sequence, "{name}");
    }
    if let Some(stopped) = expected.get("stopped").and_then(Value::as_bool) {
        assert_eq!(gate.cutoff().is_some(), stopped, "{name}");
    }
    Ok(())
}

fn expected_chunks(value: &Value, key: &str, owner: &str) -> Result<Vec<(u64, String)>, String> {
    let Some(raw_chunks) = value.get(key) else {
        return Ok(Vec::new());
    };
    let chunks = raw_chunks
        .as_array()
        .ok_or_else(|| format!("{owner} field {key} must be an array"))?;
    chunks
        .iter()
        .map(|chunk| {
            Ok((
                required_u64(chunk, "sequence", owner)?,
                required_str(chunk, "text", owner)?.to_owned(),
            ))
        })
        .collect()
}

fn chunk_pairs(chunks: &[GenerationChunk]) -> Vec<(u64, String)> {
    chunks
        .iter()
        .map(|chunk| (chunk.sequence, chunk.text.clone()))
        .collect()
}

fn required_str<'a>(value: &'a Value, key: &str, owner: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{owner} is missing string field {key}"))
}

fn optional_str<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn required_u64(value: &Value, key: &str, owner: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("{owner} is missing integer field {key}"))
}

fn optional_u64(value: &Value, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}

fn provider_cancellation(raw: &str, name: &str) -> Result<ProviderCancellation, String> {
    match raw {
        "none" => Ok(ProviderCancellation::None),
        "request" => Ok(ProviderCancellation::Request),
        "required_if_supported" => Ok(ProviderCancellation::RequiredIfSupported),
        other => Err(format!(
            "policy TCK case {name} has unknown provider cancellation {other}"
        )),
    }
}

fn provider_cancellation_name(cancellation: &ProviderCancellation) -> &'static str {
    match cancellation {
        ProviderCancellation::None => "none",
        ProviderCancellation::Request => "request",
        ProviderCancellation::RequiredIfSupported => "required_if_supported",
    }
}

fn draft_disposition(raw: &str, name: &str) -> Result<DraftDisposition, String> {
    match raw {
        "keep" => Ok(DraftDisposition::Keep),
        "mark_incomplete" => Ok(DraftDisposition::MarkIncomplete),
        "retract" => Ok(DraftDisposition::Retract),
        other => Err(format!(
            "policy TCK case {name} has unknown draft disposition {other}"
        )),
    }
}

fn draft_disposition_name(disposition: &DraftDisposition) -> &'static str {
    match disposition {
        DraftDisposition::Keep => "keep",
        DraftDisposition::MarkIncomplete => "mark_incomplete",
        DraftDisposition::Retract => "retract",
    }
}

fn pending_tool_calls(raw: &str, name: &str) -> Result<PendingToolCallsDisposition, String> {
    match raw {
        "keep" => Ok(PendingToolCallsDisposition::Keep),
        "deny" => Ok(PendingToolCallsDisposition::Deny),
        "cancel_admitted" => Ok(PendingToolCallsDisposition::CancelAdmitted),
        other => Err(format!(
            "policy TCK case {name} has unknown pending tool disposition {other}"
        )),
    }
}

fn pending_tool_calls_name(disposition: &PendingToolCallsDisposition) -> &'static str {
    match disposition {
        PendingToolCallsDisposition::Keep => "keep",
        PendingToolCallsDisposition::Deny => "deny",
        PendingToolCallsDisposition::CancelAdmitted => "cancel_admitted",
    }
}

fn gate_error_name(error: &OutputGateError) -> &'static str {
    match error {
        OutputGateError::PolicyStopped => "policy_stopped",
        OutputGateError::BoundedHoldbackExceeded { .. } => "bounded_holdback_bytes",
        OutputGateError::BoundedHoldbackTokensExceeded { .. } => "bounded_holdback_tokens",
        OutputGateError::AcceptedSequenceBeyondGenerated { .. } => {
            "accepted_sequence_beyond_generated"
        }
        OutputGateError::NonContiguousSequence { .. } => "non_contiguous_sequence",
        OutputGateError::NonMonotonicSequence { .. } => "non_monotonic_sequence",
        OutputGateError::StreamMismatch { .. } => "stream_mismatch",
        OutputGateError::ResponseMismatch { .. } => "response_mismatch",
        OutputGateError::EmptyIdentityField { .. } => "empty_identity_field",
        OutputGateError::InvalidGenerationChunk { .. } => "invalid_generation_chunk",
        OutputGateError::InvalidDeliveryPolicy { .. } => "invalid_delivery_policy",
        OutputGateError::MissingDecisionId => "missing_decision_id",
        OutputGateError::MissingInputDigest { .. } => "missing_input_digest",
        OutputGateError::ReplacementContentMissing { .. } => "replacement_content_missing",
        OutputGateError::InvalidRedactionInstruction { .. } => "invalid_redaction_instruction",
        OutputGateError::InvalidReasonCode { .. } => "invalid_reason_code",
        OutputGateError::InvalidPolicyRef { .. } => "invalid_policy_ref",
        OutputGateError::MissingOccurredAtUnixMs => "missing_occurred_at_unix_ms",
        OutputGateError::PendingChunkAlreadyDelivered { .. } => "pending_chunk_already_delivered",
        OutputGateError::PendingChunkBeyondGenerated { .. } => "pending_chunk_beyond_generated",
        OutputGateError::DuplicatePendingChunk { .. } => "duplicate_pending_chunk",
        OutputGateError::MissingPendingChunk { .. } => "missing_pending_chunk",
        OutputGateError::InvalidCutoff { .. } => "invalid_cutoff",
        OutputGateError::ClientDeliveredSequenceBeyondGenerated { .. } => {
            "client_delivered_sequence_beyond_generated"
        }
    }
}
