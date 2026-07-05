from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from graphblocks.canonical import canonical_hash


VoiceTransportKind = Literal["websocket", "webrtc", "provider_realtime"]
VoiceSessionState = Literal["open", "interrupted", "closed"]
VadDecisionKind = Literal["silence", "speech_start", "speech", "speech_end"]
PlaybackStatus = Literal["queued", "started", "completed", "interrupted"]
InterruptionKind = Literal["continue", "interrupt"]


class VoiceContractError(ValueError):
    """Raised when a voice contract is invalid."""


def _require_non_empty(field_name: str, value: str) -> None:
    if not value.strip():
        raise VoiceContractError(f"{field_name} must not be empty")


def _positive_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VoiceContractError(f"{field_name} must be an integer")
    if value <= 0:
        raise VoiceContractError(f"{field_name} must be positive")
    return value


def _non_negative_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VoiceContractError(f"{field_name} must be an integer")
    if value < 0:
        raise VoiceContractError(f"{field_name} must be non-negative")
    return value


def _optional_non_negative_int(field_name: str, value: object | None) -> int | None:
    if value is None:
        return None
    return _non_negative_int(field_name, value)


@dataclass(frozen=True, slots=True)
class VoiceTransport:
    kind: VoiceTransportKind
    uri: str | None = None
    codec: str = "pcm16"
    sample_rate_hz: int = 24_000
    channels: int = 1

    def __post_init__(self) -> None:
        if self.kind not in {"websocket", "webrtc", "provider_realtime"}:
            raise VoiceContractError(f"unsupported voice transport kind {self.kind!r}")
        if self.uri is not None:
            _require_non_empty("transport uri", self.uri)
        _require_non_empty("transport codec", self.codec)
        object.__setattr__(self, "sample_rate_hz", _positive_int("sample_rate_hz", self.sample_rate_hz))
        object.__setattr__(self, "channels", _positive_int("channels", self.channels))

    @classmethod
    def websocket(
        cls,
        uri: str,
        *,
        codec: str = "pcm16",
        sample_rate_hz: int = 24_000,
        channels: int = 1,
    ) -> VoiceTransport:
        return cls("websocket", uri=uri, codec=codec, sample_rate_hz=sample_rate_hz, channels=channels)

    def contract(self) -> dict[str, object]:
        contract: dict[str, object] = {
            "kind": self.kind,
            "uri": self.uri,
            "codec": self.codec,
            "sampleRateHz": self.sample_rate_hz,
            "channels": self.channels,
        }
        return contract


