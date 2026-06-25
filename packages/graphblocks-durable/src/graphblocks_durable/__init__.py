from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Literal


DeliveryGuarantee = Literal["best_effort", "at_most_once", "at_least_once"]
WatermarkKind = Literal["event_time", "processing_time"]
AccumulationMode = Literal["discarding", "accumulating"]


class DurableError(ValueError):
    """Base error for durable stream contracts."""


class InvalidDemandError(DurableError):
    pass


class SourcePausedError(DurableError):
    pass


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


@dataclass(frozen=True, slots=True, order=True)
class SourceCursor:
    stream: str
    partition: int
    offset: int

    def __post_init__(self) -> None:
        if not self.stream.strip():
            raise DurableError("stream must not be empty")
        if self.partition < 0:
            raise DurableError("partition must be non-negative")
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


@dataclass(frozen=True, slots=True)
class SourceBatch:
    guarantee: DeliveryGuarantee
    events: tuple[SourceEvent, ...]
    watermark: Watermark | None = None

    @classmethod
    def new(
        cls,
        guarantee: DeliveryGuarantee,
        events: list[SourceEvent] | tuple[SourceEvent, ...],
        watermark: Watermark | None,
        demand: int,
    ) -> SourceBatch:
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

    def __post_init__(self) -> None:
        self.events = sorted(self.events, key=lambda event: event.cursor)

    def poll(self, cursor: SourceCursor | None, *, demand: int) -> SourceBatch:
        if self.paused:
            raise SourcePausedError("source is paused")
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
        if self.committed_cursor is not None and cursor < self.committed_cursor:
            raise StaleCommitError(self.committed_cursor, cursor)
        self.committed_cursor = cursor

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


@dataclass(frozen=True, slots=True)
class WindowPolicy:
    size_ms: int
    allowed_lateness_ms: int
    accumulation_mode: AccumulationMode

    def __post_init__(self) -> None:
        if self.size_ms <= 0:
            raise InvalidWindowSizeError("window size_ms must be positive")
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
    "DurableError",
    "IdempotencyConflictError",
    "InMemoryCheckpointStore",
    "InMemoryDurableSink",
    "InMemoryDurableSource",
    "InvalidDemandError",
    "InvalidWindowSizeError",
    "LateEventError",
    "MissingIdempotencyKeyError",
    "MissingNodeAttemptIdError",
    "MissingNodeIdError",
    "MissingRunIdError",
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
    "Watermark",
    "WatermarkKind",
    "WindowAccumulator",
    "WindowPane",
    "WindowPolicy",
]
