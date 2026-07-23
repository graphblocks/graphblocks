from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graphblocks.durable import SinkCommitRequest, SourceCursor, SourceEvent


class KafkaAdapterError(ValueError):
    """Base error for Kafka durable adapter contracts."""


_MAX_KAFKA_OFFSET = (1 << 63) - 1


def _validate_topic(topic: object) -> None:
    if not isinstance(topic, str) or not topic.strip():
        raise KafkaAdapterError("topic must not be empty")


def _non_negative_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KafkaAdapterError(f"{field_name} must be an integer")
    if value < 0:
        raise KafkaAdapterError(f"{field_name} must be non-negative")
    return value


def _optional_non_negative_int(field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    return _non_negative_int(field_name, value)


def _offset(field_name: str, value: object) -> int:
    offset = _non_negative_int(field_name, value)
    if offset > _MAX_KAFKA_OFFSET:
        raise KafkaAdapterError(f"{field_name} must not exceed signed 64-bit range")
    return offset


def _headers(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise KafkaAdapterError("headers must be a mapping of strings")
    headers = dict(value)
    if any(
        not isinstance(name, str) or not isinstance(header_value, str)
        for name, header_value in headers.items()
    ):
        raise KafkaAdapterError("headers must be a mapping of strings")
    return dict(sorted(headers.items()))


@dataclass(frozen=True, slots=True)
class KafkaRecord:
    topic: str
    partition: int
    offset: int
    value: object
    key: object | None = None
    timestamp_unix_ms: int | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_topic(self.topic)
        object.__setattr__(self, "partition", _non_negative_int("partition", self.partition))
        object.__setattr__(self, "offset", _offset("offset", self.offset))
        object.__setattr__(
            self,
            "timestamp_unix_ms",
            _optional_non_negative_int("timestamp_unix_ms", self.timestamp_unix_ms),
        )
        object.__setattr__(self, "headers", _headers(self.headers))

    def to_source_event(self) -> SourceEvent:
        return SourceEvent(
            SourceCursor(self.topic, self.partition, self.offset),
            {"key": self.key, "value": self.value, "headers": dict(self.headers)},
            event_time_unix_ms=self.timestamp_unix_ms,
        )


@dataclass(frozen=True, slots=True)
class KafkaConsumerCursor:
    group_id: str
    topic: str
    partition: int
    next_offset: int

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, str) or not self.group_id.strip():
            raise KafkaAdapterError("group_id must not be empty")
        _validate_topic(self.topic)
        object.__setattr__(self, "partition", _non_negative_int("partition", self.partition))
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
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_topic(self.topic)
        object.__setattr__(self, "partition", _optional_non_negative_int("partition", self.partition))
        object.__setattr__(self, "headers", _headers(self.headers))

    @classmethod
    def from_sink_commit(
        cls,
        *,
        topic: str,
        request: SinkCommitRequest,
        key_field: str | None = None,
        partition: int | None = None,
    ) -> KafkaSinkRecord:
        if key_field is None:
            key: object | None = request.idempotency_key
        else:
            if not isinstance(request.payload, dict) or key_field not in request.payload:
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
