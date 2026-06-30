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


def test_output_policy_decision_rejects_invalid_redaction_instructions() -> None:
    with pytest.raises(ValueError, match="redaction path must not be empty"):
        OutputPolicyDecision.redact(
            "decision-redact",
            accepted_through_sequence=1,
            redactions=({"path": " ", "start": 0, "end": 6, "replacement": "[redacted]"},),
            input_digest="sha256:redact",
        )

    with pytest.raises(ValueError, match="redaction range must not be reversed"):
        OutputPolicyDecision.redact(
            "decision-redact",
            accepted_through_sequence=1,
            redactions=(
                {"path": "/chunks/1/text", "start": 6, "end": 5, "replacement": "[redacted]"},
            ),
            input_digest="sha256:redact",
        )


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


def test_declarative_output_policy_rule_rejects_invalid_metadata_values() -> None:
    with pytest.raises(ValueError, match="output policy rule reason codes must be non-empty strings"):
        DeclarativeOutputPolicyRule(
            rule_id="blocked-secret",
            literal="secret",
            disposition="abort_response",
            reason_codes=("secret.detected", " "),
        )

    with pytest.raises(ValueError, match="output policy rule policy refs must be non-empty strings"):
        DeclarativeOutputPolicyRule(
            rule_id="blocked-secret",
            literal="secret",
            disposition="abort_response",
            policy_refs=(1,),  # type: ignore[arg-type]
        )


def test_declarative_output_policy_rule_rejects_invalid_field_types() -> None:
    with pytest.raises(ValueError, match="output policy rule literal must be a string"):
        DeclarativeOutputPolicyRule(
            rule_id="blocked-secret",
            literal=["secret"],  # type: ignore[arg-type]
            disposition="abort_response",
        )

    with pytest.raises(ValueError, match="output policy rule replacement must be a string"):
        DeclarativeOutputPolicyRule(
            rule_id="redact-secret",
            literal="secret",
            disposition="redact",
            replacement={"text": "[redacted]"},  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="output policy rule priority must be an integer"):
        DeclarativeOutputPolicyRule(
            rule_id="blocked-secret",
            literal="secret",
            disposition="abort_response",
            priority="high",  # type: ignore[arg-type]
        )


