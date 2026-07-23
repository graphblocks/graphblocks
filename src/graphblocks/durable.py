from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from graphblocks.canonical import canonical_dumps, canonical_loads
from graphblocks.documents import FrozenDict, FrozenList
from graphblocks.output_policy import (
    DraftDisposition as OutputCutoffDraftDisposition,
    OutputDurableResult as OutputCutoffDurableResult,
    TerminalReason as OutputCutoffTerminalReason,
    VALID_DRAFT_DISPOSITIONS as VALID_OUTPUT_CUTOFF_DRAFT_DISPOSITIONS,
    VALID_OUTPUT_DURABLE_RESULTS as VALID_OUTPUT_CUTOFF_DURABLE_RESULTS,
    VALID_TERMINAL_REASONS as VALID_OUTPUT_CUTOFF_TERMINAL_REASONS,
)
from graphblocks.tools import ToolResultStatus, VALID_TOOL_RESULT_STATUSES

if TYPE_CHECKING:
    from graphblocks.output_policy import OutputCutoff
    from graphblocks.tools import ToolResult


DeliveryGuarantee = Literal["best_effort", "at_most_once", "at_least_once"]
WatermarkKind = Literal["event_time", "processing_time"]
AccumulationMode = Literal["discarding", "accumulating"]
DurableToolTerminalState = ToolResultStatus | Literal["expired"]

VALID_DELIVERY_GUARANTEES = frozenset({"best_effort", "at_most_once", "at_least_once"})
VALID_DURABLE_TOOL_TERMINAL_STATES = VALID_TOOL_RESULT_STATUSES | frozenset({"expired"})


class DurableError(ValueError):
    """Base error for durable stream contracts."""


class InvalidDemandError(DurableError):
    pass


class SourcePausedError(DurableError):
    pass


class UnknownSourceCursorError(DurableError):
    def __init__(self, cursor: SourceCursor) -> None:
        self.cursor = cursor
        super().__init__(f"source cursor is not known to this source: {cursor.partition_key()!r}")


class DemandExceededError(DurableError):
    def __init__(self, demand: int, actual: int) -> None:
        self.demand = demand
        self.actual = actual
        super().__init__(f"source batch has {actual} events, exceeding demand {demand}")


class StaleCommitError(DurableError):
    def __init__(self, current: SourceCursor, attempted: SourceCursor) -> None:
        self.current = current
        self.attempted = attempted
        super().__init__(f"stale source cursor commit: current={current}, attempted={attempted}")


class ConflictingSourceOffsetError(DurableError):
    def __init__(self, cursor: SourceCursor) -> None:
        self.cursor = cursor
        super().__init__(f"source offset is reused with conflicting event data: {cursor}")


class InvalidWindowSizeError(DurableError):
    pass


class MissingEventTimeError(DurableError):
    def __init__(self, cursor: SourceCursor) -> None:
        self.cursor = cursor
        super().__init__(f"event-time window input is missing event time: {cursor.partition_key()!r}")


class LateEventError(DurableError):
    def __init__(self, event_time_unix_ms: int, watermark_unix_ms: int, allowed_lateness_ms: int) -> None:
        self.event_time_unix_ms = event_time_unix_ms
        self.watermark_unix_ms = watermark_unix_ms
        self.allowed_lateness_ms = allowed_lateness_ms
        super().__init__(
            f"event time {event_time_unix_ms} is late for watermark {watermark_unix_ms} "
            f"with allowed lateness {allowed_lateness_ms}"
        )


class SinkCommitError(DurableError):
    pass


class MissingRunIdError(SinkCommitError):
    pass


class MissingNodeIdError(SinkCommitError):
    pass


class MissingNodeAttemptIdError(SinkCommitError):
    pass


class MissingIdempotencyKeyError(SinkCommitError):
    pass


class IdempotencyConflictError(SinkCommitError):
    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"idempotency conflict for key {idempotency_key!r}")


class ToolTerminalStoreError(DurableError):
    pass


