from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from graphblocks.voice import VoiceTransport


class OpenAIRealtimeAdapterError(ValueError):
    """Base error for OpenAI Realtime adapter contracts."""


def _require_non_empty(field_name: str, value: str) -> None:
    if not value.strip():
        raise OpenAIRealtimeAdapterError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeSessionConfig:
    model: str
    instructions: str
    voice: str = "marin"
    modalities: tuple[str, ...] = ("audio",)
    metadata: dict[str, str] = field(default_factory=dict)
    codec: str = "pcm16"
    sample_rate_hz: int = 24_000
    channels: int = 1

    def __post_init__(self) -> None:
        _require_non_empty("model", self.model)
        _require_non_empty("instructions", self.instructions)
        _require_non_empty("voice", self.voice)
        _require_non_empty("codec", self.codec)
        if self.sample_rate_hz <= 0:
            raise OpenAIRealtimeAdapterError("sample_rate_hz must be positive")
        if self.channels <= 0:
            raise OpenAIRealtimeAdapterError("channels must be positive")
        modalities = tuple(sorted({str(modality).strip() for modality in self.modalities}))
        if not modalities:
            raise OpenAIRealtimeAdapterError("modalities must not be empty")
        for modality in modalities:
            _require_non_empty("modality", modality)
        object.__setattr__(self, "modalities", modalities)
        object.__setattr__(
            self,
            "metadata",
            {str(key): str(value) for key, value in sorted(dict(self.metadata).items())},
        )

    def session_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": "realtime",
            "model": self.model,
            "instructions": self.instructions,
            "modalities": list(self.modalities),
            "audio": {"output": {"voice": self.voice}},
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    def to_voice_transport(self) -> VoiceTransport:
        return VoiceTransport(
            "provider_realtime",
            uri=f"openai-realtime:{self.model}",
            codec=self.codec,
            sample_rate_hz=self.sample_rate_hz,
            channels=self.channels,
        )


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeWebRtcCall:
    session_config: OpenAIRealtimeSessionConfig
    offer_sdp: str
    api_base_url: str = "https://api.openai.com"
    safety_identifier: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("offer_sdp", self.offer_sdp)
        _require_non_empty("api_base_url", self.api_base_url)
        if not self.api_base_url.startswith(("http://", "https://")):
            raise OpenAIRealtimeAdapterError("api_base_url must use http:// or https://")
        if self.safety_identifier is not None:
            _require_non_empty("safety_identifier", self.safety_identifier)
        object.__setattr__(self, "api_base_url", self.api_base_url.rstrip("/"))

    def request_contract(self) -> dict[str, object]:
        headers = {}
        if self.safety_identifier is not None:
            headers["OpenAI-Safety-Identifier"] = self.safety_identifier
        return {
            "method": "POST",
            "url": f"{self.api_base_url}/v1/realtime/calls",
            "contentType": "multipart/form-data",
            "fields": {
                "sdp": self.offer_sdp,
                "session": self.session_config.session_payload(),
            },
            "headers": headers,
            "requiresBearerToken": True,
        }

    def answer_description(self, answer_sdp: str) -> dict[str, str]:
        _require_non_empty("answer_sdp", answer_sdp)
        return {"type": "answer", "sdp": answer_sdp}


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeClientSecretRequest:
    session_config: OpenAIRealtimeSessionConfig
    api_base_url: str = "https://api.openai.com"
    safety_identifier: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("api_base_url", self.api_base_url)
        if not self.api_base_url.startswith(("http://", "https://")):
            raise OpenAIRealtimeAdapterError("api_base_url must use http:// or https://")
        if self.safety_identifier is not None:
            _require_non_empty("safety_identifier", self.safety_identifier)
        object.__setattr__(self, "api_base_url", self.api_base_url.rstrip("/"))

    def request_contract(self) -> dict[str, object]:
        headers = {}
        if self.safety_identifier is not None:
            headers["OpenAI-Safety-Identifier"] = self.safety_identifier
        return {
            "method": "POST",
            "url": f"{self.api_base_url}/v1/realtime/client_secrets",
            "contentType": "application/json",
            "body": {"session": self.session_config.session_payload()},
            "headers": headers,
            "requiresBearerToken": True,
        }


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeWebSocketSession:
    session_config: OpenAIRealtimeSessionConfig
    base_url: str = "wss://api.openai.com/v1/realtime"
    protocol: str = "realtime"
    safety_identifier: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("base_url", self.base_url)
        _require_non_empty("protocol", self.protocol)
        if not self.base_url.startswith(("ws://", "wss://")):
            raise OpenAIRealtimeAdapterError("base_url must use ws:// or wss://")
        if self.safety_identifier is not None:
            _require_non_empty("safety_identifier", self.safety_identifier)
        object.__setattr__(self, "base_url", self.base_url.rstrip("?&"))

    def connection_contract(self) -> dict[str, object]:
        headers = {}
        if self.safety_identifier is not None:
            headers["OpenAI-Safety-Identifier"] = self.safety_identifier
        separator = "&" if "?" in self.base_url else "?"
        return {
            "url": f"{self.base_url}{separator}{urlencode({'model': self.session_config.model})}",
            "protocol": self.protocol,
            "headers": headers,
            "requiresBearerToken": True,
        }


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeEvent:
    event_type: str
    payload: dict[str, object] = field(default_factory=dict)
    event_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("event_type", self.event_type)
        if self.event_id is not None:
            _require_non_empty("event_id", self.event_id)
        object.__setattr__(self, "payload", dict(self.payload))

    @classmethod
    def session_update(
        cls,
        session_config: OpenAIRealtimeSessionConfig,
        *,
        event_id: str | None = None,
    ) -> OpenAIRealtimeEvent:
        return cls("session.update", {"session": session_config.session_payload()}, event_id)

    @classmethod
    def input_audio_append(cls, audio: str, *, event_id: str | None = None) -> OpenAIRealtimeEvent:
        _require_non_empty("audio", audio)
        return cls("input_audio_buffer.append", {"audio": audio}, event_id)

    @classmethod
    def user_text_message(cls, text: str, *, event_id: str | None = None) -> OpenAIRealtimeEvent:
        _require_non_empty("text", text)
        return cls(
            "conversation.item.create",
            {
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            },
            event_id,
        )

    def event_contract(self) -> dict[str, object]:
        contract: dict[str, object] = {}
        if self.event_id is not None:
            contract["event_id"] = self.event_id
        contract["type"] = self.event_type
        contract.update(self.payload)
        return contract


__all__ = [
    "OpenAIRealtimeAdapterError",
    "OpenAIRealtimeClientSecretRequest",
    "OpenAIRealtimeEvent",
    "OpenAIRealtimeSessionConfig",
    "OpenAIRealtimeWebRtcCall",
    "OpenAIRealtimeWebSocketSession",
]