def test_output_policy_contract_rejects_unknown_literals() -> None:
    with pytest.raises(ValueError, match="invalid output disposition stream"):
        OutputPolicyDecision("decision-1", disposition="stream")

    with pytest.raises(ValueError, match="output policy decisions require a decision id"):
        OutputPolicyDecision.allow(
            " ",
            accepted_through_sequence=1,
            input_digest="sha256:input",
        )

    with pytest.raises(ValueError, match="output policy decisions require an input digest"):
        OutputPolicyDecision("decision-1", disposition="allow")

    with pytest.raises(ValueError, match="replace output policy decisions require replacement content"):
        OutputPolicyDecision.replace(
            "decision-replace",
            accepted_through_sequence=1,
            input_digest="sha256:replace",
        )

    with pytest.raises(ValueError, match="output policy replacement parts must be ContentPart"):
        OutputPolicyDecision.replace(
            "decision-replace",
            accepted_through_sequence=1,
            replacement_parts=("approved",),  # type: ignore[arg-type]
            input_digest="sha256:replace",
        )

    with pytest.raises(ValueError, match="output policy replacement parts must be ContentPart"):
        OutputPolicyDecision.redact(
            "decision-redact",
            accepted_through_sequence=1,
            replacement_parts=("approved",),  # type: ignore[arg-type]
            input_digest="sha256:redact",
        )

    with pytest.raises(ValueError, match="output policy reason codes must be non-empty strings"):
        OutputPolicyDecision.hold("decision-hold", input_digest="sha256:hold").with_reason_codes((" ",))

    with pytest.raises(ValueError, match="output policy policy refs must be non-empty strings"):
        OutputPolicyDecision.hold("decision-hold", input_digest="sha256:hold").with_policy_refs((1,))  # type: ignore[arg-type]

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

    with pytest.raises(ValueError, match="output cutoff stream_id must not be empty"):
        OutputCutoff(stream_id=" ", response_id="response-1")

    with pytest.raises(ValueError, match="output cutoff response_id must not be empty"):
        OutputCutoff(stream_id="stream-1", response_id="")

    with pytest.raises(ValueError, match="output cutoff turn_id must not be empty"):
        OutputCutoff(stream_id="stream-1", response_id="response-1", turn_id=" ")

    with pytest.raises(ValueError, match="output cutoff policy_decision_id must not be empty"):
        OutputCutoff(stream_id="stream-1", response_id="response-1", policy_decision_id=" ")

    with pytest.raises(ValueError, match="output cutoff occurred_at must not be empty"):
        OutputCutoff(stream_id="stream-1", response_id="response-1", occurred_at=" ")

    with pytest.raises(ValueError, match="invalid output durable result committed"):
        OutputCutoff(stream_id="stream-1", response_id="response-1", durable_result="committed")

    with pytest.raises(ValueError, match="generation chunk sequence must be non-negative"):
        GenerationChunk.text("stream-1", "response-1", -1, "late")

    with pytest.raises(ValueError, match="generation chunk stream_id must not be empty"):
        GenerationChunk.text(" ", "response-1", 1, "late")

    with pytest.raises(ValueError, match="generation chunk response_id must not be empty"):
        GenerationChunk.text("stream-1", "", 1, "late")

    with pytest.raises(ValueError, match="generation chunk text must be a string"):
        GenerationChunk.text("stream-1", "response-1", 1, {"kind": "text"})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="output gate stream_id must not be empty"):
        OutputDeliveryGate(" ", "response-1")

    with pytest.raises(ValueError, match="output gate response_id must not be empty"):
        OutputDeliveryGate("stream-1", "")

    with pytest.raises(ValueError, match="output gate turn_id must not be empty"):
        OutputDeliveryGate("stream-1", "response-1", turn_id=" ")

    with pytest.raises(ValueError, match="accepted_through_sequence must be non-negative"):
        OutputPolicyDecision.allow(
            "decision-1",
            accepted_through_sequence=-1,
            input_digest="sha256:input",
        )

    with pytest.raises(ValueError, match="last_generated_sequence must be non-negative"):
        OutputCutoff(stream_id="stream-1", response_id="response-1", last_generated_sequence=-1)

    with pytest.raises(ValueError, match="last_policy_accepted_sequence cannot exceed last_generated_sequence"):
        OutputCutoff(
            stream_id="stream-1",
            response_id="response-1",
            last_generated_sequence=1,
            last_policy_accepted_sequence=2,
        )

    with pytest.raises(ValueError, match="last_client_delivered_sequence cannot exceed last_generated_sequence"):
        OutputCutoff(
            stream_id="stream-1",
            response_id="response-1",
            last_generated_sequence=1,
            last_client_delivered_sequence=2,
        )


def test_output_policy_contract_rejects_non_string_identifiers() -> None:
    with pytest.raises(ValueError, match="generation chunk stream_id must be a string"):
        GenerationChunk.text(object(), "response-1", 1, "late")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="generation chunk response_id must be a string"):
        GenerationChunk.text("stream-1", object(), 1, "late")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="output policy decision_id must be a string"):
        OutputPolicyDecision.allow(
            object(),  # type: ignore[arg-type]
            accepted_through_sequence=1,
            input_digest="sha256:input",
        )

    with pytest.raises(ValueError, match="output policy input_digest must be a string"):
        OutputPolicyDecision.allow(
            "decision-1",
            accepted_through_sequence=1,
            input_digest=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="output policy replacement parts must be ContentPart"):
        OutputPolicyDecision.replace(
            "decision-replace",
            accepted_through_sequence=1,
            replacement_parts="approved",  # type: ignore[arg-type]
            input_digest="sha256:replace",
        )

    with pytest.raises(ValueError, match="redaction path must be a string"):
        OutputPolicyDecision.redact(
            "decision-redact",
            accepted_through_sequence=1,
            redactions=({"path": object(), "start": 0, "end": 6, "replacement": "[redacted]"},),
            input_digest="sha256:redact",
        )