def _require_tool_terminal_integer(field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolTerminalStoreError(f"{field_name} must be an integer")
    return value


def _require_tool_terminal_string(field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ToolTerminalStoreError(f"{field_name} must be a string")
    if not value.strip():
        raise ToolTerminalStoreError(f"{field_name} must not be empty")
    if value != value.strip():
        raise ToolTerminalStoreError(
            f"{field_name} must not contain surrounding whitespace"
        )
    return value


class ToolTerminalStateConflictError(ToolTerminalStoreError):
    def __init__(self, response_id: str, tool_call_id: str, revision: int) -> None:
        self.response_id = response_id
        self.tool_call_id = tool_call_id
        self.revision = revision
        super().__init__(
            f"tool terminal state conflict for response {response_id!r}, "
            f"tool call {tool_call_id!r}, revision {revision}"
        )


class ResponsePolicyStopConflictError(ToolTerminalStoreError):
    def __init__(self, response_id: str) -> None:
        self.response_id = response_id
        super().__init__(f"response policy stop conflict for response {response_id!r}")


class DurableResultAlreadyCommittedError(ToolTerminalStoreError):
    def __init__(self, response_id: str) -> None:
        self.response_id = response_id
        super().__init__(f"durable result already committed for response {response_id!r}")


class ResponsePolicyStoppedError(ToolTerminalStoreError):
    def __init__(self, response_id: str) -> None:
        self.response_id = response_id
        super().__init__(f"response {response_id!r} is policy stopped")


class CheckpointBarrierError(DurableError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"checkpoint barrier invalid: {reason}")


class CheckpointStoreError(DurableError):
    pass


class StaleCheckpointError(CheckpointStoreError):
    def __init__(self, run_id: str, current: int, attempted: int) -> None:
        self.run_id = run_id
        self.current = current
        self.attempted = attempted
        super().__init__(
            f"stale checkpoint state revision for run {run_id!r}: current={current}, attempted={attempted}"
        )


def _require_integer(field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DurableError(f"{field_name} must be an integer")
    return value


def _require_delivery_guarantee(value: object) -> None:
    if not isinstance(value, str) or value not in VALID_DELIVERY_GUARANTEES:
        raise DurableError(f"unsupported delivery guarantee {value!r}")


@dataclass(frozen=True, slots=True, order=True)
class SourceCursor:
    stream: str
    partition: int
    offset: int

    def __post_init__(self) -> None:
        if not isinstance(self.stream, str):
            raise DurableError("stream must be a string")
        if not self.stream.strip():
            raise DurableError("stream must not be empty")
        if self.stream != self.stream.strip():
            raise DurableError("stream must not contain surrounding whitespace")
        _require_integer("partition", self.partition)
        if self.partition < 0:
            raise DurableError("partition must be non-negative")
        _require_integer("offset", self.offset)
        if self.offset < 0:
            raise DurableError("offset must be non-negative")

    def partition_key(self) -> str:
        return f"{self.stream}:{self.partition}"


@dataclass(frozen=True, slots=True)
class Watermark:
    kind: WatermarkKind
    unix_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "event_time",
            "processing_time",
        }:
            raise DurableError(f"unsupported watermark kind {self.kind!r}")
        _require_integer("watermark unix_ms", self.unix_ms)
        if self.unix_ms < 0:
            raise DurableError("watermark unix_ms must be non-negative")

    @classmethod
    def event_time(cls, unix_ms: int) -> Watermark:
        return cls("event_time", unix_ms)

    @classmethod
    def processing_time(cls, unix_ms: int) -> Watermark:
        return cls("processing_time", unix_ms)


@dataclass(frozen=True, slots=True)
class SourceEvent:
    cursor: SourceCursor
    payload: object
    event_time_unix_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cursor, SourceCursor):
            raise DurableError("source event cursor must be a SourceCursor")
        object.__setattr__(self, "payload", deepcopy(self.payload))
        if self.event_time_unix_ms is None:
            return
        _require_integer("event_time_unix_ms", self.event_time_unix_ms)
        if self.event_time_unix_ms < 0:
            raise DurableError("event_time_unix_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class SourceBatch:
    guarantee: DeliveryGuarantee
    events: tuple[SourceEvent, ...]
    watermark: Watermark | None = None

    def __post_init__(self) -> None:
        _require_delivery_guarantee(self.guarantee)
        try:
            events = tuple(self.events)
        except TypeError:
            raise DurableError("source batch events must be a sequence") from None
        if any(not isinstance(event, SourceEvent) for event in events):
            raise DurableError("source batch events must be SourceEvent")
        if self.watermark is not None and not isinstance(self.watermark, Watermark):
            raise DurableError("source batch watermark must be a Watermark")
        object.__setattr__(
            self,
            "events",
            tuple(
                SourceEvent(
                    cursor=event.cursor,
                    payload=event.payload,
                    event_time_unix_ms=event.event_time_unix_ms,
                )
                for event in events
            ),
        )

    @classmethod
    def new(
        cls,
        guarantee: DeliveryGuarantee,
        events: list[SourceEvent] | tuple[SourceEvent, ...],
        watermark: Watermark | None,
        demand: int,
    ) -> SourceBatch:
        demand = _require_integer("demand", demand)
        if demand <= 0:
            raise InvalidDemandError("demand must be positive")
        events = tuple(events)
        if len(events) > demand:
            raise DemandExceededError(demand, len(events))
        return cls(guarantee=guarantee, events=events, watermark=watermark)

    def high_cursor(self) -> SourceCursor | None:
        if not self.events:
            return None
        return max(event.cursor for event in self.events)


@dataclass(slots=True)
class InMemoryDurableSource:
    guarantee: DeliveryGuarantee
    events: list[SourceEvent] | tuple[SourceEvent, ...]
    committed_cursor: SourceCursor | None = None
    paused: bool = False
    _known_streams: frozenset[str] = field(init=False, repr=False)
    _known_partitions: frozenset[tuple[str, int]] = field(init=False, repr=False)
    _known_cursors: frozenset[SourceCursor] = field(init=False, repr=False)
    _committed_cursors: dict[tuple[str, int], SourceCursor] = field(
        init=False,
        repr=False,
        default_factory=dict,
    )
    _watermark_unix_ms: int | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        _require_delivery_guarantee(self.guarantee)
        if not isinstance(self.paused, bool):
            raise DurableError("source paused must be a boolean")
        try:
            events = tuple(self.events)
        except TypeError:
            raise DurableError("source events must be a sequence") from None
        if any(not isinstance(event, SourceEvent) for event in events):
            raise DurableError("source events must be SourceEvent")
        events_by_cursor: dict[SourceCursor, SourceEvent] = {}
        for event in events:
            existing = events_by_cursor.get(event.cursor)
            if existing is not None and existing != event:
                raise ConflictingSourceOffsetError(event.cursor)
            events_by_cursor[event.cursor] = event
        self.events = tuple(sorted(
            (
                SourceEvent(
                    cursor=event.cursor,
                    payload=event.payload,
                    event_time_unix_ms=event.event_time_unix_ms,
                )
                for event in events_by_cursor.values()
            ),
            key=lambda event: event.cursor,
        ))
        self._known_streams = frozenset(event.cursor.stream for event in self.events)
        self._known_partitions = frozenset(
            (event.cursor.stream, event.cursor.partition) for event in self.events
        )
        self._known_cursors = frozenset(event.cursor for event in self.events)
        if self.committed_cursor is not None:
            if not isinstance(self.committed_cursor, SourceCursor):
                raise DurableError("source committed_cursor must be a SourceCursor")
            self._validate_cursor(self.committed_cursor)
            self._committed_cursors[
                (self.committed_cursor.stream, self.committed_cursor.partition)
            ] = self.committed_cursor

    def poll(self, cursor: SourceCursor | None, *, demand: int) -> SourceBatch:
        if self.paused:
            raise SourcePausedError("source is paused")
        demand = _require_integer("demand", demand)
        if demand <= 0:
            raise InvalidDemandError("demand must be positive")
        if cursor is not None:
            self._validate_cursor(cursor)
        replay_cursors = dict(self._committed_cursors)
        if cursor is not None:
            replay_cursors[(cursor.stream, cursor.partition)] = cursor
        events = [
            event
            for event in self.events
            if (
                (replay_cursor := replay_cursors.get(
                    (event.cursor.stream, event.cursor.partition)
                ))
                is None
                or event.cursor.offset > replay_cursor.offset
            )
        ][:demand]
        event_times = [event.event_time_unix_ms for event in events if event.event_time_unix_ms is not None]
        if event_times:
            batch_watermark = max(event_times)
            self._watermark_unix_ms = (
                batch_watermark
                if self._watermark_unix_ms is None
                else max(self._watermark_unix_ms, batch_watermark)
            )
        watermark = (
            None
            if self._watermark_unix_ms is None
            else Watermark.event_time(self._watermark_unix_ms)
        )
        return SourceBatch.new(
            self.guarantee,
            tuple(
                SourceEvent(
                    cursor=event.cursor,
                    payload=event.payload,
                    event_time_unix_ms=event.event_time_unix_ms,
                )
                for event in events
            ),
            watermark,
            demand,
        )

    def commit(self, cursor: SourceCursor) -> None:
        self._validate_cursor_partition(cursor)
        partition_key = (cursor.stream, cursor.partition)
        current = self._committed_cursors.get(partition_key)
        if current is not None and cursor.offset < current.offset:
            raise StaleCommitError(current, cursor)
        self._validate_cursor(cursor)
        self._committed_cursors[partition_key] = cursor
        self.committed_cursor = cursor

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def _validate_cursor(self, cursor: SourceCursor) -> None:
        self._validate_cursor_partition(cursor)
        if cursor not in self._known_cursors:
            raise UnknownSourceCursorError(cursor)

    def _validate_cursor_partition(self, cursor: SourceCursor) -> None:
        if cursor.stream not in self._known_streams:
            raise UnknownSourceCursorError(cursor)
        if (cursor.stream, cursor.partition) not in self._known_partitions:
            raise UnknownSourceCursorError(cursor)


@dataclass(frozen=True, slots=True)
class WindowPolicy:
    size_ms: int
    allowed_lateness_ms: int
    accumulation_mode: AccumulationMode

    def __post_init__(self) -> None:
        _require_integer("window size_ms", self.size_ms)
        if self.size_ms <= 0:
            raise InvalidWindowSizeError("window size_ms must be positive")
        _require_integer("allowed_lateness_ms", self.allowed_lateness_ms)
        if self.allowed_lateness_ms < 0:
            raise DurableError("allowed_lateness_ms must be non-negative")
        if (
            not isinstance(self.accumulation_mode, str)
            or self.accumulation_mode not in {"discarding", "accumulating"}
        ):
            raise DurableError(f"unsupported accumulation mode {self.accumulation_mode!r}")

    @classmethod
    def tumbling_event_time(
        cls,
        *,
        size_ms: int,
        allowed_lateness_ms: int,
        accumulation_mode: AccumulationMode,
    ) -> WindowPolicy:
        return cls(size_ms=size_ms, allowed_lateness_ms=allowed_lateness_ms, accumulation_mode=accumulation_mode)


@dataclass(frozen=True, slots=True)
class WindowPane:
    start_unix_ms: int
    end_unix_ms: int
    events: tuple[SourceEvent, ...]
    revision: int = 0
    is_final: bool = True


@dataclass(slots=True)
class WindowAccumulator:
    policy: WindowPolicy
    watermark: Watermark | None = None
    windows: dict[int, list[SourceEvent]] = field(default_factory=dict)
    _on_time_emitted: set[int] = field(default_factory=set, init=False, repr=False)

    def ingest(self, event: SourceEvent) -> None:
        if not isinstance(event, SourceEvent):
            raise DurableError("window event must be a SourceEvent")
        if event.event_time_unix_ms is None:
            raise MissingEventTimeError(event.cursor)
        event_time_unix_ms = event.event_time_unix_ms
        start_unix_ms = event_time_unix_ms - (event_time_unix_ms % self.policy.size_ms)
        deadline_unix_ms = (
            start_unix_ms + self.policy.size_ms + self.policy.allowed_lateness_ms
        )
        if (
            self.watermark is not None
            and deadline_unix_ms <= self.watermark.unix_ms
        ):
            raise LateEventError(
                event_time_unix_ms,
                self.watermark.unix_ms,
                self.policy.allowed_lateness_ms,
            )
        self.windows.setdefault(start_unix_ms, []).append(
            SourceEvent(
                cursor=event.cursor,
                payload=event.payload,
                event_time_unix_ms=event.event_time_unix_ms,
            )
        )

    def advance_watermark(self, watermark: Watermark) -> list[WindowPane]:
        if watermark.kind != "event_time":
            return []
        if self.watermark is not None and watermark.unix_ms <= self.watermark.unix_ms:
            return []
        self.watermark = watermark
        triggerable = [
            start_unix_ms
            for start_unix_ms in sorted(self.windows)
            if start_unix_ms + self.policy.size_ms <= watermark.unix_ms
        ]
        emitted: list[WindowPane] = []
        for start_unix_ms in triggerable:
            end_unix_ms = start_unix_ms + self.policy.size_ms
            deadline_unix_ms = end_unix_ms + self.policy.allowed_lateness_ms
            if deadline_unix_ms <= watermark.unix_ms:
                events = tuple(
                    sorted(
                        self.windows.pop(start_unix_ms),
                        key=lambda event: event.cursor,
                    )
                )
                revision = 1 if start_unix_ms in self._on_time_emitted else 0
                self._on_time_emitted.discard(start_unix_ms)
                emitted.append(
                    WindowPane(
                        start_unix_ms=start_unix_ms,
                        end_unix_ms=end_unix_ms,
                        events=events,
                        revision=revision,
                        is_final=True,
                    )
                )
            elif (
                self.policy.accumulation_mode == "accumulating"
                and start_unix_ms not in self._on_time_emitted
            ):
                events = tuple(
                    sorted(self.windows[start_unix_ms], key=lambda event: event.cursor)
                )
                self._on_time_emitted.add(start_unix_ms)
                emitted.append(
                    WindowPane(
                        start_unix_ms=start_unix_ms,
                        end_unix_ms=end_unix_ms,
                        events=events,
                        revision=0,
                        is_final=False,
                    )
                )
        return emitted


@dataclass(frozen=True, slots=True)
class SinkCommitRequest:
    run_id: str
    node_id: str
    node_attempt_id: str
    idempotency_key: str
    payload: object
    precondition_digest: str | None = None

    def __post_init__(self) -> None:
        for field_name, error_type in (
            ("run_id", MissingRunIdError),
            ("node_id", MissingNodeIdError),
            ("node_attempt_id", MissingNodeAttemptIdError),
            ("idempotency_key", MissingIdempotencyKeyError),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise error_type(f"{field_name} must not be empty")
            if value != value.strip():
                raise error_type(f"{field_name} must not contain surrounding whitespace")
        if self.precondition_digest is not None:
            if (
                not isinstance(self.precondition_digest, str)
                or not self.precondition_digest.strip()
            ):
                raise SinkCommitError("precondition_digest must not be empty")
            if self.precondition_digest != self.precondition_digest.strip():
                raise SinkCommitError(
                    "precondition_digest must not contain surrounding whitespace"
                )
        object.__setattr__(self, "payload", deepcopy(self.payload))

    def with_precondition_digest(self, precondition_digest: str) -> SinkCommitRequest:
        return SinkCommitRequest(
            run_id=self.run_id,
            node_id=self.node_id,
            node_attempt_id=self.node_attempt_id,
            idempotency_key=self.idempotency_key,
            payload=self.payload,
            precondition_digest=precondition_digest,
        )


@dataclass(frozen=True, slots=True)
class SinkCommitResult:
    sink_id: str
    idempotency_key: str
    precondition_digest: str | None
    sequence: int
    metadata: object
    replayed: bool

    def __post_init__(self) -> None:
        for field_name in ("sink_id", "idempotency_key"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SinkCommitError(f"{field_name} must not be empty")
            if value != value.strip():
                raise SinkCommitError(
                    f"{field_name} must not contain surrounding whitespace"
                )
        if self.precondition_digest is not None and (
            not isinstance(self.precondition_digest, str)
            or not self.precondition_digest.strip()
        ):
            raise SinkCommitError("precondition_digest must not be empty")
        if (
            self.precondition_digest is not None
            and self.precondition_digest != self.precondition_digest.strip()
        ):
            raise SinkCommitError(
                "precondition_digest must not contain surrounding whitespace"
            )
        sequence = _require_integer("sink commit sequence", self.sequence)
        if sequence <= 0:
            raise SinkCommitError("sink commit sequence must be positive")
        if not isinstance(self.replayed, bool):
            raise SinkCommitError("sink commit replayed must be a boolean")
        object.__setattr__(self, "metadata", deepcopy(self.metadata))


@dataclass(slots=True)
class InMemoryDurableSink:
    sink_id: str
    next_sequence: int = 1
    commits_by_idempotency_key: dict[str, tuple[SinkCommitRequest, SinkCommitResult]] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.sink_id, str) or not self.sink_id.strip():
            raise SinkCommitError("sink_id must not be empty")
        if self.sink_id != self.sink_id.strip():
            raise SinkCommitError("sink_id must not contain surrounding whitespace")
        next_sequence = _require_integer("sink next_sequence", self.next_sequence)
        if next_sequence <= 0:
            raise SinkCommitError("sink next_sequence must be positive")
        if not isinstance(self.commits_by_idempotency_key, Mapping):
            raise SinkCommitError("restored sink commits must be a mapping")
        commits = dict(self.commits_by_idempotency_key)
        restored: dict[str, tuple[SinkCommitRequest, SinkCommitResult]] = {}
        seen_sequences: set[int] = set()
        highest_sequence = 0
        for idempotency_key, pair in commits.items():
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], SinkCommitRequest)
                or not isinstance(pair[1], SinkCommitResult)
            ):
                raise SinkCommitError(
                    "restored sink commits must contain request/result pairs"
                )
            request, result = pair
            if (
                idempotency_key != request.idempotency_key
                or idempotency_key != result.idempotency_key
            ):
                raise SinkCommitError(
                    "restored sink commit key must match idempotency_key"
                )
            if result.sink_id != self.sink_id:
                raise SinkCommitError("restored sink commit must match sink_id")
            if result.precondition_digest != request.precondition_digest:
                raise SinkCommitError(
                    "restored sink commit precondition must match request"
                )
            if result.replayed:
                raise SinkCommitError("restored sink commits must not be replay projections")
            if result.sequence in seen_sequences:
                raise SinkCommitError("restored sink commit sequences must be unique")
            seen_sequences.add(result.sequence)
            highest_sequence = max(highest_sequence, result.sequence)
            request_snapshot = replace(request, payload=deepcopy(request.payload))
            result_snapshot = replace(
                result,
                metadata=deepcopy(result.metadata),
                replayed=False,
            )
            if result_snapshot.metadata != request_snapshot.payload:
                raise SinkCommitError(
                    "restored sink commit metadata must match request payload"
                )
            restored[idempotency_key] = (request_snapshot, result_snapshot)
        self.commits_by_idempotency_key = restored
        self.next_sequence = max(next_sequence, highest_sequence + 1)

    def commit(self, request: SinkCommitRequest) -> SinkCommitResult:
        if not isinstance(request, SinkCommitRequest):
            raise SinkCommitError("request must be a SinkCommitRequest")
        with self._lock:
            if request.idempotency_key in self.commits_by_idempotency_key:
                existing_request, existing_result = self.commits_by_idempotency_key[
                    request.idempotency_key
                ]
                if existing_request != request:
                    raise IdempotencyConflictError(request.idempotency_key)
                return SinkCommitResult(
                    sink_id=existing_result.sink_id,
                    idempotency_key=existing_result.idempotency_key,
                    precondition_digest=existing_result.precondition_digest,
                    sequence=existing_result.sequence,
                    metadata=deepcopy(existing_result.metadata),
                    replayed=True,
                )
            payload_snapshot = deepcopy(request.payload)
            stored_request = replace(request, payload=payload_snapshot)
            result = SinkCommitResult(
                sink_id=self.sink_id,
                idempotency_key=request.idempotency_key,
                precondition_digest=request.precondition_digest,
                sequence=self._allocate_sequence(),
                metadata=deepcopy(payload_snapshot),
                replayed=False,
            )
            self.commits_by_idempotency_key[request.idempotency_key] = (
                stored_request,
                result,
            )
            return replace(result, metadata=deepcopy(result.metadata))

    def committed_count(self) -> int:
        with self._lock:
            return len(self.commits_by_idempotency_key)

    def _allocate_sequence(self) -> int:
        next_sequence = _require_integer("sink next_sequence", self.next_sequence)
        if next_sequence <= 0:
            raise SinkCommitError("sink next_sequence must be positive")
        highest_sequence = max(
            (
                result.sequence
                for _request, result in self.commits_by_idempotency_key.values()
            ),
            default=0,
        )
        sequence = max(next_sequence, highest_sequence + 1)
        self.next_sequence = sequence + 1
        return sequence


