from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal, Mapping

from .output_policy import GenerationChunk, OutputCutoff, OutputPolicyDecision
from .policy import PolicyDecision
from .tools import ToolApprovalRequest, ToolCall, ToolCallDraft, ToolResult, ToolResultEvent


ApplicationEventKind = Literal[
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

TOOL_DRAFT_APPLICATION_EVENT_KINDS: frozenset[ApplicationEventKind] = frozenset(
    (
        "ToolCallProposed",
        "ToolCallArgumentsDelta",
        "ToolCallArgumentsCompleted",
    )
)

POST_CUTOFF_TOOL_APPLICATION_EVENT_KINDS: frozenset[ApplicationEventKind] = frozenset(
    (
        "ToolCallDenied",
        "ToolCallCancelled",
        "ToolCallPolicyStopped",
    )
)

ApplicationCommandKind = Literal[
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
]

APPLICATION_COMMAND_KINDS: tuple[ApplicationCommandKind, ...] = (
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

ApplicationProtocolEventKind = Literal[
    "RunStarted",
    "TurnStarted",
    "ContextReady",
    "AssistantDraftStarted",
    "AssistantDraftDelta",
    "AssistantCommitted",
    "AssistantRetracted",
    "ToolStarted",
    "ToolCompleted",
    "ApprovalRequested",
    "ReviewRequested",
    "BudgetConstrained",
    "BudgetExhausted",
    "BudgetExtensionRequested",
    "BudgetExtensionGranted",
    "PolicyDecisionRequired",
    "ExecutionDegraded",
    "FilePatchPreview",
    "JobProgress",
    "ArtifactReady",
    "StateSnapshot",
    "RunCompleted",
    "RunFailed",
    "RunCancelled",
]

APPLICATION_PROTOCOL_EVENT_KINDS: tuple[ApplicationProtocolEventKind, ...] = (
    "RunStarted",
    "TurnStarted",
    "ContextReady",
    "AssistantDraftStarted",
    "AssistantDraftDelta",
    "AssistantCommitted",
    "AssistantRetracted",
    "ToolStarted",
    "ToolCompleted",
    "ApprovalRequested",
    "ReviewRequested",
    "BudgetConstrained",
    "BudgetExhausted",
    "BudgetExtensionRequested",
    "BudgetExtensionGranted",
    "PolicyDecisionRequired",
    "ExecutionDegraded",
    "FilePatchPreview",
    "JobProgress",
    "ArtifactReady",
    "StateSnapshot",
    "RunCompleted",
    "RunFailed",
    "RunCancelled",
)


class ApplicationEventError(RuntimeError):
    pass


class ApplicationProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApplicationCommandMetadata:
    command_id: str
    protocol_version: str
    run_id: str
    sequence: int
    issued_at_unix_ms: int
    turn_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("command_id", "protocol_version", "run_id"):
            if not getattr(self, field_name).strip():
                label = "id" if field_name == "command_id" else field_name
                raise ApplicationProtocolError(
                    f"application command {label} must not be empty"
                )
        for field_name in ("turn_id", "idempotency_key"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ApplicationProtocolError(f"application command {field_name} must not be empty")
        if self.sequence < 0:
            raise ApplicationProtocolError("application command sequence must be non-negative")
        if self.issued_at_unix_ms < 0:
            raise ApplicationProtocolError("application command issued_at_unix_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class ApplicationCommand:
    kind: ApplicationCommandKind
    metadata: ApplicationCommandMetadata
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in APPLICATION_COMMAND_KINDS:
            raise ApplicationProtocolError(f"unknown application command kind {self.kind}")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def new(
        cls,
        kind: ApplicationCommandKind,
        metadata: ApplicationCommandMetadata,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> ApplicationCommand:
        return cls(kind=kind, metadata=metadata, payload=dict(payload or {}))


@dataclass(frozen=True, slots=True)
class ApplicationProtocolEventMetadata:
    event_id: str
    protocol_version: str
    run_id: str
    sequence: int
    occurred_at_unix_ms: int
    turn_id: str | None = None
    cursor: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_id", "protocol_version", "run_id"):
            if not getattr(self, field_name).strip():
                label = "id" if field_name == "event_id" else field_name
                raise ApplicationProtocolError(
                    f"application event {label} must not be empty"
                )
        for field_name in ("turn_id", "cursor"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ApplicationProtocolError(f"application event {field_name} must not be empty")
        if self.sequence < 0:
            raise ApplicationProtocolError("application event sequence must be non-negative")
        if self.occurred_at_unix_ms < 0:
            raise ApplicationProtocolError("application event occurred_at_unix_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class ApplicationProtocolEvent:
    kind: ApplicationProtocolEventKind
    metadata: ApplicationProtocolEventMetadata
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in APPLICATION_PROTOCOL_EVENT_KINDS:
            raise ApplicationProtocolError(f"unknown application protocol event kind {self.kind}")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def new(
        cls,
        kind: ApplicationProtocolEventKind,
        metadata: ApplicationProtocolEventMetadata,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> ApplicationProtocolEvent:
        return cls(kind=kind, metadata=metadata, payload=dict(payload or {}))

    @classmethod
    def tool_result_stream(
        cls,
        metadata: ApplicationProtocolEventMetadata,
        event: ToolResultEvent,
    ) -> ApplicationProtocolEvent | None:
        if event.kind == "delta":
            return cls.new(
                "JobProgress",
                metadata,
                payload={
                    "tool_call_id": event.tool_call_id,
                    "tool_result_sequence": event.sequence,
                    "output": [
                        {
                            "kind": part.kind,
                            "text": part.text,
                            "data": part.data,
                            "metadata": dict(part.metadata),
                        }
                        for part in event.output
                    ],
                },
            )
        if event.kind == "artifact_ready" and event.artifact is not None:
            return cls.new(
                "ArtifactReady",
                metadata,
                payload={
                    "tool_call_id": event.tool_call_id,
                    "tool_result_sequence": event.sequence,
                    "artifact": {
                        "artifact_id": event.artifact.artifact_id,
                        "uri": event.artifact.uri,
                        "checksum": event.artifact.checksum,
                        "media_type": event.artifact.media_type,
                    },
                },
            )
        return None


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

    def __post_init__(self) -> None:
        for field_name, value in (
            ("event_id", self.event_id),
            ("run_id", self.run_id),
            ("response_id", self.response_id),
            ("release_id", self.release_id),
            ("policy_snapshot_id", self.policy_snapshot_id),
        ):
            if not value.strip():
                raise ApplicationEventError(f"application event {field_name} must not be empty")
        if self.turn_id is not None and not self.turn_id.strip():
            raise ApplicationEventError("application event turn_id must not be empty")
        if self.sequence < 0:
            raise ApplicationEventError("application event sequence must be non-negative")


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    kind: ApplicationEventKind
    metadata: ApplicationEventMetadata
    payload: Mapping[str, object] = field(default_factory=dict)
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

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
    def tool_call_state(
        cls,
        metadata: ApplicationEventMetadata,
        call: ToolCall,
    ) -> ApplicationEvent | None:
        if call.status == "validated":
            kind: ApplicationEventKind = "ToolCallValidated"
        elif call.status == "admitted":
            kind = "ToolCallAdmitted"
        else:
            return None

        return cls.tool(
            kind,
            metadata,
            tool_call_id=call.tool_call_id,
            payload={
                "tool_name": call.name,
                "resolved_tool_id": call.resolved_tool_id,
                "status": call.status,
                "arguments_digest": call.arguments_digest,
                "revision": call.revision,
                "depends_on": list(call.depends_on),
                "created_at": call.created_at,
                "admitted_at": call.admitted_at,
                "completed_at": call.completed_at,
            },
        )

    @classmethod
    def tool_call_policy_evaluated(
        cls,
        metadata: ApplicationEventMetadata,
        call: ToolCall,
        decision: PolicyDecision,
    ) -> ApplicationEvent:
        return cls.tool(
            "ToolCallPolicyEvaluated",
            metadata,
            tool_call_id=call.tool_call_id,
            payload={
                "tool_name": call.name,
                "resolved_tool_id": call.resolved_tool_id,
                "status": call.status,
                "arguments_digest": call.arguments_digest,
                "revision": call.revision,
                "decision_id": decision.decision_id,
                "effect": decision.effect,
                "reason_codes": list(decision.reason_codes),
                "policy_refs": list(decision.policy_refs),
                "obligation_count": len(decision.obligations),
                "advice_count": len(decision.advice),
                "evaluated_at": decision.evaluated_at,
                "valid_until": decision.valid_until,
                "input_digest": decision.input_digest,
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
    def output_policy_evaluation_started(
        cls,
        metadata: ApplicationEventMetadata,
        chunk: GenerationChunk,
        *,
        input_digest: str,
    ) -> ApplicationEvent:
        if not input_digest.strip():
            raise ApplicationEventError("output policy evaluation input_digest must not be empty")
        return cls.new(
            "OutputPolicyEvaluationStarted",
            metadata,
            payload={
                "stream_id": chunk.stream_id,
                "response_id": chunk.response_id,
                "chunk_sequence": chunk.sequence,
                "input_digest": input_digest,
                "chunk_text_bytes": len(chunk.text.encode("utf-8")),
            },
        )

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
                "provider_cancellation": decision.provider_cancellation,
                "draft_disposition": decision.draft_disposition,
                "pending_tool_calls": decision.pending_tool_calls,
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


@dataclass(slots=True)
class ApplicationEventStreamState:
    cutoffs: dict[str, OutputCutoff] = field(default_factory=dict)
    accepted_events: list[ApplicationEvent] = field(default_factory=list)

    def accept(self, event: ApplicationEvent) -> ApplicationEvent | None:
        if event.kind == "OutputCutoff":
            payload = event.payload
            response_id = str(payload.get("response_id", event.metadata.response_id))
            if response_id in self.cutoffs:
                return None
            cutoff = OutputCutoff(
                stream_id=str(payload["stream_id"]),
                response_id=response_id,
                turn_id=None if payload.get("turn_id") is None else str(payload.get("turn_id")),
                last_generated_sequence=int(payload["last_generated_sequence"]),
                last_policy_accepted_sequence=int(payload["last_policy_accepted_sequence"]),
                last_client_delivered_sequence=int(payload["last_client_delivered_sequence"]),
                terminal_reason=str(payload["terminal_reason"]),
                draft_disposition=str(payload["draft_disposition"]),
                durable_result=str(payload["durable_result"]),
                policy_decision_id=(
                    None
                    if payload.get("policy_decision_id") is None
                    else str(payload.get("policy_decision_id"))
                ),
                occurred_at=str(payload["occurred_at"]),
            )
            self.cutoffs[response_id] = cutoff
            self.accepted_events.append(event)
            return event

        response_id = str(event.payload.get("response_id", event.metadata.response_id))
        cutoff = self.cutoffs.get(response_id)
        if cutoff is not None:
            if event.kind in {"AssistantRetracted", "AssistantIncomplete"}:
                self.accepted_events.append(event)
                return event
            chunk_sequence = event.payload.get("chunk_sequence")
            if isinstance(chunk_sequence, int):
                return None
            if str(event.kind).startswith("OutputPolicy"):
                return None
            if event.kind in TOOL_APPLICATION_EVENT_KINDS and event.kind not in POST_CUTOFF_TOOL_APPLICATION_EVENT_KINDS:
                return None

        self.accepted_events.append(event)
        return event
