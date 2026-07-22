#![allow(clippy::panic)]

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use graphblocks_runtime_core::voice::{
    AudioFrame, DuplexSession, InterruptionClassifier, InterruptionKind, PlaybackEntry,
    PlaybackLedger, PlaybackStatus, ProviderInterruptionDecision, RealtimeSessionRequest,
    VadAuthority, VoiceContractError, VoiceSessionState, VoiceTransport, VoiceTransportKind,
};
use serde_json::{Map, Value, json};

#[test]
fn voice_tck_cases_match_runtime_core() {
    let mut fixture_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    fixture_path.push("tests/fixtures/voice-cases.json");
    let raw_fixture = fs::read_to_string(&fixture_path).expect("voice fixture is readable");
    let cases: Vec<Value> = serde_json::from_str(&raw_fixture).expect("voice fixture is valid");

    for raw_case in cases {
        let case = raw_case.as_object().expect("voice case is object");
        let name = required_str(case, &["name", "caseId", "case_id"]);
        let kind = required_str(case, &["kind"]);
        let observed = match kind.as_str() {
            "session_request" => {
                let transport = voice_transport_from(
                    case.get("transport")
                        .and_then(Value::as_object)
                        .expect("voice transport"),
                )
                .expect("transport is valid");
                let session = duplex_session_from(
                    case.get("session")
                        .and_then(Value::as_object)
                        .expect("voice session"),
                    transport,
                )
                .expect("session is valid");
                let request = realtime_request_from(
                    session.clone(),
                    case.get("request")
                        .and_then(Value::as_object)
                        .expect("realtime request"),
                )
                .expect("request is valid");
                let contract = request.provider_contract();
                json!({
                    "sessionState": session.state.as_str(),
                    "currentTurnId": session.current_turn_id,
                    "contractSessionId": contract["sessionId"],
                    "transportKind": contract["transport"]["kind"],
                    "transportSampleRateHz": contract["transport"]["sampleRateHz"],
                    "modalities": contract["modalities"],
                    "tools": contract["tools"],
                })
            }
            "vad_interruption" => {
                let authority = vad_authority_from(
                    case.get("authority")
                        .and_then(Value::as_object)
                        .expect("vad authority"),
                )
                .expect("authority is valid");
                let decisions = case
                    .get("frames")
                    .and_then(Value::as_array)
                    .expect("audio frames")
                    .iter()
                    .map(|raw_frame| {
                        let raw_frame = raw_frame.as_object().expect("audio frame");
                        let frame = audio_frame_from(raw_frame).expect("audio frame is valid");
                        authority.evaluate(
                            &frame,
                            optional_bool(raw_frame, &["alreadyInSpeech", "already_in_speech"]),
                        )
                    })
                    .collect::<Vec<_>>();
                let playback = playback_ledger_from(
                    case.get("playback")
                        .and_then(Value::as_array)
                        .expect("playback ledger"),
                )
                .expect("playback ledger is valid");
                let classifier = case
                    .get("classifier")
                    .and_then(Value::as_object)
                    .expect("classifier");
                let mut classifier_impl = InterruptionClassifier::new(required_str(
                    classifier,
                    &["classifierId", "classifier_id"],
                ))
                .expect("classifier is valid");
                if let Some(provider_authority_id) = optional_str(
                    classifier,
                    &["providerAuthorityId", "provider_authority_id"],
                ) {
                    classifier_impl = classifier_impl
                        .with_provider_authority_id(provider_authority_id)
                        .expect("provider authority is valid");
                }
                let provider_decision = classifier
                    .get("providerDecision")
                    .or_else(|| classifier.get("provider_decision"))
                    .and_then(Value::as_object)
                    .map(provider_interruption_decision_from)
                    .transpose()
                    .expect("provider decision is valid");
                let decision = classifier_impl
                    .classify_with_provider_decision(
                        required_str(classifier, &["sessionId", "session_id"]),
                        decisions.last().expect("last vad decision"),
                        &playback,
                        required_u64(classifier, &["occurredAtMs", "occurred_at_ms"]),
                        provider_decision.as_ref(),
                    )
                    .expect("interruption classification succeeds");
                json!({
                    "decisionKinds": decisions.iter().map(|decision| decision.kind.as_str()).collect::<Vec<_>>(),
                    "interruptionKind": decision.kind.as_str(),
                    "interruptedPlaybackIds": decision.interrupted_playback_ids,
                    "interruptionReason": decision.reason,
                })
            }
            "playback_interrupt" => {
                let ledger = playback_ledger_from(
                    case.get("entries")
                        .and_then(Value::as_array)
                        .expect("playback entries"),
                )
                .expect("playback entries are valid");
                let active_before = ledger.active_playback_ids();
                let interrupt = case
                    .get("interrupt")
                    .and_then(Value::as_object)
                    .expect("interrupt");
                let interrupted = ledger
                    .interrupt_active(
                        required_u64(interrupt, &["occurredAtMs", "occurred_at_ms"]),
                        required_str(interrupt, &["reason"]),
                    )
                    .expect("interrupt succeeds");
                json!({
                    "activeBefore": active_before,
                    "statuses": interrupted.entries.iter().map(|entry| entry.status.as_str()).collect::<Vec<_>>(),
                    "completedAtMs": interrupted.entries.iter().map(|entry| entry.completed_at_ms).collect::<Vec<_>>(),
                    "reasons": interrupted.entries.iter().map(|entry| entry.reason.clone()).collect::<Vec<_>>(),
                    "digest": interrupted.content_digest(),
                })
            }
            "validation_errors" => {
                let invalid_transport = voice_transport_from(
                    case.get("invalidTransport")
                        .and_then(Value::as_object)
                        .expect("invalid transport"),
                );
                let valid_transport = VoiceTransport::websocket("wss://voice.example.com/session")
                    .expect("valid transport");
                let invalid_session = duplex_session_from(
                    case.get("invalidSession")
                        .and_then(Value::as_object)
                        .expect("invalid session"),
                    valid_transport,
                );
                let invalid_frame = audio_frame_from(
                    case.get("invalidFrame")
                        .and_then(Value::as_object)
                        .expect("invalid frame"),
                );
                json!({
                    "transportError": voice_error_code(invalid_transport.expect_err("transport fails")),
                    "sessionError": voice_error_code(invalid_session.expect_err("session fails")),
                    "frameError": voice_error_code(invalid_frame.expect_err("frame fails")),
                })
            }
            other => panic!("unsupported voice TCK case kind {other:?}"),
        };

        let expected = case.get("expected").expect("expected result");
        assert_eq!(observed, *expected, "case {name} failed");
    }
}