@dataclass(frozen=True, slots=True)
class DurableToolTerminalRecord:
    run_id: str
    response_id: str
    tool_call_id: str
    revision: int
    terminal_state: DurableToolTerminalState
    arguments_digest: str
    completed_at_unix_ms: int
    output_digest: str | None = None
    idempotency_key: str | None = None
    effect_committed: bool = False
    durable_result_committed: bool = False

    @classmethod
    def from_tool_result(
        cls,
        result: ToolResult,
        *,
        run_id: str,
        response_id: str,
        revision: int,
        arguments_digest: str,
        completed_at_unix_ms: int,
        idempotency_key: str | None = None,
        durable_result_committed: bool = False,
    ) -> DurableToolTerminalRecord:
        if result.status not in VALID_DURABLE_TOOL_TERMINAL_STATES:
            raise ToolTerminalStoreError(f"invalid tool result status {result.status!r}")
        return cls(
            run_id=run_id,
            response_id=response_id,
            tool_call_id=result.tool_call_id,
            revision=revision,
            terminal_state=result.status,
            arguments_digest=arguments_digest,
            completed_at_unix_ms=completed_at_unix_ms,
            output_digest=result.output_digest,
            idempotency_key=idempotency_key,
            effect_committed=result.effect_was_committed(),
            durable_result_committed=durable_result_committed,
        )

    def __post_init__(self) -> None:
        _require_tool_terminal_string("run_id", self.run_id)
        _require_tool_terminal_string("response_id", self.response_id)
        _require_tool_terminal_string("tool_call_id", self.tool_call_id)
        revision = _require_tool_terminal_integer("revision", self.revision)
        if revision <= 0:
            raise ToolTerminalStoreError("revision must be positive")
        if (
            not isinstance(self.terminal_state, str)
            or self.terminal_state not in VALID_DURABLE_TOOL_TERMINAL_STATES
        ):
            raise ToolTerminalStoreError(f"invalid terminal_state {self.terminal_state}")
        _require_tool_terminal_string("arguments_digest", self.arguments_digest)
        completed_at_unix_ms = _require_tool_terminal_integer(
            "completed_at_unix_ms",
            self.completed_at_unix_ms,
        )
        if completed_at_unix_ms <= 0:
            raise ToolTerminalStoreError("completed_at_unix_ms must be positive")
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "completed_at_unix_ms", completed_at_unix_ms)
        if self.output_digest is not None:
            _require_tool_terminal_string("output_digest", self.output_digest)
        if self.idempotency_key is not None:
            _require_tool_terminal_string("idempotency_key", self.idempotency_key)
        if not isinstance(self.effect_committed, bool):
            raise ToolTerminalStoreError("effect_committed must be a boolean")
        if not isinstance(self.durable_result_committed, bool):
            raise ToolTerminalStoreError(
                "durable_result_committed must be a boolean"
            )
        if self.terminal_state == "denied" and self.effect_committed:
            raise ToolTerminalStoreError("denied terminal records cannot have committed effects")
        if self.terminal_state == "expired" and self.effect_committed:
            raise ToolTerminalStoreError("expired terminal records cannot have committed effects")


