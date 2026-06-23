from __future__ import annotations

import pytest

from graphblocks import (
    GenerationChunk,
    OutputCutoff,
    OutputDeliveryPolicy,
    OutputDeliveryPolicyError,
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

    assert cutoff.accepts(accepted) is True
    assert cutoff.accepts(delayed) is False
