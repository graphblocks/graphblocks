from __future__ import annotations

from dataclasses import dataclass, field

from graphblocks.canonical import canonical_hash
from graphblocks.voice import VoiceTransport


class WebRtcAdapterError(ValueError):
    """Base error for WebRTC voice adapter contracts."""


_MAX_U16 = (1 << 16) - 1
_MAX_U32 = (1 << 32) - 1
_MAX_U64 = (1 << 64) - 1


def _require_non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebRtcAdapterError(f"{field_name} must not be empty")
    if any("\ud800" <= character <= "\udfff" for character in value):
        raise WebRtcAdapterError(f"{field_name} must contain Unicode scalar values")
    return value


def _require_exact_non_empty(field_name: str, value: object) -> str:
    normalized = _require_non_empty(field_name, value)
    if normalized != normalized.strip() or any(
        character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        for character in normalized
    ):
        raise WebRtcAdapterError(
            f"{field_name} must be an exact non-empty string"
        )
    return normalized


def _positive_int(field_name: str, value: object, *, maximum: int = _MAX_U64) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WebRtcAdapterError(f"{field_name} must be a positive integer")
    if value > maximum:
        raise WebRtcAdapterError(f"{field_name} must not exceed {maximum}")
    return value


def _non_negative_int(field_name: str, value: object, *, maximum: int = _MAX_U64) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebRtcAdapterError(f"{field_name} must be an integer")
    if value < 0:
        raise WebRtcAdapterError(f"{field_name} must be non-negative")
    if value > maximum:
        raise WebRtcAdapterError(f"{field_name} must not exceed {maximum}")
    return value


def _optional_non_negative_int(field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    return _non_negative_int(field_name, value, maximum=_MAX_U16)


def _require_sdp(field_name: str, value: object) -> str:
    sdp = _require_non_empty(field_name, value)
    if any(
        (ord(character) < 0x20 and character not in "\t\r\n")
        or ord(character) == 0x7F
        for character in sdp
    ):
        raise WebRtcAdapterError(f"{field_name} contains invalid control characters")
    return sdp


@dataclass(frozen=True, slots=True)
class WebRtcSessionDescription:
    type: str
    sdp: str

    def __post_init__(self) -> None:
        if self.type not in {"offer", "answer", "pranswer"}:
            raise WebRtcAdapterError(f"unsupported session description type {self.type!r}")
        _require_sdp("sdp", self.sdp)

    def contract(self) -> dict[str, object]:
        return {"type": self.type, "sdp": self.sdp}


@dataclass(frozen=True, slots=True)
class WebRtcIceCandidate:
    candidate: str
    sdp_mid: str | None = None
    sdp_mline_index: int | None = None
    username_fragment: str | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        candidate = _require_non_empty("candidate", self.candidate)
        if candidate != candidate.strip() or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in candidate
        ):
            raise WebRtcAdapterError(
                "candidate must be an exact non-empty string"
            )
        if self.sdp_mid is not None:
            _require_exact_non_empty("sdp_mid", self.sdp_mid)
        object.__setattr__(
            self,
            "sdp_mline_index",
            _optional_non_negative_int("sdp_mline_index", self.sdp_mline_index),
        )
        if self.username_fragment is not None:
            _require_exact_non_empty(
                "username_fragment",
                self.username_fragment,
            )
        object.__setattr__(self, "sequence", _non_negative_int("sequence", self.sequence))

    def contract(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "sdpMid": self.sdp_mid,
            "sdpMLineIndex": self.sdp_mline_index,
            "usernameFragment": self.username_fragment,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class WebRtcSession:
    session_id: str
    peer_id: str
    offer: WebRtcSessionDescription
    answer: WebRtcSessionDescription | None = None
    ice_candidates: tuple[WebRtcIceCandidate, ...] = field(default_factory=tuple)
    codec: str = "opus"
    sample_rate_hz: int = 48_000
    channels: int = 1

    def __post_init__(self) -> None:
        _require_exact_non_empty("session_id", self.session_id)
        _require_exact_non_empty("peer_id", self.peer_id)
        if not isinstance(self.offer, WebRtcSessionDescription):
            raise WebRtcAdapterError("offer must be a WebRtcSessionDescription")
        if self.offer.type != "offer":
            raise WebRtcAdapterError("offer must have type 'offer'")
        if self.answer is not None:
            if not isinstance(self.answer, WebRtcSessionDescription):
                raise WebRtcAdapterError("answer must be a WebRtcSessionDescription")
            if self.answer.type not in {"answer", "pranswer"}:
                raise WebRtcAdapterError("answer must have type 'answer' or 'pranswer'")
        _require_exact_non_empty("codec", self.codec)
        object.__setattr__(
            self,
            "sample_rate_hz",
            _positive_int("sample_rate_hz", self.sample_rate_hz, maximum=_MAX_U32),
        )
        object.__setattr__(
            self,
            "channels",
            _positive_int("channels", self.channels, maximum=_MAX_U16),
        )
        try:
            ice_candidates = tuple(self.ice_candidates)
        except TypeError as error:
            raise WebRtcAdapterError("ice_candidates must be a sequence") from error
        if any(
            not isinstance(candidate, WebRtcIceCandidate)
            for candidate in ice_candidates
        ):
            raise WebRtcAdapterError(
                "ICE candidate entries must be WebRtcIceCandidate values"
            )
        object.__setattr__(
            self,
            "ice_candidates",
            tuple(
                sorted(
                    set(ice_candidates),
                    key=lambda candidate: (
                        candidate.sequence,
                        candidate.candidate,
                        candidate.sdp_mid or "",
                        candidate.sdp_mline_index if candidate.sdp_mline_index is not None else -1,
                        candidate.username_fragment or "",
                    ),
                )
            ),
        )

    def to_voice_transport(self) -> VoiceTransport:
        return VoiceTransport(
            "webrtc",
            uri=f"webrtc:{self.session_id}",
            codec=self.codec,
            sample_rate_hz=self.sample_rate_hz,
            channels=self.channels,
        )

    def signaling_contract(self) -> dict[str, object]:
        return {
            "sessionId": self.session_id,
            "peerId": self.peer_id,
            "offer": self.offer.contract(),
            "answer": self.answer.contract() if self.answer is not None else None,
            "iceCandidates": [candidate.contract() for candidate in self.ice_candidates],
            "transport": self.to_voice_transport().contract(),
        }

    def content_digest(self) -> str:
        return canonical_hash(self.signaling_contract())


__all__ = [
    "WebRtcAdapterError",
    "WebRtcIceCandidate",
    "WebRtcSession",
    "WebRtcSessionDescription",
]
