from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graphblocks.durable import SinkCommitRequest, SourceCursor, SourceEvent
from graphblocks.integrations._wire import (
    FrozenWireJsonObject,
    snapshot_wire_json,
    thaw_wire_json,
)


class SqsAdapterError(ValueError):
    """Base error for SQS durable adapter contracts."""


def _stable_string(field_name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise SqsAdapterError(f"{field_name} must be a stable non-empty string")
    return value


def _string_mapping(field_name: str, value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise SqsAdapterError(f"{field_name} must be a mapping")
    normalized: dict[str, str] = {}
    for key, item in tuple(value.items()):
        if not isinstance(item, str):
            raise SqsAdapterError(f"{field_name} values must be strings")
        normalized_key = _stable_string(f"{field_name} key", key)
        if normalized_key in normalized:
            raise SqsAdapterError(
                f"{field_name} must not contain duplicate key {normalized_key!r}"
            )
        normalized[normalized_key] = item
    return FrozenWireJsonObject(dict(sorted(normalized.items())))


def _wire_value(field_name: str, value: object) -> object:
    try:
        return snapshot_wire_json(value, field_name=field_name)
    except ValueError as error:
        raise SqsAdapterError(str(error)) from error


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
    attributes: Mapping[str, str] = field(default_factory=dict)
    message_attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _stable_string("queue", self.queue)
        object.__setattr__(self, "receive_sequence", _positive_int("receive_sequence", self.receive_sequence))
        _stable_string("message_id", self.message_id)
        _stable_string("receipt_handle", self.receipt_handle)
        object.__setattr__(
            self,
            "sent_timestamp_unix_ms",
            _optional_non_negative_int("sent_timestamp_unix_ms", self.sent_timestamp_unix_ms),
        )
        object.__setattr__(self, "body", _wire_value("body", self.body))
        object.__setattr__(self, "attributes", _string_mapping("attributes", self.attributes))
        object.__setattr__(
            self,
            "message_attributes",
            _string_mapping("message_attributes", self.message_attributes),
        )

    def to_source_event(self) -> SourceEvent:
        return SourceEvent(
            SourceCursor(self.queue, 0, self.receive_sequence),
            {
                "message_id": self.message_id,
                "receipt_handle": self.receipt_handle,
                "body": thaw_wire_json(self.body),
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
        _stable_string("queue", self.queue)
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
    message_attributes: Mapping[str, str] = field(default_factory=dict)
    message_group_id: str | None = None
    message_deduplication_id: str | None = None

    def __post_init__(self) -> None:
        _stable_string("queue", self.queue)
        if self.message_group_id is not None:
            _stable_string("message_group_id", self.message_group_id)
        if self.message_deduplication_id is not None:
            _stable_string("message_deduplication_id", self.message_deduplication_id)
        object.__setattr__(self, "body", _wire_value("body", self.body))
        object.__setattr__(
            self,
            "message_attributes",
            _string_mapping("message_attributes", self.message_attributes),
        )

    @classmethod
    def from_sink_commit(
        cls,
        *,
        queue: str,
        request: SinkCommitRequest,
        fifo: bool = False,
        message_group_id: str | None = None,
    ) -> SqsSendMessage:
        if not isinstance(fifo, bool):
            raise SqsAdapterError("fifo must be a boolean")
        if not isinstance(request, SinkCommitRequest):
            raise SqsAdapterError("request must be a SinkCommitRequest")
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
