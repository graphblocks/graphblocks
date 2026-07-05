from __future__ import annotations

from dataclasses import dataclass, field

from graphblocks.canonical import canonical_hash
from graphblocks_voice import VoiceTransport


class WebRtcAdapterError(ValueError):
    """Base error for WebRTC voice adapter contracts."""


def _require_non_empty(field_name: str, value: str) -> None:
    if not value.strip():
        raise WebRtcAdapterError(f"{field_name} must not be empty")


def _non_negative_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebRtcAdapterError(f"{field_name} must be an integer")
    if value < 0:
        raise WebRtcAdapterError(f"{field_name} must be non-negative")
    return value


def _optional_non_negative_int(field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    return _non_negative_int(field_name, value)


@dataclass(frozen=True, slots=True)
class WebRtcSessionDescription:
    type: str
    sdp: str

    def __post_init__(self) -> None:
        if self.type not in {"offer", "answer", "pranswer"}:
            raise WebRtcAdapterError(f"unsupported session description type {self.type!r}")
        _require_non_empty("sdp", self.sdp)

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
        _require_non_empty("candidate", self.candidate)
        if self.sdp_mid is not None:
            _require_non_empty("sdp_mid", self.sdp_mid)
        object.__setattr__(
            self,
            "sdp_mline_index",
            _optional_non_negative_int("sdp_mline_index", self.sdp_mline_index),
        )
        if self.username_fragment is not None:
            _require_non_empty("username_fragment", self.username_fragment)
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
        _require_non_empty("session_id", self.session_id)
        _require_non_empty("peer_id", self.peer_id)
        if self.offer.type != "offer":
            raise WebRtcAdapterError("offer must have type 'offer'")
        if self.answer is not None and self.answer.type not in {"answer", "pranswer"}:
            raise WebRtcAdapterError("answer must have type 'answer' or 'pranswer'")
        _require_non_empty("codec", self.codec)
        if self.sample_rate_hz <= 0:
            raise WebRtcAdapterError("sample_rate_hz must be positive")
        if self.channels <= 0:
            raise WebRtcAdapterError("channels must be positive")
        object.__setattr__(
            self,
            "ice_candidates",
            tuple(sorted(self.ice_candidates, key=lambda candidate: candidate.sequence)),
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
