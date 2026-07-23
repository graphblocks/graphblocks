from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graphblocks.durable import SinkCommitRequest, SourceCursor, SourceEvent
from graphblocks.integrations._wire import (
    FrozenWireJsonObject,
    snapshot_wire_json,
    thaw_wire_json,
)


class PubsubAdapterError(ValueError):
    """Base error for Pub/Sub durable adapter contracts."""


def _stable_string(field_name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise PubsubAdapterError(f"{field_name} must be a stable non-empty string")
    return value


def _string_mapping(field_name: str, value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise PubsubAdapterError(f"{field_name} must be a mapping")
    normalized: dict[str, str] = {}
    for key, item in tuple(value.items()):
        if not isinstance(item, str):
            raise PubsubAdapterError(f"{field_name} values must be strings")
        normalized_key = _stable_string(f"{field_name} key", key)
        if normalized_key in normalized:
            raise PubsubAdapterError(
                f"{field_name} must not contain duplicate key {normalized_key!r}"
            )
        normalized[normalized_key] = item
    return FrozenWireJsonObject(dict(sorted(normalized.items())))


def _wire_value(field_name: str, value: object) -> object:
    try:
        return snapshot_wire_json(value, field_name=field_name)
    except ValueError as error:
        raise PubsubAdapterError(str(error)) from error


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
    attributes: Mapping[str, str] = field(default_factory=dict)
    ordering_key: str | None = None
    delivery_attempt: int | None = None

    def __post_init__(self) -> None:
        _stable_string("subscription", self.subscription)
        object.__setattr__(self, "receive_sequence", _positive_int("receive_sequence", self.receive_sequence))
        _stable_string("message_id", self.message_id)
        _stable_string("ack_id", self.ack_id)
        object.__setattr__(
            self,
            "publish_time_unix_ms",
            _optional_non_negative_int("publish_time_unix_ms", self.publish_time_unix_ms),
        )
        if self.ordering_key is not None:
            _stable_string("ordering_key", self.ordering_key)
        object.__setattr__(
            self,
            "delivery_attempt",
            _optional_positive_int("delivery_attempt", self.delivery_attempt),
        )
        object.__setattr__(self, "data", _wire_value("data", self.data))
        object.__setattr__(self, "attributes", _string_mapping("attributes", self.attributes))

    def to_source_event(self) -> SourceEvent:
        return SourceEvent(
            SourceCursor(self.subscription, 0, self.receive_sequence),
            {
                "message_id": self.message_id,
                "ack_id": self.ack_id,
                "data": thaw_wire_json(self.data),
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
        _stable_string("subscription", self.subscription)
        object.__setattr__(self, "next_sequence", _positive_int("next_sequence", self.next_sequence))

    @classmethod
    def from_source_cursor(cls, cursor: SourceCursor) -> PubsubSubscriptionCursor:
        if not isinstance(cursor, SourceCursor):
            raise PubsubAdapterError("cursor must be a SourceCursor")
        if cursor.partition != 0:
            raise PubsubAdapterError("Pub/Sub source cursors must use partition 0")
        if cursor.offset == 0:
            raise PubsubAdapterError("Pub/Sub source cursor offset must be positive")
        return cls(cursor.stream, cursor.offset + 1)

    def to_source_cursor(self) -> SourceCursor | None:
        if self.next_sequence == 1:
            return None
        return SourceCursor(self.subscription, 0, self.next_sequence - 1)


@dataclass(frozen=True, slots=True)
class PubsubPublishMessage:
    topic: str
    data: object
    attributes: Mapping[str, str] = field(default_factory=dict)
    ordering_key: str | None = None

    def __post_init__(self) -> None:
        _stable_string("topic", self.topic)
        if self.ordering_key is not None:
            _stable_string("ordering_key", self.ordering_key)
        object.__setattr__(self, "data", _wire_value("data", self.data))
        object.__setattr__(self, "attributes", _string_mapping("attributes", self.attributes))

    @classmethod
    def from_sink_commit(
        cls,
        *,
        topic: str,
        request: SinkCommitRequest,
        ordering_key_field: str | None = None,
    ) -> PubsubPublishMessage:
        if not isinstance(request, SinkCommitRequest):
            raise PubsubAdapterError("request must be a SinkCommitRequest")
        if ordering_key_field is None:
            ordering_key = None
        else:
            _stable_string("ordering_key_field", ordering_key_field)
            if not isinstance(request.payload, Mapping) or ordering_key_field not in request.payload:
                raise PubsubAdapterError(f"payload does not contain ordering key field {ordering_key_field!r}")
            ordering_key = _stable_string(
                "payload ordering key", request.payload[ordering_key_field]
            )
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
