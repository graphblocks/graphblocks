from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal

from .output_policy import GenerationChunk, OutputCutoff, OutputPolicyDecision
from .policy import PolicyDecision
from .tools import (
    ContentPart,
    ToolApprovalRequest,
    ToolCall,
    ToolCallDraft,
    ToolResult,
    ToolResultEvent,
)


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
    "ToolCallIncomplete",
    "ToolResultStarted",
    "ToolResultDelta",
    "ToolResultArtifactReady",
    "ToolResultCompleted",
    "ToolResultFailed",
    "ToolResultDenied",
    "ToolResultCancelled",
    "ToolResultPolicyStopped",
    "ToolResultIncomplete",
    "OutputPolicyEvaluationStarted",
    "OutputPolicyAllowed",
    "OutputPolicyHeld",
    "OutputPolicyRedacted",
    "OutputPolicyReplaced",
    "OutputPolicyViolationDetected",
    "OutputCutoff",
    "AssistantIncomplete",
    "AssistantRetracted",
    "RunCompleted",
    "RunExpired",
    "RunPolicyStopped",
    "AsyncOperationStarted",
    "AsyncOperationWaitingCallback",
    "AsyncOperationPolling",
    "AsyncOperationCompleted",
    "AsyncOperationFailed",
    "AsyncOperationCancelled",
    "AsyncOperationExpired",
    "ExternalCallbackReceived",
    "ExternalCallbackRejected",
    "LateExternalCallbackReceived",
    "RunResuming",
    "RunPausedBudget",
    "RunPausedPolicy",
    "RunPausedOperator",
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
    "ToolCallIncomplete",
    "ToolResultStarted",
    "ToolResultDelta",
    "ToolResultArtifactReady",
    "ToolResultCompleted",
    "ToolResultFailed",
    "ToolResultDenied",
    "ToolResultCancelled",
    "ToolResultPolicyStopped",
    "ToolResultIncomplete",
    "OutputPolicyEvaluationStarted",
    "OutputPolicyAllowed",
    "OutputPolicyHeld",
    "OutputPolicyRedacted",
    "OutputPolicyReplaced",
    "OutputPolicyViolationDetected",
    "OutputCutoff",
    "AssistantIncomplete",
    "AssistantRetracted",
    "RunCompleted",
    "RunExpired",
    "RunPolicyStopped",
    "AsyncOperationStarted",
    "AsyncOperationWaitingCallback",
    "AsyncOperationPolling",
    "AsyncOperationCompleted",
    "AsyncOperationFailed",
    "AsyncOperationCancelled",
    "AsyncOperationExpired",
    "ExternalCallbackReceived",
    "ExternalCallbackRejected",
    "LateExternalCallbackReceived",
    "RunResuming",
    "RunPausedBudget",
    "RunPausedPolicy",
    "RunPausedOperator",
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
        "ToolCallIncomplete",
        "ToolResultStarted",
        "ToolResultDelta",
        "ToolResultArtifactReady",
        "ToolResultCompleted",
        "ToolResultFailed",
        "ToolResultDenied",
        "ToolResultCancelled",
        "ToolResultPolicyStopped",
        "ToolResultIncomplete",
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
        "ToolCallIncomplete",
        "ToolResultDenied",
        "ToolResultCancelled",
        "ToolResultPolicyStopped",
        "ToolResultIncomplete",
    )
)

ApplicationEventVisibility = Literal["client", "operator", "internal", "audit_only"]
APPLICATION_EVENT_VISIBILITIES: tuple[ApplicationEventVisibility, ...] = (
    "client",
    "operator",
    "internal",
    "audit_only",
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
    "GetRunStatus",
    "ListRuns",
    "AttachToRun",
    "DetachFromRun",
    "SubscribeEvents",
    "UnsubscribeEvents",
    "AckEvent",
    "RegisterCallback",
    "RevokeCallback",
    "SubmitAsyncCallback",
    "PauseRun",
    "ResumeRun",
    "ExpireRun",
    "RedriveCallbackDelivery",
    "MoveCallbackToDeadLetter",
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
    "GetRunStatus",
    "ListRuns",
    "AttachToRun",
    "DetachFromRun",
    "SubscribeEvents",
    "UnsubscribeEvents",
    "AckEvent",
    "RegisterCallback",
    "RevokeCallback",
    "SubmitAsyncCallback",
    "PauseRun",
    "ResumeRun",
    "ExpireRun",
    "RedriveCallbackDelivery",
    "MoveCallbackToDeadLetter",
)
APPLICATION_PROTOCOL_TCK_COMMAND_KINDS: tuple[ApplicationCommandKind, ...] = APPLICATION_COMMAND_KINDS