@dataclass(frozen=True, slots=True)
class DurableToolTerminalCommit:
    sequence: int
    record: DurableToolTerminalRecord
    replayed: bool

    def __post_init__(self) -> None:
        sequence = _require_tool_terminal_integer("sequence", self.sequence)
        if sequence <= 0:
            raise ToolTerminalStoreError("sequence must be positive")
        if not isinstance(self.record, DurableToolTerminalRecord):
            raise ToolTerminalStoreError(
                "record must be a DurableToolTerminalRecord"
            )
        if not isinstance(self.replayed, bool):
            raise ToolTerminalStoreError("replayed must be a boolean")
        object.__setattr__(self, "sequence", sequence)


@dataclass(frozen=True, slots=True)
class DurableResponsePolicyStopRecord:
    response_id: str
    policy_decision_id: str
    last_policy_accepted_sequence: int
    occurred_at_unix_ms: int
    stream_id: str | None = None
    last_generated_sequence: int = 0
    last_client_delivered_sequence: int = 0
    terminal_reason: OutputCutoffTerminalReason = "policy_denied"
    draft_disposition: OutputCutoffDraftDisposition = "retract"
    durable_result: OutputCutoffDurableResult = "none"
    turn_id: str | None = None

    def __post_init__(self) -> None:
        _require_tool_terminal_string("response_id", self.response_id)
        _require_tool_terminal_string(
            "policy_decision_id",
            self.policy_decision_id,
        )
        if self.stream_id is None:
            object.__setattr__(self, "stream_id", self.response_id)
        _require_tool_terminal_string("stream_id", self.stream_id)
        if self.turn_id is not None:
            _require_tool_terminal_string("turn_id", self.turn_id)
        last_generated_sequence = _require_tool_terminal_integer(
            "last_generated_sequence",
            self.last_generated_sequence,
        )
        last_policy_accepted_sequence = _require_tool_terminal_integer(
            "last_policy_accepted_sequence",
            self.last_policy_accepted_sequence,
        )
        last_client_delivered_sequence = _require_tool_terminal_integer(
            "last_client_delivered_sequence",
            self.last_client_delivered_sequence,
        )
        occurred_at_unix_ms = _require_tool_terminal_integer(
            "occurred_at_unix_ms",
            self.occurred_at_unix_ms,
        )
        if last_generated_sequence < 0:
            raise ToolTerminalStoreError("last_generated_sequence must be non-negative")
        if last_policy_accepted_sequence < 0:
            raise ToolTerminalStoreError("last_policy_accepted_sequence must be non-negative")
        if last_client_delivered_sequence < 0:
            raise ToolTerminalStoreError("last_client_delivered_sequence must be non-negative")
        if last_policy_accepted_sequence > last_generated_sequence:
            raise ToolTerminalStoreError("last_policy_accepted_sequence cannot exceed last_generated_sequence")
        if last_client_delivered_sequence > last_generated_sequence:
            raise ToolTerminalStoreError("last_client_delivered_sequence cannot exceed last_generated_sequence")
        if (
            not isinstance(self.terminal_reason, str)
            or self.terminal_reason not in VALID_OUTPUT_CUTOFF_TERMINAL_REASONS
        ):
            raise ToolTerminalStoreError(f"invalid terminal_reason {self.terminal_reason}")
        if (
            not isinstance(self.draft_disposition, str)
            or self.draft_disposition not in VALID_OUTPUT_CUTOFF_DRAFT_DISPOSITIONS
        ):
            raise ToolTerminalStoreError(f"invalid draft_disposition {self.draft_disposition}")
        if (
            not isinstance(self.durable_result, str)
            or self.durable_result not in VALID_OUTPUT_CUTOFF_DURABLE_RESULTS
        ):
            raise ToolTerminalStoreError(f"invalid durable_result {self.durable_result}")
        if occurred_at_unix_ms <= 0:
            raise ToolTerminalStoreError("occurred_at_unix_ms must be positive")
        object.__setattr__(self, "last_generated_sequence", last_generated_sequence)
        object.__setattr__(self, "last_policy_accepted_sequence", last_policy_accepted_sequence)
        object.__setattr__(self, "last_client_delivered_sequence", last_client_delivered_sequence)
        object.__setattr__(self, "occurred_at_unix_ms", occurred_at_unix_ms)

    def to_output_cutoff(self, *, occurred_at: str) -> OutputCutoff:
        if not isinstance(occurred_at, str) or not occurred_at.strip():
            raise ToolTerminalStoreError("occurred_at must not be empty")

        from graphblocks.output_policy import OutputCutoff

        return OutputCutoff(
            stream_id=self.stream_id,
            response_id=self.response_id,
            turn_id=self.turn_id,
            last_generated_sequence=self.last_generated_sequence,
            last_policy_accepted_sequence=self.last_policy_accepted_sequence,
            last_client_delivered_sequence=self.last_client_delivered_sequence,
            terminal_reason=self.terminal_reason,
            draft_disposition=self.draft_disposition,
            durable_result=self.durable_result,
            policy_decision_id=self.policy_decision_id,
            occurred_at=occurred_at,
        )


