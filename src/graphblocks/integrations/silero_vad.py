from __future__ import annotations

from dataclasses import dataclass, field
import math
from threading import RLock

from graphblocks.voice import AudioFrame, VadDecision, VadDecisionKind


class SileroVadAdapterError(ValueError):
    """Base error for Silero VAD adapter contracts."""


_MAX_U32 = (1 << 32) - 1
_MAX_U64 = (1 << 64) - 1


def _require_non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SileroVadAdapterError(f"{field_name} must not be empty")
    if any("\ud800" <= character <= "\udfff" for character in value):
        raise SileroVadAdapterError(
            f"{field_name} must contain Unicode scalar values"
        )
    return value


def _require_exact_non_empty(field_name: str, value: object) -> str:
    normalized = _require_non_empty(field_name, value)
    if normalized != normalized.strip() or any(
        character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        for character in normalized
    ):
        raise SileroVadAdapterError(
            f"{field_name} must be an exact non-empty string"
        )
    return normalized


def _integer(
    field_name: str,
    value: object,
    *,
    positive: bool = False,
    maximum: int = _MAX_U64,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SileroVadAdapterError(f"{field_name} must be an integer")
    if (positive and value <= 0) or (not positive and value < 0):
        requirement = "positive" if positive else "non-negative"
        raise SileroVadAdapterError(f"{field_name} must be {requirement}")
    if value > maximum:
        raise SileroVadAdapterError(f"{field_name} must not exceed {maximum}")
    return value


def _probability(field_name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SileroVadAdapterError(f"{field_name} must be numeric")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise SileroVadAdapterError(f"{field_name} must be between 0 and 1") from error
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise SileroVadAdapterError(f"{field_name} must be between 0 and 1")
    return normalized


def _validate_sample_window(sample_rate_hz: int, window_size_samples: int) -> None:
    if (sample_rate_hz, window_size_samples) not in {(8_000, 256), (16_000, 512)}:
        raise SileroVadAdapterError(
            "Silero VAD requires 16000 Hz/512 samples or 8000 Hz/256 samples"
        )


@dataclass(slots=True)
class _SileroVadStreamState:
    in_speech: bool = False
    pending_speech_ms: int = 0
    pending_silence_ms: int = 0
    last_sequence: int | None = None
    last_end_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SileroVadFrame:
    stream_id: str
    sequence: int
    start_ms: int
    duration_ms: int
    speech_probability: float
    sample_rate_hz: int = 16_000
    window_size_samples: int = 512
    model_ref: str = "silero-vad"

    def __post_init__(self) -> None:
        _require_exact_non_empty("stream_id", self.stream_id)
        object.__setattr__(self, "sequence", _integer("sequence", self.sequence))
        object.__setattr__(self, "start_ms", _integer("start_ms", self.start_ms))
        object.__setattr__(
            self, "duration_ms", _integer("duration_ms", self.duration_ms, positive=True)
        )
        object.__setattr__(
            self,
            "speech_probability",
            _probability("speech_probability", self.speech_probability),
        )
        object.__setattr__(
            self,
            "sample_rate_hz",
            _integer(
                "sample_rate_hz",
                self.sample_rate_hz,
                positive=True,
                maximum=_MAX_U32,
            ),
        )
        object.__setattr__(
            self,
            "window_size_samples",
            _integer(
                "window_size_samples",
                self.window_size_samples,
                positive=True,
                maximum=_MAX_U32,
            ),
        )
        _validate_sample_window(self.sample_rate_hz, self.window_size_samples)
        if self.start_ms > _MAX_U64 - self.duration_ms:
            raise SileroVadAdapterError(
                "frame end must not exceed unsigned 64-bit range"
            )
        expected_duration_ms = self.window_size_samples * 1_000 // self.sample_rate_hz
        if self.duration_ms != expected_duration_ms:
            raise SileroVadAdapterError(
                f"duration_ms must be {expected_duration_ms} for the configured Silero window"
            )
        _require_exact_non_empty("model_ref", self.model_ref)

    def to_audio_frame(self) -> AudioFrame:
        return AudioFrame(
            stream_id=self.stream_id,
            sequence=self.sequence,
            start_ms=self.start_ms,
            duration_ms=self.duration_ms,
            speech_probability=self.speech_probability,
        )

    def contract(self) -> dict[str, object]:
        return {
            "streamId": self.stream_id,
            "sequence": self.sequence,
            "startMs": self.start_ms,
            "durationMs": self.duration_ms,
            "speechProbability": self.speech_probability,
            "sampleRateHz": self.sample_rate_hz,
            "windowSizeSamples": self.window_size_samples,
            "modelRef": self.model_ref,
        }


@dataclass(frozen=True, slots=True)
class SileroVadAuthority:
    authority_id: str
    speech_threshold: float = 0.5
    min_speech_ms: int = 40
    min_silence_ms: int = 80
    sample_rate_hz: int = 16_000
    window_size_samples: int = 512
    model_ref: str = "silero-vad"
    _states: dict[str, _SileroVadStreamState] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_exact_non_empty("authority_id", self.authority_id)
        object.__setattr__(
            self, "speech_threshold", _probability("speech_threshold", self.speech_threshold)
        )
        for field_name in (
            "min_speech_ms",
            "min_silence_ms",
            "sample_rate_hz",
            "window_size_samples",
        ):
            object.__setattr__(
                self,
                field_name,
                _integer(field_name, getattr(self, field_name), positive=True),
            )
        _validate_sample_window(self.sample_rate_hz, self.window_size_samples)
        _require_exact_non_empty("model_ref", self.model_ref)

    def evaluate(
        self,
        frame: SileroVadFrame,
        *,
        already_in_speech: bool | None = None,
    ) -> VadDecision:
        if not isinstance(frame, SileroVadFrame):
            raise SileroVadAdapterError("frame must be a SileroVadFrame")
        if already_in_speech is not None and not isinstance(already_in_speech, bool):
            raise SileroVadAdapterError("already_in_speech must be a boolean")
        if (frame.sample_rate_hz, frame.window_size_samples) != (
            self.sample_rate_hz,
            self.window_size_samples,
        ):
            raise SileroVadAdapterError("frame sample rate and window must match VAD authority")
        with self._lock:
            state = self._states.get(frame.stream_id)
            if state is None:
                state = _SileroVadStreamState(in_speech=already_in_speech or False)
                self._states[frame.stream_id] = state
            elif state.last_sequence is not None and frame.sequence <= state.last_sequence:
                raise SileroVadAdapterError(
                    "frame sequence must increase within a Silero VAD stream"
                )
            elif state.last_end_ms is not None and frame.start_ms < state.last_end_ms:
                raise SileroVadAdapterError(
                    "frame timing must not overlap within a Silero VAD stream"
                )
            elif already_in_speech is not None and already_in_speech != state.in_speech:
                state.in_speech = already_in_speech
                state.pending_speech_ms = 0
                state.pending_silence_ms = 0
            if state.last_end_ms is not None and frame.start_ms > state.last_end_ms:
                state.pending_speech_ms = 0
                state.pending_silence_ms = 0

            is_speech = frame.speech_probability >= self.speech_threshold
            kind: VadDecisionKind
            if state.in_speech:
                state.pending_speech_ms = 0
                if is_speech:
                    state.pending_silence_ms = 0
                    kind = "speech"
                else:
                    state.pending_silence_ms += frame.duration_ms
                    if state.pending_silence_ms >= self.min_silence_ms:
                        state.in_speech = False
                        state.pending_silence_ms = 0
                        kind = "speech_end"
                    else:
                        kind = "speech"
            else:
                state.pending_silence_ms = 0
                if is_speech:
                    state.pending_speech_ms += frame.duration_ms
                    if state.pending_speech_ms >= self.min_speech_ms:
                        state.in_speech = True
                        state.pending_speech_ms = 0
                        kind = "speech_start"
                    else:
                        kind = "silence"
                else:
                    state.pending_speech_ms = 0
                    kind = "silence"
            state.last_sequence = frame.sequence
            state.last_end_ms = frame.start_ms + frame.duration_ms

        return VadDecision(
            authority_id=self.authority_id,
            stream_id=frame.stream_id,
            sequence=frame.sequence,
            kind=kind,
            speech_probability=frame.speech_probability,
        )

    def config_contract(self) -> dict[str, object]:
        return {
            "authorityId": self.authority_id,
            "speechThreshold": self.speech_threshold,
            "minSpeechMs": self.min_speech_ms,
            "minSilenceMs": self.min_silence_ms,
            "sampleRateHz": self.sample_rate_hz,
            "windowSizeSamples": self.window_size_samples,
            "modelRef": self.model_ref,
        }


__all__ = [
    "AudioFrame",
    "SileroVadAdapterError",
    "SileroVadAuthority",
    "SileroVadFrame",
    "VadDecision",
]
