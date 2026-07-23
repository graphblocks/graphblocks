from __future__ import annotations

import importlib
from pathlib import Path

import pytest

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


def test_duplex_session_rejects_closed_transitions_and_records_interrupt_time(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    transport = graphblocks_voice.VoiceTransport.websocket("wss://voice.example.com/session")
    session = graphblocks_voice.DuplexSession(
        "session-1",
        transport,
        started_at_ms=100,
    )

    interrupted = session.interrupt(occurred_at_ms=125, reason="barge_in")

    assert interrupted.state == "interrupted"
    assert interrupted.interrupted_at_ms == 125
    assert interrupted.contract()["interruptedAtMs"] == 125
    resumed = interrupted.begin_turn("turn-2")
    assert resumed.interrupted_at_ms is None
    assert resumed.interruption_reason is None

    closed = interrupted.close(occurred_at_ms=150)
    for transition in (
        lambda: closed.begin_turn("turn-3"),
        lambda: closed.interrupt(occurred_at_ms=175, reason="late_barge_in"),
        lambda: closed.close(occurred_at_ms=200),
    ):
        try:
            transition()
        except graphblocks_voice.VoiceContractError:
            continue
        raise AssertionError("closed duplex sessions must be terminal")

    assert closed.state == "closed"
    assert closed.closed_at_ms == 150
    assert closed.interrupted_at_ms == 125

    try:
        interrupted.close(occurred_at_ms=124)
    except graphblocks_voice.VoiceContractError as error:
        assert "before session interruption" in str(error)
    else:
        raise AssertionError("session close cannot precede its interruption")


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


def test_voice_restored_session_state_is_consistent_and_detached(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    transport = graphblocks_voice.VoiceTransport.websocket(
        "wss://voice.example.com/session"
    )

    invalid_sessions = (
        lambda: graphblocks_voice.DuplexSession("session-1", object()),
        lambda: graphblocks_voice.DuplexSession(
            "session-1",
            transport,
            state="open",
            closed_at_ms=1,
        ),
        lambda: graphblocks_voice.DuplexSession(
            "session-1",
            transport,
            state="interrupted",
            interrupted_at_ms=1,
        ),
        lambda: graphblocks_voice.DuplexSession(
            "session-1",
            transport,
            state="closed",
        ),
        lambda: graphblocks_voice.DuplexSession(
            "session-1",
            transport,
            metadata={"attempt": 1},
        ),
    )
    for factory in invalid_sessions:
        with pytest.raises(graphblocks_voice.VoiceContractError):
            factory()

    metadata = {"tenant": "trusted"}
    session = graphblocks_voice.DuplexSession(
        "session-1",
        transport,
        metadata=metadata,
    )
    metadata["tenant"] = "caller-mutated"

    assert session.contract()["metadata"] == {"tenant": "trusted"}
    with pytest.raises(TypeError):
        session.metadata["tenant"] = "consumer-mutated"


def test_voice_decisions_and_realtime_request_reject_coercive_values(
    monkeypatch,
) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    transport = graphblocks_voice.VoiceTransport.websocket(
        "wss://voice.example.com/session"
    )
    session = graphblocks_voice.DuplexSession("session-1", transport)

    invalid_values = (
        lambda: graphblocks_voice.AudioFrame("mic", 1, 0, 20, True),
        lambda: graphblocks_voice.AudioFrame("mic", 1, 0, 20, float("nan")),
        lambda: graphblocks_voice.VadAuthority("vad", speech_threshold=True),
        lambda: graphblocks_voice.VadAuthority("vad").evaluate(
            graphblocks_voice.AudioFrame("mic", 1, 0, 20, 0.5),
            already_in_speech=1,
        ),
        lambda: graphblocks_voice.VadDecision("vad", "mic", True, "speech", 0.5),
        lambda: graphblocks_voice.VadDecision("vad", "mic", 1, "unknown", 0.5),
        lambda: graphblocks_voice.VadDecision("vad", "mic", 1, "speech", True),
        lambda: graphblocks_voice.InterruptionDecision(
            "classifier",
            "session-1",
            "continue",
            1,
            interrupted_playback_ids=("audio-1",),
        ),
        lambda: graphblocks_voice.InterruptionDecision(
            "classifier",
            "session-1",
            "interrupt",
            1,
            interrupted_playback_ids=("audio-1", "audio-1"),
        ),
        lambda: graphblocks_voice.InterruptionDecision(
            "classifier",
            "session-1",
            "interrupt",
            1,
            interrupted_playback_ids=(7,),
        ),
        lambda: graphblocks_voice.RealtimeSessionRequest(
            object(),
            "model",
            "instructions",
        ),
        lambda: graphblocks_voice.RealtimeSessionRequest(
            session,
            "model",
            "instructions",
            modalities="audio",
        ),
        lambda: graphblocks_voice.RealtimeSessionRequest(
            session,
            "model",
            "instructions",
            modalities=("audio", "audio"),
        ),
        lambda: graphblocks_voice.RealtimeSessionRequest(
            session,
            "model",
            "instructions",
            modalities=("video",),
        ),
        lambda: graphblocks_voice.RealtimeSessionRequest(
            session,
            "model",
            "instructions",
            tools="knowledge.search",
        ),
        lambda: graphblocks_voice.RealtimeSessionRequest(
            session,
            "model",
            "instructions",
            tools=("",),
        ),
    )
    for factory in invalid_values:
        with pytest.raises(graphblocks_voice.VoiceContractError):
            factory()

    request = graphblocks_voice.RealtimeSessionRequest(
        session,
        "model",
        "instructions",
    ).with_tool("knowledge.search")
    assert request.with_tool("knowledge.search") is request


def test_voice_interruption_replay_and_provider_time_are_monotonic(monkeypatch) -> None:
    graphblocks_voice = _import_voice(monkeypatch)
    transport = graphblocks_voice.VoiceTransport.websocket(
        "wss://voice.example.com/session"
    )
    interrupted = graphblocks_voice.DuplexSession(
        "session-1",
        transport,
        started_at_ms=10,
    ).interrupt(occurred_at_ms=20, reason="barge_in")

    assert interrupted.interrupt(occurred_at_ms=20, reason="barge_in") is interrupted
    with pytest.raises(
        graphblocks_voice.VoiceContractError,
        match="conflicts",
    ):
        interrupted.interrupt(occurred_at_ms=21, reason="different")

    interrupted_turn = graphblocks_voice.DuplexSession(
        "session-2",
        transport,
        started_at_ms=10,
    ).begin_turn("turn-1").interrupt(occurred_at_ms=20, reason="barge_in")
    with pytest.raises(
        graphblocks_voice.VoiceContractError,
        match="cannot replay",
    ):
        interrupted_turn.begin_turn("turn-1")
    resumed = interrupted_turn.begin_turn("turn-2")
    assert resumed.state == "open"
    assert resumed.current_turn_id == "turn-2"

    classifier = graphblocks_voice.InterruptionClassifier(
        "barge-in",
        provider_authority_id="provider",
    )
    with pytest.raises(
        graphblocks_voice.VoiceContractError,
        match="predates",
    ):
        classifier.classify(
            session_id="session-1",
            vad_decision=graphblocks_voice.VadDecision(
                "vad",
                "mic",
                1,
                "speech_start",
                0.9,
            ),
            playback=graphblocks_voice.PlaybackLedger(),
            occurred_at_ms=20,
            provider_decision=graphblocks_voice.ProviderInterruptionDecision(
                "provider",
                "session-1",
                "interrupt",
                19,
            ),
        )


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