def test_output_policy_contract_rejects_non_integer_sequences_and_bounds() -> None:
    with pytest.raises(ValueError, match="generation chunk sequence must be an integer"):
        GenerationChunk.text("stream-1", "response-1", True, "late")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="generation chunk sequence must be an integer"):
        GenerationChunk.text("stream-1", "response-1", "1", "late")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="accepted_through_sequence must be an integer"):
        OutputPolicyDecision.allow(
            "decision-1",
            accepted_through_sequence=True,  # type: ignore[arg-type]
            input_digest="sha256:input",
        )

    with pytest.raises(ValueError, match="redaction range must use integer start and end"):
        OutputPolicyDecision.redact(
            "decision-redact",
            accepted_through_sequence=1,
            redactions=({"path": "/chunks/1/text", "start": False, "end": 6, "replacement": "[redacted]"},),
            input_digest="sha256:redact",
        )

    with pytest.raises(ValueError, match="holdback_max_tokens must be an integer"):
        OutputDeliveryPolicy.bounded_holdback(
            on_violation="abort_response",
            holdback_max_tokens=True,  # type: ignore[arg-type]
        ).validate()

    with pytest.raises(ValueError, match="last_generated_sequence must be an integer"):
        OutputCutoff(
            stream_id="stream-1",
            response_id="response-1",
            last_generated_sequence=True,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="output gate last_generated_sequence must be an integer"):
        OutputDeliveryGate(
            "stream-1",
            "response-1",
            last_generated_sequence=True,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="last_policy_accepted_sequence cannot exceed last_generated_sequence"):
        OutputDeliveryGate(
            "stream-1",
            "response-1",
            last_generated_sequence=1,
            last_policy_accepted_sequence=2,
        )

    with pytest.raises(ValueError, match="last_client_delivered_sequence cannot exceed last_generated_sequence"):
        OutputDeliveryGate(
            "stream-1",
            "response-1",
            last_generated_sequence=1,
            last_client_delivered_sequence=2,
        )


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
    assert cutoff.accepts_sequence(-1) is False
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


def test_output_delivery_gate_resumes_pending_holdback_state() -> None:
    gate = OutputDeliveryGate.from_state(
        "stream-1",
        "response-1",
        pending=(GenerationChunk.text("stream-1", "response-1", 2, "held"),),
        last_generated_sequence=2,
        last_policy_accepted_sequence=1,
        last_client_delivered_sequence=1,
    )

    update = gate.apply_decision(
        OutputPolicyDecision.allow(
            "decision-2",
            accepted_through_sequence=2,
            input_digest="sha256:second",
        ),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert [(chunk.sequence, chunk.text) for chunk in update.deliverable] == [(2, "held")]
    assert gate.last_generated_sequence == 2
    assert gate.last_policy_accepted_sequence == 2
    assert gate.last_client_delivered_sequence == 2

    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 3, "next"))
    assert [(chunk.sequence, chunk.text) for chunk in gate.pending_chunks()] == [(3, "next")]


def test_output_delivery_gate_rejects_missing_pending_resume_chunk() -> None:
    with pytest.raises(OutputGateError) as error:
        OutputDeliveryGate.from_state(
            "stream-1",
            "response-1",
            pending=(),
            last_generated_sequence=2,
            last_policy_accepted_sequence=1,
            last_client_delivered_sequence=1,
        )

    assert str(error.value) == "missing pending chunk 2"