@dataclass(frozen=True, slots=True)
class DurableResponsePolicyStopCommit:
    sequence: int
    record: DurableResponsePolicyStopRecord
    replayed: bool

    def __post_init__(self) -> None:
        sequence = _require_tool_terminal_integer("sequence", self.sequence)
        if sequence <= 0:
            raise ToolTerminalStoreError("sequence must be positive")
        if not isinstance(self.record, DurableResponsePolicyStopRecord):
            raise ToolTerminalStoreError(
                "record must be a DurableResponsePolicyStopRecord"
            )
        if not isinstance(self.replayed, bool):
            raise ToolTerminalStoreError("replayed must be a boolean")
        object.__setattr__(self, "sequence", sequence)


@dataclass(slots=True)
class InMemoryDurableToolTerminalStore:
    next_sequence: int = 1
    terminal_records: dict[tuple[str, str, int], DurableToolTerminalCommit] = field(default_factory=dict)
    policy_stopped_responses: dict[str, DurableResponsePolicyStopCommit] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.terminal_records, Mapping):
            raise ToolTerminalStoreError("terminal_records must be a mapping")
        if not isinstance(self.policy_stopped_responses, Mapping):
            raise ToolTerminalStoreError(
                "policy_stopped_responses must be a mapping"
            )
        terminal_records = dict(self.terminal_records)
        policy_stopped_responses = dict(self.policy_stopped_responses)
        seen_sequences: set[int] = set()
        highest_sequence = 0
        for key, commit in terminal_records.items():
            if not isinstance(commit, DurableToolTerminalCommit):
                raise ToolTerminalStoreError(
                    "terminal_records values must be DurableToolTerminalCommit"
                )
            expected_key = (
                commit.record.response_id,
                commit.record.tool_call_id,
                commit.record.revision,
            )
            if key != expected_key:
                raise ToolTerminalStoreError("terminal_records key does not match record")
            if commit.replayed:
                raise ToolTerminalStoreError(
                    "restored terminal records must not be replay projections"
                )
            sequence = _require_tool_terminal_integer("sequence", commit.sequence)
            if sequence <= 0 or sequence in seen_sequences:
                raise ToolTerminalStoreError("restored terminal sequence must be unique and positive")
            seen_sequences.add(sequence)
            highest_sequence = max(highest_sequence, sequence)
        for response_id, commit in policy_stopped_responses.items():
            if not isinstance(commit, DurableResponsePolicyStopCommit):
                raise ToolTerminalStoreError(
                    "policy_stopped_responses values must be DurableResponsePolicyStopCommit"
                )
            if response_id != commit.record.response_id:
                raise ToolTerminalStoreError(
                    "policy_stopped_responses key does not match record"
                )
            if commit.replayed:
                raise ToolTerminalStoreError(
                    "restored policy stops must not be replay projections"
                )
            sequence = _require_tool_terminal_integer("sequence", commit.sequence)
            if sequence <= 0 or sequence in seen_sequences:
                raise ToolTerminalStoreError("restored terminal sequence must be unique and positive")
            seen_sequences.add(sequence)
            highest_sequence = max(highest_sequence, sequence)
        committed_result_response_ids = {
            commit.record.response_id
            for commit in terminal_records.values()
            if commit.record.durable_result_committed
        }
        conflicting_response_ids = (
            committed_result_response_ids & policy_stopped_responses.keys()
        )
        if conflicting_response_ids:
            response_id = min(conflicting_response_ids)
            raise ToolTerminalStoreError(
                "restored durable result cannot coexist with a policy-stopped "
                f"response {response_id!r}"
            )
        next_sequence = _require_tool_terminal_integer("next_sequence", self.next_sequence)
        if next_sequence <= 0:
            raise ToolTerminalStoreError("next_sequence must be positive")
        self.terminal_records = terminal_records
        self.policy_stopped_responses = policy_stopped_responses
        self.next_sequence = max(next_sequence, highest_sequence + 1)

    def record_tool_terminal(self, record: DurableToolTerminalRecord) -> DurableToolTerminalCommit:
        if not isinstance(record, DurableToolTerminalRecord):
            raise ToolTerminalStoreError("record must be a DurableToolTerminalRecord")
        with self._lock:
            key = (record.response_id, record.tool_call_id, record.revision)
            existing = self.terminal_records.get(key)
            if existing is not None:
                if existing.record != record:
                    raise ToolTerminalStateConflictError(record.response_id, record.tool_call_id, record.revision)
                return DurableToolTerminalCommit(
                    sequence=existing.sequence,
                    record=existing.record,
                    replayed=True,
                )

            if record.durable_result_committed and record.response_id in self.policy_stopped_responses:
                raise ResponsePolicyStoppedError(record.response_id)

            committed = DurableToolTerminalCommit(
                sequence=self._allocate_sequence(),
                record=record,
                replayed=False,
            )
            self.terminal_records[key] = committed
            return committed

    def tool_terminal_count(self) -> int:
        with self._lock:
            return len(self.terminal_records)

    def record_response_policy_stopped(
        self,
        response_id: str,
        policy_decision_id: str,
        *,
        stream_id: str | None = None,
        turn_id: str | None = None,
        last_generated_sequence: int | None = None,
        last_policy_accepted_sequence: int,
        last_client_delivered_sequence: int | None = None,
        terminal_reason: OutputCutoffTerminalReason = "policy_denied",
        draft_disposition: OutputCutoffDraftDisposition = "retract",
        durable_result: OutputCutoffDurableResult = "none",
        occurred_at_unix_ms: int,
    ) -> DurableResponsePolicyStopCommit:
        last_generated_sequence = (
            last_policy_accepted_sequence if last_generated_sequence is None else last_generated_sequence
        )
        record = DurableResponsePolicyStopRecord(
            response_id=response_id,
            policy_decision_id=policy_decision_id,
            last_policy_accepted_sequence=last_policy_accepted_sequence,
            occurred_at_unix_ms=occurred_at_unix_ms,
            stream_id=stream_id,
            last_generated_sequence=last_generated_sequence,
            last_client_delivered_sequence=(
                last_policy_accepted_sequence
                if last_client_delivered_sequence is None
                else last_client_delivered_sequence
            ),
            terminal_reason=terminal_reason,
            draft_disposition=draft_disposition,
            durable_result=durable_result,
            turn_id=turn_id,
        )
        with self._lock:
            existing = self.policy_stopped_responses.get(record.response_id)
            if existing is not None:
                if existing.record != record:
                    raise ResponsePolicyStopConflictError(record.response_id)
                return DurableResponsePolicyStopCommit(
                    sequence=existing.sequence,
                    record=existing.record,
                    replayed=True,
                )
            if any(
                commit.record.response_id == record.response_id and commit.record.durable_result_committed
                for commit in self.terminal_records.values()
            ):
                raise DurableResultAlreadyCommittedError(record.response_id)

            committed = DurableResponsePolicyStopCommit(
                sequence=self._allocate_sequence(),
                record=record,
                replayed=False,
            )
            self.policy_stopped_responses[record.response_id] = committed
            return committed

    def _allocate_sequence(self) -> int:
        highest_sequence = max(
            (
                commit.sequence
                for commit in (
                    *self.terminal_records.values(),
                    *self.policy_stopped_responses.values(),
                )
            ),
            default=0,
        )
        self.next_sequence = max(self.next_sequence, highest_sequence + 1)
        sequence = self.next_sequence
        self.next_sequence += 1
        return sequence


