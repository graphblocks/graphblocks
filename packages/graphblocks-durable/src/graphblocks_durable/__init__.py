from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Literal


DeliveryGuarantee = Literal["best_effort", "at_most_once", "at_least_once"]
WatermarkKind = Literal["event_time", "processing_time"]
AccumulationMode = Literal["discarding", "accumulating"]
OutputCutoffTerminalReason = Literal["policy_denied", "budget_exhausted", "cancelled", "client_disconnected"]
OutputCutoffDraftDisposition = Literal["keep", "mark_incomplete", "retract"]
OutputCutoffDurableResult = Literal["none", "incomplete", "partial"]
DurableToolTerminalState = Literal[
    "completed",
    "failed",
    "denied",
    "cancelled",
    "policy_stopped",
    "incomplete",
    "expired",
]

VALID_DELIVERY_GUARANTEES = frozenset({"best_effort", "at_most_once", "at_least_once"})
VALID_OUTPUT_CUTOFF_TERMINAL_REASONS = frozenset(
    {"policy_denied", "budget_exhausted", "cancelled", "client_disconnected"}
)
VALID_OUTPUT_CUTOFF_DRAFT_DISPOSITIONS = frozenset({"keep", "mark_incomplete", "retract"})
VALID_OUTPUT_CUTOFF_DURABLE_RESULTS = frozenset({"none", "incomplete", "partial"})
VALID_DURABLE_TOOL_TERMINAL_STATES = frozenset(
    {
        "completed",
        "failed",
        "denied",
        "cancelled",
        "policy_stopped",
        "incomplete",
        "expired",
    }
)


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


class InvalidWindowSizeError(DurableError):
    pass


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
    if value not in VALID_DELIVERY_GUARANTEES:
        raise DurableError(f"unsupported delivery guarantee {value!r}")


