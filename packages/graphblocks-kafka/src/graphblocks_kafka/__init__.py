from __future__ import annotations

from dataclasses import dataclass, field

from graphblocks_durable import SinkCommitRequest, SourceCursor, SourceEvent


class KafkaAdapterError(ValueError):
    """Base error for Kafka durable adapter contracts."""


def _validate_topic(topic: str) -> None:
    if not topic.strip():
        raise KafkaAdapterError("topic must not be empty")


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
        if self.partition < 0:
            raise KafkaAdapterError("partition must be non-negative")
        if self.offset < 0:
            raise KafkaAdapterError("offset must be non-negative")
        if self.timestamp_unix_ms is not None and self.timestamp_unix_ms < 0:
            raise KafkaAdapterError("timestamp_unix_ms must be non-negative")
        object.__setattr__(self, "headers", dict(sorted(self.headers.items())))

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
        if not self.group_id.strip():
            raise KafkaAdapterError("group_id must not be empty")
        _validate_topic(self.topic)
        if self.partition < 0:
            raise KafkaAdapterError("partition must be non-negative")
        if self.next_offset < 0:
            raise KafkaAdapterError("next_offset must be non-negative")

    @classmethod
    def from_source_cursor(cls, group_id: str, cursor: SourceCursor) -> KafkaConsumerCursor:
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
        if self.partition is not None and self.partition < 0:
            raise KafkaAdapterError("partition must be non-negative")
        object.__setattr__(self, "headers", dict(sorted(self.headers.items())))

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
