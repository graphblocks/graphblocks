from __future__ import annotations

import pytest

from graphblocks import (
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventMetadata,
    OutputCutoff,
    OutputPolicyDecision,
    STANDARD_APPLICATION_EVENT_KINDS,
    TOOL_APPLICATION_EVENT_KINDS,
)


def _metadata() -> ApplicationEventMetadata:
    return ApplicationEventMetadata(
        event_id="event-1",
        run_id="run-1",
        response_id="response-1",
        turn_id="turn-1",
        sequence=7,
        release_id="release-1",
        policy_snapshot_id="policy-1",
        occurred_at="2026-06-23T00:00:00Z",
    )


def test_standard_application_event_names_match_tool_and_output_policy_contract() -> None:
    assert STANDARD_APPLICATION_EVENT_KINDS == (
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
        "AssistantRetracted",
    )
    assert "ToolCallCompleted" in TOOL_APPLICATION_EVENT_KINDS
    assert "OutputCutoff" not in TOOL_APPLICATION_EVENT_KINDS


def test_tool_events_carry_tool_call_id_and_required_envelope_fields() -> None:
    event = ApplicationEvent.tool(
        "ToolCallCompleted",
        _metadata(),
        tool_call_id="tool-call-1",
        payload={"status": "completed"},
    )

    assert event.kind == "ToolCallCompleted"
    assert event.tool_call_id == "tool-call-1"
    assert event.metadata.event_id == "event-1"
    assert event.metadata.run_id == "run-1"
    assert event.metadata.response_id == "response-1"
    assert event.metadata.turn_id == "turn-1"
    assert event.metadata.sequence == 7
    assert event.metadata.release_id == "release-1"
    assert event.metadata.policy_snapshot_id == "policy-1"
    assert event.payload == {"status": "completed"}


def test_tool_events_cannot_be_created_without_tool_call_id() -> None:
    with pytest.raises(ApplicationEventError) as error:
        ApplicationEvent.new("ToolCallStarted", _metadata(), payload={"status": "running"})

    assert str(error.value) == "tool event ToolCallStarted requires tool_call_id"


def test_non_tool_events_reject_tool_event_constructor() -> None:
    with pytest.raises(ApplicationEventError) as error:
        ApplicationEvent.tool(
            "OutputCutoff",
            _metadata(),
            tool_call_id="tool-call-1",
            payload={"terminal_reason": "policy_denied"},
        )

    assert str(error.value) == "event OutputCutoff is not a tool event"


def test_output_policy_decision_event_maps_disposition_and_metadata_payload() -> None:
    decision = (
        OutputPolicyDecision.redact(
            "decision-redact",
            accepted_through_sequence=4,
            input_digest="sha256:redact",
        )
        .with_reason_codes(("pii.detected",))
        .with_policy_refs(("policy/output-standard",))
        .evaluated_at_time("2026-06-23T00:00:00Z")
    )

    event = ApplicationEvent.output_policy_decision(_metadata(), decision)

    assert event.kind == "OutputPolicyRedacted"
    assert event.tool_call_id is None
    assert event.payload == {
        "decision_id": "decision-redact",
        "disposition": "redact",
        "accepted_through_sequence": 4,
        "reason_codes": ["pii.detected"],
        "policy_refs": ["policy/output-standard"],
        "evaluated_at": "2026-06-23T00:00:00Z",
        "input_digest": "sha256:redact",
        "replacement_part_count": 0,
        "redaction_count": 0,
    }


def test_output_cutoff_events_include_cutoff_and_retraction_semantics() -> None:
    cutoff = OutputCutoff(
        stream_id="stream-1",
        response_id="response-1",
        turn_id="turn-1",
        last_generated_sequence=4,
        last_policy_accepted_sequence=2,
        last_client_delivered_sequence=2,
        terminal_reason="policy_denied",
        draft_disposition="retract",
        durable_result="none",
        policy_decision_id="decision-abort",
        occurred_at="2026-06-23T00:00:01Z",
    )

    events = ApplicationEvent.output_cutoff(_metadata(), cutoff)

    assert [event.kind for event in events] == ["OutputCutoff", "AssistantRetracted"]
    assert events[0].metadata.event_id == "event-1"
    assert events[1].metadata.event_id == "event-1:draft"
    assert events[1].metadata.sequence == events[0].metadata.sequence + 1
    assert events[0].payload == {
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
        "occurred_at": "2026-06-23T00:00:01Z",
    }
    assert events[1].payload == {
        "response_id": "response-1",
        "last_client_delivered_sequence": 2,
        "policy_decision_id": "decision-abort",
    }
