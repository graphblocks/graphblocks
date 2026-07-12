from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]


def _import_voice(monkeypatch):
    return importlib.import_module("graphblocks.voice")


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

    classifier = graphblocks_voice.InterruptionClassifier(
        "barge-in",
        provider_authority_id="provider-realtime",
    )
    advisory = classifier.classify(
        session_id="session-1",
        vad_decision=speech,
        playback=playback,
        occurred_at_ms=25,
    )
    provider_continue = classifier.classify(
        session_id="session-1",
        vad_decision=speech,
        playback=playback,
        occurred_at_ms=25,
        provider_decision=graphblocks_voice.ProviderInterruptionDecision(
            authority_id="provider-realtime",
            session_id="session-1",
            kind="continue",
            occurred_at_ms=26,
            reason="provider_turn_continues",
        ),
    )
    provider_interrupt = classifier.classify(
        session_id="session-1",
        vad_decision=silence,
        playback=playback,
        occurred_at_ms=27,
        provider_decision=graphblocks_voice.ProviderInterruptionDecision(
            authority_id="provider-realtime",
            session_id="session-1",
            kind="interrupt",
            occurred_at_ms=27,
            reason="provider_confirmed_barge_in",
        ),
    )

    assert silence.kind == "silence"
    assert speech.kind == "speech_start"
    assert advisory.kind == "continue"
    assert advisory.interrupted_playback_ids == ()
    assert advisory.reason == "awaiting_provider_confirmation"
    assert provider_continue.kind == "continue"
    assert provider_continue.reason == "provider_turn_continues"
    assert provider_interrupt.kind == "interrupt"
    assert provider_interrupt.interrupted_playback_ids == ("assistant-audio-1",)
    assert provider_interrupt.reason == "provider_confirmed_barge_in"


