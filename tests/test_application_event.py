from __future__ import annotations

import pytest

from graphblocks import (
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventMetadata,
    BlockToolImplementation,
    ContentPart,
    OutputCutoff,
    OutputPolicyDecision,
    PolicyDecision,
    STANDARD_APPLICATION_EVENT_KINDS,
    TOOL_APPLICATION_EVENT_KINDS,
    ToolApprovalRequest,
    ToolBinding,
    ToolCatalog,
    ToolCallDraft,
    ToolDefinition,
    ToolResolutionScope,
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


def test_tool_call_drafts_map_to_argument_lifecycle_application_events() -> None:
    draft = ToolCallDraft.proposed("response-1", "call-1", "knowledge.search")
    proposed = ApplicationEvent.tool_call_draft(_metadata(), draft)

    streaming = draft.append_argument_fragment('{"query"')
    delta = ApplicationEvent.tool_call_draft(
        ApplicationEventMetadata(
            event_id="event-2",
            run_id="run-1",
            response_id="response-1",
            turn_id="turn-1",
            sequence=8,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-06-23T00:00:01Z",
        ),
        streaming,
    )
    completed_draft = streaming.append_argument_fragment(':"runtime"}').complete_arguments()
    completed = ApplicationEvent.tool_call_draft(
        ApplicationEventMetadata(
            event_id="event-3",
            run_id="run-1",
            response_id="response-1",
            turn_id="turn-1",
            sequence=9,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-06-23T00:00:02Z",
        ),
        completed_draft,
    )

    assert proposed.kind == "ToolCallProposed"
    assert proposed.tool_call_id == "call-1"
    assert proposed.payload == {
        "tool_name": "knowledge.search",
        "status": "proposed",
        "draft_sequence": 0,
        "fragment_count": 0,
    }
    assert delta.kind == "ToolCallArgumentsDelta"
    assert delta.payload == {
        "tool_name": "knowledge.search",
        "status": "arguments_streaming",
        "draft_sequence": 1,
        "fragment_count": 1,
        "argument_fragment": '{"query"',
    }
    assert completed.kind == "ToolCallArgumentsCompleted"
    assert completed.payload == {
        "tool_name": "knowledge.search",
        "status": "arguments_complete",
        "draft_sequence": 2,
        "fragment_count": 2,
    }


def test_final_tool_calls_map_to_validated_and_admitted_application_events() -> None:
    call = (
        ToolCallDraft.proposed("response-1", "call-1", "knowledge.search")
        .append_argument_fragment('{"query":"runtime"}')
        .complete_arguments()
        .into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:00Z")
    )
    validated = ApplicationEvent.tool_call_state(_metadata(), call)

    admitted_call = call.with_status("admitted", admitted_at="2026-06-23T00:00:01Z")
    admitted = ApplicationEvent.tool_call_state(
        ApplicationEventMetadata(
            event_id="event-2",
            run_id="run-1",
            response_id="response-1",
            turn_id="turn-1",
            sequence=8,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-06-23T00:00:01Z",
        ),
        admitted_call,
    )

    assert validated is not None
    assert validated.kind == "ToolCallValidated"
    assert validated.tool_call_id == "call-1"
    assert validated.payload == {
        "tool_name": "knowledge.search",
        "resolved_tool_id": "resolved-tool-1",
        "status": "validated",
        "arguments_digest": call.arguments_digest,
        "revision": 1,
        "depends_on": [],
        "created_at": "2026-06-23T00:00:00Z",
        "admitted_at": None,
        "completed_at": None,
    }
    assert admitted is not None
    assert admitted.kind == "ToolCallAdmitted"
    assert admitted.payload == {
        "tool_name": "knowledge.search",
        "resolved_tool_id": "resolved-tool-1",
        "status": "admitted",
        "arguments_digest": admitted_call.arguments_digest,
        "revision": 1,
        "depends_on": [],
        "created_at": "2026-06-23T00:00:00Z",
        "admitted_at": "2026-06-23T00:00:01Z",
        "completed_at": None,
    }


def test_tool_policy_decisions_map_to_policy_evaluated_application_events() -> None:
    call = (
        ToolCallDraft.proposed("response-1", "call-1", "knowledge.search")
        .append_argument_fragment('{"query":"runtime"}')
        .complete_arguments()
        .into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:00Z")
    )
    decision = PolicyDecision(
        decision_id="decision-1",
        effect="deny",
        reason_codes=["tool.denied"],
        policy_refs=["policy/tool-safety"],
        advice=[{"message": "tool denied"}],
        evaluated_at="2026-06-23T00:00:01Z",
        valid_until="2026-06-23T00:05:01Z",
        input_digest="sha256:policy-input",
    )

    event = ApplicationEvent.tool_call_policy_evaluated(_metadata(), call, decision)

    assert event.kind == "ToolCallPolicyEvaluated"
    assert event.tool_call_id == "call-1"
    assert event.payload == {
        "tool_name": "knowledge.search",
        "resolved_tool_id": "resolved-tool-1",
        "status": "validated",
        "arguments_digest": call.arguments_digest,
        "revision": 1,
        "decision_id": "decision-1",
        "effect": "deny",
        "reason_codes": ["tool.denied"],
        "policy_refs": ["policy/tool-safety"],
        "obligation_count": 0,
        "advice_count": 1,
        "evaluated_at": "2026-06-23T00:00:01Z",
        "valid_until": "2026-06-23T00:05:01Z",
        "input_digest": "sha256:policy-input",
    }


def test_tool_approval_request_maps_to_standard_application_event() -> None:
    catalog = ToolCatalog(
        definitions=(
            ToolDefinition("ticket.create", "Create a support ticket.", "schemas/TicketCreate@1"),
        ),
        bindings=(
            ToolBinding("binding-ticket", "ticket.create", BlockToolImplementation("blocks.ticket.create")),
        ),
    )
    resolved = catalog.resolve(ToolResolutionScope(), effective_policy_snapshot_id="policy-snapshot-1")[0]
    call = (
        ToolCallDraft.proposed("response-1", "call-1", "ticket.create")
        .append_argument_fragment('{"title":"Need help"}')
        .complete_arguments()
        .into_tool_call(resolved.resolved_tool_id, created_at="2026-06-23T00:00:00Z")
    )
    approval = ToolApprovalRequest.for_call(
        "approval-1",
        resolved,
        call,
        principal_id="user-1",
        requested_at=1_100,
        expires_at=2_000,
    )

    event = ApplicationEvent.tool_approval_requested(_metadata(), approval)

    assert event.kind == "ToolCallApprovalRequested"
    assert event.tool_call_id == "call-1"
    assert event.payload == {
        "approval_id": "approval-1",
        "tool_name": "ticket.create",
        "revision": 1,
        "definition_digest": resolved.definition_digest,
        "binding_digest": resolved.binding_digest,
        "arguments_digest": call.arguments_digest,
        "policy_snapshot_id": "policy-snapshot-1",
        "principal_id": "user-1",
        "requested_at": 1_100,
        "expires_at": 2_000,
    }


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
