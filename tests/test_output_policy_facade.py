from __future__ import annotations

import pytest

from graphblocks import (
    ContentPart,
    DeclarativeOutputPolicyEvaluator,
    DeclarativeOutputPolicyRule,
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

    redactions[0]["replacement"] = "[mutated]"
    assert decision.redactions == ({"path": "/parts/0/text", "start": 5, "end": 11, "replacement": "[redacted]"},)
    with pytest.raises(TypeError):
        decision.redactions[0]["replacement"] = "[direct-mutation]"


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
    with pytest.raises(TypeError):
        decision.redactions[0]["replacement"] = "[mutated]"


def test_output_policy_contract_rejects_unknown_literals() -> None:
    with pytest.raises(ValueError, match="invalid output disposition stream"):
        OutputPolicyDecision("decision-1", disposition="stream")

    with pytest.raises(ValueError, match="output policy decisions require an input digest"):
        OutputPolicyDecision("decision-1", disposition="allow")

    with pytest.raises(ValueError, match="invalid provider cancellation force"):
        OutputPolicyDecision.abort_response("decision-1", input_digest="sha256:input").with_provider_cancellation(
            "force"
        )

    with pytest.raises(ValueError, match="invalid output delivery mode stream"):
        OutputDeliveryPolicy(mode="stream")

    with pytest.raises(ValueError, match="invalid flush boundary clause"):
        OutputDeliveryPolicy.bounded_holdback(
            on_violation="abort_response",
            holdback_max_tokens=1,
            flush_boundaries=frozenset({"clause"}),
        )

    with pytest.raises(ValueError, match="invalid terminal reason throttled"):
        OutputCutoff(stream_id="stream-1", response_id="response-1", terminal_reason="throttled")

    with pytest.raises(ValueError, match="invalid output durable result committed"):
        OutputCutoff(stream_id="stream-1", response_id="response-1", durable_result="committed")

    with pytest.raises(ValueError, match="generation chunk sequence must be non-negative"):
        GenerationChunk.text("stream-1", "response-1", -1, "late")

    with pytest.raises(ValueError, match="accepted_through_sequence must be non-negative"):
        OutputPolicyDecision.allow(
            "decision-1",
            accepted_through_sequence=-1,
            input_digest="sha256:input",
        )

    with pytest.raises(ValueError, match="last_generated_sequence must be non-negative"):
        OutputCutoff(stream_id="stream-1", response_id="response-1", last_generated_sequence=-1)


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


def test_declarative_output_policy_evaluator_allows_unmatched_chunk() -> None:
    evaluator = DeclarativeOutputPolicyEvaluator(
        rules=(
            DeclarativeOutputPolicyRule(
                rule_id="blocked-secret",
                literal="secret",
                disposition="abort_response",
            ),
        )
    )
    chunk = GenerationChunk.text("stream-1", "response-1", 3, "safe response")

    decision = evaluator.evaluate_chunk(chunk, evaluated_at="2026-06-23T00:00:00Z")

    assert decision.disposition == "allow"
    assert decision.accepted_through_sequence == 3
    assert decision.reason_codes == ()
    assert decision.policy_refs == ()
    assert decision.input_digest.startswith("sha256:")
    assert decision.evaluated_at == "2026-06-23T00:00:00Z"


def test_declarative_output_policy_evaluator_redacts_literal_match() -> None:
    evaluator = DeclarativeOutputPolicyEvaluator(
        rules=(
            DeclarativeOutputPolicyRule(
                rule_id="redact-secret",
                literal="secret",
                disposition="redact",
                replacement="[redacted]",
                reason_codes=("secret.detected",),
                policy_refs=("policy/output-standard#redact-secret",),
            ),
        )
    )
    chunk = GenerationChunk.text("stream-1", "response-1", 4, "safe secret suffix")

    decision = evaluator.evaluate_chunk(chunk, evaluated_at="2026-06-23T00:00:01Z")

    assert decision.disposition == "redact"
    assert decision.accepted_through_sequence == 4
    assert decision.redactions == (
        {"path": "/chunks/4/text", "start": 5, "end": 11, "replacement": "[redacted]"},
    )
    assert decision.reason_codes == ("secret.detected",)
    assert decision.policy_refs == ("policy/output-standard#redact-secret",)
    assert decision.evaluated_at == "2026-06-23T00:00:01Z"


def test_declarative_output_policy_evaluator_aborts_on_blocked_literal() -> None:
    evaluator = DeclarativeOutputPolicyEvaluator(
        rules=(
            DeclarativeOutputPolicyRule(
                rule_id="blocked-secret",
                literal="secret",
                disposition="abort_response",
                reason_codes=("secret.detected",),
            ),
        )
    )

    decision = evaluator.evaluate_chunk(
        GenerationChunk.text("stream-1", "response-1", 5, "unsafe secret"),
        evaluated_at="2026-06-23T00:00:02Z",
    )

    assert decision.disposition == "abort_response"
    assert decision.accepted_through_sequence is None
    assert decision.pending_tool_calls == "deny"
    assert decision.draft_disposition == "retract"
    assert decision.reason_codes == ("secret.detected",)
    assert decision.policy_refs == ("blocked-secret",)


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


def test_output_delivery_gate_rejects_non_contiguous_generation_sequence() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")

    with pytest.raises(OutputGateError) as error:
        gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "late"))

    assert str(error.value) == "chunk sequence 2 must be next after 0"
    assert gate.last_generated_sequence == 0
    assert gate.last_client_delivered_sequence == 0


