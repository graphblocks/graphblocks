from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from graphblocks.canonical import canonical_hash
from graphblocks.voice import VoiceTransport


WebSocketMediaMessageKind = Literal["audio_delta", "control", "transcript_delta"]


class WebSocketMediaAdapterError(ValueError):
    """Base error for WebSocket media adapter contracts."""


def _require_non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebSocketMediaAdapterError(f"{field_name} must not be empty")
    return value


def _require_exact_non_empty(field_name: str, value: object) -> str:
    normalized = _require_non_empty(field_name, value)
    if normalized != normalized.strip() or any(
        character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        for character in normalized
    ):
        raise WebSocketMediaAdapterError(
            f"{field_name} must be an exact non-empty string"
        )
    return normalized


def _positive_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WebSocketMediaAdapterError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WebSocketMediaAdapterError(f"{field_name} must be a non-negative integer")
    return value


def _string_mapping(
    field_name: str,
    value: object,
) -> MappingProxyType[str, str]:
    if not isinstance(value, Mapping):
        raise WebSocketMediaAdapterError(f"{field_name} must be a mapping")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key
            or key != key.strip()
            or any(
                character.isspace()
                or ord(character) < 0x20
                or ord(character) == 0x7F
                for character in key
            )
        ):
            raise WebSocketMediaAdapterError(
                f"{field_name} keys must be stable non-empty strings"
            )
        if not isinstance(item, str):
            raise WebSocketMediaAdapterError(f"{field_name} values must be strings")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in item):
            raise WebSocketMediaAdapterError(
                f"{field_name} values must not contain control characters"
            )
        normalized[key] = item
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class WebSocketMediaEndpoint:
    uri: str
    protocol: str = "graphblocks.voice.v1"
    codec: str = "pcm16"
    sample_rate_hz: int = 24_000
    channels: int = 1
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty("uri", self.uri)
        try:
            parsed_uri = urlsplit(self.uri)
            hostname = parsed_uri.hostname
            _ = parsed_uri.port
        except ValueError as error:
            raise WebSocketMediaAdapterError("uri must be an absolute WebSocket URI") from error
        if parsed_uri.scheme not in {"ws", "wss"}:
            raise WebSocketMediaAdapterError("uri must use ws:// or wss://")
        raw_authority = parsed_uri.netloc.rsplit("@", 1)[-1]
        if (
            hostname is None
            or parsed_uri.username is not None
            or parsed_uri.password is not None
            or raw_authority.endswith(":")
            or parsed_uri.fragment
            or any(
                character.isspace()
                or ord(character) < 0x20
                or ord(character) == 0x7F
                for character in self.uri
            )
        ):
            raise WebSocketMediaAdapterError("uri must be an absolute WebSocket URI")
        _require_exact_non_empty("protocol", self.protocol)
        _require_exact_non_empty("codec", self.codec)
        object.__setattr__(
            self,
            "sample_rate_hz",
            _positive_int("sample_rate_hz", self.sample_rate_hz),
        )
        object.__setattr__(self, "channels", _positive_int("channels", self.channels))
        object.__setattr__(self, "headers", _string_mapping("headers", self.headers))

    def to_voice_transport(self) -> VoiceTransport:
        return VoiceTransport.websocket(
            self.uri,
            codec=self.codec,
            sample_rate_hz=self.sample_rate_hz,
            channels=self.channels,
        )

    def handshake_contract(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "protocol": self.protocol,
            "codec": self.codec,
            "sampleRateHz": self.sample_rate_hz,
            "channels": self.channels,
            "headers": dict(self.headers),
        }


@dataclass(frozen=True, slots=True)
class WebSocketMediaMessage:
    stream_id: str
    sequence: int
    kind: WebSocketMediaMessageKind
    audio_ref: str | None = None
    duration_ms: int | None = None
    text: str | None = None
    control: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_exact_non_empty("stream_id", self.stream_id)
        object.__setattr__(
            self,
            "sequence",
            _non_negative_int("sequence", self.sequence),
        )
        if self.kind not in {"audio_delta", "control", "transcript_delta"}:
            raise WebSocketMediaAdapterError(f"unsupported media message kind {self.kind!r}")
        if self.audio_ref is not None:
            _require_exact_non_empty("audio_ref", self.audio_ref)
        if self.duration_ms is not None:
            object.__setattr__(
                self,
                "duration_ms",
                _positive_int("duration_ms", self.duration_ms),
            )
        if self.text is not None:
            _require_non_empty("text", self.text)
        if self.control is not None:
            _require_exact_non_empty("control", self.control)
        if self.kind == "audio_delta":
            if self.audio_ref is None or self.duration_ms is None:
                raise WebSocketMediaAdapterError(
                    "audio_delta requires audio_ref and duration_ms"
                )
            if self.text is not None or self.control is not None:
                raise WebSocketMediaAdapterError(
                    "audio_delta must not define text or control"
                )
        elif self.kind == "control":
            if self.control is None:
                raise WebSocketMediaAdapterError("control requires control")
            if (
                self.audio_ref is not None
                or self.duration_ms is not None
                or self.text is not None
            ):
                raise WebSocketMediaAdapterError(
                    "control must not define audio or transcript fields"
                )
        else:
            if self.text is None:
                raise WebSocketMediaAdapterError("transcript_delta requires text")
            if (
                self.audio_ref is not None
                or self.duration_ms is not None
                or self.control is not None
            ):
                raise WebSocketMediaAdapterError(
                    "transcript_delta must not define audio or control fields"
                )
        object.__setattr__(self, "metadata", _string_mapping("metadata", self.metadata))

    @classmethod
    def audio_delta(
        cls,
        stream_id: str,
        sequence: int,
        *,
        audio_ref: str,
        duration_ms: int,
    ) -> WebSocketMediaMessage:
        return cls(
            stream_id,
            sequence,
            "audio_delta",
            audio_ref=audio_ref,
            duration_ms=duration_ms,
        )

    def contract(self) -> dict[str, object]:
        return {
            "streamId": self.stream_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "audioRef": self.audio_ref,
            "durationMs": self.duration_ms,
            "text": self.text,
            "control": self.control,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WebSocketMediaStream:
    stream_id: str
    messages: tuple[WebSocketMediaMessage, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_exact_non_empty("stream_id", self.stream_id)
        try:
            messages = tuple(self.messages)
        except TypeError as error:
            raise WebSocketMediaAdapterError(
                "media stream messages must be a sequence"
            ) from error
        sequences: set[int] = set()
        for message in messages:
            if not isinstance(message, WebSocketMediaMessage):
                raise WebSocketMediaAdapterError(
                    "media stream message entries must be WebSocketMediaMessage values"
                )
            if message.stream_id != self.stream_id:
                raise WebSocketMediaAdapterError("media message stream_id does not match stream")
            if message.sequence in sequences:
                raise WebSocketMediaAdapterError(
                    f"media message sequence {message.sequence} must be unique within a stream"
                )
            sequences.add(message.sequence)
        object.__setattr__(
            self,
            "messages",
            tuple(sorted(messages, key=lambda message: message.sequence)),
        )

    def stream_contract(self) -> dict[str, object]:
        return {
            "streamId": self.stream_id,
            "messages": [message.contract() for message in self.messages],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.stream_contract())


__all__ = [
    "WebSocketMediaAdapterError",
    "WebSocketMediaEndpoint",
    "WebSocketMediaMessage",
    "WebSocketMediaMessageKind",
    "WebSocketMediaStream",
]
