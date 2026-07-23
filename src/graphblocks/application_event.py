from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import wraps
from threading import RLock
from types import MappingProxyType
from typing import Literal, ParamSpec, TypeVar, cast

from .canonical import _has_unicode_surrogate, canonical_dumps, canonical_loads
from .output_policy import (
    VALID_DRAFT_DISPOSITIONS,
    VALID_TERMINAL_REASONS,
    GenerationChunk,
    OutputCutoff,
    OutputPolicyDecision,
)
from .policy import PolicyDecision
from .tools import (
    ContentPart,
    ToolApprovalRequest,
    ToolCall,
    ToolCallDraft,
    ToolResult,
    ToolResultEvent,
)


_P = ParamSpec("_P")
_R = TypeVar("_R")
_MAX_U64 = (1 << 64) - 1


def _with_application_protocol_log_lock(
    method: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(method)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        log = cast("ApplicationProtocolLog", args[0])
        with log._lock:
            return method(*args, **kwargs)

    return locked


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
    "RunPausedCallbackDelivery",
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
    "RunPausedCallbackDelivery",
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
    "RunPausedCallbackDelivery",
    "RunPausedPolicy",
    "RunPausedOperator",
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
    "RunPausedCallbackDelivery",
    "RunPausedPolicy",
    "RunPausedOperator",
)
APPLICATION_PROTOCOL_TCK_EVENT_KINDS: tuple[ApplicationProtocolEventKind, ...] = APPLICATION_PROTOCOL_EVENT_KINDS
TERMINAL_APPLICATION_PROTOCOL_EVENT_KINDS = frozenset(
    {"RunCompleted", "RunFailed", "RunCancelled", "RunPolicyStopped", "RunExpired"}
)
TERMINAL_APPLICATION_EVENT_KINDS = frozenset(
    {
        "RunCompleted",
        "RunSucceeded",
        "RunFailed",
        "RunCancelled",
        "RunPolicyStopped",
        "RunExpired",
    }
)
POST_TERMINAL_APPLICATION_EVENT_KINDS = frozenset(
    {"LateExternalCallbackReceived"}
)


class ApplicationEventError(RuntimeError):
    pass


class ApplicationProtocolError(RuntimeError):
    pass


class _FrozenPayloadMapping(Mapping[str, object]):
    __slots__ = ("__values",)

    def __init__(self, values: Mapping[str, object]) -> None:
        object.__setattr__(
            self,
            "_FrozenPayloadMapping__values",
            MappingProxyType(dict(values)),
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("frozen payload mapping cannot be mutated")

    def __getitem__(self, key: str) -> object:
        return self.__values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    def __repr__(self) -> str:
        return repr(dict(self.__values))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and self.__values == dict(other)

    def __setitem__(self, key: str, value: object) -> None:
        raise TypeError("frozen payload mapping cannot be mutated")

    def __delitem__(self, key: str) -> None:
        raise TypeError("frozen payload mapping cannot be mutated")

    def clear(self) -> None:
        raise TypeError("frozen payload mapping cannot be mutated")

    def pop(self, key: str, default: object = None) -> object:
        raise TypeError("frozen payload mapping cannot be mutated")

    def popitem(self) -> tuple[str, object]:
        raise TypeError("frozen payload mapping cannot be mutated")

    def setdefault(self, key: str, default: object = None) -> object:
        raise TypeError("frozen payload mapping cannot be mutated")

    def update(self, *args: object, **kwargs: object) -> None:
        raise TypeError("frozen payload mapping cannot be mutated")

    def __ior__(self, other: object) -> _FrozenPayloadMapping:
        raise TypeError("frozen payload mapping cannot be mutated")

    def __or__(self, other: object) -> dict[str, object]:
        if not isinstance(other, Mapping):
            return NotImplemented
        return dict(self.__values) | dict(other)

    def __ror__(self, other: object) -> dict[str, object]:
        if not isinstance(other, Mapping):
            return NotImplemented
        return dict(other) | dict(self.__values)

    def copy(self) -> dict[str, object]:
        return dict(self.__values)

    def __copy__(self) -> _FrozenPayloadMapping:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
        return {
            key: _payload_projection(item)
            for key, item in self.__values.items()
        }

    def __reduce_ex__(
        self,
        protocol: int,
    ) -> tuple[type[_FrozenPayloadMapping], tuple[dict[str, object]]]:
        del protocol
        return type(self), (dict(self.__values),)


class _FrozenPayloadList(tuple[object, ...]):
    __slots__ = ()
    __hash__ = None

    def __new__(cls, values: Iterable[object] = ()) -> _FrozenPayloadList:
        return super().__new__(cls, values)

    def __repr__(self) -> str:
        return repr(list(self))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, (list, tuple)) and tuple(self) == tuple(other)

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __getitem__(self, key: int | slice) -> object:
        value = super().__getitem__(key)
        return list(value) if isinstance(key, slice) else value

    def __setitem__(self, index: object, value: object) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def __delitem__(self, index: object) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def append(self, item: object) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def clear(self) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def extend(self, items: object) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def insert(self, index: int, item: object) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def pop(self, index: int = -1) -> object:
        raise TypeError("frozen payload list cannot be mutated")

    def remove(self, item: object) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def reverse(self) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def sort(self, *args: object, **kwargs: object) -> None:
        raise TypeError("frozen payload list cannot be mutated")

    def __iadd__(self, items: object) -> _FrozenPayloadList:
        raise TypeError("frozen payload list cannot be mutated")

    def __imul__(self, multiplier: int) -> _FrozenPayloadList:
        raise TypeError("frozen payload list cannot be mutated")

    def __add__(self, other: object) -> list[object]:
        if not isinstance(other, (list, _FrozenPayloadList)):
            return NotImplemented
        return [*self, *other]

    def __radd__(self, other: object) -> list[object]:
        if not isinstance(other, list):
            return NotImplemented
        return [*other, *self]

    def __mul__(self, multiplier: int) -> list[object]:
        return list(self) * multiplier

    def __rmul__(self, multiplier: int) -> list[object]:
        return multiplier * list(self)

    def copy(self) -> list[object]:
        return list(self)

    def __copy__(self) -> _FrozenPayloadList:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> list[object]:
        return [_payload_projection(item) for item in self]

    def __reduce_ex__(
        self,
        protocol: int,
    ) -> tuple[type[_FrozenPayloadList], tuple[tuple[object, ...]]]:
        del protocol
        return type(self), (tuple(self),)


