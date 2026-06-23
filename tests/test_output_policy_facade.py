from __future__ import annotations

import pytest

from graphblocks import (
    ContentPart,
    GenerationChunk,
    OutputCutoff,
    OutputDeliveryGate,
    OutputDeliveryPolicy,
    OutputDeliveryPolicyError,
    OutputGateError,
    OutputPolicyDecision,
)


def test_output_policy_decision_abort_response_sets_safe_defaults() -> None:
    decision = OutputPolicyDecision.abort_response("decision-1", input_digest="sha256:input")

    assert decision.disposition == "abort_response"
    assert decision.accepted_through_sequence is None
    assert decision.provider_cancellation == "request"
    assert decision.draft_disposition == "retract"
    assert decision.pending_tool_calls == "deny"
    assert decision.input_digest == "sha256:input"


def test_output_policy_decision_replace_carries_replacement_parts() -> None:
    replacement = [ContentPart(kind="text", text="policy-approved replacement")]

    decision = OutputPolicyDecision.replace(
        "decision-replace",
        accepted_through_sequence=3,
        replacement_parts=replacement,
        input_digest="sha256:replace",
    )

    assert decision.disposition == "replace"
    assert decision.accepted_through_sequence == 3
    assert decision.replacement_parts == tuple(replacement)
    assert decision.redactions == ()
    assert decision.provider_cancellation == "request"
    assert decision.draft_disposition == "keep"
    assert decision.pending_tool_calls == "keep"
    assert decision.input_digest == "sha256:replace"


def test_output_policy_decision_redact_carries_redaction_instructions() -> None:
    replacement = [ContentPart(kind="text", text="[redacted]")]
    redactions = [{"path": "/parts/0/text", "start": 5, "end": 11, "replacement": "[redacted]"}]

    decision = OutputPolicyDecision.redact(
        "decision-redact",
        accepted_through_sequence=4,
        replacement_parts=replacement,
        redactions=redactions,
        input_digest="sha256:redact",
    )

    assert decision.disposition == "redact"
    assert decision.accepted_through_sequence == 4
    assert decision.replacement_parts == tuple(replacement)
    assert decision.redactions == tuple(redactions)
    assert decision.provider_cancellation == "request"
    assert decision.draft_disposition == "keep"
    assert decision.pending_tool_calls == "keep"
    assert decision.input_digest == "sha256:redact"


def test_output_policy_decision_metadata_builders_are_chainable() -> None:
    decision = (
        OutputPolicyDecision.hold("decision-hold", input_digest="sha256:hold")
        .with_reason_codes(("pii.detected", "secret.detected"))
        .with_policy_refs(("policy/output-standard", "rule/pii"))
        .with_redactions(({"path": "/parts/0/text", "replacement": "[redacted]"},))
        .evaluated_at_time("2026-06-23T00:00:00Z")
    )

    assert decision.reason_codes == ("pii.detected", "secret.detected")
    assert decision.policy_refs == ("policy/output-standard", "rule/pii")
    assert decision.redactions == ({"path": "/parts/0/text", "replacement": "[redacted]"},)
    assert decision.evaluated_at == "2026-06-23T00:00:00Z"


def test_bounded_holdback_requires_size_or_time_bound() -> None:
    policy = OutputDeliveryPolicy.bounded_holdback(on_violation="abort_response")

    with pytest.raises(OutputDeliveryPolicyError) as error:
        policy.validate()

    assert str(error.value) == "bounded_holdback output delivery requires a token, byte, or duration bound"


def test_output_delivery_policy_accepts_bounded_holdback_and_rejects_unsafe_immediate_draft() -> None:
    policy = OutputDeliveryPolicy.bounded_holdback(
        on_violation="abort_response",
        holdback_max_tokens=48,
        holdback_max_duration_ms=250,
        flush_boundaries=frozenset({"sentence", "paragraph", "tool_call"}),
    )

    assert policy.validate() is policy

    unsafe = OutputDeliveryPolicy.immediate_draft(
        on_violation="abort_response",
        delivered_draft_disposition="keep",
    )
    with pytest.raises(OutputDeliveryPolicyError) as error:
        unsafe.validate()

    assert str(error.value) == "immediate_draft requires incomplete or retracted draft semantics"


