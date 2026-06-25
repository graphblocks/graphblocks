from __future__ import annotations

from dataclasses import dataclass, field

from graphblocks_durable import SinkCommitRequest, SourceCursor, SourceEvent


class NatsAdapterError(ValueError):
    """Base error for NATS durable adapter contracts."""


def _validate_stream(stream: str) -> None:
    if not stream.strip():
        raise NatsAdapterError("stream must not be empty")


def _validate_subject(subject: str) -> None:
    if not subject.strip():
        raise NatsAdapterError("subject must not be empty")


@dataclass(frozen=True, slots=True)
class NatsMessage:
    stream: str
    subject: str
    sequence: int
    payload: object
    timestamp_unix_ms: int | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_stream(self.stream)
        _validate_subject(self.subject)
        if self.sequence <= 0:
            raise NatsAdapterError("sequence must be positive")
        if self.timestamp_unix_ms is not None and self.timestamp_unix_ms < 0:
            raise NatsAdapterError("timestamp_unix_ms must be non-negative")
        object.__setattr__(self, "headers", dict(sorted(self.headers.items())))

    def to_source_event(self) -> SourceEvent:
        return SourceEvent(
            SourceCursor(self.stream, 0, self.sequence),
            {"subject": self.subject, "payload": self.payload, "headers": dict(self.headers)},
            event_time_unix_ms=self.timestamp_unix_ms,
        )


@dataclass(frozen=True, slots=True)
class NatsConsumerCursor:
    durable_name: str
    stream: str
    next_sequence: int

    def __post_init__(self) -> None:
        if not self.durable_name.strip():
            raise NatsAdapterError("durable_name must not be empty")
        _validate_stream(self.stream)
        if self.next_sequence <= 0:
            raise NatsAdapterError("next_sequence must be positive")

    @classmethod
    def from_source_cursor(cls, durable_name: str, cursor: SourceCursor) -> NatsConsumerCursor:
        if cursor.partition != 0:
            raise NatsAdapterError("NATS source cursors must use partition 0")
        return cls(durable_name, cursor.stream, cursor.offset + 1)

    def to_source_cursor(self) -> SourceCursor | None:
        if self.next_sequence == 1:
            return None
        return SourceCursor(self.stream, 0, self.next_sequence - 1)


@dataclass(frozen=True, slots=True)
class NatsPublishMessage:
    subject: str
    payload: object
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_subject(self.subject)
        object.__setattr__(self, "headers", dict(sorted(self.headers.items())))

    @classmethod
    def from_sink_commit(
        cls,
        *,
        subject: str,
        request: SinkCommitRequest,
    ) -> NatsPublishMessage:
        headers = {
            "Nats-Msg-Id": request.idempotency_key,
            "graphblocks-idempotency-key": request.idempotency_key,
            "graphblocks-node-attempt-id": request.node_attempt_id,
            "graphblocks-node-id": request.node_id,
            "graphblocks-run-id": request.run_id,
        }
        if request.precondition_digest is not None:
            headers["graphblocks-precondition-digest"] = request.precondition_digest
        return cls(subject=subject, payload=request.payload, headers=headers)


__all__ = [
    "NatsAdapterError",
    "NatsConsumerCursor",
    "NatsMessage",
    "NatsPublishMessage",
    "SinkCommitRequest",
    "SourceCursor",
    "SourceEvent",
]
