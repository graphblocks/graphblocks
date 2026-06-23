from __future__ import annotations

import pytest

from graphblocks import (
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventMetadata,
    ContentPart,
    OutputCutoff,
    OutputPolicyDecision,
    STANDARD_APPLICATION_EVENT_KINDS,
    TOOL_APPLICATION_EVENT_KINDS,
    ToolResult,
    ToolResultEvent,
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


def test_tool_result_events_map_to_standard_tool_application_events() -> None:
    completed = ToolResult.completed(
        "call-1",
        (ContentPart(kind="text", text="done"),),
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    )
    failed = ToolResult.failed(
        "call-2",
        error={"code": "tool.failed", "message": "tool execution failed"},
        started_at="2026-06-23T00:00:02Z",
        completed_at="2026-06-23T00:00:03Z",
    )
    denied = ToolResult.denied(
        "call-3",
        error={"code": "tool.denied", "message": "tool execution was denied"},
        completed_at="2026-06-23T00:00:04Z",
    )
    cancelled = ToolResult.cancelled(
        "call-4",
        started_at="2026-06-23T00:00:05Z",
        completed_at="2026-06-23T00:00:06Z",
    )
    policy_stopped = ToolResult.policy_stopped(
        "call-5",
        error={"code": "policy.denied", "message": "tool result was stopped by policy"},
        started_at="2026-06-23T00:00:07Z",
        completed_at="2026-06-23T00:00:08Z",
    )

    events = [
        ToolResultEvent.started("call-0", 1, started_at="2026-06-23T00:00:00Z"),
        ToolResultEvent.completed("call-1", 2, completed),
        ToolResultEvent.failed("call-2", 3, failed),
        ToolResultEvent.denied("call-3", 4, denied),
        ToolResultEvent.cancelled("call-4", 5, cancelled),
        ToolResultEvent.policy_stopped("call-5", 6, policy_stopped),
    ]
    converted = [ApplicationEvent.tool_result_event(_metadata(), event) for event in events]

    assert [event.kind for event in converted] == [
        "ToolCallStarted",
        "ToolCallCompleted",
        "ToolCallFailed",
        "ToolCallDenied",
        "ToolCallCancelled",
        "ToolCallPolicyStopped",
    ]
    assert converted[0].tool_call_id == "call-0"
    assert converted[1].payload["status"] == "completed"
    assert converted[2].payload["status"] == "failed"
    assert converted[3].payload["status"] == "denied"
    assert converted[4].payload["status"] == "cancelled"
    assert converted[5].payload["status"] == "policy_stopped"


def test_tool_result_delta_does_not_become_application_event() -> None:
    delta = ToolResultEvent.delta("call-1", 7, (ContentPart(kind="text", text="draft"),))

    assert ApplicationEvent.tool_result_event(_metadata(), delta) is None