def test_output_delivery_gate_resumes_terminal_cutoff_state() -> None:
    cutoff = OutputCutoff(
        stream_id="stream-1",
        response_id="response-1",
        turn_id="turn-1",
        last_generated_sequence=2,
        last_policy_accepted_sequence=1,
        last_client_delivered_sequence=1,
        terminal_reason="policy_denied",
        draft_disposition="retract",
        durable_result="none",
        policy_decision_id="decision-abort",
        occurred_at="2026-06-23T00:00:02Z",
    )
    gate = OutputDeliveryGate.from_cutoff(cutoff)

    assert gate.cutoff == cutoff
    assert gate.last_generated_sequence == 2
    assert gate.last_policy_accepted_sequence == 1
    assert gate.last_client_delivered_sequence == 1

    with pytest.raises(OutputGateError) as chunk_error:
        gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 3, "late"))
    with pytest.raises(OutputGateError) as decision_error:
        gate.apply_decision(
            OutputPolicyDecision.allow(
                "decision-late",
                accepted_through_sequence=2,
                input_digest="sha256:late",
            ),
            occurred_at="2026-06-23T00:00:03Z",
        )

    assert str(chunk_error.value) == "output gate is policy stopped"
    assert str(decision_error.value) == "output gate is policy stopped"


def test_output_delivery_gate_turn_id_update_keeps_restored_cutoff_in_sync() -> None:
    gate = OutputDeliveryGate.from_cutoff(
        OutputCutoff(
            stream_id="stream-1",
            response_id="response-1",
            turn_id="turn-original",
            last_generated_sequence=2,
            last_policy_accepted_sequence=1,
            last_client_delivered_sequence=1,
            terminal_reason="policy_denied",
            draft_disposition="retract",
            durable_result="none",
            policy_decision_id="decision-abort",
            occurred_at="2026-06-23T00:00:02Z",
        )
    ).with_turn_id("turn-updated")

    assert gate.cutoff is not None
    assert gate.cutoff.turn_id == "turn-updated"


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


def test_output_delivery_gate_enforces_bounded_holdback_byte_limit() -> None:
    gate = OutputDeliveryGate(
        "stream-1",
        "response-1",
        delivery_policy=OutputDeliveryPolicy.bounded_holdback(
            on_violation="abort_response",
            holdback_max_bytes=8,
        ),
    )

    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "safe"))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "text"))

    with pytest.raises(OutputGateError) as error:
        gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 3, "!"))

    assert str(error.value) == "bounded_holdback pending output exceeds 8 bytes"
    assert gate.last_generated_sequence == 2
    assert sorted(gate.pending) == [1, 2]


def test_output_delivery_gate_enforces_bounded_holdback_token_limit() -> None:
    gate = OutputDeliveryGate(
        "stream-1",
        "response-1",
        delivery_policy=OutputDeliveryPolicy.bounded_holdback(
            on_violation="abort_response",
            holdback_max_tokens=3,
        ),
    )

    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "safe text"))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "still"))

    with pytest.raises(OutputGateError) as error:
        gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 3, "blocked"))

    assert str(error.value) == "bounded_holdback pending output exceeds 3 tokens"
    assert gate.last_generated_sequence == 2
    assert sorted(gate.pending) == [1, 2]


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


def test_output_delivery_gate_rejects_redaction_instruction_without_range() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello secret world"))

    with pytest.raises(OutputGateError) as error:
        gate.apply_decision(
            OutputPolicyDecision.redact(
                "decision-redact",
                accepted_through_sequence=1,
                redactions=({"path": "/chunks/1/text", "replacement": "[redacted]"},),
                input_digest="sha256:redact",
            ),
            occurred_at="2026-06-23T00:00:01Z",
        )

    assert str(error.value) == "invalid redaction range for '/chunks/1/text'"


def test_output_delivery_gate_revalidates_redaction_range_types_at_delivery_boundary() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello secret world"))
    decision = OutputPolicyDecision.redact(
        "decision-redact",
        accepted_through_sequence=1,
        redactions=({"path": "/chunks/1/text", "start": 6, "end": 12, "replacement": "[redacted]"},),
        input_digest="sha256:redact",
    )
    object.__setattr__(
        decision,
        "redactions",
        ({"path": "/chunks/1/text", "start": False, "end": 12, "replacement": "[redacted]"},),
    )

    with pytest.raises(OutputGateError) as error:
        gate.apply_decision(decision, occurred_at="2026-06-23T00:00:01Z")

    assert str(error.value) == "invalid redaction range for '/chunks/1/text'"


