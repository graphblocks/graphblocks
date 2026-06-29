from __future__ import annotations

import pytest

import graphblocks
from graphblocks import (
    APPLICATION_COMMAND_KINDS,
    APPLICATION_PROTOCOL_EVENT_KINDS,
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventKind,
    ApplicationEventMetadata,
    ApplicationEventStreamState,
    ApplicationCommand,
    ApplicationCommandMetadata,
    ApplicationProtocolError,
    ApplicationProtocolEvent,
    ApplicationProtocolEventMetadata,
    ArtifactRef,
    BlockToolImplementation,
    ContentPart,
    GenerationChunk,
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


def test_application_event_metadata_rejects_empty_required_ids_and_negative_sequence() -> None:
    with pytest.raises(ApplicationEventError, match="application event event_id must not be empty"):
        ApplicationEventMetadata(
            event_id=" ",
            run_id="run-1",
            response_id="response-1",
            sequence=1,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-06-23T00:00:00Z",
        )
    with pytest.raises(ApplicationEventError, match="application event sequence must be non-negative"):
        ApplicationEventMetadata(
            event_id="event-1",
            run_id="run-1",
            response_id="response-1",
            sequence=-1,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-06-23T00:00:00Z",
        )
    with pytest.raises(ApplicationEventError, match="application event turn_id must not be empty"):
        ApplicationEventMetadata(
            event_id="event-1",
            run_id="run-1",
            response_id="response-1",
            turn_id=" ",
            sequence=1,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-06-23T00:00:00Z",
        )


def test_standard_application_event_names_match_tool_and_output_policy_contract() -> None:
    assert STANDARD_APPLICATION_EVENT_KINDS == (
        "RunStarted",
        "RunSucceeded",
        "RunFailed",
        "RunCancelled",
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
        "ToolCallIncomplete",
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


def test_top_level_package_exports_application_event_kind() -> None:
    assert graphblocks.ApplicationEventKind == ApplicationEventKind


def test_application_protocol_command_and_event_envelopes_match_client_contract() -> None:
    assert APPLICATION_COMMAND_KINDS == (
        "InvokeGraph",
        "CancelRun",
        "SubmitInput",
        "ApproveEffect",
        "DenyEffect",
        "SubmitReview",
        "RequestBudgetExtension",
        "ApplyPolicyOverride",
        "ResumeInterrupt",
        "SelectCandidate",
        "OpenArtifact",
        "SetBreakpoint",
        "RequestSnapshot",
    )
    assert APPLICATION_PROTOCOL_EVENT_KINDS == (
        "RunStarted",
        "TurnStarted",
        "ContextReady",
        "AssistantDraftStarted",
        "AssistantDraftDelta",
        "AssistantCommitted",
        "AssistantIncomplete",
        "AssistantRetracted",
        "ToolStarted",
        "ToolCompleted",
        "ToolCallApprovalRequested",
        "ApprovalRequested",
        "ReviewRequested",
        "BudgetConstrained",
        "BudgetExhausted",
        "BudgetExtensionRequested",
        "BudgetExtensionGranted",
        "PolicyDecisionRequired",
        "ExecutionDegraded",
        "OutputCutoff",
        "FilePatchPreview",
        "JobProgress",
        "ArtifactReady",
        "StateSnapshot",
        "RunCompleted",
        "RunFailed",
        "RunCancelled",
    )

    command_payload = {"tool_call_id": "tool-call-1"}
    command = ApplicationCommand.new(
        "ApproveEffect",
        ApplicationCommandMetadata(
            command_id="command-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            turn_id="turn-1",
            sequence=3,
            idempotency_key="idem-1",
            issued_at_unix_ms=1_765_843_200_000,
        ),
        payload=command_payload,
    )
    command_payload["tool_call_id"] = "mutated"

    event_payload = {"delta": "hello"}
    event = ApplicationProtocolEvent.new(
        "AssistantDraftDelta",
        ApplicationProtocolEventMetadata(
            event_id="event-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            turn_id="turn-1",
            sequence=4,
            cursor="cursor-4",
            occurred_at_unix_ms=1_765_843_201_000,
        ),
        payload=event_payload,
    )
    event_payload["delta"] = "mutated"

    assert command.kind == "ApproveEffect"
    assert command.metadata.idempotency_key == "idem-1"
    assert command.payload == {"tool_call_id": "tool-call-1"}
    assert event.kind == "AssistantDraftDelta"
    assert event.metadata.cursor == "cursor-4"
    assert event.payload == {"delta": "hello"}
    with pytest.raises(TypeError):
        command.payload["tool_call_id"] = "mutated"
    with pytest.raises(TypeError):
        event.payload["delta"] = "mutated"
    with pytest.raises(ApplicationProtocolError, match="application command id must not be empty"):
        ApplicationCommand.new(
            "CancelRun",
            ApplicationCommandMetadata(
                command_id=" ",
                protocol_version="graphblocks.app.v1",
                run_id="run-1",
                sequence=1,
                issued_at_unix_ms=1_765_843_200_000,
            ),
            payload={},
        )


def test_application_protocol_metadata_rejects_empty_required_fields() -> None:
    with pytest.raises(
        ApplicationProtocolError,
        match="application command protocol_version must not be empty",
    ):
        ApplicationCommandMetadata(
            command_id="command-1",
            protocol_version=" ",
            run_id="run-1",
            sequence=1,
            issued_at_unix_ms=1_765_843_200_000,
        )
    with pytest.raises(
        ApplicationProtocolError,
        match="application command run_id must not be empty",
    ):
        ApplicationCommandMetadata(
            command_id="command-1",
            protocol_version="graphblocks.app.v1",
            run_id="",
            sequence=1,
            issued_at_unix_ms=1_765_843_200_000,
        )
    with pytest.raises(
        ApplicationProtocolError,
        match="application command turn_id must not be empty",
    ):
        ApplicationCommandMetadata(
            command_id="command-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            turn_id=" ",
            sequence=1,
            issued_at_unix_ms=1_765_843_200_000,
        )
    with pytest.raises(
        ApplicationProtocolError,
        match="application command idempotency_key must not be empty",
    ):
        ApplicationCommandMetadata(
            command_id="command-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            idempotency_key="",
            sequence=1,
            issued_at_unix_ms=1_765_843_200_000,
        )
    with pytest.raises(
        ApplicationProtocolError,
        match="application event protocol_version must not be empty",
    ):
        ApplicationProtocolEventMetadata(
            event_id="event-1",
            protocol_version="",
            run_id="run-1",
            sequence=1,
            occurred_at_unix_ms=1_765_843_201_000,
        )
    with pytest.raises(
        ApplicationProtocolError,
        match="application event run_id must not be empty",
    ):
        ApplicationProtocolEventMetadata(
            event_id="event-1",
            protocol_version="graphblocks.app.v1",
            run_id=" ",
            sequence=1,
            occurred_at_unix_ms=1_765_843_201_000,
        )
    with pytest.raises(
        ApplicationProtocolError,
        match="application event turn_id must not be empty",
    ):
        ApplicationProtocolEventMetadata(
            event_id="event-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            turn_id=" ",
            sequence=1,
            occurred_at_unix_ms=1_765_843_201_000,
        )
    with pytest.raises(
        ApplicationProtocolError,
        match="application event cursor must not be empty",
    ):
        ApplicationProtocolEventMetadata(
            event_id="event-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            cursor="",
            sequence=1,
            occurred_at_unix_ms=1_765_843_201_000,
        )


def test_protocol_events_represent_streaming_tool_result_deltas_and_artifacts() -> None:
    delta = ToolResultEvent.delta(
        "call-1",
        7,
        (
            ContentPart(
                kind="text",
                text="draft chunk",
                metadata={"trust_designation": "untrusted_external"},
            ),
            ContentPart(kind="json", data={"items": 2}),
        ),
    )
    artifact = ToolResultEvent.artifact_ready(
        "call-1",
        8,
        ArtifactRef(
            "artifact-1",
            "file:///tmp/result.json",
            checksum="sha256:artifact",
            media_type="application/json",
        ),
    )

    delta_event = ApplicationProtocolEvent.tool_result_stream(
        ApplicationProtocolEventMetadata(
            event_id="event-delta",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            turn_id="turn-1",
            sequence=7,
            cursor="cursor-7",
            occurred_at_unix_ms=1_765_843_201_000,
        ),
        delta,
    )
    artifact_event = ApplicationProtocolEvent.tool_result_stream(
        ApplicationProtocolEventMetadata(
            event_id="event-artifact",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            turn_id="turn-1",
            sequence=8,
            cursor="cursor-8",
            occurred_at_unix_ms=1_765_843_202_000,
        ),
        artifact,
    )
    completed = ToolResultEvent.completed(
        "call-1",
        9,
        ToolResult.completed(
            "call-1",
            (ContentPart(kind="text", text="done"),),
            started_at="2026-06-23T00:00:00Z",
            completed_at="2026-06-23T00:00:01Z",
        ),
    )

    assert delta_event is not None
    assert delta_event.kind == "JobProgress"
    assert delta_event.payload == {
        "tool_call_id": "call-1",
        "tool_result_sequence": 7,
        "output": [
            {
                "kind": "text",
                "text": "draft chunk",
                "data": None,
                "metadata": {"trust_designation": "untrusted_external"},
            },
            {
                "kind": "json",
                "text": None,
                "data": {"items": 2},
                "metadata": {},
            },
        ],
    }
    assert artifact_event is not None
    assert artifact_event.kind == "ArtifactReady"
    assert artifact_event.payload == {
        "tool_call_id": "call-1",
        "tool_result_sequence": 8,
        "artifact": {
            "artifact_id": "artifact-1",
            "uri": "file:///tmp/result.json",
            "checksum": "sha256:artifact",
            "media_type": "application/json",
        },
    }
    assert (
        ApplicationProtocolEvent.tool_result_stream(
            ApplicationProtocolEventMetadata(
                event_id="event-complete",
                protocol_version="graphblocks.app.v1",
                run_id="run-1",
                turn_id="turn-1",
                sequence=9,
                cursor="cursor-9",
                occurred_at_unix_ms=1_765_843_203_000,
            ),
            completed,
        )
        is None
    )


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


def test_application_event_payloads_are_copied_and_read_only() -> None:
    payload = {"status": "running"}
    event = ApplicationEvent.new("RunStarted", _metadata(), payload=payload)
    payload["status"] = "mutated"

    assert event.payload == {"status": "running"}
    with pytest.raises(TypeError):
        event.payload["status"] = "mutated"


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


def test_output_policy_evaluation_start_event_identifies_chunk_without_text_payload() -> None:
    chunk = GenerationChunk.text("stream-1", "response-1", 4, "sensitive text")

    event = ApplicationEvent.output_policy_evaluation_started(
        _metadata(),
        chunk,
        input_digest="sha256:pending-window",
    )

    assert event.kind == "OutputPolicyEvaluationStarted"
    assert event.tool_call_id is None
    assert event.payload == {
        "stream_id": "stream-1",
        "response_id": "response-1",
        "chunk_sequence": 4,
        "input_digest": "sha256:pending-window",
        "chunk_text_bytes": 14,
    }
    assert "text" not in event.payload

    with pytest.raises(ApplicationEventError, match="output policy evaluation input_digest must not be empty"):
        ApplicationEvent.output_policy_evaluation_started(
            _metadata(),
            chunk,
            input_digest=" ",
        )


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
        "provider_cancellation": "request",
        "draft_disposition": "keep",
        "pending_tool_calls": "keep",
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
        "terminal_reason": "policy_denied",
        "draft_disposition": "retract",
        "policy_decision_id": "decision-abort",
    }

    incomplete_cutoff = OutputCutoff(
        stream_id="stream-1",
        response_id="response-2",
        last_generated_sequence=3,
        last_policy_accepted_sequence=1,
        last_client_delivered_sequence=1,
        terminal_reason="cancelled",
        draft_disposition="mark_incomplete",
        durable_result="incomplete",
        occurred_at="2026-06-23T00:00:02Z",
    )

    incomplete_events = ApplicationEvent.output_cutoff(_metadata(), incomplete_cutoff)

    assert incomplete_events[1].kind == "AssistantIncomplete"
    assert incomplete_events[1].payload["terminal_reason"] == "cancelled"
    assert incomplete_events[1].payload["draft_disposition"] == "mark_incomplete"


def test_application_event_stream_state_rejects_invalid_output_cutoff_payload() -> None:
    state = ApplicationEventStreamState()
    invalid_cutoff = ApplicationEvent.new(
        "OutputCutoff",
        _metadata(),
        payload={
            "stream_id": "stream-1",
            "response_id": "response-1",
            "turn_id": "turn-1",
            "last_generated_sequence": 1,
            "last_policy_accepted_sequence": 1,
            "last_client_delivered_sequence": 2,
            "terminal_reason": "policy_denied",
            "draft_disposition": "retract",
            "durable_result": "none",
            "policy_decision_id": "decision-abort",
            "occurred_at": "2026-06-23T00:00:01Z",
        },
    )

    assert state.accept(invalid_cutoff) is None
    assert state.cutoffs == {}
    assert state.accepted_events == []

    non_string_identity = ApplicationEvent.new(
        "OutputCutoff",
        _metadata(),
        payload={
            "stream_id": 123,
            "response_id": "response-1",
            "turn_id": "turn-1",
            "last_generated_sequence": 1,
            "last_policy_accepted_sequence": 1,
            "last_client_delivered_sequence": 1,
            "terminal_reason": "policy_denied",
            "draft_disposition": "retract",
            "durable_result": "none",
            "policy_decision_id": "decision-abort",
            "occurred_at": "2026-06-23T00:00:01Z",
        },
    )

    assert state.accept(non_string_identity) is None
    assert state.cutoffs == {}
    assert state.accepted_events == []


def test_application_event_stream_state_discards_late_output_after_cutoff() -> None:
    state = ApplicationEventStreamState()
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
        policy_decision_id="decision-abort",
        occurred_at="2026-06-23T00:00:01Z",
    )
    cutoff_event, retraction_event = ApplicationEvent.output_cutoff(_metadata(), cutoff)
    late_output = ApplicationEvent.output_policy_evaluation_started(
        _metadata(),
        GenerationChunk.text("stream-1", "response-1", 2, "blocked"),
        input_digest="sha256:late",
    )
    replacement_response = ApplicationEvent.output_policy_evaluation_started(
        _metadata(),
        GenerationChunk.text("stream-1", "response-2", 1, "replacement"),
        input_digest="sha256:replacement",
    )
    late_tool_draft = ApplicationEvent.tool_call_draft(
        _metadata(),
        ToolCallDraft.proposed("response-1", "call-draft", "ticket.create"),
    )
    validated_tool = ApplicationEvent.tool(
        "ToolCallValidated",
        _metadata(),
        tool_call_id="call-validated",
        payload={"status": "validated"},
    )
    admitted_tool = ApplicationEvent.tool(
        "ToolCallAdmitted",
        _metadata(),
        tool_call_id="call-admitted",
        payload={"status": "admitted"},
    )
    started_tool = ApplicationEvent.tool(
        "ToolCallStarted",
        _metadata(),
        tool_call_id="call-started",
        payload={"status": "running"},
    )
    completed_tool = ApplicationEvent.tool(
        "ToolCallCompleted",
        _metadata(),
        tool_call_id="call-completed",
        payload={"status": "completed"},
    )
    committed_run = ApplicationEvent.new(
        "RunSucceeded",
        _metadata(),
        payload={"status": "succeeded", "outputs": {"answer": "should not commit"}},
    )
    replacement_tool_draft = ApplicationEvent.tool_call_draft(
        ApplicationEventMetadata(
            event_id="event-replacement-tool",
            run_id="run-1",
            response_id="response-2",
            turn_id="turn-1",
            sequence=8,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-06-23T00:00:02Z",
        ),
        ToolCallDraft.proposed("response-2", "call-replacement", "knowledge.search"),
    )
    denied_tool = ApplicationEvent.tool(
        "ToolCallDenied",
        _metadata(),
        tool_call_id="call-1",
        payload={"status": "denied"},
    )
    cancelled_tool = ApplicationEvent.tool(
        "ToolCallCancelled",
        _metadata(),
        tool_call_id="call-2",
        payload={"status": "cancelled"},
    )
    policy_stopped_tool = ApplicationEvent.tool(
        "ToolCallPolicyStopped",
        _metadata(),
        tool_call_id="call-3",
        payload={"status": "policy_stopped"},
    )
    incomplete_tool = ApplicationEvent.tool(
        "ToolCallIncomplete",
        _metadata(),
        tool_call_id="call-4",
        payload={"status": "incomplete"},
    )

    assert state.accept(cutoff_event) == cutoff_event
    assert state.accept(retraction_event) == retraction_event
    assert state.accept(late_output) is None
    assert state.accept(late_tool_draft) is None
    assert state.accept(validated_tool) is None
    assert state.accept(admitted_tool) is None
    assert state.accept(started_tool) is None
    assert state.accept(completed_tool) is None
    assert state.accept(committed_run) is None
    assert state.accept(replacement_response) == replacement_response
    assert state.accept(replacement_tool_draft) == replacement_tool_draft
    assert state.accept(denied_tool) == denied_tool
    assert state.accept(cancelled_tool) == cancelled_tool
    assert state.accept(policy_stopped_tool) == policy_stopped_tool
    assert state.accept(incomplete_tool) == incomplete_tool
    assert [event.kind for event in state.accepted_events] == [
        "OutputCutoff",
        "AssistantRetracted",
        "OutputPolicyEvaluationStarted",
        "ToolCallProposed",
        "ToolCallDenied",
        "ToolCallCancelled",
        "ToolCallPolicyStopped",
        "ToolCallIncomplete",
    ]


