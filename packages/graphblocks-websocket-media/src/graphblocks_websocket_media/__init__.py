from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from graphblocks.canonical import canonical_hash
from graphblocks_voice import VoiceTransport


WebSocketMediaMessageKind = Literal["audio_delta", "control", "transcript_delta"]


class WebSocketMediaAdapterError(ValueError):
    """Base error for WebSocket media adapter contracts."""


def _require_non_empty(field_name: str, value: str) -> None:
    if not value.strip():
        raise WebSocketMediaAdapterError(f"{field_name} must not be empty")


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
        if not (self.uri.startswith("ws://") or self.uri.startswith("wss://")):
            raise WebSocketMediaAdapterError("uri must use ws:// or wss://")
        _require_non_empty("protocol", self.protocol)
        _require_non_empty("codec", self.codec)
        if self.sample_rate_hz <= 0:
            raise WebSocketMediaAdapterError("sample_rate_hz must be positive")
        if self.channels <= 0:
            raise WebSocketMediaAdapterError("channels must be positive")
        object.__setattr__(self, "headers", dict(sorted(self.headers.items())))

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
        _require_non_empty("stream_id", self.stream_id)
        if self.sequence < 0:
            raise WebSocketMediaAdapterError("sequence must be non-negative")
        if self.kind not in {"audio_delta", "control", "transcript_delta"}:
            raise WebSocketMediaAdapterError(f"unsupported media message kind {self.kind!r}")
        if self.audio_ref is not None:
            _require_non_empty("audio_ref", self.audio_ref)
        if self.duration_ms is not None and self.duration_ms <= 0:
            raise WebSocketMediaAdapterError("duration_ms must be positive")
        if self.text is not None:
            _require_non_empty("text", self.text)
        if self.control is not None:
            _require_non_empty("control", self.control)
        object.__setattr__(self, "metadata", dict(sorted(self.metadata.items())))

    @classmethod
    def audio_delta(
        cls,
        stream_id: str,
        sequence: int,
        *,
        audio_ref: str,
        duration_ms: int,
    ) -> WebSocketMediaMessage:
        return cls(stream_id, sequence, "audio_delta", audio_ref=audio_ref, duration_ms=duration_ms)

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
        _require_non_empty("stream_id", self.stream_id)
        for message in self.messages:
            if message.stream_id != self.stream_id:
                raise WebSocketMediaAdapterError("media message stream_id does not match stream")
        object.__setattr__(self, "messages", tuple(sorted(self.messages, key=lambda message: message.sequence)))

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
