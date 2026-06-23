from __future__ import annotations

import pytest

from graphblocks import (
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventMetadata,
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