@dataclass(frozen=True, slots=True, order=True)
class SourceCursor:
    stream: str
    partition: int
    offset: int

    def __post_init__(self) -> None:
        if not self.stream.strip():
            raise DurableError("stream must not be empty")
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
        if self.kind not in {"event_time", "processing_time"}:
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
    events: list[SourceEvent]
    committed_cursor: SourceCursor | None = None
    paused: bool = False
    _known_streams: frozenset[str] = field(init=False, repr=False)
    _known_partitions: frozenset[tuple[str, int]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _require_delivery_guarantee(self.guarantee)
        self.events = sorted(self.events, key=lambda event: event.cursor)
        self._known_streams = frozenset(event.cursor.stream for event in self.events)
        self._known_partitions = frozenset(
            (event.cursor.stream, event.cursor.partition) for event in self.events
        )
        if self.committed_cursor is not None:
            self._validate_cursor(self.committed_cursor)

    def poll(self, cursor: SourceCursor | None, *, demand: int) -> SourceBatch:
        if self.paused:
            raise SourcePausedError("source is paused")
        if cursor is not None:
            self._validate_cursor(cursor)
        replay_cursor = cursor if cursor is not None else self.committed_cursor
        events = [
            event
            for event in self.events
            if replay_cursor is None or event.cursor > replay_cursor
        ][:demand]
        event_times = [event.event_time_unix_ms for event in events if event.event_time_unix_ms is not None]
        watermark = Watermark.event_time(max(event_times)) if event_times else None
        return SourceBatch.new(self.guarantee, tuple(events), watermark, demand)

    def commit(self, cursor: SourceCursor) -> None:
        self._validate_cursor(cursor)
        if self.committed_cursor is not None and cursor < self.committed_cursor:
            raise StaleCommitError(self.committed_cursor, cursor)
        self.committed_cursor = cursor

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def _validate_cursor(self, cursor: SourceCursor) -> None:
        if self._known_streams and cursor.stream not in self._known_streams:
            raise UnknownSourceCursorError(cursor)
        if self._known_partitions and (cursor.stream, cursor.partition) not in self._known_partitions:
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
        if self.accumulation_mode not in {"discarding", "accumulating"}:
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


@dataclass(slots=True)
class WindowAccumulator:
    policy: WindowPolicy
    watermark: Watermark | None = None
    windows: dict[int, list[SourceEvent]] = field(default_factory=dict)

    def ingest(self, event: SourceEvent) -> None:
        event_time_unix_ms = event.event_time_unix_ms or 0
        if (
            self.watermark is not None
            and event_time_unix_ms + self.policy.allowed_lateness_ms < self.watermark.unix_ms
        ):
            raise LateEventError(
                event_time_unix_ms,
                self.watermark.unix_ms,
                self.policy.allowed_lateness_ms,
            )
        start_unix_ms = event_time_unix_ms - (event_time_unix_ms % self.policy.size_ms)
        self.windows.setdefault(start_unix_ms, []).append(event)

    def advance_watermark(self, watermark: Watermark) -> list[WindowPane]:
        self.watermark = watermark
        closable = [
            start_unix_ms
            for start_unix_ms in sorted(self.windows)
            if start_unix_ms + self.policy.size_ms + self.policy.allowed_lateness_ms <= watermark.unix_ms
        ]
        closed: list[WindowPane] = []
        for start_unix_ms in closable:
            events = tuple(sorted(self.windows.pop(start_unix_ms), key=lambda event: event.cursor))
            closed.append(
                WindowPane(
                    start_unix_ms=start_unix_ms,
                    end_unix_ms=start_unix_ms + self.policy.size_ms,
                    events=events,
                )
            )
        return closed


@dataclass(frozen=True, slots=True)
class SinkCommitRequest:
    run_id: str
    node_id: str
    node_attempt_id: str
    idempotency_key: str
    payload: object
    precondition_digest: str | None = None

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


@dataclass(slots=True)
class InMemoryDurableSink:
    sink_id: str
    next_sequence: int = 1
    commits_by_idempotency_key: dict[str, tuple[SinkCommitRequest, SinkCommitResult]] = field(default_factory=dict)

    def commit(self, request: SinkCommitRequest) -> SinkCommitResult:
        if not request.run_id.strip():
            raise MissingRunIdError("run_id must not be empty")
        if not request.node_id.strip():
            raise MissingNodeIdError("node_id must not be empty")
        if not request.node_attempt_id.strip():
            raise MissingNodeAttemptIdError("node_attempt_id must not be empty")
        if not request.idempotency_key.strip():
            raise MissingIdempotencyKeyError("idempotency_key must not be empty")
        if request.idempotency_key in self.commits_by_idempotency_key:
            existing_request, existing_result = self.commits_by_idempotency_key[request.idempotency_key]
            if existing_request != request:
                raise IdempotencyConflictError(request.idempotency_key)
            return SinkCommitResult(
                sink_id=existing_result.sink_id,
                idempotency_key=existing_result.idempotency_key,
                precondition_digest=existing_result.precondition_digest,
                sequence=existing_result.sequence,
                metadata=existing_result.metadata,
                replayed=True,
            )
        result = SinkCommitResult(
            sink_id=self.sink_id,
            idempotency_key=request.idempotency_key,
            precondition_digest=request.precondition_digest,
            sequence=self.next_sequence,
            metadata=request.payload,
            replayed=False,
        )
        self.next_sequence += 1
        self.commits_by_idempotency_key[request.idempotency_key] = (request, result)
        return result

    def committed_count(self) -> int:
        return len(self.commits_by_idempotency_key)


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

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ToolTerminalStoreError("run_id must not be empty")
        if not self.response_id.strip():
            raise ToolTerminalStoreError("response_id must not be empty")
        if not self.tool_call_id.strip():
            raise ToolTerminalStoreError("tool_call_id must not be empty")
        if self.revision <= 0:
            raise ToolTerminalStoreError("revision must be positive")
        if self.terminal_state not in VALID_DURABLE_TOOL_TERMINAL_STATES:
            raise ToolTerminalStoreError(f"invalid terminal_state {self.terminal_state}")
        if not self.arguments_digest.strip():
            raise ToolTerminalStoreError("arguments_digest must not be empty")
        if self.completed_at_unix_ms <= 0:
            raise ToolTerminalStoreError("completed_at_unix_ms must be positive")
        if self.output_digest is not None and not self.output_digest.strip():
            raise ToolTerminalStoreError("output_digest must not be empty")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ToolTerminalStoreError("idempotency_key must not be empty")


@dataclass(frozen=True, slots=True)
class DurableToolTerminalCommit:
    sequence: int
    record: DurableToolTerminalRecord
    replayed: bool


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
        if not self.response_id.strip():
            raise ToolTerminalStoreError("response_id must not be empty")
        if not self.policy_decision_id.strip():
            raise ToolTerminalStoreError("policy_decision_id must not be empty")
        if self.stream_id is None:
            object.__setattr__(self, "stream_id", self.response_id)
        if not self.stream_id.strip():
            raise ToolTerminalStoreError("stream_id must not be empty")
        if self.turn_id is not None and not self.turn_id.strip():
            raise ToolTerminalStoreError("turn_id must not be empty")
        if self.last_generated_sequence < 0:
            raise ToolTerminalStoreError("last_generated_sequence must be non-negative")
        if self.last_policy_accepted_sequence < 0:
            raise ToolTerminalStoreError("last_policy_accepted_sequence must be non-negative")
        if self.last_client_delivered_sequence < 0:
            raise ToolTerminalStoreError("last_client_delivered_sequence must be non-negative")
        if self.last_policy_accepted_sequence > self.last_generated_sequence:
            raise ToolTerminalStoreError("last_policy_accepted_sequence cannot exceed last_generated_sequence")
        if self.last_client_delivered_sequence > self.last_generated_sequence:
            raise ToolTerminalStoreError("last_client_delivered_sequence cannot exceed last_generated_sequence")
        if self.terminal_reason not in VALID_OUTPUT_CUTOFF_TERMINAL_REASONS:
            raise ToolTerminalStoreError(f"invalid terminal_reason {self.terminal_reason}")
        if self.draft_disposition not in VALID_OUTPUT_CUTOFF_DRAFT_DISPOSITIONS:
            raise ToolTerminalStoreError(f"invalid draft_disposition {self.draft_disposition}")
        if self.durable_result not in VALID_OUTPUT_CUTOFF_DURABLE_RESULTS:
            raise ToolTerminalStoreError(f"invalid durable_result {self.durable_result}")
        if self.occurred_at_unix_ms <= 0:
            raise ToolTerminalStoreError("occurred_at_unix_ms must be positive")


@dataclass(frozen=True, slots=True)
class DurableResponsePolicyStopCommit:
    sequence: int
    record: DurableResponsePolicyStopRecord
    replayed: bool


@dataclass(slots=True)
class InMemoryDurableToolTerminalStore:
    next_sequence: int = 1
    terminal_records: dict[tuple[str, str, int], DurableToolTerminalCommit] = field(default_factory=dict)
    policy_stopped_responses: dict[str, DurableResponsePolicyStopCommit] = field(default_factory=dict)

    def record_tool_terminal(self, record: DurableToolTerminalRecord) -> DurableToolTerminalCommit:
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
            sequence=self.next_sequence,
            record=record,
            replayed=False,
        )
        self.next_sequence += 1
        self.terminal_records[key] = committed
        return committed

    def tool_terminal_count(self) -> int:
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
            sequence=self.next_sequence,
            record=record,
            replayed=False,
        )
        self.next_sequence += 1
        self.policy_stopped_responses[record.response_id] = committed
        return committed