def test_application_event_stream_state_uses_metadata_response_when_payload_response_id_is_invalid() -> None:
    state = ApplicationEventStreamState()
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
        policy_decision_id="decision-abort",
        occurred_at="2026-06-23T00:00:01Z",
    )
    cutoff_event, _ = ApplicationEvent.output_cutoff(_metadata(), cutoff)
    invalid_payload_response = ApplicationEvent.new(
        "OutputPolicyEvaluationStarted",
        _metadata(),
        payload={
            "stream_id": "stream-1",
            "response_id": 123,
            "chunk_sequence": 2,
            "text": "blocked",
            "input_digest": "sha256:late",
        },
    )

    assert state.accept(cutoff_event) == cutoff_event
    assert state.accept(invalid_payload_response) is None


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
    incomplete = ToolResult.incomplete(
        "call-6",
        started_at="2026-06-23T00:00:09Z",
        completed_at="2026-06-23T00:00:10Z",
    )

    events = [
        ToolResultEvent.started("call-0", 1, started_at="2026-06-23T00:00:00Z"),
        ToolResultEvent.completed("call-1", 2, completed),
        ToolResultEvent.failed("call-2", 3, failed),
        ToolResultEvent.denied("call-3", 4, denied),
        ToolResultEvent.cancelled("call-4", 5, cancelled),
        ToolResultEvent.policy_stopped("call-5", 6, policy_stopped),
        ToolResultEvent.incomplete("call-6", 7, incomplete),
    ]
    converted = [ApplicationEvent.tool_result_event(_metadata(), event) for event in events]

    assert [event.kind for event in converted] == [
        "ToolCallStarted",
        "ToolCallCompleted",
        "ToolCallFailed",
        "ToolCallDenied",
        "ToolCallCancelled",
        "ToolCallPolicyStopped",
        "ToolCallIncomplete",
    ]
    assert converted[0].tool_call_id == "call-0"
    assert converted[1].payload["status"] == "completed"
    assert converted[2].payload["status"] == "failed"
    assert converted[3].payload["status"] == "denied"
    assert converted[4].payload["status"] == "cancelled"
    assert converted[5].payload["status"] == "policy_stopped"
    assert converted[6].payload["status"] == "incomplete"


def test_tool_result_delta_does_not_become_application_event() -> None:
    delta = ToolResultEvent.delta("call-1", 7, (ContentPart(kind="text", text="draft"),))

    assert ApplicationEvent.tool_result_event(_metadata(), delta) is None