fn required_str(mapping: &Map<String, Value>, keys: &[&str]) -> String {
    for key in keys {
        if let Some(value) = mapping.get(*key).and_then(Value::as_str) {
            return value.to_owned();
        }
    }
    panic!("missing required string field {keys:?}");
}

fn optional_str(mapping: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| mapping.get(*key).and_then(Value::as_str))
        .map(str::to_owned)
}

fn required_u64(mapping: &Map<String, Value>, keys: &[&str]) -> u64 {
    for key in keys {
        if let Some(value) = mapping.get(*key).and_then(Value::as_u64) {
            return value;
        }
    }
    panic!("missing required u64 field {keys:?}");
}

fn optional_u64(mapping: &Map<String, Value>, keys: &[&str], default: u64) -> u64 {
    keys.iter()
        .find_map(|key| mapping.get(*key).and_then(Value::as_u64))
        .unwrap_or(default)
}

fn required_f64(mapping: &Map<String, Value>, keys: &[&str]) -> f64 {
    for key in keys {
        if let Some(value) = mapping.get(*key).and_then(Value::as_f64) {
            return value;
        }
    }
    panic!("missing required f64 field {keys:?}");
}

fn optional_bool(mapping: &Map<String, Value>, keys: &[&str]) -> bool {
    keys.iter()
        .find_map(|key| mapping.get(*key).and_then(Value::as_bool))
        .unwrap_or(false)
}

fn string_list(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| item.as_str().expect("string list item").to_owned())
            .collect(),
        Some(Value::String(item)) => vec![item.to_owned()],
        _ => Vec::new(),
    }
}

fn transport_kind_from(raw: &str) -> VoiceTransportKind {
    match raw {
        "websocket" => VoiceTransportKind::Websocket,
        "webrtc" => VoiceTransportKind::WebRtc,
        "provider_realtime" => VoiceTransportKind::ProviderRealtime,
        other => panic!("unsupported voice transport kind {other:?}"),
    }
}

fn voice_transport_from(raw: &Map<String, Value>) -> Result<VoiceTransport, VoiceContractError> {
    VoiceTransport::new(
        transport_kind_from(&required_str(raw, &["kind"])),
        optional_str(raw, &["uri"]),
        optional_str(raw, &["codec"]).unwrap_or_else(|| "pcm16".to_string()),
        optional_u64(raw, &["sampleRateHz", "sample_rate_hz"], 24_000) as u32,
        optional_u64(raw, &["channels"], 1) as u16,
    )
}

fn metadata_from(raw: Option<&Value>) -> BTreeMap<String, String> {
    raw.and_then(Value::as_object)
        .map(|mapping| {
            mapping
                .iter()
                .map(|(key, value)| {
                    (
                        key.clone(),
                        value
                            .as_str()
                            .map(str::to_owned)
                            .unwrap_or_else(|| value.to_string()),
                    )
                })
                .collect()
        })
        .unwrap_or_default()
}