def _payload_projection(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _payload_projection(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_payload_projection(item) for item in value]
    return value


def _validate_non_empty_string(error_type: type[RuntimeError], label: str, value: object) -> None:
    if not isinstance(value, str):
        raise error_type(f"{label} must be a string")
    if not value.strip():
        raise error_type(f"{label} must not be empty")
    if _has_unicode_surrogate(value):
        raise error_type(f"{label} must contain only Unicode scalar values")


def _validate_exact_non_empty_string(error_type: type[RuntimeError], label: str, value: object) -> None:
    _validate_non_empty_string(error_type, label, value)
    if value != value.strip():
        raise error_type(f"{label} must not contain surrounding whitespace")


def _validate_optional_non_empty_string(
    error_type: type[RuntimeError],
    label: str,
    value: object,
) -> None:
    if value is not None:
        _validate_non_empty_string(error_type, label, value)


def _validate_optional_exact_non_empty_string(
    error_type: type[RuntimeError],
    label: str,
    value: object,
) -> None:
    if value is not None:
        _validate_exact_non_empty_string(error_type, label, value)


def _validate_non_negative_integer(error_type: type[RuntimeError], label: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(f"{label} must be an integer")
    if value < 0:
        raise error_type(f"{label} must be non-negative")
    if value > _MAX_U64:
        raise error_type(f"{label} must not exceed {_MAX_U64}")


def _copy_payload_value(error_type: type[RuntimeError], label: str, value: object) -> object:
    if isinstance(value, Mapping):
        copied = dict(value)
        if any(not isinstance(key, str) or not key.strip() for key in copied):
            raise error_type(f"{label} keys must be non-empty strings")
        if any(key != key.strip() for key in copied):
            raise error_type(f"{label} keys must not contain surrounding whitespace")
        return _FrozenPayloadMapping(
            {key: _copy_payload_value(error_type, f"{label}.{key}", item) for key, item in copied.items()}
        )
    if isinstance(value, list):
        return _FrozenPayloadList(_copy_payload_value(error_type, label, item) for item in value)
    if isinstance(value, tuple):
        return tuple(_copy_payload_value(error_type, label, item) for item in value)
    return value


def _freeze_payload(error_type: type[RuntimeError], label: str, payload: object) -> _FrozenPayloadMapping:
    if not isinstance(payload, Mapping):
        raise error_type(f"{label} must be a mapping")
    try:
        normalized = dict(payload)
    except (RuntimeError, TypeError, ValueError) as error:
        raise error_type(f"{label} must be a stable mapping") from error
    if any(not isinstance(key, str) or not key.strip() for key in normalized):
        raise error_type(f"{label} keys must be non-empty strings")
    if any(key != key.strip() for key in normalized):
        raise error_type(f"{label} keys must not contain surrounding whitespace")
    try:
        snapshot = canonical_loads(canonical_dumps(normalized))
    except (RecursionError, TypeError, ValueError) as error:
        raise error_type(f"{label} must contain only canonical JSON values") from error
    assert isinstance(snapshot, dict)
    return _FrozenPayloadMapping(
        {
            key: _copy_payload_value(error_type, f"{label}.{key}", value)
            for key, value in snapshot.items()
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
            _validate_exact_non_empty_string(
                ApplicationProtocolError,
                f"application command {label}",
                getattr(self, field_name),
            )
        for field_name in ("turn_id", "idempotency_key"):
            _validate_optional_exact_non_empty_string(
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
    release_id: str = "local"
    turn_id: str | None = None
    operation_id: str | None = None
    cursor: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_id", "protocol_version", "run_id", "release_id"):
            label = "id" if field_name == "event_id" else field_name
            _validate_exact_non_empty_string(
                ApplicationProtocolError,
                f"application event {label}",
                getattr(self, field_name),
            )
        for field_name in ("turn_id", "operation_id", "cursor"):
            _validate_optional_exact_non_empty_string(
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


@dataclass(slots=True, init=False)
class ApplicationProtocolLog:
    _events: list[ApplicationProtocolEvent] = field(default_factory=list, init=False, repr=False)
    _event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _events_by_id: dict[str, ApplicationProtocolEvent] = field(default_factory=dict, init=False, repr=False)
    _events_by_cursor: dict[str, ApplicationProtocolEvent] = field(default_factory=dict, init=False, repr=False)
    _last_sequence: int | None = field(default=None, init=False, repr=False)
    _run_id: str | None = field(default=None, init=False, repr=False)
    _terminal_event: ApplicationProtocolEvent | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __init__(self, events: Iterable[ApplicationProtocolEvent] = ()) -> None:
        self._events = []
        self._event_ids = set()
        self._events_by_id = {}
        self._events_by_cursor = {}
        self._last_sequence = None
        self._run_id = None
        self._terminal_event = None
        self._lock = RLock()
        try:
            initial_events = tuple(events)
        except (RuntimeError, TypeError, ValueError) as error:
            raise ApplicationProtocolError(
                "application protocol log events must be iterable"
            ) from error
        for event in initial_events:
            self.append(event)

    @property
    def events(self) -> tuple[ApplicationProtocolEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @_with_application_protocol_log_lock
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
        if (
            self._terminal_event is not None
            and event.kind not in POST_TERMINAL_APPLICATION_EVENT_KINDS
        ):
            raise ApplicationProtocolError(
                "application protocol log already contains terminal event "
                f"{self._terminal_event.kind}"
            )
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
        if event.kind in TERMINAL_APPLICATION_PROTOCOL_EVENT_KINDS:
            self._terminal_event = event
        self._events.append(event)
        return True

    @_with_application_protocol_log_lock
    def replay_after(
        self,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[ApplicationProtocolEvent, ...]:
        if cursor is not None and not isinstance(cursor, str):
            raise ApplicationProtocolError("application protocol replay cursor must be a string")
        if cursor is not None and not cursor.strip():
            raise ApplicationProtocolError("application protocol replay cursor must not be empty")
        if cursor is not None and cursor != cursor.strip():
            raise ApplicationProtocolError("application protocol replay cursor must not contain surrounding whitespace")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ApplicationProtocolError("application protocol replay limit must be an integer")
        if limit < 0:
            raise ApplicationProtocolError("application protocol replay limit must be non-negative")
        start_index = 0
        if cursor is not None:
            matched_event = self._events_by_cursor.get(cursor)
            if matched_event is None:
                matched_event = next(
                    (
                        event
                        for event in self._events
                        if str(event.metadata.sequence) == cursor
                    ),
                    None,
                )
            if matched_event is None:
                raise ApplicationProtocolError("application protocol replay cursor was not found")
            start_index = self._events.index(matched_event) + 1
        return tuple(self._events[start_index : start_index + limit])

    @_with_application_protocol_log_lock
    def __len__(self) -> int:
        return len(self._events)

    @_with_application_protocol_log_lock
    def is_empty(self) -> bool:
        return not self._events


@dataclass(slots=True)
class ApplicationProtocolStreamState:
    cutoffs: dict[str, dict[str, object]] = field(default_factory=dict)
    accepted_events: list[ApplicationProtocolEvent] = field(default_factory=list)
    accepted_events_by_id: dict[str, ApplicationProtocolEvent] = field(
        default_factory=dict,
        compare=False,
        init=False,
        repr=False,
    )
    last_sequence_by_run_id: dict[str, int] = field(
        default_factory=dict,
        compare=False,
        init=False,
        repr=False,
    )
    terminal_events_by_run_id: dict[str, ApplicationProtocolEvent] = field(
        default_factory=dict,
    )

    def __post_init__(self) -> None:
        try:
            restored_events = tuple(self.accepted_events)
            restored_cutoffs = dict(self.cutoffs)
            restored_terminals = dict(self.terminal_events_by_run_id)
        except (TypeError, ValueError) as error:
            raise ApplicationProtocolError(
                "application protocol stream state must contain valid collections"
            ) from error
        self.cutoffs = {}
        self.accepted_events = []
        self.accepted_events_by_id = {}
        self.last_sequence_by_run_id = {}
        self.terminal_events_by_run_id = {}
        for event in restored_events:
            if not isinstance(event, ApplicationProtocolEvent):
                raise ApplicationProtocolError(
                    "application protocol stream state events must be "
                    "ApplicationProtocolEvent"
                )
            if self.accept(event) != event:
                raise ApplicationProtocolError(
                    "application protocol stream state events are inconsistent"
                )
        if len(self.accepted_events) != len(restored_events):
            raise ApplicationProtocolError(
                "application protocol stream state events must be unique"
            )
        if restored_cutoffs and restored_cutoffs != self.cutoffs:
            raise ApplicationProtocolError(
                "application protocol stream state cutoffs do not match events"
            )
        if restored_terminals and restored_terminals != self.terminal_events_by_run_id:
            raise ApplicationProtocolError(
                "application protocol stream state terminal events do not match events"
            )

    def accept(self, event: ApplicationProtocolEvent) -> ApplicationProtocolEvent | None:
        if not isinstance(event, ApplicationProtocolEvent):
            raise ApplicationProtocolError(
                "application protocol stream state event must be ApplicationProtocolEvent"
            )
        existing_event = self.accepted_events_by_id.get(event.metadata.event_id)
        if existing_event is not None:
            return existing_event if existing_event == event else None
        last_sequence = self.last_sequence_by_run_id.get(event.metadata.run_id)
        if last_sequence is not None and event.metadata.sequence <= last_sequence:
            return None
        if (
            event.metadata.run_id in self.terminal_events_by_run_id
            and event.kind not in POST_TERMINAL_APPLICATION_EVENT_KINDS
        ):
            return None
        payload_response_id = event.payload.get("response_id")
        if "response_id" in event.payload and (
            not isinstance(payload_response_id, str)
            or not payload_response_id.strip()
            or payload_response_id != payload_response_id.strip()
        ):
            return None
        if event.kind == "OutputCutoff":
            response_id = payload_response_id
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
                or terminal_reason not in VALID_TERMINAL_REASONS
                or not isinstance(draft_disposition, str)
                or draft_disposition not in VALID_DRAFT_DISPOSITIONS
                or (
                    policy_decision_id is not None
                    and (
                        not isinstance(policy_decision_id, str)
                        or not policy_decision_id.strip()
                        or policy_decision_id != policy_decision_id.strip()
                    )
                )
                or response_id in self.cutoffs
            ):
                return None
            self.cutoffs[response_id] = {
                "last_client_delivered_sequence": last_client_delivered_sequence,
                "terminal_reason": terminal_reason,
                "draft_disposition": draft_disposition,
                "policy_decision_id": policy_decision_id,
            }
            self._record(event)
            return event

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
                self._record(event)
                return event
            if event.kind == "AssistantDraftDelta":
                return None
            return None

        self._record(event)
        return event

    def _record(self, event: ApplicationProtocolEvent) -> None:
        self.accepted_events_by_id[event.metadata.event_id] = event
        self.last_sequence_by_run_id[event.metadata.run_id] = event.metadata.sequence
        if event.kind in TERMINAL_APPLICATION_PROTOCOL_EVENT_KINDS:
            self.terminal_events_by_run_id[event.metadata.run_id] = event
        self.accepted_events.append(event)

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
            _validate_exact_non_empty_string(
                ApplicationEventError,
                f"application event {field_name}",
                value,
            )
        _validate_optional_exact_non_empty_string(
            ApplicationEventError,
            "application event turn_id",
            self.turn_id,
        )
        for field_name in ("cursor", "graph_id", "node_id", "operation_id"):
            _validate_optional_exact_non_empty_string(
                ApplicationEventError,
                f"application event {field_name}",
                getattr(self, field_name),
            )
        if not isinstance(self.visibility, str):
            raise ApplicationEventError("application event visibility must be a string")
        if not self.visibility.strip():
            raise ApplicationEventError("application event visibility must not be empty")
        if self.visibility != self.visibility.strip():
            raise ApplicationEventError("application event visibility must not contain surrounding whitespace")
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
    accepted_events_by_id: dict[str, ApplicationEvent] = field(default_factory=dict)
    last_sequence_by_run_id: dict[str, int] = field(default_factory=dict)
    terminal_events_by_run_id: dict[str, ApplicationEvent] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            restored_events = tuple(self.accepted_events)
            restored_cutoffs = dict(self.cutoffs)
            restored_events_by_id = dict(self.accepted_events_by_id)
            restored_sequences = dict(self.last_sequence_by_run_id)
            restored_terminals = dict(self.terminal_events_by_run_id)
        except (TypeError, ValueError) as error:
            raise ApplicationEventError(
                "application event stream state must contain valid collections"
            ) from error
        self.cutoffs = {}
        self.accepted_events = []
        self.accepted_events_by_id = {}
        self.last_sequence_by_run_id = {}
        self.terminal_events_by_run_id = {}
        for event in restored_events:
            if not isinstance(event, ApplicationEvent):
                raise ApplicationEventError(
                    "application event stream state events must be ApplicationEvent"
                )
            if self.accept(event) != event:
                raise ApplicationEventError(
                    "application event stream state events are inconsistent"
                )
        if len(self.accepted_events) != len(restored_events):
            raise ApplicationEventError(
                "application event stream state events must be unique"
            )
        for label, restored, derived in (
            ("cutoffs", restored_cutoffs, self.cutoffs),
            ("event index", restored_events_by_id, self.accepted_events_by_id),
            ("sequence index", restored_sequences, self.last_sequence_by_run_id),
            ("terminal events", restored_terminals, self.terminal_events_by_run_id),
        ):
            if restored and restored != derived:
                raise ApplicationEventError(
                    f"application event stream state {label} do not match events"
                )

    def accept(self, event: ApplicationEvent) -> ApplicationEvent | None:
        if not isinstance(event, ApplicationEvent):
            raise ApplicationEventError(
                "application event stream state event must be ApplicationEvent"
            )
        existing_event = self.accepted_events_by_id.get(event.metadata.event_id)
        if existing_event is not None:
            if existing_event == event:
                return existing_event
            return None
        last_sequence = self.last_sequence_by_run_id.get(event.metadata.run_id)
        if last_sequence is not None and event.metadata.sequence <= last_sequence:
            return None
        if (
            event.metadata.run_id in self.terminal_events_by_run_id
            and event.kind not in POST_TERMINAL_APPLICATION_EVENT_KINDS
        ):
            return None
        payload_response_id = event.payload.get("response_id")
        if "response_id" in event.payload and (
            not isinstance(payload_response_id, str)
            or not payload_response_id.strip()
            or payload_response_id != payload_response_id.strip()
            or payload_response_id != event.metadata.response_id
        ):
            return None
        if event.kind == "OutputCutoff":
            payload = event.payload
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
            self._record(event)
            return event

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
                self._record(event)
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

        self._record(event)
        return event

    def _record(self, event: ApplicationEvent) -> None:
        self.accepted_events_by_id[event.metadata.event_id] = event
        self.last_sequence_by_run_id[event.metadata.run_id] = event.metadata.sequence
        if event.kind in TERMINAL_APPLICATION_EVENT_KINDS:
            self.terminal_events_by_run_id[event.metadata.run_id] = event
        self.accepted_events.append(event)
