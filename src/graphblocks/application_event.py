from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .output_policy import OutputCutoff, OutputPolicyDecision


ApplicationEventKind = Literal[
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
]


STANDARD_APPLICATION_EVENT_KINDS: tuple[ApplicationEventKind, ...] = (
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

TOOL_APPLICATION_EVENT_KINDS: frozenset[ApplicationEventKind] = frozenset(
    (
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
    )
)


class ApplicationEventError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApplicationEventMetadata:
    event_id: str
    run_id: str
    response_id: str
    sequence: int
    release_id: str
    policy_snapshot_id: str
    occurred_at: str
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    kind: ApplicationEventKind
    metadata: ApplicationEventMetadata
    payload: dict[str, object] = field(default_factory=dict)
    tool_call_id: str | None = None

    @classmethod
    def output_policy_decision(
        cls,
        metadata: ApplicationEventMetadata,
        decision: OutputPolicyDecision,
    ) -> ApplicationEvent:
        kind: ApplicationEventKind
        if decision.disposition == "allow":
            kind = "OutputPolicyAllowed"
        elif decision.disposition == "hold":
            kind = "OutputPolicyHeld"
        elif decision.disposition == "redact":
            kind = "OutputPolicyRedacted"
        elif decision.disposition == "replace":
            kind = "OutputPolicyReplaced"
        else:
            kind = "OutputPolicyViolationDetected"
        return cls.new(
            kind,
            metadata,
            payload={
                "decision_id": decision.decision_id,
                "disposition": decision.disposition,
                "accepted_through_sequence": decision.accepted_through_sequence,
                "reason_codes": list(decision.reason_codes),
                "policy_refs": list(decision.policy_refs),
                "evaluated_at": decision.evaluated_at,
                "input_digest": decision.input_digest,
                "replacement_part_count": len(decision.replacement_parts),
                "redaction_count": len(decision.redactions),
            },
        )

    @classmethod
    def output_cutoff(
        cls,
        metadata: ApplicationEventMetadata,
        cutoff: OutputCutoff,
    ) -> list[ApplicationEvent]:
        events = [
            cls.new(
                "OutputCutoff",
                metadata,
                payload={
                    "stream_id": cutoff.stream_id,
                    "response_id": cutoff.response_id,
                    "turn_id": cutoff.turn_id,
                    "last_generated_sequence": cutoff.last_generated_sequence,
                    "last_policy_accepted_sequence": cutoff.last_policy_accepted_sequence,
                    "last_client_delivered_sequence": cutoff.last_client_delivered_sequence,
                    "terminal_reason": cutoff.terminal_reason,
                    "draft_disposition": cutoff.draft_disposition,
                    "durable_result": cutoff.durable_result,
                    "policy_decision_id": cutoff.policy_decision_id,
                    "occurred_at": cutoff.occurred_at,
                },
            )
        ]
        if cutoff.draft_disposition != "keep":
            if cutoff.draft_disposition == "retract":
                draft_kind: ApplicationEventKind = "AssistantRetracted"
            else:
                draft_kind = "AssistantIncomplete"
            events.append(
                cls.new(
                    draft_kind,
                    replace(
                        metadata,
                        event_id=f"{metadata.event_id}:draft",
                        sequence=metadata.sequence + 1,
                    ),
                    payload={
                        "response_id": cutoff.response_id,
                        "last_client_delivered_sequence": cutoff.last_client_delivered_sequence,
                        "policy_decision_id": cutoff.policy_decision_id,
                    },
                )
            )
        return events

    @classmethod
    def new(
        cls,
        kind: ApplicationEventKind,
        metadata: ApplicationEventMetadata,
        *,
        payload: dict[str, object] | None = None,
    ) -> ApplicationEvent:
        if kind not in STANDARD_APPLICATION_EVENT_KINDS:
            raise ApplicationEventError(f"unknown application event kind {kind}")
        if kind in TOOL_APPLICATION_EVENT_KINDS:
            raise ApplicationEventError(f"tool event {kind} requires tool_call_id")
        return cls(kind=kind, metadata=metadata, payload=dict(payload or {}), tool_call_id=None)

    @classmethod
    def tool(
        cls,
        kind: ApplicationEventKind,
        metadata: ApplicationEventMetadata,
        *,
        tool_call_id: str,
        payload: dict[str, object] | None = None,
    ) -> ApplicationEvent:
        if kind not in STANDARD_APPLICATION_EVENT_KINDS:
            raise ApplicationEventError(f"unknown application event kind {kind}")
        if kind not in TOOL_APPLICATION_EVENT_KINDS:
            raise ApplicationEventError(f"event {kind} is not a tool event")
        if not tool_call_id.strip():
            raise ApplicationEventError("tool_call_id must not be empty")
        return cls(kind=kind, metadata=metadata, payload=dict(payload or {}), tool_call_id=tool_call_id)
