from __future__ import annotations

from dataclasses import dataclass, field

from graphblocks.durable import SinkCommitRequest, SourceCursor, SourceEvent


class SqsAdapterError(ValueError):
    """Base error for SQS durable adapter contracts."""


def _validate_queue(queue: str) -> None:
    if not queue.strip():
        raise SqsAdapterError("queue must not be empty")


def _positive_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SqsAdapterError(f"{field_name} must be an integer")
    if value <= 0:
        raise SqsAdapterError(f"{field_name} must be positive")
    return value


def _non_negative_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SqsAdapterError(f"{field_name} must be an integer")
    if value < 0:
        raise SqsAdapterError(f"{field_name} must be non-negative")
    return value


def _optional_non_negative_int(field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    return _non_negative_int(field_name, value)


@dataclass(frozen=True, slots=True)
class SqsMessage:
    queue: str
    receive_sequence: int
    message_id: str
    receipt_handle: str
    body: object
    sent_timestamp_unix_ms: int | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    message_attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_queue(self.queue)
        object.__setattr__(self, "receive_sequence", _positive_int("receive_sequence", self.receive_sequence))
        if not self.message_id.strip():
            raise SqsAdapterError("message_id must not be empty")
        if not self.receipt_handle.strip():
            raise SqsAdapterError("receipt_handle must not be empty")
        object.__setattr__(
            self,
            "sent_timestamp_unix_ms",
            _optional_non_negative_int("sent_timestamp_unix_ms", self.sent_timestamp_unix_ms),
        )
        object.__setattr__(self, "attributes", dict(sorted(self.attributes.items())))
        object.__setattr__(self, "message_attributes", dict(sorted(self.message_attributes.items())))

    def to_source_event(self) -> SourceEvent:
        return SourceEvent(
            SourceCursor(self.queue, 0, self.receive_sequence),
            {
                "message_id": self.message_id,
                "receipt_handle": self.receipt_handle,
                "body": self.body,
                "attributes": dict(self.attributes),
                "message_attributes": dict(self.message_attributes),
            },
            event_time_unix_ms=self.sent_timestamp_unix_ms,
        )


@dataclass(frozen=True, slots=True)
class SqsReceiveCursor:
    queue: str
    next_sequence: int

    def __post_init__(self) -> None:
        _validate_queue(self.queue)
        object.__setattr__(self, "next_sequence", _positive_int("next_sequence", self.next_sequence))

    @classmethod
    def from_source_cursor(cls, cursor: SourceCursor) -> SqsReceiveCursor:
        if not isinstance(cursor, SourceCursor):
            raise SqsAdapterError("cursor must be a SourceCursor")
        if cursor.partition != 0:
            raise SqsAdapterError("SQS source cursors must use partition 0")
        if cursor.offset == 0:
            raise SqsAdapterError("SQS source cursor offset must be positive")
        return cls(cursor.stream, cursor.offset + 1)

    def to_source_cursor(self) -> SourceCursor | None:
        if self.next_sequence == 1:
            return None
        return SourceCursor(self.queue, 0, self.next_sequence - 1)


@dataclass(frozen=True, slots=True)
class SqsSendMessage:
    queue: str
    body: object
    message_attributes: dict[str, str] = field(default_factory=dict)
    message_group_id: str | None = None
    message_deduplication_id: str | None = None

    def __post_init__(self) -> None:
        _validate_queue(self.queue)
        if self.message_group_id is not None and not self.message_group_id.strip():
            raise SqsAdapterError("message_group_id must not be empty")
        if self.message_deduplication_id is not None and not self.message_deduplication_id.strip():
            raise SqsAdapterError("message_deduplication_id must not be empty")
        object.__setattr__(self, "message_attributes", dict(sorted(self.message_attributes.items())))

    @classmethod
    def from_sink_commit(
        cls,
        *,
        queue: str,
        request: SinkCommitRequest,
        fifo: bool = False,
        message_group_id: str | None = None,
    ) -> SqsSendMessage:
        if fifo and message_group_id is None:
            raise SqsAdapterError("FIFO SQS sends require message_group_id")
        message_attributes = {
            "graphblocks-idempotency-key": request.idempotency_key,
            "graphblocks-node-attempt-id": request.node_attempt_id,
            "graphblocks-node-id": request.node_id,
            "graphblocks-run-id": request.run_id,
        }
        if request.precondition_digest is not None:
            message_attributes["graphblocks-precondition-digest"] = request.precondition_digest
        return cls(
            queue=queue,
            body=request.payload,
            message_attributes=message_attributes,
            message_group_id=message_group_id,
            message_deduplication_id=request.idempotency_key if fifo else None,
        )


__all__ = [
    "SinkCommitRequest",
    "SourceCursor",
    "SourceEvent",
    "SqsAdapterError",
    "SqsMessage",
    "SqsReceiveCursor",
    "SqsSendMessage",
]
