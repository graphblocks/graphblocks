from __future__ import annotations

import importlib

import pytest

from graphblocks.packages import load_package_catalog, package_rows


def _import_silero_vad(monkeypatch):
    return importlib.import_module("graphblocks.integrations.silero_vad")


def test_silero_vad_frame_projects_to_core_audio_frame(monkeypatch) -> None:
    graphblocks_silero_vad = _import_silero_vad(monkeypatch)
    frame = graphblocks_silero_vad.SileroVadFrame(
        stream_id="mic",
        sequence=7,
        start_ms=140,
        duration_ms=20,
        speech_probability=0.83,
        sample_rate_hz=16_000,
        window_size_samples=512,
    )

    audio_frame = frame.to_audio_frame()

    assert audio_frame.stream_id == "mic"
    assert audio_frame.sequence == 7
    assert audio_frame.speech_probability == 0.83
    assert frame.contract() == {
        "streamId": "mic",
        "sequence": 7,
        "startMs": 140,
        "durationMs": 20,
        "speechProbability": 0.83,
        "sampleRateHz": 16000,
        "windowSizeSamples": 512,
        "modelRef": "silero-vad",
    }


def test_silero_vad_authority_uses_core_vad_decisions(monkeypatch) -> None:
    graphblocks_silero_vad = _import_silero_vad(monkeypatch)
    authority = graphblocks_silero_vad.SileroVadAuthority(
        authority_id="silero-local",
        speech_threshold=0.6,
        min_speech_ms=40,
        min_silence_ms=60,
    )

    silence = authority.evaluate(
        graphblocks_silero_vad.SileroVadFrame("mic", 1, 0, 20, 0.2),
        already_in_speech=False,
    )
    speech = authority.evaluate(
        graphblocks_silero_vad.SileroVadFrame("mic", 2, 20, 20, 0.9),
        already_in_speech=False,
    )

    assert silence.kind == "silence"
    assert speech.kind == "speech_start"
    assert authority.config_contract() == {
        "authorityId": "silero-local",
        "speechThreshold": 0.6,
        "minSpeechMs": 40,
        "minSilenceMs": 60,
        "sampleRateHz": 16000,
        "windowSizeSamples": 512,
        "modelRef": "silero-vad",
    }


def test_silero_vad_validates_runtime_free_contract(monkeypatch) -> None:
    graphblocks_silero_vad = _import_silero_vad(monkeypatch)

    with pytest.raises(graphblocks_silero_vad.SileroVadAdapterError):
        graphblocks_silero_vad.SileroVadFrame("mic", 1, 0, 20, 1.5)
    with pytest.raises(graphblocks_silero_vad.SileroVadAdapterError):
        graphblocks_silero_vad.SileroVadAuthority("silero-local", min_speech_ms=0)


def test_silero_vad_package_is_cataloged_as_optional_voice_adapter(monkeypatch) -> None:
    _import_silero_vad(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-silero-vad"] == {
        "distribution": "graphblocks-silero-vad",
        "artifact": "graphblocks",
        "component": "graphblocks-silero-vad",
        "import": "graphblocks.integrations.silero_vad",
        "default": False,
        "layer": "voice_vad_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