def test_output_delivery_gate_rejects_negative_redaction_chunk_sequence() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello secret world"))

    with pytest.raises(OutputGateError) as error:
        gate.apply_decision(
            OutputPolicyDecision.redact(
                "decision-redact",
                accepted_through_sequence=1,
                redactions=({"path": "/chunks/-1/text", "start": 6, "end": 12, "replacement": "[redacted]"},),
                input_digest="sha256:redact",
            ),
            occurred_at="2026-06-23T00:00:01Z",
        )

    assert str(error.value) == "invalid redaction path '/chunks/-1/text'"


@pytest.mark.parametrize("path", ("/chunks/+1/text", "/chunks/01/text"))
def test_output_delivery_gate_rejects_noncanonical_redaction_chunk_sequence(path: str) -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello secret world"))

    with pytest.raises(OutputGateError) as error:
        gate.apply_decision(
            OutputPolicyDecision.redact(
                "decision-redact",
                accepted_through_sequence=1,
                redactions=({"path": path, "start": 6, "end": 12, "replacement": "[redacted]"},),
                input_digest="sha256:redact",
            ),
            occurred_at="2026-06-23T00:00:01Z",
        )

    assert str(error.value) == f"invalid redaction path {path!r}"


def test_output_delivery_gate_rejects_already_delivered_redaction_target() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello secret world"))
    gate.apply_decision(
        OutputPolicyDecision.allow(
            "decision-allow",
            accepted_through_sequence=1,
            input_digest="sha256:allow",
        ),
        occurred_at="2026-06-23T00:00:00Z",
    )

    with pytest.raises(OutputGateError) as error:
        gate.apply_decision(
            OutputPolicyDecision.redact(
                "decision-redact",
                accepted_through_sequence=1,
                redactions=(
                    {
                        "path": "/chunks/1/text",
                        "start": 6,
                        "end": 12,
                        "replacement": "[redacted]",
                    },
                ),
                input_digest="sha256:redact",
            ),
            occurred_at="2026-06-23T00:00:01Z",
        )

    assert str(error.value) == "redaction target 1 is already delivered through 1"


def test_output_delivery_gate_rejects_future_redaction_target() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "hello secret world"))

    with pytest.raises(OutputGateError) as error:
        gate.apply_decision(
            OutputPolicyDecision.redact(
                "decision-redact",
                accepted_through_sequence=1,
                redactions=(
                    {
                        "path": "/chunks/2/text",
                        "start": 0,
                        "end": 6,
                        "replacement": "[redacted]",
                    },
                ),
                input_digest="sha256:redact",
            ),
            occurred_at="2026-06-23T00:00:01Z",
        )

    assert str(error.value) == "redaction target 2 exceeds last generated sequence 1"


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


def test_output_delivery_gate_replace_preserves_earlier_pending_chunks() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "safe "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "context "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 3, "secret"))

    replaced = gate.apply_decision(
        OutputPolicyDecision.replace(
            "decision-replace",
            accepted_through_sequence=3,
            replacement_parts=(ContentPart(kind="text", text="[redacted]"),),
            input_digest="sha256:replace",
        ),
        occurred_at="2026-06-23T00:00:01Z",
    )

    assert [(chunk.sequence, chunk.text) for chunk in replaced.deliverable] == [
        (1, "safe "),
        (2, "context "),
        (3, "[redacted]"),
    ]
    assert gate.last_policy_accepted_sequence == 3
    assert gate.last_client_delivered_sequence == 3


def test_output_delivery_gate_rejects_non_text_replacement_parts() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "blocked draft"))

    with pytest.raises(OutputGateError) as error:
        gate.apply_decision(
            OutputPolicyDecision.replace(
                "decision-replace",
                accepted_through_sequence=1,
                replacement_parts=(ContentPart(kind="json", data={"message": "approved"}),),
                input_digest="sha256:replace",
            ),
            occurred_at="2026-06-23T00:00:01Z",
        )

    assert str(error.value) == "replacement part 0 must be text"


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


