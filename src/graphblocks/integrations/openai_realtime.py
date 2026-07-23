from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from graphblocks import canonical_dumps, canonical_loads
from graphblocks.voice import VoiceTransport


class OpenAIRealtimeAdapterError(ValueError):
    """Base error for OpenAI Realtime adapter contracts."""


_OUTPUT_AUDIO_FORMATS = {
    "pcm16": ("audio/pcm", 24_000),
    "g711_ulaw": ("audio/pcmu", 8_000),
    "g711_alaw": ("audio/pcma", 8_000),
}


def _require_non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpenAIRealtimeAdapterError(f"{field_name} must not be empty")
    return value


def _require_positive_integer(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OpenAIRealtimeAdapterError(f"{field_name} must be a positive integer")
    return value


def _require_base_url(field_name: str, value: object, *, schemes: set[str], query: bool) -> str:
    url = _require_non_empty(field_name, value)
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise OpenAIRealtimeAdapterError(f"{field_name} must be an absolute URL") from error
    if (
        parsed.scheme not in schemes
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not query)
        or parsed.netloc.rsplit("@", 1)[-1].endswith(":")
        or any(character.isspace() or ord(character) < 0x20 for character in url)
    ):
        raise OpenAIRealtimeAdapterError(f"{field_name} must be an absolute URL")
    return url


def _string_mapping(field_name: str, value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise OpenAIRealtimeAdapterError(f"{field_name} must be a mapping")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or key != key.strip():
            raise OpenAIRealtimeAdapterError(
                f"{field_name} keys must be stable non-empty strings"
            )
        if not isinstance(item, str):
            raise OpenAIRealtimeAdapterError(f"{field_name} values must be strings")
        normalized[key] = item
    return dict(sorted(normalized.items()))


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
        codec = self.codec.strip().lower()
        if codec not in _OUTPUT_AUDIO_FORMATS:
            raise OpenAIRealtimeAdapterError(
                "codec must be one of pcm16, g711_ulaw, or g711_alaw"
            )
        expected_sample_rate = _OUTPUT_AUDIO_FORMATS[codec][1]
        sample_rate_hz = _require_positive_integer("sample_rate_hz", self.sample_rate_hz)
        if sample_rate_hz != expected_sample_rate:
            raise OpenAIRealtimeAdapterError(
                f"{codec} sample_rate_hz must be {expected_sample_rate}"
            )
        channels = _require_positive_integer("channels", self.channels)
        if channels != 1:
            raise OpenAIRealtimeAdapterError("OpenAI Realtime audio must be mono")
        if isinstance(self.modalities, (str, bytes)):
            raise OpenAIRealtimeAdapterError("modalities must be a sequence of strings")
        try:
            raw_modalities = tuple(self.modalities)
        except TypeError as error:
            raise OpenAIRealtimeAdapterError("modalities must be a sequence of strings") from error
        if any(not isinstance(modality, str) for modality in raw_modalities):
            raise OpenAIRealtimeAdapterError("modalities must be a sequence of strings")
        modalities = tuple(sorted(set(raw_modalities)))
        if not modalities:
            raise OpenAIRealtimeAdapterError("modalities must not be empty")
        for modality in modalities:
            _require_non_empty("modality", modality)
        if modalities not in {("audio",), ("text",)}:
            raise OpenAIRealtimeAdapterError(
                "output modalities must be exactly ('audio',) or ('text',)"
            )
        object.__setattr__(self, "codec", codec)
        object.__setattr__(self, "sample_rate_hz", sample_rate_hz)
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "modalities", modalities)
        object.__setattr__(self, "metadata", _string_mapping("metadata", self.metadata))

    def session_payload(self) -> dict[str, object]:
        format_type = _OUTPUT_AUDIO_FORMATS[self.codec][0]
        output_format: dict[str, object] = {"type": format_type}
        if self.codec == "pcm16":
            output_format["rate"] = self.sample_rate_hz
        payload: dict[str, object] = {
            "type": "realtime",
            "model": self.model,
            "instructions": self.instructions,
            "output_modalities": list(self.modalities),
            "audio": {
                "input": {"format": dict(output_format)},
                "output": {"format": output_format, "voice": self.voice},
            },
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
        if not isinstance(self.session_config, OpenAIRealtimeSessionConfig):
            raise OpenAIRealtimeAdapterError(
                "session_config must be an OpenAIRealtimeSessionConfig"
            )
        _require_non_empty("offer_sdp", self.offer_sdp)
        api_base_url = _require_base_url(
            "api_base_url", self.api_base_url, schemes={"http", "https"}, query=False
        )
        if self.safety_identifier is not None:
            _require_non_empty("safety_identifier", self.safety_identifier)
        object.__setattr__(self, "api_base_url", api_base_url.rstrip("/"))

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
        if not isinstance(self.session_config, OpenAIRealtimeSessionConfig):
            raise OpenAIRealtimeAdapterError(
                "session_config must be an OpenAIRealtimeSessionConfig"
            )
        api_base_url = _require_base_url(
            "api_base_url", self.api_base_url, schemes={"http", "https"}, query=False
        )
        if self.safety_identifier is not None:
            _require_non_empty("safety_identifier", self.safety_identifier)
        object.__setattr__(self, "api_base_url", api_base_url.rstrip("/"))

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
        if not isinstance(self.session_config, OpenAIRealtimeSessionConfig):
            raise OpenAIRealtimeAdapterError(
                "session_config must be an OpenAIRealtimeSessionConfig"
            )
        base_url = _require_base_url(
            "base_url", self.base_url, schemes={"ws", "wss"}, query=True
        )
        _require_non_empty("protocol", self.protocol)
        if self.safety_identifier is not None:
            _require_non_empty("safety_identifier", self.safety_identifier)
        object.__setattr__(self, "base_url", base_url.rstrip("?&"))

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
        if not isinstance(self.payload, Mapping):
            raise OpenAIRealtimeAdapterError("event payload must be a mapping")
        reserved_fields = {"type", "event_id"}.intersection(self.payload)
        if reserved_fields:
            fields = ", ".join(sorted(reserved_fields))
            raise OpenAIRealtimeAdapterError(
                f"event payload must not contain reserved envelope fields: {fields}"
            )
        try:
            payload = canonical_loads(canonical_dumps(self.payload))
        except (TypeError, ValueError) as error:
            raise OpenAIRealtimeAdapterError(
                "event payload must be a strict JSON object"
            ) from error
        if not isinstance(payload, dict):
            raise OpenAIRealtimeAdapterError("event payload must be a strict JSON object")
        object.__setattr__(self, "payload", payload)

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
        contract.update(deepcopy(self.payload))
        return contract


__all__ = [
    "OpenAIRealtimeAdapterError",
    "OpenAIRealtimeClientSecretRequest",
    "OpenAIRealtimeEvent",
    "OpenAIRealtimeSessionConfig",
    "OpenAIRealtimeWebRtcCall",
    "OpenAIRealtimeWebSocketSession",
]
