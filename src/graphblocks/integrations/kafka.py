from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graphblocks.durable import SinkCommitRequest, SourceCursor, SourceEvent
from graphblocks.integrations._wire import (
    FrozenWireJsonObject,
    snapshot_wire_json,
    thaw_wire_json,
)


class KafkaAdapterError(ValueError):
    """Base error for Kafka durable adapter contracts."""


_MAX_KAFKA_OFFSET = (1 << 63) - 1
_MAX_KAFKA_PARTITION = (1 << 31) - 1
_MAX_KAFKA_TOPIC_LENGTH = 249


def _validate_topic(topic: object) -> None:
    if not isinstance(topic, str) or not topic.strip():
        raise KafkaAdapterError("topic must not be empty")
    if topic != topic.strip():
        raise KafkaAdapterError("topic must not contain surrounding whitespace")
    if len(topic) > _MAX_KAFKA_TOPIC_LENGTH:
        raise KafkaAdapterError("topic must not exceed 249 characters")
    if topic in {".", ".."} or any(
        not (character.isascii() and (character.isalnum() or character in "._-"))
        for character in topic
    ):
        raise KafkaAdapterError("topic contains unsupported characters")


def _non_negative_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KafkaAdapterError(f"{field_name} must be an integer")
    if value < 0:
        raise KafkaAdapterError(f"{field_name} must be non-negative")
    return value


def _offset(field_name: str, value: object) -> int:
    offset = _non_negative_int(field_name, value)
    if offset > _MAX_KAFKA_OFFSET:
        raise KafkaAdapterError(f"{field_name} must not exceed signed 64-bit range")
    return offset


def _partition(value: object) -> int:
    partition = _non_negative_int("partition", value)
    if partition > _MAX_KAFKA_PARTITION:
        raise KafkaAdapterError("partition must not exceed signed 32-bit range")
    return partition


def _headers(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise KafkaAdapterError("headers must be a mapping of strings")
    headers: dict[str, str] = {}
    try:
        for name, header_value in tuple(value.items()):
            if (
                not isinstance(name, str)
                or not name
                or name != name.strip()
                or any(
                    ord(character) < 0x20
                    or ord(character) == 0x7F
                    or "\ud800" <= character <= "\udfff"
                    for character in name
                )
                or not isinstance(header_value, str)
                or any(
                    (ord(character) < 0x20 and character != "\t")
                    or ord(character) == 0x7F
                    or "\ud800" <= character <= "\udfff"
                    for character in header_value
                )
            ):
                raise KafkaAdapterError("headers must be stable HTTP-compatible strings")
            if name in headers:
                raise KafkaAdapterError(f"headers must not contain duplicate key {name!r}")
            headers[name] = header_value
    except KafkaAdapterError:
        raise
    except (TypeError, ValueError, RuntimeError) as error:
        raise KafkaAdapterError("headers must be a stable mapping of strings") from error
    return FrozenWireJsonObject(dict(sorted(headers.items())))


def _wire_value(field_name: str, value: object) -> object:
    try:
        return snapshot_wire_json(value, field_name=field_name)
    except ValueError as error:
        raise KafkaAdapterError(str(error)) from error


@dataclass(frozen=True, slots=True)
class KafkaRecord:
    topic: str
    partition: int
    offset: int
    value: object
    key: object | None = None
    timestamp_unix_ms: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_topic(self.topic)
        object.__setattr__(self, "partition", _partition(self.partition))
        object.__setattr__(self, "offset", _offset("offset", self.offset))
        timestamp_unix_ms = self.timestamp_unix_ms
        if timestamp_unix_ms is not None:
            timestamp_unix_ms = _offset("timestamp_unix_ms", timestamp_unix_ms)
        object.__setattr__(self, "timestamp_unix_ms", timestamp_unix_ms)
        object.__setattr__(self, "headers", _headers(self.headers))
        object.__setattr__(self, "value", _wire_value("value", self.value))
        object.__setattr__(self, "key", _wire_value("key", self.key))

    def to_source_event(self) -> SourceEvent:
        return SourceEvent(
            SourceCursor(self.topic, self.partition, self.offset),
            {
                "key": thaw_wire_json(self.key),
                "value": thaw_wire_json(self.value),
                "headers": dict(self.headers),
            },
            event_time_unix_ms=self.timestamp_unix_ms,
        )


@dataclass(frozen=True, slots=True)
class KafkaConsumerCursor:
    group_id: str
    topic: str
    partition: int
    next_offset: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.group_id, str)
            or not self.group_id.strip()
            or self.group_id != self.group_id.strip()
            or any(
                ord(character) < 0x20
                or ord(character) == 0x7F
                or "\ud800" <= character <= "\udfff"
                for character in self.group_id
            )
        ):
            raise KafkaAdapterError("group_id must not be empty")
        _validate_topic(self.topic)
        object.__setattr__(self, "partition", _partition(self.partition))
        object.__setattr__(self, "next_offset", _offset("next_offset", self.next_offset))

    @classmethod
    def from_source_cursor(cls, group_id: str, cursor: SourceCursor) -> KafkaConsumerCursor:
        if not isinstance(cursor, SourceCursor):
            raise KafkaAdapterError("cursor must be a SourceCursor")
        if cursor.offset == _MAX_KAFKA_OFFSET:
            raise KafkaAdapterError("source cursor offset cannot advance beyond signed 64-bit range")
        return cls(group_id, cursor.stream, cursor.partition, cursor.offset + 1)

    def to_source_cursor(self) -> SourceCursor | None:
        if self.next_offset == 0:
            return None
        return SourceCursor(self.topic, self.partition, self.next_offset - 1)


@dataclass(frozen=True, slots=True)
class KafkaSinkRecord:
    topic: str
    value: object
    key: object | None = None
    partition: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_topic(self.topic)
        partition = self.partition
        if partition is not None:
            partition = _partition(partition)
        object.__setattr__(self, "partition", partition)
        object.__setattr__(self, "headers", _headers(self.headers))
        object.__setattr__(self, "value", _wire_value("value", self.value))
        object.__setattr__(self, "key", _wire_value("key", self.key))

    @classmethod
    def from_sink_commit(
        cls,
        *,
        topic: str,
        request: SinkCommitRequest,
        key_field: str | None = None,
        partition: int | None = None,
    ) -> KafkaSinkRecord:
        if not isinstance(request, SinkCommitRequest):
            raise KafkaAdapterError("request must be a SinkCommitRequest")
        if key_field is None:
            key: object | None = request.idempotency_key
        else:
            if (
                not isinstance(key_field, str)
                or not key_field.strip()
                or key_field != key_field.strip()
            ):
                raise KafkaAdapterError("key_field must be a stable non-empty string")
            if not isinstance(request.payload, Mapping) or key_field not in request.payload:
                raise KafkaAdapterError(f"payload does not contain key field {key_field!r}")
            key = request.payload[key_field]
        headers = {
            "graphblocks-idempotency-key": request.idempotency_key,
            "graphblocks-node-attempt-id": request.node_attempt_id,
            "graphblocks-node-id": request.node_id,
            "graphblocks-run-id": request.run_id,
        }
        if request.precondition_digest is not None:
            headers["graphblocks-precondition-digest"] = request.precondition_digest
        return cls(
            topic=topic,
            value=request.payload,
            key=key,
            partition=partition,
            headers=headers,
        )


__all__ = [
    "KafkaAdapterError",
    "KafkaConsumerCursor",
    "KafkaRecord",
    "KafkaSinkRecord",
    "SinkCommitRequest",
    "SourceCursor",
    "SourceEvent",
]
