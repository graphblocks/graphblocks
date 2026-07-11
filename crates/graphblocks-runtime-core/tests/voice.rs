use std::collections::BTreeMap;
use std::error::Error;

use graphblocks_runtime_core::voice::{
    AudioFrame, DuplexSession, InterruptionClassifier, InterruptionKind, PlaybackEntry,
    PlaybackLedger, PlaybackStatus, ProviderInterruptionDecision, RealtimeProviderAdapter,
    RealtimeSessionRequest, VadAuthority, VadDecisionKind, VoiceContractError, VoiceSessionState,
    VoiceTransport, VoiceTransportKind,
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
    )?;
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
fn provider_authority_waits_for_confirmation() -> Result<(), Box<dyn Error>> {
    let speech = VadAuthority::new("vad-local", 0.6)?
        .evaluate(&AudioFrame::new("mic", 2, 20, 20, 0.9)?, false);
    let playback = PlaybackLedger::new().append(
        PlaybackEntry::new("assistant-audio-1", 1, PlaybackStatus::Started)?.with_started_at_ms(0),
    )?;
    let classifier =
        InterruptionClassifier::new("barge-in")?.with_provider_authority_id("provider-realtime")?;

    let advisory =
        classifier.classify_with_provider_decision("session-1", &speech, &playback, 25, None)?;
    let provider_continue = classifier.classify_with_provider_decision(
        "session-1",
        &speech,
        &playback,
        26,
        Some(
            &ProviderInterruptionDecision::new(
                "provider-realtime",
                "session-1",
                InterruptionKind::Continue,
                26,
            )?
            .with_reason("provider_turn_continues")?,
        ),
    )?;
    let provider_interrupt = classifier.classify_with_provider_decision(
        "session-1",
        &speech,
        &playback,
        27,
        Some(
            &ProviderInterruptionDecision::new(
                "provider-realtime",
                "session-1",
                InterruptionKind::Interrupt,
                27,
            )?
            .with_reason("provider_confirmed_barge_in")?,
        ),
    )?;

    assert_eq!(advisory.kind, InterruptionKind::Continue);
    assert!(advisory.interrupted_playback_ids.is_empty());
    assert_eq!(
        advisory.reason.as_deref(),
        Some("awaiting_provider_confirmation")
    );
    assert_eq!(provider_continue.kind, InterruptionKind::Continue);
    assert_eq!(
        provider_continue.reason.as_deref(),
        Some("provider_turn_continues")
    );
    assert_eq!(provider_interrupt.kind, InterruptionKind::Interrupt);
    assert_eq!(
        provider_interrupt.interrupted_playback_ids,
        vec!["assistant-audio-1"]
    );
    assert_eq!(
        provider_interrupt.reason.as_deref(),
        Some("provider_confirmed_barge_in")
    );

    assert!(matches!(
        classifier.classify_with_provider_decision(
            "session-1",
            &speech,
            &playback,
            28,
            Some(&ProviderInterruptionDecision::new(
                "other-provider",
                "session-1",
                InterruptionKind::Interrupt,
                28,
            )?),
        ),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        classifier.classify_with_provider_decision(
            "session-1",
            &speech,
            &playback,
            28,
            Some(&ProviderInterruptionDecision::new(
                "provider-realtime",
                "other-session",
                InterruptionKind::Interrupt,
                28,
            )?),
        ),
        Err(VoiceContractError::Invalid { .. })
    ));
    Ok(())
}

#[test]
fn provider_interruption_does_not_apply_to_playback_started_after_the_decision()
-> Result<(), Box<dyn Error>> {
    let speech = VadAuthority::new("vad-local", 0.6)?
        .evaluate(&AudioFrame::new("mic", 2, 80, 20, 0.9)?, false);
    let playback = PlaybackLedger::new()
        .append(
            PlaybackEntry::new("assistant-audio-old", 1, PlaybackStatus::Started)?
                .with_started_at_ms(50),
        )?
        .append(
            PlaybackEntry::new("assistant-audio-new", 2, PlaybackStatus::Started)?
                .with_started_at_ms(110),
        )?;
    let classifier =
        InterruptionClassifier::new("barge-in")?.with_provider_authority_id("provider-realtime")?;
    let provider_decision = ProviderInterruptionDecision::new(
        "provider-realtime",
        "session-1",
        InterruptionKind::Interrupt,
        100,
    )?;

    let decision = classifier.classify_with_provider_decision(
        "session-1",
        &speech,
        &playback,
        120,
        Some(&provider_decision),
    )?;

    assert_eq!(decision.kind, InterruptionKind::Interrupt);
    assert_eq!(
        decision.interrupted_playback_ids,
        vec!["assistant-audio-old"]
    );
    Ok(())
}

#[test]
fn provider_interruption_decision_after_classification_time_is_rejected()
-> Result<(), Box<dyn Error>> {
    let speech = VadAuthority::new("vad-local", 0.6)?
        .evaluate(&AudioFrame::new("mic", 2, 80, 20, 0.9)?, false);
    let playback = PlaybackLedger::new().append(
        PlaybackEntry::new("assistant-audio-1", 1, PlaybackStatus::Started)?.with_started_at_ms(50),
    )?;
    let classifier =
        InterruptionClassifier::new("barge-in")?.with_provider_authority_id("provider-realtime")?;
    let provider_decision = ProviderInterruptionDecision::new(
        "provider-realtime",
        "session-1",
        InterruptionKind::Interrupt,
        121,
    )?;

    assert!(matches!(
        classifier.classify_with_provider_decision(
            "session-1",
            &speech,
            &playback,
            120,
            Some(&provider_decision),
        ),
        Err(VoiceContractError::Invalid {
            field_name: "provider_interruption_decision",
            ..
        })
    ));
    Ok(())
}

