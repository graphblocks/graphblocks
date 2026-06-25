from __future__ import annotations

from dataclasses import dataclass

from graphblocks_voice import AudioFrame, VadAuthority, VadDecision


class SileroVadAdapterError(ValueError):
    """Base error for Silero VAD adapter contracts."""


def _require_non_empty(field_name: str, value: str) -> None:
    if not value.strip():
        raise SileroVadAdapterError(f"{field_name} must not be empty")


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
        _require_non_empty("stream_id", self.stream_id)
        if self.sequence < 0:
            raise SileroVadAdapterError("sequence must be non-negative")
        if self.start_ms < 0:
            raise SileroVadAdapterError("start_ms must be non-negative")
        if self.duration_ms <= 0:
            raise SileroVadAdapterError("duration_ms must be positive")
        if not 0 <= self.speech_probability <= 1:
            raise SileroVadAdapterError("speech_probability must be between 0 and 1")
        if self.sample_rate_hz <= 0:
            raise SileroVadAdapterError("sample_rate_hz must be positive")
        if self.window_size_samples <= 0:
            raise SileroVadAdapterError("window_size_samples must be positive")
        _require_non_empty("model_ref", self.model_ref)

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

    def __post_init__(self) -> None:
        _require_non_empty("authority_id", self.authority_id)
        if not 0 <= self.speech_threshold <= 1:
            raise SileroVadAdapterError("speech_threshold must be between 0 and 1")
        if self.min_speech_ms <= 0:
            raise SileroVadAdapterError("min_speech_ms must be positive")
        if self.min_silence_ms <= 0:
            raise SileroVadAdapterError("min_silence_ms must be positive")
        if self.sample_rate_hz <= 0:
            raise SileroVadAdapterError("sample_rate_hz must be positive")
        if self.window_size_samples <= 0:
            raise SileroVadAdapterError("window_size_samples must be positive")
        _require_non_empty("model_ref", self.model_ref)

    def evaluate(self, frame: SileroVadFrame, *, already_in_speech: bool = False) -> VadDecision:
        authority = VadAuthority(self.authority_id, speech_threshold=self.speech_threshold)
        return authority.evaluate(frame.to_audio_frame(), already_in_speech=already_in_speech)

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
