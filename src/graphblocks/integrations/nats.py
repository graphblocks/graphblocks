from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graphblocks.durable import SinkCommitRequest, SourceCursor, SourceEvent
from graphblocks.integrations._wire import (
    FrozenWireJsonObject,
    snapshot_wire_json,
    thaw_wire_json,
)


class NatsAdapterError(ValueError):
    """Base error for NATS durable adapter contracts."""


_MAX_NATS_SEQUENCE = (1 << 64) - 1
_MAX_TIMESTAMP_UNIX_MS = (1 << 63) - 1


def _stable_string(field_name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or "\ud800" <= character <= "\udfff"
            for character in value
        )
    ):
        raise NatsAdapterError(f"{field_name} must be a stable non-empty string")
    return value


def _subject(value: object) -> str:
    subject = _stable_string("subject", value)
    if any(character.isspace() or character in "*>" for character in subject):
        raise NatsAdapterError("subject must not contain whitespace or wildcards")
    if any(not token for token in subject.split(".")):
        raise NatsAdapterError("subject must not contain empty tokens")
    return subject


def _header_name(field_name: str, value: object) -> str:
    name = _stable_string(f"{field_name} key", value)
    token_punctuation = frozenset("!#$%&'*+-.^_`|~")
    if not all(
        character.isascii()
        and (character.isalnum() or character in token_punctuation)
        for character in name
    ):
        raise NatsAdapterError(f"{field_name} key must be an HTTP token")
    return name


def _string_mapping(field_name: str, value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise NatsAdapterError(f"{field_name} must be a mapping")
    normalized: dict[str, str] = {}
    identities: set[str] = set()
    try:
        for key, item in tuple(value.items()):
            if not isinstance(item, str) or any(
                (ord(character) < 0x20 and character != "\t")
                or ord(character) == 0x7F
                or "\ud800" <= character <= "\udfff"
                for character in item
            ):
                raise NatsAdapterError(f"{field_name} values must be strings")
            normalized_key = _header_name(field_name, key)
            identity = normalized_key.casefold()
            if identity in identities:
                raise NatsAdapterError(
                    f"{field_name} must not contain duplicate key {normalized_key!r}"
                )
            identities.add(identity)
            normalized[normalized_key] = item
    except NatsAdapterError:
        raise
    except (TypeError, ValueError, RuntimeError) as error:
        raise NatsAdapterError(f"{field_name} must be a stable mapping") from error
    return FrozenWireJsonObject(dict(sorted(normalized.items())))


def _wire_value(field_name: str, value: object) -> object:
    try:
        return snapshot_wire_json(value, field_name=field_name)
    except ValueError as error:
        raise NatsAdapterError(str(error)) from error


def _validate_integer(
    field_name: str,
    value: object,
    *,
    minimum: int,
    maximum: int,
    range_name: str,
) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        requirement = "positive" if minimum == 1 else "non-negative"
        raise NatsAdapterError(f"{field_name} must be a {requirement} integer")
    if value > maximum:
        raise NatsAdapterError(f"{field_name} must not exceed {range_name} range")


@dataclass(frozen=True, slots=True)
class NatsMessage:
    stream: str
    subject: str
    sequence: int
    payload: object
    timestamp_unix_ms: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _stable_string("stream", self.stream)
        _subject(self.subject)
        _validate_integer(
            "sequence",
            self.sequence,
            minimum=1,
            maximum=_MAX_NATS_SEQUENCE,
            range_name="unsigned 64-bit",
        )
        if self.timestamp_unix_ms is not None:
            _validate_integer(
                "timestamp_unix_ms",
                self.timestamp_unix_ms,
                minimum=0,
                maximum=_MAX_TIMESTAMP_UNIX_MS,
                range_name="signed 64-bit",
            )
        object.__setattr__(self, "payload", _wire_value("payload", self.payload))
        object.__setattr__(self, "headers", _string_mapping("headers", self.headers))

    def to_source_event(self) -> SourceEvent:
        return SourceEvent(
            SourceCursor(self.stream, 0, self.sequence),
            {
                "subject": self.subject,
                "payload": thaw_wire_json(self.payload),
                "headers": dict(self.headers),
            },
            event_time_unix_ms=self.timestamp_unix_ms,
        )


@dataclass(frozen=True, slots=True)
class NatsConsumerCursor:
    durable_name: str
    stream: str
    next_sequence: int

    def __post_init__(self) -> None:
        _stable_string("durable_name", self.durable_name)
        _stable_string("stream", self.stream)
        _validate_integer(
            "next_sequence",
            self.next_sequence,
            minimum=1,
            maximum=_MAX_NATS_SEQUENCE,
            range_name="unsigned 64-bit",
        )

    @classmethod
    def from_source_cursor(cls, durable_name: str, cursor: SourceCursor) -> NatsConsumerCursor:
        if not isinstance(cursor, SourceCursor):
            raise NatsAdapterError("cursor must be a SourceCursor")
        if cursor.partition != 0:
            raise NatsAdapterError("NATS source cursors must use partition 0")
        if cursor.offset == 0:
            raise NatsAdapterError("NATS source cursor offset must be positive")
        if cursor.offset >= _MAX_NATS_SEQUENCE:
            raise NatsAdapterError(
                "NATS source cursor offset cannot advance beyond unsigned 64-bit range"
            )
        return cls(durable_name, cursor.stream, cursor.offset + 1)

    def to_source_cursor(self) -> SourceCursor | None:
        if self.next_sequence == 1:
            return None
        return SourceCursor(self.stream, 0, self.next_sequence - 1)


@dataclass(frozen=True, slots=True)
class NatsPublishMessage:
    subject: str
    payload: object
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _subject(self.subject)
        object.__setattr__(self, "payload", _wire_value("payload", self.payload))
        object.__setattr__(self, "headers", _string_mapping("headers", self.headers))

    @classmethod
    def from_sink_commit(
        cls,
        *,
        subject: str,
        request: SinkCommitRequest,
    ) -> NatsPublishMessage:
        if not isinstance(request, SinkCommitRequest):
            raise NatsAdapterError("request must be a SinkCommitRequest")
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