def test_output_delivery_gate_terminal_decision_records_accepted_prefix() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "safe "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "blocked"))

    stopped = gate.apply_decision(
        OutputPolicyDecision.abort_response("decision-abort", input_digest="sha256:blocked")
        .with_accepted_through_sequence(1),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert stopped.cutoff is not None
    assert stopped.cutoff.last_generated_sequence == 2
    assert stopped.cutoff.last_policy_accepted_sequence == 1
    assert stopped.cutoff.last_client_delivered_sequence == 0
    assert gate.last_policy_accepted_sequence == 1


def test_output_delivery_gate_terminal_decision_requires_occurred_at() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "blocked"))

    with pytest.raises(ValueError, match="output gate occurred_at must not be empty"):
        gate.apply_decision(
            OutputPolicyDecision.abort_response("decision-abort", input_digest="sha256:blocked"),
            occurred_at=" ",
        )

    assert gate.cutoff is None
    assert gate.last_generated_sequence == 1
    assert gate.last_policy_accepted_sequence == 0
    assert gate.last_client_delivered_sequence == 0
    assert [(chunk.sequence, chunk.text) for chunk in gate.pending_chunks()] == [(1, "blocked")]


def test_output_delivery_gate_policy_abort_forces_kept_pending_tool_calls_to_denied_cleanup() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "blocked"))

    stopped = gate.apply_decision(
        OutputPolicyDecision.abort_response("decision-abort", input_digest="sha256:blocked").with_pending_tool_calls(
            "keep"
        ),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert stopped.pending_tool_calls == "deny"


def test_output_delivery_gate_deny_commit_preserves_kept_pending_tool_calls() -> None:
    gate = OutputDeliveryGate("stream-1", "response-1")
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "blocked"))

    stopped = gate.apply_decision(
        OutputPolicyDecision.deny_commit("decision-deny-commit", input_digest="sha256:blocked").with_pending_tool_calls(
            "keep"
        ),
        occurred_at="2026-06-23T00:00:02Z",
    )

    assert stopped.pending_tool_calls == "keep"


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


def test_output_delivery_gate_sentence_flush_boundary_holds_incomplete_suffix() -> None:
    gate = OutputDeliveryGate(
        "stream-1",
        "response-1",
        delivery_policy=OutputDeliveryPolicy.bounded_holdback(
            on_violation="abort_response",
            holdback_max_tokens=16,
            flush_boundaries=frozenset({"sentence"}),
        ),
    )
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "Hello "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "world. "))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 3, "Next"))

    update = gate.apply_decision(
        OutputPolicyDecision.allow("decision-1", accepted_through_sequence=3, input_digest="sha256:accepted"),
        occurred_at="2026-06-23T00:00:01Z",
    )

    assert [(chunk.sequence, chunk.text) for chunk in update.deliverable] == [(1, "Hello "), (2, "world. ")]
    assert gate.last_policy_accepted_sequence == 3
    assert gate.last_client_delivered_sequence == 2
    assert [(chunk.sequence, chunk.text) for chunk in gate.commit_accepted_output()] == [(3, "Next")]


def test_output_delivery_gate_paragraph_flush_boundary_waits_for_blank_line() -> None:
    gate = OutputDeliveryGate(
        "stream-1",
        "response-1",
        delivery_policy=OutputDeliveryPolicy.bounded_holdback(
            on_violation="abort_response",
            holdback_max_tokens=16,
            flush_boundaries=frozenset({"paragraph"}),
        ),
    )
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 1, "First"))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 2, "\n\n"))
    gate.record_chunk(GenerationChunk.text("stream-1", "response-1", 3, "Second"))

    update = gate.apply_decision(
        OutputPolicyDecision.allow("decision-1", accepted_through_sequence=3, input_digest="sha256:accepted"),
        occurred_at="2026-06-23T00:00:01Z",
    )

    assert [(chunk.sequence, chunk.text) for chunk in update.deliverable] == [(1, "First"), (2, "\n\n")]
    assert gate.last_client_delivered_sequence == 2
