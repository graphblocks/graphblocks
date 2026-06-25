from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]


def _import_voice(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-voice" / "src"))
    return importlib.import_module("graphblocks_voice")


def test_voice_package_tracks_duplex_session_and_realtime_request(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)

    transport = graphblocks_voice.VoiceTransport.websocket(
        "wss://voice.example.com/session",
        codec="pcm16",
        sample_rate_hz=24_000,
    )
    session = graphblocks_voice.DuplexSession("session-1", transport).begin_turn("turn-1")
    request = graphblocks_voice.RealtimeSessionRequest(
        session=session,
        model="realtime-support",
        instructions="Answer with concise support guidance.",
        modalities=("audio", "text"),
    ).with_tool("knowledge.search")

    contract = request.provider_contract()

    assert session.state == "open"
    assert session.current_turn_id == "turn-1"
    assert contract["sessionId"] == "session-1"
    assert contract["transport"] == {
        "kind": "websocket",
        "uri": "wss://voice.example.com/session",
        "codec": "pcm16",
        "sampleRateHz": 24000,
        "channels": 1,
    }
    assert contract["tools"] == ["knowledge.search"]
    assert "DuplexSession" in graphblocks_voice.__all__


def test_voice_vad_authority_and_interruption_classifier(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)

    authority = graphblocks_voice.VadAuthority("vad-local", speech_threshold=0.6)
    silence = authority.evaluate(graphblocks_voice.AudioFrame("mic", sequence=1, start_ms=0, duration_ms=20, speech_probability=0.2))
    speech = authority.evaluate(graphblocks_voice.AudioFrame("mic", sequence=2, start_ms=20, duration_ms=20, speech_probability=0.9))
    playback = graphblocks_voice.PlaybackLedger().append(
        graphblocks_voice.PlaybackEntry("assistant-audio-1", sequence=1, status="started", started_at_ms=0)
    )

    decision = graphblocks_voice.InterruptionClassifier("barge-in").classify(
        session_id="session-1",
        vad_decision=speech,
        playback=playback,
        occurred_at_ms=25,
    )

    assert silence.kind == "silence"
    assert speech.kind == "speech_start"
    assert decision.kind == "interrupt"
    assert decision.interrupted_playback_ids == ("assistant-audio-1",)
    assert decision.reason == "user_speech_during_playback"


def test_voice_playback_ledger_marks_active_items_interrupted(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    ledger = (
        graphblocks_voice.PlaybackLedger()
        .append(graphblocks_voice.PlaybackEntry("audio-1", sequence=1, status="completed", started_at_ms=0, completed_at_ms=100))
        .append(graphblocks_voice.PlaybackEntry("audio-2", sequence=2, status="started", started_at_ms=110))
    )

    interrupted = ledger.interrupt_active(occurred_at_ms=150, reason="barge_in")

    assert interrupted.entries[0].status == "completed"
    assert interrupted.entries[1].status == "interrupted"
    assert interrupted.entries[1].completed_at_ms == 150
    assert interrupted.entries[1].reason == "barge_in"
    assert interrupted.content_digest().startswith("sha256:")


def test_voice_package_is_cataloged_as_optional_extension(monkeypatch) -> None:
    _import_voice(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-voice"] == {
        "distribution": "graphblocks-voice",
        "import": "graphblocks_voice",
        "default": False,
        "layer": "voice",
        "kind": "pure_python",
        "implementationPhase": 7,
        "stability": "experimental-extension",
    }