#[test]
fn playback_ledger_acknowledges_terminal_entries() -> Result<(), Box<dyn Error>> {
    let queued = PlaybackEntry::new("audio-1", 1, PlaybackStatus::Queued)?
        .with_audio_ref("artifact://voice/audio-1")?;
    let ledger = PlaybackLedger::new().append(queued.clone())?;

    assert_eq!(ledger.clone().append(queued)?, ledger);
    let started = ledger.start("audio-1", 10)?;
    assert_eq!(started.entries[0].status, PlaybackStatus::Started);
    assert_eq!(started.entries[0].started_at_ms, Some(10));
    let completed = started.complete("audio-1", 30)?;
    assert_eq!(completed.entries[0].status, PlaybackStatus::Completed);
    assert_eq!(completed.entries[0].completed_at_ms, Some(30));
    let acknowledged = completed.acknowledge("audio-1", 35)?;
    assert_eq!(acknowledged.entries[0].acknowledged_at_ms, Some(35));
    assert_eq!(
        acknowledged.clone().acknowledge("audio-1", 35)?,
        acknowledged
    );

    assert!(matches!(
        ledger.acknowledge("audio-1", 5),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        completed.acknowledge("audio-1", 29),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        acknowledged.acknowledge("audio-1", 36),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        ledger
            .clone()
            .append(PlaybackEntry::new("audio-2", 1, PlaybackStatus::Queued)?),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        ledger
            .clone()
            .append(PlaybackEntry::new("audio-1", 2, PlaybackStatus::Queued)?),
        Err(VoiceContractError::Invalid { .. })
    ));
    Ok(())
}

#[test]
fn playback_ledger_interrupts_active_items_only() -> Result<(), Box<dyn Error>> {
    let ledger = PlaybackLedger::new()
        .append(
            PlaybackEntry::new("audio-1", 1, PlaybackStatus::Completed)?
                .with_started_at_ms(0)
                .with_completed_at_ms(100),
        )?
        .append(
            PlaybackEntry::new("audio-2", 2, PlaybackStatus::Started)?.with_started_at_ms(110),
        )?;

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

#[test]
fn realtime_provider_adapter_builds_stable_provider_session_request() -> Result<(), Box<dyn Error>>
{
    let transport = VoiceTransport::new(
        VoiceTransportKind::ProviderRealtime,
        Some("wss://realtime.example.com/v1/sessions".to_owned()),
        "pcm16",
        24_000,
        1,
    )?;
    let session = DuplexSession::new("session-voice-1", transport)?.begin_turn("turn-7")?;
    let adapter = RealtimeProviderAdapter::new(
        "openai-realtime",
        "https://api.example.com/v1/realtime/sessions",
        "secret://providers/openai",
    )?
    .with_default_model("gpt-realtime")
    .with_default_instructions("Use voice-safe concise answers.")?
    .with_option("voice", serde_json::json!("alloy"))?
    .with_option("temperature", serde_json::json!(0.4))?;
    let request = adapter.build_session_request(
        session,
        None,
        None,
        ["audio", "text"],
        ["knowledge.search", "ticket.create"],
    )?;
    let envelope = request.provider_envelope();

    assert_eq!(request.adapter_id, "openai-realtime");
    assert_eq!(request.auth_secret_ref, "secret://providers/openai");
    assert_eq!(envelope["provider"], "openai-realtime");
    assert_eq!(
        envelope["endpoint"],
        "https://api.example.com/v1/realtime/sessions"
    );
    assert_eq!(envelope["authSecretRef"], "secret://providers/openai");
    assert_eq!(envelope["request"]["model"], "gpt-realtime");
    assert_eq!(
        envelope["request"]["instructions"],
        "Use voice-safe concise answers."
    );
    assert_eq!(
        envelope["request"]["modalities"],
        serde_json::json!(["audio", "text"])
    );
    assert_eq!(
        envelope["request"]["tools"],
        serde_json::json!(["knowledge.search", "ticket.create"])
    );
    assert_eq!(envelope["options"]["voice"], serde_json::json!("alloy"));
    assert_eq!(
        request.content_digest(),
        "sha256:0450d3dc36db2cc56d14189f617d6abc44dbee5ff9aeacbcec8f07bd28ae5d6a"
    );
    Ok(())
}

#[test]
fn realtime_provider_adapter_validates_identity_and_defaults() -> Result<(), Box<dyn Error>> {
    assert!(matches!(
        RealtimeProviderAdapter::new(
            " ",
            "https://api.example.com/v1/realtime/sessions",
            "secret://provider"
        ),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        RealtimeProviderAdapter::new("provider", " ", "secret://provider"),
        Err(VoiceContractError::Invalid { .. })
    ));
    assert!(matches!(
        RealtimeProviderAdapter::new(
            "provider",
            "https://api.example.com/v1/realtime/sessions",
            " "
        ),
        Err(VoiceContractError::Invalid { .. })
    ));

    let adapter = RealtimeProviderAdapter::new(
        "provider",
        "https://api.example.com/v1/realtime/sessions",
        "secret://provider",
    )?;
    let session = DuplexSession::new(
        "session-1",
        VoiceTransport::websocket("wss://voice.example.com/session")?,
    )?;

    assert!(matches!(
        adapter.build_session_request(session, None, None, ["audio"], [] as [&str; 0]),
        Err(VoiceContractError::Invalid { .. })
    ));
    Ok(())
}