def test_voice_interruption_classifier_rejects_wrong_provider_authority_or_session(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    classifier = graphblocks_voice.InterruptionClassifier(
        "barge-in",
        provider_authority_id="provider-realtime",
    )
    vad = graphblocks_voice.VadDecision("vad-local", "mic", 1, "speech_start", 0.9)
    playback = graphblocks_voice.PlaybackLedger().append(
        graphblocks_voice.PlaybackEntry("audio-1", sequence=1, status="started", started_at_ms=0)
    )

    decisions = (
        graphblocks_voice.ProviderInterruptionDecision(
            "other-provider",
            "session-1",
            "interrupt",
            20,
        ),
        graphblocks_voice.ProviderInterruptionDecision(
            "provider-realtime",
            "other-session",
            "interrupt",
            20,
        ),
    )

    for provider_decision in decisions:
        try:
            classifier.classify(
                session_id="session-1",
                vad_decision=vad,
                playback=playback,
                occurred_at_ms=20,
                provider_decision=provider_decision,
            )
        except graphblocks_voice.VoiceContractError:
            continue
        raise AssertionError("provider interruption authority mismatch must fail closed")


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


def test_voice_playback_ledger_enforces_lifecycle_acknowledgement_and_idempotency(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    queued = graphblocks_voice.PlaybackEntry(
        "audio-1",
        sequence=1,
        status="queued",
        audio_ref="artifact://voice/audio-1",
    )
    ledger = graphblocks_voice.PlaybackLedger().append(queued)

    assert ledger.append(queued) == ledger
    started = ledger.start("audio-1", occurred_at_ms=10)
    assert started.entries[0].status == "started"
    assert started.entries[0].started_at_ms == 10
    completed = started.complete("audio-1", occurred_at_ms=30)
    assert completed.entries[0].status == "completed"
    assert completed.entries[0].completed_at_ms == 30
    acknowledged = completed.acknowledge("audio-1", occurred_at_ms=35)
    assert acknowledged.entries[0].acknowledged_at_ms == 35
    assert acknowledged.acknowledge("audio-1", occurred_at_ms=35) == acknowledged

    invalid_actions = (
        lambda: ledger.acknowledge("audio-1", occurred_at_ms=5),
        lambda: completed.acknowledge("audio-1", occurred_at_ms=29),
        lambda: acknowledged.acknowledge("audio-1", occurred_at_ms=36),
        lambda: ledger.append(
            graphblocks_voice.PlaybackEntry("audio-2", sequence=1, status="queued")
        ),
        lambda: ledger.append(
            graphblocks_voice.PlaybackEntry("audio-1", sequence=2, status="queued")
        ),
        lambda: ledger.append(
            graphblocks_voice.PlaybackEntry("audio-2", sequence=0, status="queued")
        ),
    )
    for action in invalid_actions:
        try:
            action()
        except graphblocks_voice.VoiceContractError:
            continue
        raise AssertionError("invalid playback mutation must fail closed")


def test_voice_playback_entries_reject_invalid_status_timestamp_combinations(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    invalid_entries = (
        lambda: graphblocks_voice.PlaybackEntry("audio-1", 1, "queued", started_at_ms=1),
        lambda: graphblocks_voice.PlaybackEntry("audio-1", 1, "started"),
        lambda: graphblocks_voice.PlaybackEntry("audio-1", 1, "completed", started_at_ms=10),
        lambda: graphblocks_voice.PlaybackEntry(
            "audio-1",
            1,
            "completed",
            started_at_ms=10,
            completed_at_ms=9,
        ),
        lambda: graphblocks_voice.PlaybackEntry(
            "audio-1",
            1,
            "interrupted",
            started_at_ms=10,
            completed_at_ms=20,
        ),
        lambda: graphblocks_voice.PlaybackEntry(
            "audio-1",
            1,
            "started",
            started_at_ms=10,
            acknowledged_at_ms=20,
        ),
    )
    for factory in invalid_entries:
        try:
            factory()
        except graphblocks_voice.VoiceContractError:
            continue
        raise AssertionError("invalid playback entry lifecycle must fail closed")


def test_voice_contracts_reject_boolean_timing_and_sequence_fields(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    transport = graphblocks_voice.VoiceTransport.websocket("wss://voice.example.com/session")

    cases = (
        lambda: graphblocks_voice.VoiceTransport("websocket", "wss://voice.example.com/session", sample_rate_hz=True),
        lambda: graphblocks_voice.VoiceTransport("websocket", "wss://voice.example.com/session", channels=True),
        lambda: graphblocks_voice.DuplexSession("session-1", transport, started_at_ms=True),
        lambda: graphblocks_voice.AudioFrame("mic", sequence=True, start_ms=0, duration_ms=20, speech_probability=0.2),
        lambda: graphblocks_voice.AudioFrame("mic", sequence=1, start_ms=True, duration_ms=20, speech_probability=0.2),
        lambda: graphblocks_voice.AudioFrame("mic", sequence=1, start_ms=0, duration_ms=True, speech_probability=0.2),
        lambda: graphblocks_voice.PlaybackEntry("audio-1", sequence=True, status="started"),
        lambda: graphblocks_voice.PlaybackLedger().interrupt_active(occurred_at_ms=True, reason="barge_in"),
        lambda: graphblocks_voice.InterruptionDecision("classifier-1", "session-1", "continue", True),
        lambda: graphblocks_voice.InterruptionClassifier("barge-in").classify(
            session_id="session-1",
            vad_decision=graphblocks_voice.VadDecision("vad-local", "mic", 1, "speech", 0.9),
            playback=graphblocks_voice.PlaybackLedger(),
            occurred_at_ms=True,
        ),
    )

    for factory in cases:
        try:
            factory()
        except graphblocks_voice.VoiceContractError:
            continue
        raise AssertionError("expected VoiceContractError")


def test_voice_package_is_cataloged_as_optional_extension(monkeypatch) -> None:
    _import_voice(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-voice"] == {
        "component": "graphblocks-voice",
        "artifact": "graphblocks",
        "distribution": "graphblocks-voice",
        "import": "graphblocks.voice",
        "default": False,
        "layer": "voice",
        "kind": "pure_python",
        "implementationPhase": 7,
        "stability": "experimental-extension",
    }
