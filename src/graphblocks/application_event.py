from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .output_policy import OutputCutoff, OutputPolicyDecision
from .tools import ToolApprovalRequest, ToolCallDraft, ToolResult, ToolResultEvent


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
    def tool_call_draft(
        cls,
        metadata: ApplicationEventMetadata,
        draft: ToolCallDraft,
    ) -> ApplicationEvent:
        if draft.status == "proposed":
            return cls.tool(
                "ToolCallProposed",
                metadata,
                tool_call_id=draft.tool_call_id,
                payload={
                    "tool_name": draft.tool_name,
                    "status": "proposed",
                    "draft_sequence": draft.sequence,
                    "fragment_count": len(draft.argument_fragments),
                },
            )
        if draft.status == "arguments_streaming":
            return cls.tool(
                "ToolCallArgumentsDelta",
                metadata,
                tool_call_id=draft.tool_call_id,
                payload={
                    "tool_name": draft.tool_name,
                    "status": "arguments_streaming",
                    "draft_sequence": draft.sequence,
                    "fragment_count": len(draft.argument_fragments),
                    "argument_fragment": draft.argument_fragments[-1] if draft.argument_fragments else None,
                },
            )
        return cls.tool(
            "ToolCallArgumentsCompleted",
            metadata,
            tool_call_id=draft.tool_call_id,
            payload={
                "tool_name": draft.tool_name,
                "status": "arguments_complete",
                "draft_sequence": draft.sequence,
                "fragment_count": len(draft.argument_fragments),
            },
        )

    @classmethod
    def tool_approval_requested(
        cls,
        metadata: ApplicationEventMetadata,
        request: ToolApprovalRequest,
    ) -> ApplicationEvent:
        return cls.tool(
            "ToolCallApprovalRequested",
            metadata,
            tool_call_id=request.tool_call_id,
            payload={
                "approval_id": request.approval_id,
                "tool_name": request.tool_name,
                "revision": request.revision,
                "definition_digest": request.definition_digest,
                "binding_digest": request.binding_digest,
                "arguments_digest": request.arguments_digest,
                "policy_snapshot_id": request.policy_snapshot_id,
                "principal_id": request.principal_id,
                "requested_at": request.requested_at,
                "expires_at": request.expires_at,
            },
        )

    @classmethod
    def tool_result_event(
        cls,
        metadata: ApplicationEventMetadata,
        event: ToolResultEvent,
    ) -> ApplicationEvent | None:
        if event.kind == "started":
            return cls.tool(
                "ToolCallStarted",
                metadata,
                tool_call_id=event.tool_call_id,
                payload={
                    "status": "running",
                    "tool_result_sequence": event.sequence,
                    "started_at": event.started_at,
                },
            )
        if event.kind == "completed" and event.result is not None:
            return cls.tool(
                "ToolCallCompleted",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "failed" and event.result is not None:
            return cls.tool(
                "ToolCallFailed",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "denied" and event.result is not None:
            return cls.tool(
                "ToolCallDenied",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "cancelled" and event.result is not None:
            return cls.tool(
                "ToolCallCancelled",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "policy_stopped" and event.result is not None:
            return cls.tool(
                "ToolCallPolicyStopped",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        return None

    @staticmethod
    def _tool_result_payload(sequence: int, result: ToolResult) -> dict[str, object]:
        error_code = None
        if result.error is not None:
            error_code_value = result.error.get("code")
            if isinstance(error_code_value, str):
                error_code = error_code_value
        return {
            "status": result.status,
            "tool_result_sequence": sequence,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "output_digest": result.output_digest,
            "effect_outcome": result.effect_outcome,
            "error_code": error_code,
        }

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