@dataclass(frozen=True, slots=True)
class SchemaRef:
    schema_id: str
    schema_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.schema_id, str):
            raise DurableError("schema_id must be a string")
        schema_version = _require_integer("schema_version", self.schema_version)
        object.__setattr__(self, "schema_version", schema_version)


@dataclass(frozen=True, slots=True)
class SourceCursorCommitPlan:
    cursors: tuple[tuple[str, SourceCursor], ...]

    def __post_init__(self) -> None:
        if isinstance(self.cursors, (str, bytes, bytearray)):
            raise DurableError("source cursor commit plan cursors must be a sequence")
        try:
            cursors = tuple(self.cursors)
        except TypeError:
            raise DurableError(
                "source cursor commit plan cursors must be a sequence"
            ) from None
        source_ids: set[str] = set()
        for item in cursors:
            if not isinstance(item, tuple) or len(item) != 2:
                raise DurableError("source cursor commit plan entries must be pairs")
            source_id, cursor = item
            if not isinstance(source_id, str) or not source_id.strip():
                raise DurableError(
                    "source cursor commit plan source ids must be non-empty strings"
                )
            if source_id != source_id.strip():
                raise DurableError(
                    "source cursor commit plan source ids must not contain surrounding whitespace"
                )
            if source_id in source_ids:
                raise DurableError(
                    "source cursor commit plan source ids must be unique"
                )
            if not isinstance(cursor, SourceCursor):
                raise DurableError(
                    "source cursor commit plan cursors must be SourceCursor"
                )
            source_ids.add(source_id)
        object.__setattr__(
            self,
            "cursors",
            tuple(sorted(cursors, key=lambda item: item[0])),
        )