@dataclass(frozen=True, slots=True)
class SchemaRef:
    schema_id: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class SourceCursorCommitPlan:
    cursors: tuple[tuple[str, SourceCursor], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cursors", tuple(sorted(self.cursors, key=lambda item: item[0])))


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
        if self.state_revision < 0:
            raise DurableError("state_revision must be non-negative")
        if self.created_at_unix_ms < 0:
            raise DurableError("created_at_unix_ms must be non-negative")
        object.__setattr__(self, "completed_nodes", tuple(str(node) for node in self.completed_nodes))
        object.__setattr__(self, "pending_nodes", tuple(str(node) for node in self.pending_nodes))
        object.__setattr__(
            self,
            "source_cursors",
            {str(source_id): cursor for source_id, cursor in sorted(dict(self.source_cursors).items())},
        )
        object.__setattr__(
            self,
            "operator_state",
            {str(key): deepcopy(value) for key, value in sorted(dict(self.operator_state).items())},
        )
        object.__setattr__(
            self,
            "sink_commit_metadata",
            {str(key): deepcopy(value) for key, value in sorted(dict(self.sink_commit_metadata).items())},
        )
        object.__setattr__(
            self,
            "schema_versions",
            {str(key): int(value) for key, value in sorted(dict(self.schema_versions).items())},
        )

    def with_source_cursor(self, source_id: str, cursor: SourceCursor) -> CheckpointBarrier:
        if not source_id.strip():
            raise DurableError("source_id must not be empty")
        source_cursors = dict(self.source_cursors)
        source_cursors[source_id] = cursor
        return replace(self, source_cursors=source_cursors)

    def validate(self) -> CheckpointBarrier:
        if not self.checkpoint_id.strip():
            raise CheckpointBarrierError("missing_checkpoint_id")
        if not self.run_id.strip():
            raise CheckpointBarrierError("missing_run_id")
        if not self.release_id.strip():
            raise CheckpointBarrierError("missing_release_id")
        if not self.deployment_revision_id.strip():
            raise CheckpointBarrierError("missing_deployment_revision_id")
        if not self.plan_hash.strip():
            raise CheckpointBarrierError("missing_plan_hash")
        if not self.checkpoint_schema.schema_id.strip() or self.checkpoint_schema.schema_version <= 0:
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

    def put(self, barrier: CheckpointBarrier) -> InMemoryCheckpointStore:
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


__all__ = [
    "AccumulationMode",
    "CheckpointBarrier",
    "CheckpointBarrierError",
    "CheckpointStoreError",
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
    "Watermark",
    "WatermarkKind",
    "WindowAccumulator",
    "WindowPane",
    "WindowPolicy",
]