def test_output_delivery_gate_rejects_future_accepted_sequence() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello"))

    with pytest.raises(OutputGateError) as error:
        gate.apply_decision(
            OutputPolicyDecision.allow(
                "decision-1",
                accepted_through_sequence=2,
                input_digest="sha256:future",
            ),
            occurred_at="2026-06-23T00:00:01Z",
        )

    assert str(error.value) == "accepted sequence 2 exceeds last generated sequence 1"
    assert gate.last_policy_accepted_sequence == 0
    assert gate.last_client_delivered_sequence == 0


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


def test_output_delivery_gate_redact_without_replacement_holds_original_pending_chunk() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "secret value"))

    redacted = gate.apply_decision(
        OutputPolicyDecision.redact(
            "decision-redact",
            accepted_through_sequence=1,
            input_digest="sha256:redact",
        ),
        occurred_at="2026-06-23T00:00:01Z",
    )

    assert redacted.deliverable == []
    assert redacted.cutoff is None
    assert gate.last_policy_accepted_sequence == 0
    assert gate.last_client_delivered_sequence == 0
    assert gate.commit_accepted_output() == []


def test_output_delivery_gate_delivers_all_replacement_parts() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "blocked draft"))

    replaced = gate.apply_decision(
        OutputPolicyDecision.replace(
            "decision-replace",
            accepted_through_sequence=1,
            replacement_parts=(
                ContentPart(kind="text", text="policy-approved "),
                ContentPart(kind="text", text="replacement"),
            ),
            input_digest="sha256:replace",
        ),
        occurred_at="2026-06-23T00:00:01Z",
    )

    assert [(chunk.sequence, chunk.text) for chunk in replaced.deliverable] == [
        (1, "policy-approved "),
        (2, "replacement"),
    ]
    assert gate.last_policy_accepted_sequence == 2
    assert gate.last_client_delivered_sequence == 2


def test_output_delivery_gate_policy_abort_cuts_off_and_rejects_late_chunks() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1", turn_id="turn-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "safe "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "blocked"))
    gate.apply_decision(
        OutputPolicyDecision.allow("decision-1", accepted_through_sequence=1, input_digest="sha256:first"),
        occurred_at="2026-06-23T00:00:01Z",
    )

    stopped = gate.apply_decision(
        OutputPolicyDecision.abort_response(
            "decision-abort",
            input_digest="sha256:abort",
        ).with_provider_cancellation("required_if_supported"),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert stopped.deliverable == []
    assert stopped.provider_cancellation == "required_if_supported"
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


def test_output_delivery_gate_policy_abort_denies_kept_pending_tool_calls() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "blocked"))

    stopped = gate.apply_decision(
        OutputPolicyDecision.abort_response("decision-abort", input_digest="sha256:blocked").with_pending_tool_calls(
            "keep"
        ),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert stopped.pending_tool_calls == "deny"


def test_output_delivery_gate_immediate_draft_delivers_before_policy_and_retracts_on_abort() -> None:
    gate = OutputDeliveryGate(
        "stream-1",
        "response-1",
        delivery_policy=OutputDeliveryPolicy.immediate_draft(
            on_violation="abort_response",
            delivered_draft_disposition="retract",
        ),
    )

    delivered = gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "provisional draft"))

    assert [(chunk.sequence, chunk.text) for chunk in delivered] == [(1, "provisional draft")]
    assert gate.last_generated_sequence == 1
    assert gate.last_policy_accepted_sequence == 0
    assert gate.last_client_delivered_sequence == 1

    stopped = gate.apply_decision(
        OutputPolicyDecision.abort_response(
            "decision-abort",
            input_digest="sha256:blocked",
        ).with_draft_disposition("retract"),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert stopped.cutoff is not None
    assert stopped.cutoff.last_generated_sequence == 1
    assert stopped.cutoff.last_policy_accepted_sequence == 0
    assert stopped.cutoff.last_client_delivered_sequence == 1
    assert stopped.cutoff.draft_disposition == "retract"
    with pytest.raises(OutputGateError) as error:
        gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "late"))
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