ApplicationProtocolEventKind = Literal[
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
    "RunPolicyStopped",
    "RunExpired",
]

APPLICATION_PROTOCOL_EVENT_KINDS: tuple[ApplicationProtocolEventKind, ...] = (
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
    "RunPolicyStopped",
    "RunExpired",
)
APPLICATION_PROTOCOL_TCK_EVENT_KINDS: tuple[ApplicationProtocolEventKind, ...] = APPLICATION_PROTOCOL_EVENT_KINDS


class ApplicationEventError(RuntimeError):
    pass


class ApplicationProtocolError(RuntimeError):
    pass


def _validate_non_empty_string(error_type: type[RuntimeError], label: str, value: object) -> None:
    if not isinstance(value, str):
        raise error_type(f"{label} must be a string")
    if not value.strip():
        raise error_type(f"{label} must not be empty")


def _validate_optional_non_empty_string(
    error_type: type[RuntimeError],
    label: str,
    value: object,
) -> None:
    if value is not None:
        _validate_non_empty_string(error_type, label, value)


def _validate_non_negative_integer(error_type: type[RuntimeError], label: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(f"{label} must be an integer")
    if value < 0:
        raise error_type(f"{label} must be non-negative")


def _copy_payload_value(error_type: type[RuntimeError], label: str, value: object) -> object:
    if isinstance(value, Mapping):
        copied = dict(value)
        if any(not isinstance(key, str) or not key.strip() for key in copied):
            raise error_type(f"{label} keys must be non-empty strings")
        return {key: _copy_payload_value(error_type, f"{label}.{key}", item) for key, item in copied.items()}
    if isinstance(value, list):
        return [_copy_payload_value(error_type, label, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_payload_value(error_type, label, item) for item in value)
    return value


def _freeze_payload(error_type: type[RuntimeError], label: str, payload: object) -> MappingProxyType[str, object]:
    if not isinstance(payload, Mapping):
        raise error_type(f"{label} must be a mapping")
    normalized = dict(payload)
    if any(not isinstance(key, str) or not key.strip() for key in normalized):
        raise error_type(f"{label} keys must be non-empty strings")
    return MappingProxyType(
        {
            key: _copy_payload_value(error_type, f"{label}.{key}", value)
            for key, value in normalized.items()
        }
    )


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
            label = "id" if field_name == "command_id" else field_name
            _validate_non_empty_string(
                ApplicationProtocolError,
                f"application command {label}",
                getattr(self, field_name),
            )
        for field_name in ("turn_id", "idempotency_key"):
            _validate_optional_non_empty_string(
                ApplicationProtocolError,
                f"application command {field_name}",
                getattr(self, field_name),
            )
        _validate_non_negative_integer(
            ApplicationProtocolError,
            "application command sequence",
            self.sequence,
        )
        _validate_non_negative_integer(
            ApplicationProtocolError,
            "application command issued_at_unix_ms",
            self.issued_at_unix_ms,
        )


@dataclass(frozen=True, slots=True)
class ApplicationCommand:
    kind: ApplicationCommandKind
    metadata: ApplicationCommandMetadata
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, ApplicationCommandMetadata):
            raise ApplicationProtocolError("application command metadata must be ApplicationCommandMetadata")
        if self.kind not in APPLICATION_COMMAND_KINDS:
            raise ApplicationProtocolError(f"unknown application command kind {self.kind}")
        object.__setattr__(
            self,
            "payload",
            _freeze_payload(ApplicationProtocolError, "application command payload", self.payload),
        )

    @classmethod
    def new(
        cls,
        kind: ApplicationCommandKind,
        metadata: ApplicationCommandMetadata,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> ApplicationCommand:
        return cls(kind=kind, metadata=metadata, payload={} if payload is None else payload)


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
            label = "id" if field_name == "event_id" else field_name
            _validate_non_empty_string(
                ApplicationProtocolError,
                f"application event {label}",
                getattr(self, field_name),
            )
        for field_name in ("turn_id", "cursor"):
            _validate_optional_non_empty_string(
                ApplicationProtocolError,
                f"application event {field_name}",
                getattr(self, field_name),
            )
        _validate_non_negative_integer(
            ApplicationProtocolError,
            "application event sequence",
            self.sequence,
        )
        _validate_non_negative_integer(
            ApplicationProtocolError,
            "application event occurred_at_unix_ms",
            self.occurred_at_unix_ms,
        )


@dataclass(frozen=True, slots=True)
class ApplicationProtocolEvent:
    kind: ApplicationProtocolEventKind
    metadata: ApplicationProtocolEventMetadata
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, ApplicationProtocolEventMetadata):
            raise ApplicationProtocolError(
                "application protocol event metadata must be ApplicationProtocolEventMetadata"
            )
        if self.kind not in APPLICATION_PROTOCOL_EVENT_KINDS:
            raise ApplicationProtocolError(f"unknown application protocol event kind {self.kind}")
        object.__setattr__(
            self,
            "payload",
            _freeze_payload(ApplicationProtocolError, "application protocol event payload", self.payload),
        )

    @classmethod
    def new(
        cls,
        kind: ApplicationProtocolEventKind,
        metadata: ApplicationProtocolEventMetadata,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> ApplicationProtocolEvent:
        return cls(kind=kind, metadata=metadata, payload={} if payload is None else payload)

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


@dataclass(slots=True)
class ApplicationProtocolLog:
    events: list[ApplicationProtocolEvent] = field(default_factory=list)
    _event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _events_by_id: dict[str, ApplicationProtocolEvent] = field(default_factory=dict, init=False, repr=False)
    _events_by_cursor: dict[str, ApplicationProtocolEvent] = field(default_factory=dict, init=False, repr=False)
    _last_sequence: int | None = field(default=None, init=False, repr=False)
    _run_id: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        initial_events = tuple(self.events)
        self.events = []
        for event in initial_events:
            self.append(event)

    def append(self, event: ApplicationProtocolEvent) -> bool:
        if not isinstance(event, ApplicationProtocolEvent):
            raise ApplicationProtocolError("application protocol log event must be ApplicationProtocolEvent")
        if event.metadata.event_id in self._event_ids:
            if self._events_by_id[event.metadata.event_id] != event:
                raise ApplicationProtocolError("application protocol log event_id conflict")
            return False
        if event.metadata.cursor is not None:
            existing_cursor_event = self._events_by_cursor.get(event.metadata.cursor)
            if existing_cursor_event is not None and existing_cursor_event != event:
                raise ApplicationProtocolError("application protocol log cursor conflict")
        if self._run_id is not None and event.metadata.run_id != self._run_id:
            raise ApplicationProtocolError("application protocol log event run_id must match first event")
        if self._last_sequence is not None and event.metadata.sequence <= self._last_sequence:
            raise ApplicationProtocolError(
                "application event sequence "
                f"{event.metadata.sequence} must be greater than previous sequence {self._last_sequence}"
            )
        if self._run_id is None:
            self._run_id = event.metadata.run_id
        self._last_sequence = event.metadata.sequence
        self._event_ids.add(event.metadata.event_id)
        self._events_by_id[event.metadata.event_id] = event
        if event.metadata.cursor is not None:
            self._events_by_cursor[event.metadata.cursor] = event
        self.events.append(event)
        return True

    def replay_after(
        self,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[ApplicationProtocolEvent, ...]:
        if cursor is not None and not isinstance(cursor, str):
            raise ApplicationProtocolError("application protocol replay cursor must be a string")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ApplicationProtocolError("application protocol replay limit must be an integer")
        if limit < 0:
            raise ApplicationProtocolError("application protocol replay limit must be non-negative")
        start_index = 0
        if cursor is not None:
            found_cursor = False
            for index, event in enumerate(self.events):
                if event.metadata.cursor == cursor or str(event.metadata.sequence) == cursor:
                    start_index = index + 1
                    found_cursor = True
                    break
            if not found_cursor:
                raise ApplicationProtocolError("application protocol replay cursor was not found")
        return tuple(self.events[start_index : start_index + limit])

    def __len__(self) -> int:
        return len(self.events)

    def is_empty(self) -> bool:
        return not self.events


@dataclass(slots=True)
class ApplicationProtocolStreamState:
    cutoffs: dict[str, dict[str, object]] = field(default_factory=dict)
    accepted_events: list[ApplicationProtocolEvent] = field(default_factory=list)

    def accept(self, event: ApplicationProtocolEvent) -> ApplicationProtocolEvent | None:
        if event.kind == "OutputCutoff":
            response_id = event.payload.get("response_id")
            last_client_delivered_sequence = event.payload.get("last_client_delivered_sequence")
            terminal_reason = event.payload.get("terminal_reason")
            draft_disposition = event.payload.get("draft_disposition")
            policy_decision_id = event.payload.get("policy_decision_id")
            if (
                not isinstance(response_id, str)
                or not response_id.strip()
                or not isinstance(last_client_delivered_sequence, int)
                or isinstance(last_client_delivered_sequence, bool)
                or last_client_delivered_sequence < 0
                or not isinstance(terminal_reason, str)
                or not terminal_reason.strip()
                or not isinstance(draft_disposition, str)
                or not draft_disposition.strip()
                or (policy_decision_id is not None and not isinstance(policy_decision_id, str))
                or response_id in self.cutoffs
            ):
                return None
            self.cutoffs[response_id] = {
                "last_client_delivered_sequence": last_client_delivered_sequence,
                "terminal_reason": terminal_reason,
                "draft_disposition": draft_disposition,
                "policy_decision_id": policy_decision_id,
            }
            self.accepted_events.append(event)
            return event

        payload_response_id = event.payload.get("response_id")
        response_id = payload_response_id if isinstance(payload_response_id, str) else None
        if response_id is not None and response_id in self.cutoffs:
            if event.kind in {"AssistantIncomplete", "AssistantRetracted"}:
                cutoff = self.cutoffs[response_id]
                last_client_delivered_sequence = event.payload.get("last_client_delivered_sequence")
                if not isinstance(last_client_delivered_sequence, int) or isinstance(
                    last_client_delivered_sequence,
                    bool,
                ):
                    return None
                if last_client_delivered_sequence != cutoff["last_client_delivered_sequence"]:
                    return None
                if event.payload.get("terminal_reason") != cutoff["terminal_reason"]:
                    return None
                if event.payload.get("draft_disposition") != cutoff["draft_disposition"]:
                    return None
                if event.payload.get("policy_decision_id") != cutoff["policy_decision_id"]:
                    return None
                if cutoff["draft_disposition"] == "retract" and event.kind != "AssistantRetracted":
                    return None
                if cutoff["draft_disposition"] == "mark_incomplete" and event.kind != "AssistantIncomplete":
                    return None
                self.accepted_events.append(event)
                return event
            if event.kind == "AssistantDraftDelta":
                return None
            return None

        self.accepted_events.append(event)
        return event

    def cutoff_for_response(self, response_id: str) -> int | None:
        cutoff = self.cutoffs.get(response_id)
        if cutoff is None:
            return None
        value = cutoff.get("last_client_delivered_sequence")
        return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    cursor: str | None = None
    graph_id: str | None = None
    node_id: str | None = None
    operation_id: str | None = None
    visibility: ApplicationEventVisibility = "client"

    def __post_init__(self) -> None:
        for field_name, value in (
            ("event_id", self.event_id),
            ("run_id", self.run_id),
            ("response_id", self.response_id),
            ("release_id", self.release_id),
            ("policy_snapshot_id", self.policy_snapshot_id),
            ("occurred_at", self.occurred_at),
        ):
            _validate_non_empty_string(
                ApplicationEventError,
                f"application event {field_name}",
                value,
            )
        _validate_optional_non_empty_string(
            ApplicationEventError,
            "application event turn_id",
            self.turn_id,
        )
        for field_name in ("cursor", "graph_id", "node_id", "operation_id"):
            _validate_optional_non_empty_string(
                ApplicationEventError,
                f"application event {field_name}",
                getattr(self, field_name),
            )
        if self.visibility not in APPLICATION_EVENT_VISIBILITIES:
            raise ApplicationEventError(
                "application event visibility must be one of "
                f"{', '.join(APPLICATION_EVENT_VISIBILITIES)}"
            )
        _validate_non_negative_integer(
            ApplicationEventError,
            "application event sequence",
            self.sequence,
        )


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    kind: ApplicationEventKind
    metadata: ApplicationEventMetadata
    payload: Mapping[str, object] = field(default_factory=dict)
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, ApplicationEventMetadata):
            raise ApplicationEventError("application event metadata must be ApplicationEventMetadata")
        if self.kind not in STANDARD_APPLICATION_EVENT_KINDS:
            raise ApplicationEventError(f"unknown application event kind {self.kind}")
        if self.kind in TOOL_APPLICATION_EVENT_KINDS:
            _validate_non_empty_string(ApplicationEventError, "tool_call_id", self.tool_call_id)
        elif self.tool_call_id is not None:
            _validate_non_empty_string(ApplicationEventError, "tool_call_id", self.tool_call_id)
        object.__setattr__(
            self,
            "payload",
            _freeze_payload(ApplicationEventError, "application event payload", self.payload),
        )

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
        elif call.status == "running":
            kind = "ToolCallStarted"
        elif call.status == "completed":
            kind = "ToolCallCompleted"
        elif call.status == "failed":
            kind = "ToolCallFailed"
        elif call.status == "denied":
            kind = "ToolCallDenied"
        elif call.status == "cancelled":
            kind = "ToolCallCancelled"
        elif call.status == "policy_stopped":
            kind = "ToolCallPolicyStopped"
        elif call.status == "expired":
            kind = "ToolCallIncomplete"
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
                "ToolResultStarted",
                metadata,
                tool_call_id=event.tool_call_id,
                payload={
                    "status": "running",
                    "tool_result_sequence": event.sequence,
                    "started_at": event.started_at,
                },
            )
        if event.kind == "delta":
            return cls.tool(
                "ToolResultDelta",
                metadata,
                tool_call_id=event.tool_call_id,
                payload={
                    "status": "incremental",
                    "tool_result_sequence": event.sequence,
                    "output": [cls._content_part_payload(part) for part in event.output],
                },
            )
        if event.kind == "artifact_ready" and event.artifact is not None:
            return cls.tool(
                "ToolResultArtifactReady",
                metadata,
                tool_call_id=event.tool_call_id,
                payload={
                    "status": "artifact_ready",
                    "tool_result_sequence": event.sequence,
                    "artifact": {
                        "artifact_id": event.artifact.artifact_id,
                        "uri": event.artifact.uri,
                        "checksum": event.artifact.checksum,
                        "media_type": event.artifact.media_type,
                    },
                },
            )
        if event.kind == "completed" and event.result is not None:
            return cls.tool(
                "ToolResultCompleted",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "failed" and event.result is not None:
            return cls.tool(
                "ToolResultFailed",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "denied" and event.result is not None:
            return cls.tool(
                "ToolResultDenied",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "cancelled" and event.result is not None:
            return cls.tool(
                "ToolResultCancelled",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "policy_stopped" and event.result is not None:
            return cls.tool(
                "ToolResultPolicyStopped",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        if event.kind == "incomplete" and event.result is not None:
            return cls.tool(
                "ToolResultIncomplete",
                metadata,
                tool_call_id=event.tool_call_id,
                payload=cls._tool_result_payload(event.sequence, event.result),
            )
        return None

    @staticmethod
    def _content_part_payload(part: ContentPart) -> dict[str, object]:
        return {
            "kind": part.kind,
            "text": part.text,
            "data": part.data,
            "metadata": dict(part.metadata),
        }

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
        if not isinstance(chunk, GenerationChunk):
            raise ApplicationEventError("output policy evaluation chunk must be a GenerationChunk")
        if not isinstance(input_digest, str):
            raise ApplicationEventError("output policy evaluation input_digest must be a string")
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
        if not isinstance(decision, OutputPolicyDecision):
            raise ApplicationEventError("output policy decision must be an OutputPolicyDecision")
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
        if not isinstance(cutoff, OutputCutoff):
            raise ApplicationEventError("output cutoff must be an OutputCutoff")
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
                        "terminal_reason": cutoff.terminal_reason,
                        "draft_disposition": cutoff.draft_disposition,
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
        return cls(
            kind=kind,
            metadata=metadata,
            payload={} if payload is None else payload,
            tool_call_id=None,
        )

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
        _validate_non_empty_string(ApplicationEventError, "tool_call_id", tool_call_id)
        return cls(
            kind=kind,
            metadata=metadata,
            payload={} if payload is None else payload,
            tool_call_id=tool_call_id,
        )


@dataclass(slots=True)
class ApplicationEventStreamState:
    cutoffs: dict[str, OutputCutoff] = field(default_factory=dict)
    accepted_events: list[ApplicationEvent] = field(default_factory=list)

    def accept(self, event: ApplicationEvent) -> ApplicationEvent | None:
        if event.kind == "OutputCutoff":
            payload = event.payload
            payload_response_id = payload.get("response_id")
            if (
                isinstance(payload_response_id, str)
                and payload_response_id != event.metadata.response_id
            ):
                return None
            response_id = event.metadata.response_id
            if response_id in self.cutoffs:
                return None
            try:
                stream_id = payload["stream_id"]
                turn_id = payload.get("turn_id")
                policy_decision_id = payload.get("policy_decision_id")
                last_generated_sequence = payload["last_generated_sequence"]
                last_policy_accepted_sequence = payload["last_policy_accepted_sequence"]
                last_client_delivered_sequence = payload["last_client_delivered_sequence"]
                terminal_reason = payload["terminal_reason"]
                draft_disposition = payload["draft_disposition"]
                durable_result = payload["durable_result"]
                occurred_at = payload["occurred_at"]
                if (
                    not isinstance(stream_id, str)
                    or (turn_id is not None and not isinstance(turn_id, str))
                    or (policy_decision_id is not None and not isinstance(policy_decision_id, str))
                    or not isinstance(last_generated_sequence, int)
                    or isinstance(last_generated_sequence, bool)
                    or not isinstance(last_policy_accepted_sequence, int)
                    or isinstance(last_policy_accepted_sequence, bool)
                    or not isinstance(last_client_delivered_sequence, int)
                    or isinstance(last_client_delivered_sequence, bool)
                    or not isinstance(terminal_reason, str)
                    or not isinstance(draft_disposition, str)
                    or not isinstance(durable_result, str)
                    or not isinstance(occurred_at, str)
                ):
                    return None
                cutoff = OutputCutoff(
                    stream_id=stream_id,
                    response_id=response_id,
                    turn_id=turn_id,
                    last_generated_sequence=last_generated_sequence,
                    last_policy_accepted_sequence=last_policy_accepted_sequence,
                    last_client_delivered_sequence=last_client_delivered_sequence,
                    terminal_reason=terminal_reason,
                    draft_disposition=draft_disposition,
                    durable_result=durable_result,
                    policy_decision_id=policy_decision_id,
                    occurred_at=occurred_at,
                )
            except (KeyError, TypeError, ValueError):
                return None
            self.cutoffs[response_id] = cutoff
            self.accepted_events.append(event)
            return event

        payload_response_id = event.payload.get("response_id")
        if (
            isinstance(payload_response_id, str)
            and payload_response_id != event.metadata.response_id
        ):
            return None
        response_id = event.metadata.response_id
        cutoff = self.cutoffs.get(response_id)
        if cutoff is not None:
            if event.kind in {"AssistantRetracted", "AssistantIncomplete"}:
                payload = event.payload
                last_client_delivered_sequence = payload.get("last_client_delivered_sequence")
                terminal_reason = payload.get("terminal_reason")
                draft_disposition = payload.get("draft_disposition")
                policy_decision_id = payload.get("policy_decision_id")
                if not isinstance(last_client_delivered_sequence, int) or isinstance(
                    last_client_delivered_sequence,
                    bool,
                ):
                    return None
                if last_client_delivered_sequence != cutoff.last_client_delivered_sequence:
                    return None
                if terminal_reason != cutoff.terminal_reason:
                    return None
                if draft_disposition != cutoff.draft_disposition:
                    return None
                if policy_decision_id != cutoff.policy_decision_id:
                    return None
                if cutoff.draft_disposition == "retract" and event.kind != "AssistantRetracted":
                    return None
                if cutoff.draft_disposition == "mark_incomplete" and event.kind != "AssistantIncomplete":
                    return None
                self.accepted_events.append(event)
                return event
            chunk_sequence = event.payload.get("chunk_sequence")
            if isinstance(chunk_sequence, int):
                return None
            if str(event.kind).startswith("OutputPolicy"):
                return None
            if event.kind == "RunSucceeded":
                return None
            if event.kind in TOOL_APPLICATION_EVENT_KINDS and event.kind not in POST_CUTOFF_TOOL_APPLICATION_EVENT_KINDS:
                return None

        self.accepted_events.append(event)
        return event
