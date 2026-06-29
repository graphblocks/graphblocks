use std::collections::BTreeMap;
use std::error::Error;

use graphblocks_runtime_core::voice::{
    AudioFrame, DuplexSession, InterruptionClassifier, InterruptionKind, PlaybackEntry,
    PlaybackLedger, PlaybackStatus, RealtimeSessionRequest, VadAuthority, VadDecisionKind,
    VoiceContractError, VoiceSessionState, VoiceTransport, VoiceTransportKind,
};

#[test]
fn voice_session_request_contract_tracks_turn_modalities_and_tools() -> Result<(), Box<dyn Error>> {
    let transport = VoiceTransport::websocket("wss://voice.example.com/session")?
        .with_codec("pcm16")?
        .with_sample_rate_hz(24_000)?;
    let session = DuplexSession::new("session-1", transport)?.begin_turn("turn-1")?;
    let request = RealtimeSessionRequest::new(
        session.clone(),
        "realtime-support",
        "Answer with concise support guidance.",
    )?
    .with_modalities(["text", "audio"])?
    .with_tools(["ticket.create", "knowledge.search"])?;
    let contract = request.provider_contract();

    assert_eq!(session.state, VoiceSessionState::Open);
    assert_eq!(session.current_turn_id.as_deref(), Some("turn-1"));
    assert_eq!(contract["sessionId"], "session-1");
    assert_eq!(contract["turnId"], "turn-1");
    assert_eq!(contract["modalities"], serde_json::json!(["audio", "text"]));
    assert_eq!(
        contract["tools"],
        serde_json::json!(["knowledge.search", "ticket.create"])
    );
    assert_eq!(
        contract["transport"],
        serde_json::json!({
            "kind": "websocket",
            "uri": "wss://voice.example.com/session",
            "codec": "pcm16",
            "sampleRateHz": 24000,
            "channels": 1,
        })
    );
    Ok(())
}

#[test]
fn vad_authority_and_interruption_classifier_detect_barge_in() -> Result<(), Box<dyn Error>> {
    let authority = VadAuthority::new("vad-local", 0.6)?;
    let silence = authority.evaluate(&AudioFrame::new("mic", 1, 0, 20, 0.2)?, false);
    let speech = authority.evaluate(&AudioFrame::new("mic", 2, 20, 20, 0.9)?, false);
    let playback = PlaybackLedger::new().append(
        PlaybackEntry::new("assistant-audio-1", 1, PlaybackStatus::Started)?.with_started_at_ms(0),
    );
    let decision =
        InterruptionClassifier::new("barge-in")?.classify("session-1", &speech, &playback, 25)?;

    assert_eq!(silence.kind, VadDecisionKind::Silence);
    assert_eq!(speech.kind, VadDecisionKind::SpeechStart);
    assert_eq!(decision.kind, InterruptionKind::Interrupt);
    assert_eq!(decision.interrupted_playback_ids, vec!["assistant-audio-1"]);
    assert_eq!(
        decision.reason.as_deref(),
        Some("user_speech_during_playback")
    );
    Ok(())
}

#[test]
fn playback_ledger_interrupts_active_items_only() -> Result<(), Box<dyn Error>> {
    let ledger = PlaybackLedger::new()
        .append(
            PlaybackEntry::new("audio-1", 1, PlaybackStatus::Completed)?
                .with_started_at_ms(0)
                .with_completed_at_ms(100),
        )
        .append(PlaybackEntry::new("audio-2", 2, PlaybackStatus::Started)?.with_started_at_ms(110));

    let interrupted = ledger.interrupt_active(150, "barge_in")?;

    assert_eq!(interrupted.active_playback_ids(), Vec::<String>::new());
    assert_eq!(interrupted.entries[0].status, PlaybackStatus::Completed);
    assert_eq!(interrupted.entries[1].status, PlaybackStatus::Interrupted);
    assert_eq!(interrupted.entries[1].completed_at_ms, Some(150));
    assert_eq!(interrupted.entries[1].reason.as_deref(), Some("barge_in"));
    assert!(interrupted.content_digest().starts_with("sha256:"));
    Ok(())
}

#[test]
fn validation_errors_are_explicit() -> Result<(), Box<dyn Error>> {
    assert!(matches!(
        VoiceTransport::new(
            VoiceTransportKind::Websocket,
            Some("wss://voice.example.com/session".to_string()),
            "pcm16",
            0,
            1,
        ),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        DuplexSession::from_parts(
            "session-1",
            VoiceTransport::websocket("wss://voice.example.com/session")?,
            VoiceSessionState::Open,
            None,
            100,
            Some(90),
            None,
            BTreeMap::new(),
        ),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        AudioFrame::new("mic", 1, 0, 20, 1.5),
        Err(VoiceContractError::Invalid { .. })
    ));
    Ok(())
}