fn duplex_session_from(
    raw: &Map<String, Value>,
    transport: VoiceTransport,
) -> Result<DuplexSession, VoiceContractError> {
    DuplexSession::from_parts(
        required_str(raw, &["sessionId", "session_id"]),
        transport,
        VoiceSessionState::Open,
        optional_str(raw, &["turnId", "turn_id"]),
        optional_u64(raw, &["startedAtMs", "started_at_ms"], 0),
        raw.get("closeAtMs")
            .or_else(|| raw.get("closedAtMs"))
            .or_else(|| raw.get("closed_at_ms"))
            .and_then(Value::as_u64),
        None,
        metadata_from(raw.get("metadata")),
    )
}

fn realtime_request_from(
    session: DuplexSession,
    raw: &Map<String, Value>,
) -> Result<RealtimeSessionRequest, VoiceContractError> {
    RealtimeSessionRequest::new(
        session,
        required_str(raw, &["model"]),
        required_str(raw, &["instructions"]),
    )?
    .with_modalities(string_list(raw.get("modalities")))?
    .with_tools(string_list(raw.get("tools")))
}

fn vad_authority_from(raw: &Map<String, Value>) -> Result<VadAuthority, VoiceContractError> {
    VadAuthority::new(
        required_str(raw, &["authorityId", "authority_id"]),
        required_f64(raw, &["speechThreshold", "speech_threshold"]),
    )
}

fn audio_frame_from(raw: &Map<String, Value>) -> Result<AudioFrame, VoiceContractError> {
    AudioFrame::new(
        required_str(raw, &["streamId", "stream_id"]),
        required_u64(raw, &["sequence"]),
        required_u64(raw, &["startMs", "start_ms"]),
        required_u64(raw, &["durationMs", "duration_ms"]),
        required_f64(raw, &["speechProbability", "speech_probability"]),
    )
}

fn playback_entry_from(raw: &Map<String, Value>) -> Result<PlaybackEntry, VoiceContractError> {
    let mut entry = PlaybackEntry::new(
        required_str(raw, &["playbackId", "playback_id"]),
        required_u64(raw, &["sequence"]),
        PlaybackStatus::from_status(&required_str(raw, &["status"]))?,
    )?;
    if let Some(audio_ref) = optional_str(raw, &["audioRef", "audio_ref"]) {
        entry = entry.with_audio_ref(audio_ref)?;
    }
    if let Some(started_at_ms) = raw
        .get("startedAtMs")
        .or_else(|| raw.get("started_at_ms"))
        .and_then(Value::as_u64)
    {
        entry = entry.with_started_at_ms(started_at_ms);
    }
    if let Some(completed_at_ms) = raw
        .get("completedAtMs")
        .or_else(|| raw.get("completed_at_ms"))
        .and_then(Value::as_u64)
    {
        entry = entry.with_completed_at_ms(completed_at_ms);
    }
    if let Some(acknowledged_at_ms) = raw
        .get("acknowledgedAtMs")
        .or_else(|| raw.get("acknowledged_at_ms"))
        .and_then(Value::as_u64)
    {
        entry = entry.with_acknowledged_at_ms(acknowledged_at_ms);
    }
    if let Some(reason) = optional_str(raw, &["reason"]) {
        entry = entry.with_reason(reason)?;
    }
    Ok(entry)
}

fn playback_ledger_from(raw: &[Value]) -> Result<PlaybackLedger, VoiceContractError> {
    let entries = raw
        .iter()
        .map(|entry| playback_entry_from(entry.as_object().expect("playback entry")))
        .collect::<Result<Vec<_>, _>>()?;
    PlaybackLedger::from_entries(entries)
}

fn provider_interruption_decision_from(
    raw: &Map<String, Value>,
) -> Result<ProviderInterruptionDecision, VoiceContractError> {
    let mut decision = ProviderInterruptionDecision::new(
        required_str(raw, &["authorityId", "authority_id"]),
        required_str(raw, &["sessionId", "session_id"]),
        match required_str(raw, &["kind"]).as_str() {
            "continue" => InterruptionKind::Continue,
            "interrupt" => InterruptionKind::Interrupt,
            other => panic!("unsupported provider interruption kind {other:?}"),
        },
        required_u64(raw, &["occurredAtMs", "occurred_at_ms"]),
    )?;
    if let Some(reason) = optional_str(raw, &["reason"]) {
        decision = decision.with_reason(reason)?;
    }
    Ok(decision)
}

fn voice_error_code(_error: VoiceContractError) -> &'static str {
    "voice_contract_error"
}
