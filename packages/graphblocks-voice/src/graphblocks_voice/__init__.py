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
        if self.sample_rate_hz <= 0:
            raise VoiceContractError("sample_rate_hz must be positive")
        if self.channels <= 0:
            raise VoiceContractError("channels must be positive")

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
        if self.started_at_ms < 0:
            raise VoiceContractError("started_at_ms must be non-negative")
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
        if occurred_at_ms < self.started_at_ms:
            raise VoiceContractError("interruption occurred before session start")
        _require_non_empty("interruption reason", reason)
        return replace(self, state="interrupted", closed_at_ms=None, interruption_reason=reason)

    def close(self, *, occurred_at_ms: int) -> DuplexSession:
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
        if self.sequence < 0:
            raise VoiceContractError("sequence must be non-negative")
        if self.start_ms < 0:
            raise VoiceContractError("start_ms must be non-negative")
        if self.duration_ms <= 0:
            raise VoiceContractError("duration_ms must be positive")
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
        if self.sequence < 0:
            raise VoiceContractError("playback sequence must be non-negative")
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
        if occurred_at_ms < 0:
            raise VoiceContractError("occurred_at_ms must be non-negative")
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