def test_output_cutoff_discards_delayed_output_after_terminal_cutoff() -> None:
    cutoff = OutputCutoff(
        stream_id="stream-1",
        response_id="response-1",
        turn_id="turn-1",
        last_generated_sequence=3,
        last_policy_accepted_sequence=1,
        last_client_delivered_sequence=1,
        terminal_reason="policy_denied",
        draft_disposition="retract",
        durable_result="none",
        policy_decision_id="decision-1",
        occurred_at="2026-06-23T00:00:00Z",
    )

    accepted = GenerationChunk.text("stream-1", "response-1", 1, "safe")
    delayed = GenerationChunk.text("stream-1", "response-1", 2, "blocked")
    other_response = GenerationChunk.text("stream-1", "response-2", 1, "replacement")

    assert cutoff.accepts(accepted) is True
    assert cutoff.accepts(delayed) is False
    assert cutoff.accepts(other_response) is False
    assert cutoff.accepts_sequence(1) is True
    with pytest.raises(TypeError) as error:
        cutoff.accepts(1)  # type: ignore[arg-type]

    assert str(error.value) == "OutputCutoff.accepts requires a GenerationChunk"


def test_output_delivery_gate_releases_only_policy_accepted_chunks() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")

    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "world"))

    first = gate.apply_decision(
        OutputPolicyDecision.allow(
            "decision-1",
            accepted_through_sequence=1,
            input_digest="sha256:first",
        ),
        occurred_at="2026-06-23T00:00:01Z",
    )
    assert [(chunk.sequence, chunk.text) for chunk in first.deliverable] == [(1, "hello ")]
    assert first.cutoff is None
    assert gate.last_policy_accepted_sequence == 1
    assert gate.last_client_delivered_sequence == 1

    second = gate.apply_decision(
        OutputPolicyDecision.allow(
            "decision-2",
            accepted_through_sequence=2,
            input_digest="sha256:second",
        ),
        occurred_at="2026-06-23T00:00:02Z",
    )
    assert [(chunk.sequence, chunk.text) for chunk in second.deliverable] == [(2, "world")]
    assert second.cutoff is None
    assert gate.last_client_delivered_sequence == 2


def test_output_delivery_gate_applies_typed_redaction_instruction_before_delivery() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello secret world"))

    redacted = gate.apply_decision(
        OutputPolicyDecision.redact(
            "decision-redact",
            accepted_through_sequence=1,
            redactions=({"path": "/chunks/1/text", "start": 6, "end": 12, "replacement": "[redacted]"},),
            input_digest="sha256:redact",
        ),
        occurred_at="2026-06-23T00:00:01Z",
    )

    assert [(chunk.sequence, chunk.text) for chunk in redacted.deliverable] == [(1, "hello [redacted] world")]
    assert gate.last_policy_accepted_sequence == 1
    assert gate.last_client_delivered_sequence == 1


def test_output_delivery_gate_policy_abort_cuts_off_and_rejects_late_chunks() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1", turn_id="turn-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "safe "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "blocked"))
    gate.apply_decision(
        OutputPolicyDecision.allow("decision-1", accepted_through_sequence=1, input_digest="sha256:first"),
        occurred_at="2026-06-23T00:00:01Z",
    )

    stopped = gate.apply_decision(
        OutputPolicyDecision.abort_response("decision-abort", input_digest="sha256:abort"),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert stopped.deliverable == []
    assert stopped.pending_tool_calls == "deny"
    assert stopped.cutoff is not None
    assert stopped.cutoff.turn_id == "turn-1"
    assert stopped.cutoff.last_generated_sequence == 2
    assert stopped.cutoff.last_policy_accepted_sequence == 1
    assert stopped.cutoff.last_client_delivered_sequence == 1
    assert stopped.cutoff.policy_decision_id == "decision-abort"

    with pytest.raises(OutputGateError) as error:
        gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 3, "late"))
    assert str(error.value) == "output gate is policy stopped"


def test_output_delivery_gate_buffer_until_commit_holds_accepted_chunks() -> None:
    gate = OutputDeliveryGate(
        "stream-1",
        "response-1",
        delivery_policy=OutputDeliveryPolicy.buffer_until_commit(on_violation="abort_response"),
    )
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "world"))

    held = gate.apply_decision(
        OutputPolicyDecision.allow("decision-1", accepted_through_sequence=2, input_digest="sha256:accepted"),
        occurred_at="2026-06-23T00:00:01Z",
    )

    assert held.deliverable == []
    assert gate.last_policy_accepted_sequence == 2
    assert gate.last_client_delivered_sequence == 0
    assert [(chunk.sequence, chunk.text) for chunk in gate.commit_accepted_output()] == [(1, "hello "), (2, "world")]