def _copy_checkpoint_mapping(
    field_name: str,
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DurableError(f"{field_name} must be a mapping")
    try:
        snapshot = canonical_loads(canonical_dumps(value))
    except (TypeError, ValueError) as error:
        raise DurableError(f"{field_name} must contain strict JSON values") from error
    if not isinstance(snapshot, dict):
        raise DurableError(f"{field_name} must be a mapping")
    copied: dict[str, object] = {}
    for key, item in snapshot.items():
        if not isinstance(key, str) or not key.strip():
            raise DurableError(f"{field_name} keys must be non-empty strings")
        if key != key.strip():
            raise DurableError(
                f"{field_name} keys must not contain surrounding whitespace"
            )
        copied[key] = _freeze_checkpoint_value(item)
    return dict(sorted(copied.items()))


def _freeze_checkpoint_value(value: object) -> object:
    if isinstance(value, dict):
        return FrozenDict(
            {
                key: _freeze_checkpoint_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return FrozenList(_freeze_checkpoint_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class CheckpointBarrier:
    checkpoint_id: str
    run_id: str
    release_id: str
    deployment_revision_id: str
    plan_hash: str
    checkpoint_schema: SchemaRef
    state_revision: int
    completed_nodes: tuple[str, ...] = field(default_factory=tuple)
    pending_nodes: tuple[str, ...] = field(default_factory=tuple)
    source_cursors: Mapping[str, SourceCursor] = field(default_factory=dict)
    operator_state: Mapping[str, object] = field(default_factory=dict)
    sink_commit_metadata: Mapping[str, object] = field(default_factory=dict)
    schema_versions: Mapping[str, int] = field(default_factory=dict)
    created_at_unix_ms: int = 0

    def __post_init__(self) -> None:
        state_revision = _require_integer("state_revision", self.state_revision)
        if state_revision < 0:
            raise DurableError("state_revision must be non-negative")
        created_at_unix_ms = _require_integer("created_at_unix_ms", self.created_at_unix_ms)
        if created_at_unix_ms < 0:
            raise DurableError("created_at_unix_ms must be non-negative")
        if not isinstance(self.schema_versions, Mapping):
            raise DurableError("schema_versions must be a mapping")
        schema_versions: dict[str, int] = {}
        for key, value in self.schema_versions.items():
            if not isinstance(key, str) or not key.strip():
                raise DurableError("schema_versions keys must be non-empty strings")
            if key != key.strip():
                raise DurableError(
                    "schema_versions keys must not contain surrounding whitespace"
                )
            schema_key = key
            schema_version = _require_integer(f"schema_versions {schema_key}", value)
            if schema_version < 0:
                raise DurableError(f"schema_versions {schema_key} must be non-negative")
            schema_versions[schema_key] = schema_version
        object.__setattr__(self, "state_revision", state_revision)
        object.__setattr__(self, "created_at_unix_ms", created_at_unix_ms)
        if isinstance(self.completed_nodes, (str, bytes, bytearray)):
            raise DurableError("completed_nodes must be a sequence")
        if isinstance(self.pending_nodes, (str, bytes, bytearray)):
            raise DurableError("pending_nodes must be a sequence")
        try:
            completed_nodes = tuple(self.completed_nodes)
            pending_nodes = tuple(self.pending_nodes)
        except TypeError:
            raise DurableError(
                "completed_nodes and pending_nodes must be sequences"
            ) from None
        if any(
            not isinstance(node, str)
            or not node.strip()
            or node != node.strip()
            for node in completed_nodes
        ):
            raise DurableError("completed_nodes must contain non-empty strings")
        if any(
            not isinstance(node, str)
            or not node.strip()
            or node != node.strip()
            for node in pending_nodes
        ):
            raise DurableError("pending_nodes must contain non-empty strings")
        if len(set(completed_nodes)) != len(completed_nodes):
            raise DurableError("completed_nodes must not contain duplicates")
        if len(set(pending_nodes)) != len(pending_nodes):
            raise DurableError("pending_nodes must not contain duplicates")
        if set(completed_nodes) & set(pending_nodes):
            raise DurableError("completed_nodes and pending_nodes must not overlap")
        object.__setattr__(self, "completed_nodes", completed_nodes)
        object.__setattr__(self, "pending_nodes", pending_nodes)
        if not isinstance(self.source_cursors, Mapping):
            raise DurableError("source_cursors must be a mapping")
        source_cursors: dict[str, SourceCursor] = {}
        for source_id, cursor in self.source_cursors.items():
            if not isinstance(source_id, str) or not source_id.strip():
                raise DurableError("source_cursors keys must be non-empty strings")
            if source_id != source_id.strip():
                raise DurableError(
                    "source_cursors keys must not contain surrounding whitespace"
                )
            if not isinstance(cursor, SourceCursor):
                raise DurableError("source_cursors values must be SourceCursor")
            source_cursors[source_id] = cursor
        object.__setattr__(
            self,
            "source_cursors",
            MappingProxyType(dict(sorted(source_cursors.items()))),
        )
        object.__setattr__(
            self,
            "operator_state",
            MappingProxyType(
                _copy_checkpoint_mapping("operator_state", self.operator_state)
            ),
        )
        object.__setattr__(
            self,
            "sink_commit_metadata",
            MappingProxyType(
                _copy_checkpoint_mapping(
                    "sink_commit_metadata",
                    self.sink_commit_metadata,
                )
            ),
        )
        object.__setattr__(
            self,
            "schema_versions",
            MappingProxyType(dict(sorted(schema_versions.items()))),
        )

    def with_source_cursor(self, source_id: str, cursor: SourceCursor) -> CheckpointBarrier:
        if not isinstance(source_id, str) or not source_id.strip():
            raise DurableError("source_id must not be empty")
        if not isinstance(cursor, SourceCursor):
            raise DurableError("cursor must be a SourceCursor")
        source_cursors = dict(self.source_cursors)
        source_cursors[source_id] = cursor
        return replace(self, source_cursors=source_cursors)

    def validate(self) -> CheckpointBarrier:
        if (
            not isinstance(self.checkpoint_id, str)
            or not self.checkpoint_id.strip()
            or self.checkpoint_id != self.checkpoint_id.strip()
        ):
            raise CheckpointBarrierError("missing_checkpoint_id")
        if (
            not isinstance(self.run_id, str)
            or not self.run_id.strip()
            or self.run_id != self.run_id.strip()
        ):
            raise CheckpointBarrierError("missing_run_id")
        if (
            not isinstance(self.release_id, str)
            or not self.release_id.strip()
            or self.release_id != self.release_id.strip()
        ):
            raise CheckpointBarrierError("missing_release_id")
        if (
            not isinstance(self.deployment_revision_id, str)
            or not self.deployment_revision_id.strip()
            or self.deployment_revision_id != self.deployment_revision_id.strip()
        ):
            raise CheckpointBarrierError("missing_deployment_revision_id")
        if (
            not isinstance(self.plan_hash, str)
            or not self.plan_hash.strip()
            or self.plan_hash != self.plan_hash.strip()
        ):
            raise CheckpointBarrierError("missing_plan_hash")
        if not isinstance(self.checkpoint_schema, SchemaRef):
            raise CheckpointBarrierError("invalid_checkpoint_schema")
        if (
            not self.checkpoint_schema.schema_id.strip()
            or self.checkpoint_schema.schema_id
            != self.checkpoint_schema.schema_id.strip()
            or self.checkpoint_schema.schema_version <= 0
        ):
            raise CheckpointBarrierError("invalid_checkpoint_schema")
        if not self.schema_versions:
            raise CheckpointBarrierError("missing_schema_versions")
        return self

    def source_commit_plan(self) -> SourceCursorCommitPlan:
        return SourceCursorCommitPlan(tuple(self.source_cursors.items()))


def _copy_checkpoint_barrier(barrier: CheckpointBarrier) -> CheckpointBarrier:
    return CheckpointBarrier(
        checkpoint_id=barrier.checkpoint_id,
        run_id=barrier.run_id,
        release_id=barrier.release_id,
        deployment_revision_id=barrier.deployment_revision_id,
        plan_hash=barrier.plan_hash,
        checkpoint_schema=SchemaRef(barrier.checkpoint_schema.schema_id, barrier.checkpoint_schema.schema_version),
        state_revision=barrier.state_revision,
        completed_nodes=tuple(barrier.completed_nodes),
        pending_nodes=tuple(barrier.pending_nodes),
        source_cursors=dict(barrier.source_cursors),
        operator_state=deepcopy(dict(barrier.operator_state)),
        sink_commit_metadata=deepcopy(dict(barrier.sink_commit_metadata)),
        schema_versions=dict(barrier.schema_versions),
        created_at_unix_ms=barrier.created_at_unix_ms,
    )


@dataclass(slots=True)
class InMemoryCheckpointStore:
    _checkpoints_by_run: dict[str, list[CheckpointBarrier]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self._checkpoints_by_run, Mapping):
            raise CheckpointStoreError("restored checkpoints must be a mapping")
        restored_items = tuple(self._checkpoints_by_run.items())
        restored: dict[str, list[CheckpointBarrier]] = {}
        for run_id, checkpoints_value in restored_items:
            if not isinstance(run_id, str) or not run_id.strip():
                raise CheckpointStoreError(
                    "restored checkpoint run ids must be non-empty strings"
                )
            if run_id != run_id.strip():
                raise CheckpointStoreError(
                    "restored checkpoint run ids must not contain surrounding whitespace"
                )
            if isinstance(checkpoints_value, (str, bytes, bytearray)):
                raise CheckpointStoreError(
                    "restored checkpoints must be sequences"
                )
            try:
                checkpoints = tuple(checkpoints_value)
            except TypeError:
                raise CheckpointStoreError(
                    "restored checkpoints must be sequences"
                ) from None
            copied: list[CheckpointBarrier] = []
            previous_revision: int | None = None
            for checkpoint in checkpoints:
                if not isinstance(checkpoint, CheckpointBarrier):
                    raise CheckpointStoreError(
                        "restored checkpoints must be CheckpointBarrier"
                    )
                checkpoint.validate()
                if checkpoint.run_id != run_id:
                    raise CheckpointStoreError(
                        "restored checkpoint run id must match mapping key"
                    )
                if (
                    previous_revision is not None
                    and checkpoint.state_revision <= previous_revision
                ):
                    raise CheckpointStoreError(
                        "restored checkpoint revisions must be strictly increasing"
                    )
                copied.append(_copy_checkpoint_barrier(checkpoint))
                previous_revision = checkpoint.state_revision
            restored[run_id] = copied
        self._checkpoints_by_run = restored

    def put(self, barrier: CheckpointBarrier) -> InMemoryCheckpointStore:
        if not isinstance(barrier, CheckpointBarrier):
            raise CheckpointStoreError("barrier must be a CheckpointBarrier")
        barrier.validate()
        checkpoints = self._checkpoints_by_run.setdefault(barrier.run_id, [])
        current = max((checkpoint.state_revision for checkpoint in checkpoints), default=None)
        if current is not None and barrier.state_revision <= current:
            raise StaleCheckpointError(barrier.run_id, current, barrier.state_revision)
        checkpoints.append(_copy_checkpoint_barrier(barrier))
        return self

    def latest_compatible(
        self,
        *,
        run_id: str,
        release_id: str,
        deployment_revision_id: str,
        plan_hash: str,
    ) -> CheckpointBarrier | None:
        compatible = [
            checkpoint
            for checkpoint in self._checkpoints_by_run.get(run_id, [])
            if checkpoint.release_id == release_id
            and checkpoint.deployment_revision_id == deployment_revision_id
            and checkpoint.plan_hash == plan_hash
        ]
        if not compatible:
            return None
        return _copy_checkpoint_barrier(max(compatible, key=lambda checkpoint: checkpoint.state_revision))


def evaluate_native_durable_tool_terminal_store(operations: object) -> dict[str, object]:
    from graphblocks_runtime import evaluate_durable_tool_terminal_store

    return evaluate_durable_tool_terminal_store(operations)


__all__ = [
    "AccumulationMode",
    "CheckpointBarrier",
    "CheckpointBarrierError",
    "CheckpointStoreError",
    "ConflictingSourceOffsetError",
    "DeliveryGuarantee",
    "DemandExceededError",
    "DurableResponsePolicyStopCommit",
    "DurableResponsePolicyStopRecord",
    "DurableResultAlreadyCommittedError",
    "DurableToolTerminalCommit",
    "DurableToolTerminalRecord",
    "DurableToolTerminalState",
    "DurableError",
    "IdempotencyConflictError",
    "InMemoryCheckpointStore",
    "InMemoryDurableSink",
    "InMemoryDurableSource",
    "InMemoryDurableToolTerminalStore",
    "InvalidDemandError",
    "InvalidWindowSizeError",
    "LateEventError",
    "MissingEventTimeError",
    "MissingIdempotencyKeyError",
    "MissingNodeAttemptIdError",
    "MissingNodeIdError",
    "MissingRunIdError",
    "OutputCutoffDraftDisposition",
    "OutputCutoffDurableResult",
    "OutputCutoffTerminalReason",
    "SinkCommitError",
    "SinkCommitRequest",
    "SinkCommitResult",
    "SchemaRef",
    "SourceBatch",
    "SourceCursor",
    "SourceCursorCommitPlan",
    "SourceEvent",
    "SourcePausedError",
    "StaleCommitError",
    "StaleCheckpointError",
    "ResponsePolicyStopConflictError",
    "ResponsePolicyStoppedError",
    "ToolTerminalStateConflictError",
    "ToolTerminalStoreError",
    "UnknownSourceCursorError",
    "VALID_DELIVERY_GUARANTEES",
    "VALID_DURABLE_TOOL_TERMINAL_STATES",
    "VALID_OUTPUT_CUTOFF_DRAFT_DISPOSITIONS",
    "VALID_OUTPUT_CUTOFF_DURABLE_RESULTS",
    "VALID_OUTPUT_CUTOFF_TERMINAL_REASONS",
    "Watermark",
    "WatermarkKind",
    "WindowAccumulator",
    "WindowPane",
    "WindowPolicy",
    "evaluate_native_durable_tool_terminal_store",
]