@dataclass(frozen=True, slots=True)
class DuplexSession:
    session_id: str
    transport: VoiceTransport
    state: VoiceSessionState = "open"
    current_turn_id: str | None = None
    started_at_ms: int = 0
    closed_at_ms: int | None = None
    interruption_reason: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty("session_id", self.session_id)
        if self.state not in {"open", "interrupted", "closed"}:
            raise VoiceContractError(f"unsupported voice session state {self.state!r}")
        object.__setattr__(self, "started_at_ms", _non_negative_int("started_at_ms", self.started_at_ms))
        object.__setattr__(self, "closed_at_ms", _optional_non_negative_int("closed_at_ms", self.closed_at_ms))
        if self.closed_at_ms is not None and self.closed_at_ms < self.started_at_ms:
            raise VoiceContractError("closed_at_ms must be greater than or equal to started_at_ms")
        object.__setattr__(
            self,
            "metadata",
            {str(key): str(value) for key, value in sorted(dict(self.metadata).items())},
        )

    def begin_turn(self, turn_id: str) -> DuplexSession:
        _require_non_empty("turn_id", turn_id)
        return replace(self, current_turn_id=turn_id, state="open")

    def interrupt(self, *, occurred_at_ms: int, reason: str) -> DuplexSession:
        occurred_at_ms = _non_negative_int("occurred_at_ms", occurred_at_ms)
        if occurred_at_ms < self.started_at_ms:
            raise VoiceContractError("interruption occurred before session start")
        _require_non_empty("interruption reason", reason)
        return replace(self, state="interrupted", closed_at_ms=None, interruption_reason=reason)

    def close(self, *, occurred_at_ms: int) -> DuplexSession:
        occurred_at_ms = _non_negative_int("occurred_at_ms", occurred_at_ms)
        if occurred_at_ms < self.started_at_ms:
            raise VoiceContractError("close occurred before session start")
        return replace(self, state="closed", closed_at_ms=occurred_at_ms)

    def contract(self) -> dict[str, object]:
        return {
            "sessionId": self.session_id,
            "state": self.state,
            "currentTurnId": self.current_turn_id,
            "startedAtMs": self.started_at_ms,
            "closedAtMs": self.closed_at_ms,
            "interruptionReason": self.interruption_reason,
            "transport": self.transport.contract(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AudioFrame:
    stream_id: str
    sequence: int
    start_ms: int
    duration_ms: int
    speech_probability: float

    def __post_init__(self) -> None:
        _require_non_empty("stream_id", self.stream_id)
        object.__setattr__(self, "sequence", _non_negative_int("sequence", self.sequence))
        object.__setattr__(self, "start_ms", _non_negative_int("start_ms", self.start_ms))
        object.__setattr__(self, "duration_ms", _positive_int("duration_ms", self.duration_ms))
        if not 0 <= self.speech_probability <= 1:
            raise VoiceContractError("speech_probability must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class VadDecision:
    authority_id: str
    stream_id: str
    sequence: int
    kind: VadDecisionKind
    speech_probability: float


@dataclass(frozen=True, slots=True)
class VadAuthority:
    authority_id: str
    speech_threshold: float = 0.5

    def __post_init__(self) -> None:
        _require_non_empty("authority_id", self.authority_id)
        if not 0 <= self.speech_threshold <= 1:
            raise VoiceContractError("speech_threshold must be between 0 and 1")

    def evaluate(self, frame: AudioFrame, *, already_in_speech: bool = False) -> VadDecision:
        if frame.speech_probability >= self.speech_threshold:
            kind: VadDecisionKind = "speech" if already_in_speech else "speech_start"
        else:
            kind = "speech_end" if already_in_speech else "silence"
        return VadDecision(
            authority_id=self.authority_id,
            stream_id=frame.stream_id,
            sequence=frame.sequence,
            kind=kind,
            speech_probability=frame.speech_probability,
        )


@dataclass(frozen=True, slots=True)
class PlaybackEntry:
    playback_id: str
    sequence: int
    status: PlaybackStatus
    audio_ref: str | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("playback_id", self.playback_id)
        object.__setattr__(self, "sequence", _non_negative_int("playback sequence", self.sequence))
        object.__setattr__(self, "started_at_ms", _optional_non_negative_int("started_at_ms", self.started_at_ms))
        object.__setattr__(self, "completed_at_ms", _optional_non_negative_int("completed_at_ms", self.completed_at_ms))
        if self.status not in {"queued", "started", "completed", "interrupted"}:
            raise VoiceContractError(f"unsupported playback status {self.status!r}")

    def contract(self) -> dict[str, object]:
        return {
            "playbackId": self.playback_id,
            "sequence": self.sequence,
            "status": self.status,
            "audioRef": self.audio_ref,
            "startedAtMs": self.started_at_ms,
            "completedAtMs": self.completed_at_ms,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PlaybackLedger:
    entries: tuple[PlaybackEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda entry: entry.sequence)))

    def append(self, entry: PlaybackEntry) -> PlaybackLedger:
        return replace(self, entries=(*self.entries, entry))

    def active_playback_ids(self) -> tuple[str, ...]:
        return tuple(entry.playback_id for entry in self.entries if entry.status == "started")

    def interrupt_active(self, *, occurred_at_ms: int, reason: str) -> PlaybackLedger:
        occurred_at_ms = _non_negative_int("occurred_at_ms", occurred_at_ms)
        _require_non_empty("interruption reason", reason)
        return replace(
            self,
            entries=tuple(
                replace(entry, status="interrupted", completed_at_ms=occurred_at_ms, reason=reason)
                if entry.status == "started"
                else entry
                for entry in self.entries
            ),
        )

    def content_digest(self) -> str:
        return canonical_hash({"entries": [entry.contract() for entry in self.entries]})


@dataclass(frozen=True, slots=True)
class InterruptionDecision:
    classifier_id: str
    session_id: str
    kind: InterruptionKind
    occurred_at_ms: int
    interrupted_playback_ids: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("classifier_id", self.classifier_id)
        _require_non_empty("session_id", self.session_id)
        if self.kind not in {"continue", "interrupt"}:
            raise VoiceContractError(f"unsupported interruption kind {self.kind!r}")
        object.__setattr__(self, "occurred_at_ms", _non_negative_int("occurred_at_ms", self.occurred_at_ms))
        object.__setattr__(self, "interrupted_playback_ids", tuple(str(item) for item in self.interrupted_playback_ids))
        if self.reason is not None:
            _require_non_empty("interruption reason", self.reason)


@dataclass(frozen=True, slots=True)
class InterruptionClassifier:
    classifier_id: str

    def __post_init__(self) -> None:
        _require_non_empty("classifier_id", self.classifier_id)

    def classify(
        self,
        *,
        session_id: str,
        vad_decision: VadDecision,
        playback: PlaybackLedger,
        occurred_at_ms: int,
    ) -> InterruptionDecision:
        _require_non_empty("session_id", session_id)
        occurred_at_ms = _non_negative_int("occurred_at_ms", occurred_at_ms)
        active_ids = playback.active_playback_ids()
        if active_ids and vad_decision.kind in {"speech_start", "speech"}:
            return InterruptionDecision(
                classifier_id=self.classifier_id,
                session_id=session_id,
                kind="interrupt",
                occurred_at_ms=occurred_at_ms,
                interrupted_playback_ids=active_ids,
                reason="user_speech_during_playback",
            )
        return InterruptionDecision(
            classifier_id=self.classifier_id,
            session_id=session_id,
            kind="continue",
            occurred_at_ms=occurred_at_ms,
        )


@dataclass(frozen=True, slots=True)
class RealtimeSessionRequest:
    session: DuplexSession
    model: str
    instructions: str
    modalities: tuple[str, ...] = ("audio",)
    tools: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_empty("model", self.model)
        _require_non_empty("instructions", self.instructions)
        object.__setattr__(self, "modalities", tuple(sorted({str(item) for item in self.modalities})))
        object.__setattr__(self, "tools", tuple(sorted({str(tool) for tool in self.tools})))

    def with_tool(self, tool_name: str) -> RealtimeSessionRequest:
        _require_non_empty("tool_name", tool_name)
        return replace(self, tools=(*self.tools, tool_name))

    def provider_contract(self) -> dict[str, object]:
        return {
            "sessionId": self.session.session_id,
            "model": self.model,
            "instructions": self.instructions,
            "modalities": list(self.modalities),
            "transport": self.session.transport.contract(),
            "tools": list(self.tools),
            "turnId": self.session.current_turn_id,
        }


__all__ = [
    "AudioFrame",
    "DuplexSession",
    "InterruptionClassifier",
    "InterruptionDecision",
    "PlaybackEntry",
    "PlaybackLedger",
    "PlaybackStatus",
    "RealtimeSessionRequest",
    "VadAuthority",
    "VadDecision",
    "VadDecisionKind",
    "VoiceContractError",
    "VoiceSessionState",
    "VoiceTransport",
    "VoiceTransportKind",
]
