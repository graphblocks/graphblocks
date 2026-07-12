from __future__ import annotations

from dataclasses import dataclass, field

from graphblocks.durable import SinkCommitRequest, SourceCursor, SourceEvent


class PubsubAdapterError(ValueError):
    """Base error for Pub/Sub durable adapter contracts."""


def _validate_subscription(subscription: str) -> None:
    if not subscription.strip():
        raise PubsubAdapterError("subscription must not be empty")


def _validate_topic(topic: str) -> None:
    if not topic.strip():
        raise PubsubAdapterError("topic must not be empty")


def _positive_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PubsubAdapterError(f"{field_name} must be an integer")
    if value <= 0:
        raise PubsubAdapterError(f"{field_name} must be positive")
    return value


def _non_negative_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PubsubAdapterError(f"{field_name} must be an integer")
    if value < 0:
        raise PubsubAdapterError(f"{field_name} must be non-negative")
    return value


def _optional_positive_int(field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    return _positive_int(field_name, value)


def _optional_non_negative_int(field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    return _non_negative_int(field_name, value)


@dataclass(frozen=True, slots=True)
class PubsubMessage:
    subscription: str
    receive_sequence: int
    message_id: str
    ack_id: str
    data: object
    publish_time_unix_ms: int | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    ordering_key: str | None = None
    delivery_attempt: int | None = None

    def __post_init__(self) -> None:
        _validate_subscription(self.subscription)
        object.__setattr__(self, "receive_sequence", _positive_int("receive_sequence", self.receive_sequence))
        if not self.message_id.strip():
            raise PubsubAdapterError("message_id must not be empty")
        if not self.ack_id.strip():
            raise PubsubAdapterError("ack_id must not be empty")
        object.__setattr__(
            self,
            "publish_time_unix_ms",
            _optional_non_negative_int("publish_time_unix_ms", self.publish_time_unix_ms),
        )
        if self.ordering_key is not None and not self.ordering_key.strip():
            raise PubsubAdapterError("ordering_key must not be empty")
        object.__setattr__(
            self,
            "delivery_attempt",
            _optional_positive_int("delivery_attempt", self.delivery_attempt),
        )
        object.__setattr__(self, "attributes", dict(sorted(self.attributes.items())))

    def to_source_event(self) -> SourceEvent:
        return SourceEvent(
            SourceCursor(self.subscription, 0, self.receive_sequence),
            {
                "message_id": self.message_id,
                "ack_id": self.ack_id,
                "data": self.data,
                "attributes": dict(self.attributes),
                "ordering_key": self.ordering_key,
                "delivery_attempt": self.delivery_attempt,
            },
            event_time_unix_ms=self.publish_time_unix_ms,
        )


@dataclass(frozen=True, slots=True)
class PubsubSubscriptionCursor:
    subscription: str
    next_sequence: int

    def __post_init__(self) -> None:
        _validate_subscription(self.subscription)
        object.__setattr__(self, "next_sequence", _positive_int("next_sequence", self.next_sequence))

    @classmethod
    def from_source_cursor(cls, cursor: SourceCursor) -> PubsubSubscriptionCursor:
        if cursor.partition != 0:
            raise PubsubAdapterError("Pub/Sub source cursors must use partition 0")
        return cls(cursor.stream, cursor.offset + 1)

    def to_source_cursor(self) -> SourceCursor | None:
        if self.next_sequence == 1:
            return None
        return SourceCursor(self.subscription, 0, self.next_sequence - 1)


@dataclass(frozen=True, slots=True)
class PubsubPublishMessage:
    topic: str
    data: object
    attributes: dict[str, str] = field(default_factory=dict)
    ordering_key: str | None = None

    def __post_init__(self) -> None:
        _validate_topic(self.topic)
        if self.ordering_key is not None and not self.ordering_key.strip():
            raise PubsubAdapterError("ordering_key must not be empty")
        object.__setattr__(self, "attributes", dict(sorted(self.attributes.items())))

    @classmethod
    def from_sink_commit(
        cls,
        *,
        topic: str,
        request: SinkCommitRequest,
        ordering_key_field: str | None = None,
    ) -> PubsubPublishMessage:
        if ordering_key_field is None:
            ordering_key = None
        else:
            if not isinstance(request.payload, dict) or ordering_key_field not in request.payload:
                raise PubsubAdapterError(f"payload does not contain ordering key field {ordering_key_field!r}")
            ordering_key = str(request.payload[ordering_key_field])
        attributes = {
            "graphblocks-idempotency-key": request.idempotency_key,
            "graphblocks-node-attempt-id": request.node_attempt_id,
            "graphblocks-node-id": request.node_id,
            "graphblocks-run-id": request.run_id,
        }
        if request.precondition_digest is not None:
            attributes["graphblocks-precondition-digest"] = request.precondition_digest
        return cls(topic=topic, data=request.payload, attributes=attributes, ordering_key=ordering_key)


__all__ = [
    "PubsubAdapterError",
    "PubsubMessage",
    "PubsubPublishMessage",
    "PubsubSubscriptionCursor",
    "SinkCommitRequest",
    "SourceCursor",
    "SourceEvent",
]
