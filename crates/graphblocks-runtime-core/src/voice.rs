use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

fn require_non_empty(field_name: &'static str, value: &str) -> Result<(), VoiceContractError> {
    if value.trim().is_empty() {
        return Err(VoiceContractError::Invalid {
            field_name,
            message: "must not be empty".to_string(),
        });
    }
    Ok(())
}

fn sorted_unique(items: impl IntoIterator<Item = String>) -> Vec<String> {
    items
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum VoiceContractError {
    Invalid {
        field_name: &'static str,
        message: String,
    },
}

impl fmt::Display for VoiceContractError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid {
                field_name,
                message,
            } => write!(formatter, "{field_name} {message}"),
        }
    }
}

impl Error for VoiceContractError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VoiceTransportKind {
    Websocket,
    WebRtc,
    ProviderRealtime,
}

impl VoiceTransportKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Websocket => "websocket",
            Self::WebRtc => "webrtc",
            Self::ProviderRealtime => "provider_realtime",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VoiceTransport {
    pub kind: VoiceTransportKind,
    pub uri: Option<String>,
    pub codec: String,
    pub sample_rate_hz: u32,
    pub channels: u16,
}

impl VoiceTransport {
    pub fn new(
        kind: VoiceTransportKind,
        uri: Option<String>,
        codec: impl Into<String>,
        sample_rate_hz: u32,
        channels: u16,
    ) -> Result<Self, VoiceContractError> {
        let transport = Self {
            kind,
            uri,
            codec: codec.into(),
            sample_rate_hz,
            channels,
        };
        transport.validate()?;
        Ok(transport)
    }

    pub fn websocket(uri: impl Into<String>) -> Result<Self, VoiceContractError> {
        Self::new(
            VoiceTransportKind::Websocket,
            Some(uri.into()),
            "pcm16",
            24_000,
            1,
        )
    }

    pub fn with_codec(mut self, codec: impl Into<String>) -> Result<Self, VoiceContractError> {
        self.codec = codec.into();
        self.validate()?;
        Ok(self)
    }

    pub fn with_sample_rate_hz(mut self, sample_rate_hz: u32) -> Result<Self, VoiceContractError> {
        self.sample_rate_hz = sample_rate_hz;
        self.validate()?;
        Ok(self)
    }

    pub fn with_channels(mut self, channels: u16) -> Result<Self, VoiceContractError> {
        self.channels = channels;
        self.validate()?;
        Ok(self)
    }

    fn validate(&self) -> Result<(), VoiceContractError> {
        if let Some(uri) = &self.uri {
            require_non_empty("transport uri", uri)?;
        }
        require_non_empty("transport codec", &self.codec)?;
        if self.sample_rate_hz == 0 {
            return Err(VoiceContractError::Invalid {
                field_name: "sample_rate_hz",
                message: "must be positive".to_string(),
            });
        }
        if self.channels == 0 {
            return Err(VoiceContractError::Invalid {
                field_name: "channels",
                message: "must be positive".to_string(),
            });
        }
        Ok(())
    }

    pub fn contract(&self) -> Value {
        json!({
            "kind": self.kind.as_str(),
            "uri": self.uri,
            "codec": self.codec,
            "sampleRateHz": self.sample_rate_hz,
            "channels": self.channels,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VoiceSessionState {
    Open,
    Interrupted,
    Closed,
}

impl VoiceSessionState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Open => "open",
            Self::Interrupted => "interrupted",
            Self::Closed => "closed",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DuplexSession {
    pub session_id: String,
    pub transport: VoiceTransport,
    pub state: VoiceSessionState,
    pub current_turn_id: Option<String>,
    pub started_at_ms: u64,
    pub closed_at_ms: Option<u64>,
    pub interrupted_at_ms: Option<u64>,
    pub interruption_reason: Option<String>,
    pub metadata: BTreeMap<String, String>,
}

impl DuplexSession {
    pub fn new(
        session_id: impl Into<String>,
        transport: VoiceTransport,
    ) -> Result<Self, VoiceContractError> {
        Self::from_parts(
            session_id,
            transport,
            VoiceSessionState::Open,
            None,
            0,
            None,
            None,
            BTreeMap::new(),
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn from_parts(
        session_id: impl Into<String>,
        transport: VoiceTransport,
        state: VoiceSessionState,
        current_turn_id: Option<String>,
        started_at_ms: u64,
        closed_at_ms: Option<u64>,
        interruption_reason: Option<String>,
        metadata: BTreeMap<String, String>,
    ) -> Result<Self, VoiceContractError> {
        // The legacy constructor predates interrupted_at_ms. A supplied reason proves that an
        // interruption occurred, so map it to the only deterministic boundary available in the
        // old representation. Calls without enough state still fail the invariant checks below.
        let interrupted_at_ms = match state {
            VoiceSessionState::Interrupted if interruption_reason.is_some() => Some(started_at_ms),
            VoiceSessionState::Closed if interruption_reason.is_some() => closed_at_ms,
            VoiceSessionState::Open
            | VoiceSessionState::Interrupted
            | VoiceSessionState::Closed => None,
        };
        Self::from_parts_with_interruption(
            session_id,
            transport,
            state,
            current_turn_id,
            started_at_ms,
            closed_at_ms,
            interrupted_at_ms,
            interruption_reason,
            metadata,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn from_parts_with_interruption(
        session_id: impl Into<String>,
        transport: VoiceTransport,
        state: VoiceSessionState,
        current_turn_id: Option<String>,
        started_at_ms: u64,
        closed_at_ms: Option<u64>,
        interrupted_at_ms: Option<u64>,
        interruption_reason: Option<String>,
        metadata: BTreeMap<String, String>,
    ) -> Result<Self, VoiceContractError> {
        let session = Self {
            session_id: session_id.into(),
            transport,
            state,
            current_turn_id,
            started_at_ms,
            closed_at_ms,
            interrupted_at_ms,
            interruption_reason,
            metadata,
        };
        session.validate()?;
        Ok(session)
    }

    fn validate(&self) -> Result<(), VoiceContractError> {
        require_non_empty("session_id", &self.session_id)?;
        if let Some(interruption_reason) = self.interruption_reason.as_deref() {
            require_non_empty("interruption reason", interruption_reason)?;
        }
        if let Some(closed_at_ms) = self.closed_at_ms
            && closed_at_ms < self.started_at_ms
        {
            return Err(VoiceContractError::Invalid {
                field_name: "closed_at_ms",
                message: "must be greater than or equal to started_at_ms".to_string(),
            });
        }
        if let Some(interrupted_at_ms) = self.interrupted_at_ms
            && interrupted_at_ms < self.started_at_ms
        {
            return Err(VoiceContractError::Invalid {
                field_name: "interrupted_at_ms",
                message: "must be greater than or equal to started_at_ms".to_string(),
            });
        }
        if let (Some(closed_at_ms), Some(interrupted_at_ms)) =
            (self.closed_at_ms, self.interrupted_at_ms)
            && closed_at_ms < interrupted_at_ms
        {
            return Err(VoiceContractError::Invalid {
                field_name: "closed_at_ms",
                message: "must be greater than or equal to interrupted_at_ms".to_string(),
            });
        }
        match self.state {
            VoiceSessionState::Open => {
                if self.closed_at_ms.is_some()
                    || self.interrupted_at_ms.is_some()
                    || self.interruption_reason.is_some()
                {
                    return Err(VoiceContractError::Invalid {
                        field_name: "open voice session",
                        message: "must not carry close or interruption state".to_string(),
                    });
                }
            }
            VoiceSessionState::Interrupted => {
                if self.closed_at_ms.is_some() {
                    return Err(VoiceContractError::Invalid {
                        field_name: "interrupted voice session",
                        message: "must not carry a close time".to_string(),
                    });
                }
                if self.interrupted_at_ms.is_none() || self.interruption_reason.is_none() {
                    return Err(VoiceContractError::Invalid {
                        field_name: "interrupted voice session",
                        message: "requires an interruption time and reason".to_string(),
                    });
                }
            }
            VoiceSessionState::Closed => {
                if self.closed_at_ms.is_none() {
                    return Err(VoiceContractError::Invalid {
                        field_name: "closed voice session",
                        message: "requires a close time".to_string(),
                    });
                }
                if self.interrupted_at_ms.is_some() != self.interruption_reason.is_some() {
                    return Err(VoiceContractError::Invalid {
                        field_name: "closed voice session",
                        message: "requires both interruption time and reason or neither"
                            .to_string(),
                    });
                }
            }
        }
        Ok(())
    }

    pub fn begin_turn(mut self, turn_id: impl Into<String>) -> Result<Self, VoiceContractError> {
        if self.state == VoiceSessionState::Closed {
            return Err(VoiceContractError::Invalid {
                field_name: "voice session",
                message: "is closed and cannot begin a turn".to_string(),
            });
        }
        let turn_id = turn_id.into();
        require_non_empty("turn_id", &turn_id)?;
        self.current_turn_id = Some(turn_id);
        self.state = VoiceSessionState::Open;
        self.interrupted_at_ms = None;
        self.interruption_reason = None;
        Ok(self)
    }

    pub fn interrupt(
        mut self,
        occurred_at_ms: u64,
        reason: impl Into<String>,
    ) -> Result<Self, VoiceContractError> {
        if self.state == VoiceSessionState::Closed {
            return Err(VoiceContractError::Invalid {
                field_name: "voice session",
                message: "is closed and cannot be interrupted".to_string(),
            });
        }
        if occurred_at_ms < self.started_at_ms {
            return Err(VoiceContractError::Invalid {
                field_name: "interruption",
                message: "occurred before session start".to_string(),
            });
        }
        let reason = reason.into();
        require_non_empty("interruption reason", &reason)?;
        self.state = VoiceSessionState::Interrupted;
        self.interrupted_at_ms = Some(occurred_at_ms);
        self.interruption_reason = Some(reason);
        Ok(self)
    }

    pub fn close(mut self, occurred_at_ms: u64) -> Result<Self, VoiceContractError> {
        if self.state == VoiceSessionState::Closed {
            return Err(VoiceContractError::Invalid {
                field_name: "voice session",
                message: "is already closed".to_string(),
            });
        }
        if occurred_at_ms < self.started_at_ms {
            return Err(VoiceContractError::Invalid {
                field_name: "close",
                message: "occurred before session start".to_string(),
            });
        }
        if self
            .interrupted_at_ms
            .is_some_and(|interrupted_at_ms| occurred_at_ms < interrupted_at_ms)
        {
            return Err(VoiceContractError::Invalid {
                field_name: "close",
                message: "occurred before session interruption".to_string(),
            });
        }
        self.state = VoiceSessionState::Closed;
        self.closed_at_ms = Some(occurred_at_ms);
        Ok(self)
    }

    pub fn contract(&self) -> Value {
        json!({
            "sessionId": self.session_id,
            "state": self.state.as_str(),
            "currentTurnId": self.current_turn_id,
            "startedAtMs": self.started_at_ms,
            "closedAtMs": self.closed_at_ms,
            "interruptedAtMs": self.interrupted_at_ms,
            "interruptionReason": self.interruption_reason,
            "transport": self.transport.contract(),
            "metadata": self.metadata,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AudioFrame {
    pub stream_id: String,
    pub sequence: u64,
    pub start_ms: u64,
    pub duration_ms: u64,
    pub speech_probability: f64,
}

impl AudioFrame {
    pub fn new(
        stream_id: impl Into<String>,
        sequence: u64,
        start_ms: u64,
        duration_ms: u64,
        speech_probability: f64,
    ) -> Result<Self, VoiceContractError> {
        let frame = Self {
            stream_id: stream_id.into(),
            sequence,
            start_ms,
            duration_ms,
            speech_probability,
        };
        frame.validate()?;
        Ok(frame)
    }

    fn validate(&self) -> Result<(), VoiceContractError> {
        require_non_empty("stream_id", &self.stream_id)?;
        if self.duration_ms == 0 {
            return Err(VoiceContractError::Invalid {
                field_name: "duration_ms",
                message: "must be positive".to_string(),
            });
        }
        if !self.speech_probability.is_finite() || !(0.0..=1.0).contains(&self.speech_probability) {
            return Err(VoiceContractError::Invalid {
                field_name: "speech_probability",
                message: "must be between 0 and 1".to_string(),
            });
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VadDecisionKind {
    Silence,
    SpeechStart,
    Speech,
    SpeechEnd,
}

impl VadDecisionKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Silence => "silence",
            Self::SpeechStart => "speech_start",
            Self::Speech => "speech",
            Self::SpeechEnd => "speech_end",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct VadDecision {
    pub authority_id: String,
    pub stream_id: String,
    pub sequence: u64,
    pub kind: VadDecisionKind,
    pub speech_probability: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct VadAuthority {
    pub authority_id: String,
    pub speech_threshold: f64,
}

impl VadAuthority {
    pub fn new(
        authority_id: impl Into<String>,
        speech_threshold: f64,
    ) -> Result<Self, VoiceContractError> {
        let authority = Self {
            authority_id: authority_id.into(),
            speech_threshold,
        };
        authority.validate()?;
        Ok(authority)
    }

    fn validate(&self) -> Result<(), VoiceContractError> {
        require_non_empty("authority_id", &self.authority_id)?;
        if !self.speech_threshold.is_finite() || !(0.0..=1.0).contains(&self.speech_threshold) {
            return Err(VoiceContractError::Invalid {
                field_name: "speech_threshold",
                message: "must be between 0 and 1".to_string(),
            });
        }
        Ok(())
    }

    pub fn evaluate(&self, frame: &AudioFrame, already_in_speech: bool) -> VadDecision {
        let kind = if frame.speech_probability >= self.speech_threshold {
            if already_in_speech {
                VadDecisionKind::Speech
            } else {
                VadDecisionKind::SpeechStart
            }
        } else if already_in_speech {
            VadDecisionKind::SpeechEnd
        } else {
            VadDecisionKind::Silence
        };
        VadDecision {
            authority_id: self.authority_id.clone(),
            stream_id: frame.stream_id.clone(),
            sequence: frame.sequence,
            kind,
            speech_probability: frame.speech_probability,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PlaybackStatus {
    Queued,
    Started,
    Completed,
    Interrupted,
}

impl PlaybackStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Started => "started",
            Self::Completed => "completed",
            Self::Interrupted => "interrupted",
        }
    }

    pub fn from_status(status: &str) -> Result<Self, VoiceContractError> {
        match status {
            "queued" => Ok(Self::Queued),
            "started" => Ok(Self::Started),
            "completed" => Ok(Self::Completed),
            "interrupted" => Ok(Self::Interrupted),
            _ => Err(VoiceContractError::Invalid {
                field_name: "playback status",
                message: format!("unsupported status {status:?}"),
            }),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlaybackEntry {
    pub playback_id: String,
    pub sequence: u64,
    pub status: PlaybackStatus,
    pub audio_ref: Option<String>,
    pub started_at_ms: Option<u64>,
    pub completed_at_ms: Option<u64>,
    pub acknowledged_at_ms: Option<u64>,
    pub reason: Option<String>,
}

impl PlaybackEntry {
    pub fn new(
        playback_id: impl Into<String>,
        sequence: u64,
        status: PlaybackStatus,
    ) -> Result<Self, VoiceContractError> {
        let entry = Self {
            playback_id: playback_id.into(),
            sequence,
            status,
            audio_ref: None,
            started_at_ms: None,
            completed_at_ms: None,
            acknowledged_at_ms: None,
            reason: None,
        };
        entry.validate()?;
        Ok(entry)
    }

    pub fn with_audio_ref(
        mut self,
        audio_ref: impl Into<String>,
    ) -> Result<Self, VoiceContractError> {
        self.audio_ref = Some(audio_ref.into());
        self.validate()?;
        Ok(self)
    }

    pub fn with_started_at_ms(mut self, started_at_ms: u64) -> Self {
        self.started_at_ms = Some(started_at_ms);
        self
    }

    pub fn with_completed_at_ms(mut self, completed_at_ms: u64) -> Self {
        self.completed_at_ms = Some(completed_at_ms);
        self
    }

    pub fn with_acknowledged_at_ms(mut self, acknowledged_at_ms: u64) -> Self {
        self.acknowledged_at_ms = Some(acknowledged_at_ms);
        self
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Result<Self, VoiceContractError> {
        let reason = reason.into();
        require_non_empty("playback reason", &reason)?;
        self.reason = Some(reason);
        Ok(self)
    }

    fn validate(&self) -> Result<(), VoiceContractError> {
        require_non_empty("playback_id", &self.playback_id)?;
        if let Some(audio_ref) = &self.audio_ref {
            require_non_empty("audio_ref", audio_ref)?;
        }
        Ok(())
    }

    fn validate_lifecycle(&self) -> Result<(), VoiceContractError> {
        self.validate()?;
        match self.status {
            PlaybackStatus::Queued => {
                if self.started_at_ms.is_some()
                    || self.completed_at_ms.is_some()
                    || self.acknowledged_at_ms.is_some()
                    || self.reason.is_some()
                {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: "queued playback must not have lifecycle timestamps or a reason"
                            .to_string(),
                    });
                }
            }
            PlaybackStatus::Started => {
                if self.started_at_ms.is_none() {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: "started playback requires started_at_ms".to_string(),
                    });
                }
                if self.completed_at_ms.is_some()
                    || self.acknowledged_at_ms.is_some()
                    || self.reason.is_some()
                {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message:
                            "started playback must not be completed, acknowledged, or have a reason"
                                .to_string(),
                    });
                }
            }
            PlaybackStatus::Completed | PlaybackStatus::Interrupted => {
                let Some(started_at_ms) = self.started_at_ms else {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: format!(
                            "{} playback requires started_at_ms",
                            self.status.as_str()
                        ),
                    });
                };
                let Some(completed_at_ms) = self.completed_at_ms else {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: format!(
                            "{} playback requires completed_at_ms",
                            self.status.as_str()
                        ),
                    });
                };
                if completed_at_ms < started_at_ms {
                    return Err(VoiceContractError::Invalid {
                        field_name: "completed_at_ms",
                        message: "must be greater than or equal to started_at_ms".to_string(),
                    });
                }
                if self.status == PlaybackStatus::Completed && self.reason.is_some() {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: "completed playback must not have a reason".to_string(),
                    });
                }
                if self.status == PlaybackStatus::Interrupted && self.reason.is_none() {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: "interrupted playback requires a reason".to_string(),
                    });
                }
                if self
                    .acknowledged_at_ms
                    .is_some_and(|acknowledged_at_ms| acknowledged_at_ms < completed_at_ms)
                {
                    return Err(VoiceContractError::Invalid {
                        field_name: "acknowledged_at_ms",
                        message: "must be greater than or equal to completed_at_ms".to_string(),
                    });
                }
            }
        }
        Ok(())
    }

    fn contract(&self) -> Value {
        json!({
            "playbackId": self.playback_id,
            "sequence": self.sequence,
            "status": self.status.as_str(),
            "audioRef": self.audio_ref,
            "startedAtMs": self.started_at_ms,
            "completedAtMs": self.completed_at_ms,
            "acknowledgedAtMs": self.acknowledged_at_ms,
            "reason": self.reason,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlaybackLedger {
    pub entries: Vec<PlaybackEntry>,
}

impl PlaybackLedger {
    pub fn new() -> Self {
        Self {
            entries: Vec::new(),
        }
    }

    pub fn from_entries<I>(entries: I) -> Result<Self, VoiceContractError>
    where
        I: IntoIterator<Item = PlaybackEntry>,
    {
        let mut ledger = Self::new();
        for entry in entries {
            ledger = ledger.append(entry)?;
        }
        Ok(ledger)
    }

    pub fn append(mut self, entry: PlaybackEntry) -> Result<Self, VoiceContractError> {
        entry.validate_lifecycle()?;
        for existing in &self.entries {
            if existing.playback_id == entry.playback_id || existing.sequence == entry.sequence {
                if existing == &entry {
                    return Ok(self);
                }
                return Err(VoiceContractError::Invalid {
                    field_name: "playback",
                    message: "playback_id and sequence must identify one immutable entry"
                        .to_string(),
                });
            }
        }
        if self
            .entries
            .last()
            .is_some_and(|last| entry.sequence <= last.sequence)
        {
            return Err(VoiceContractError::Invalid {
                field_name: "playback",
                message: "playback entries must be appended in sequence order".to_string(),
            });
        }
        self.entries.push(entry);
        Ok(self)
    }

    pub fn active_playback_ids(&self) -> Vec<String> {
        self.entries
            .iter()
            .filter(|entry| entry.status == PlaybackStatus::Started)
            .map(|entry| entry.playback_id.clone())
            .collect()
    }

    pub fn interrupt_active(
        &self,
        occurred_at_ms: u64,
        reason: impl Into<String>,
    ) -> Result<Self, VoiceContractError> {
        let reason = reason.into();
        require_non_empty("interruption reason", &reason)?;
        let mut entries = Vec::with_capacity(self.entries.len());
        for entry in &self.entries {
            let mut entry = entry.clone();
            if entry.status == PlaybackStatus::Started {
                if entry
                    .started_at_ms
                    .is_some_and(|started_at_ms| occurred_at_ms < started_at_ms)
                {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: "playback interruption occurred before playback start".to_string(),
                    });
                }
                entry.status = PlaybackStatus::Interrupted;
                entry.completed_at_ms = Some(occurred_at_ms);
                entry.acknowledged_at_ms = None;
                entry.reason = Some(reason.clone());
            }
            entries.push(entry);
        }
        Self::from_entries(entries)
    }

    pub fn start(
        &self,
        playback_id: impl Into<String>,
        occurred_at_ms: u64,
    ) -> Result<Self, VoiceContractError> {
        let playback_id = playback_id.into();
        require_non_empty("playback_id", &playback_id)?;
        let mut entries = Vec::with_capacity(self.entries.len());
        let mut found = false;
        for entry in &self.entries {
            let mut entry = entry.clone();
            if entry.playback_id == playback_id {
                found = true;
                if entry.status == PlaybackStatus::Started
                    && entry.started_at_ms == Some(occurred_at_ms)
                {
                    entries.push(entry);
                    continue;
                }
                if entry.status != PlaybackStatus::Queued {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: format!("cannot start playback in {:?} status", entry.status),
                    });
                }
                entry.status = PlaybackStatus::Started;
                entry.started_at_ms = Some(occurred_at_ms);
            }
            entries.push(entry);
        }
        if !found {
            return Err(VoiceContractError::Invalid {
                field_name: "playback_id",
                message: format!("{playback_id:?} is unknown"),
            });
        }
        Self::from_entries(entries)
    }

    pub fn complete(
        &self,
        playback_id: impl Into<String>,
        occurred_at_ms: u64,
    ) -> Result<Self, VoiceContractError> {
        let playback_id = playback_id.into();
        require_non_empty("playback_id", &playback_id)?;
        let mut entries = Vec::with_capacity(self.entries.len());
        let mut found = false;
        for entry in &self.entries {
            let mut entry = entry.clone();
            if entry.playback_id == playback_id {
                found = true;
                if entry.status == PlaybackStatus::Completed
                    && entry.completed_at_ms == Some(occurred_at_ms)
                {
                    entries.push(entry);
                    continue;
                }
                if entry.status != PlaybackStatus::Started {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: format!("cannot complete playback in {:?} status", entry.status),
                    });
                }
                if entry
                    .started_at_ms
                    .is_some_and(|started_at_ms| occurred_at_ms < started_at_ms)
                {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: "playback completion occurred before playback start".to_string(),
                    });
                }
                entry.status = PlaybackStatus::Completed;
                entry.completed_at_ms = Some(occurred_at_ms);
            }
            entries.push(entry);
        }
        if !found {
            return Err(VoiceContractError::Invalid {
                field_name: "playback_id",
                message: format!("{playback_id:?} is unknown"),
            });
        }
        Self::from_entries(entries)
    }

    pub fn acknowledge(
        &self,
        playback_id: impl Into<String>,
        occurred_at_ms: u64,
    ) -> Result<Self, VoiceContractError> {
        let playback_id = playback_id.into();
        require_non_empty("playback_id", &playback_id)?;
        let mut entries = Vec::with_capacity(self.entries.len());
        let mut found = false;
        for entry in &self.entries {
            let mut entry = entry.clone();
            if entry.playback_id == playback_id {
                found = true;
                if !matches!(
                    entry.status,
                    PlaybackStatus::Completed | PlaybackStatus::Interrupted
                ) {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: format!(
                            "cannot acknowledge playback in {:?} status",
                            entry.status
                        ),
                    });
                }
                if entry
                    .completed_at_ms
                    .is_some_and(|completed_at_ms| occurred_at_ms < completed_at_ms)
                {
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message: "playback acknowledgement occurred before playback completion"
                            .to_string(),
                    });
                }
                if let Some(acknowledged_at_ms) = entry.acknowledged_at_ms {
                    if acknowledged_at_ms == occurred_at_ms {
                        entries.push(entry);
                        continue;
                    }
                    return Err(VoiceContractError::Invalid {
                        field_name: "playback",
                        message:
                            "playback acknowledgement conflicts with the recorded acknowledgement"
                                .to_string(),
                    });
                }
                entry.acknowledged_at_ms = Some(occurred_at_ms);
            }
            entries.push(entry);
        }
        if !found {
            return Err(VoiceContractError::Invalid {
                field_name: "playback_id",
                message: format!("{playback_id:?} is unknown"),
            });
        }
        Self::from_entries(entries)
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "entries": self.entries.iter().map(PlaybackEntry::contract).collect::<Vec<_>>(),
        }))
    }
}

impl Default for PlaybackLedger {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InterruptionKind {
    Continue,
    Interrupt,
}

impl InterruptionKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Continue => "continue",
            Self::Interrupt => "interrupt",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct InterruptionDecision {
    pub classifier_id: String,
    pub session_id: String,
    pub kind: InterruptionKind,
    pub occurred_at_ms: u64,
    pub interrupted_playback_ids: Vec<String>,
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProviderInterruptionDecision {
    pub authority_id: String,
    pub session_id: String,
    pub kind: InterruptionKind,
    pub occurred_at_ms: u64,
    pub reason: Option<String>,
}

impl ProviderInterruptionDecision {
    pub fn new(
        authority_id: impl Into<String>,
        session_id: impl Into<String>,
        kind: InterruptionKind,
        occurred_at_ms: u64,
    ) -> Result<Self, VoiceContractError> {
        let decision = Self {
            authority_id: authority_id.into(),
            session_id: session_id.into(),
            kind,
            occurred_at_ms,
            reason: None,
        };
        decision.validate()?;
        Ok(decision)
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Result<Self, VoiceContractError> {
        let reason = reason.into();
        require_non_empty("interruption reason", &reason)?;
        self.reason = Some(reason);
        self.validate()?;
        Ok(self)
    }

    fn validate(&self) -> Result<(), VoiceContractError> {
        require_non_empty("authority_id", &self.authority_id)?;
        require_non_empty("session_id", &self.session_id)?;
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct InterruptionClassifier {
    pub classifier_id: String,
    pub provider_authority_id: Option<String>,
}

impl InterruptionClassifier {
    pub fn new(classifier_id: impl Into<String>) -> Result<Self, VoiceContractError> {
        let classifier = Self {
            classifier_id: classifier_id.into(),
            provider_authority_id: None,
        };
        require_non_empty("classifier_id", &classifier.classifier_id)?;
        Ok(classifier)
    }

    pub fn with_provider_authority_id(
        mut self,
        provider_authority_id: impl Into<String>,
    ) -> Result<Self, VoiceContractError> {
        let provider_authority_id = provider_authority_id.into();
        require_non_empty("provider_authority_id", &provider_authority_id)?;
        self.provider_authority_id = Some(provider_authority_id);
        Ok(self)
    }

    pub fn classify(
        &self,
        session_id: impl Into<String>,
        vad_decision: &VadDecision,
        playback: &PlaybackLedger,
        occurred_at_ms: u64,
    ) -> Result<InterruptionDecision, VoiceContractError> {
        self.classify_with_provider_decision(
            session_id,
            vad_decision,
            playback,
            occurred_at_ms,
            None,
        )
    }

    pub fn classify_with_provider_decision(
        &self,
        session_id: impl Into<String>,
        vad_decision: &VadDecision,
        playback: &PlaybackLedger,
        occurred_at_ms: u64,
        provider_decision: Option<&ProviderInterruptionDecision>,
    ) -> Result<InterruptionDecision, VoiceContractError> {
        let session_id = session_id.into();
        require_non_empty("session_id", &session_id)?;
        let active_ids = playback.active_playback_ids();
        if let Some(provider_authority_id) = &self.provider_authority_id {
            let Some(provider_decision) = provider_decision else {
                return Ok(InterruptionDecision {
                    classifier_id: self.classifier_id.clone(),
                    session_id,
                    kind: InterruptionKind::Continue,
                    occurred_at_ms,
                    interrupted_playback_ids: Vec::new(),
                    reason: Some("awaiting_provider_confirmation".to_string()),
                });
            };
            if provider_decision.authority_id != *provider_authority_id {
                return Err(VoiceContractError::Invalid {
                    field_name: "provider_authority_id",
                    message: "does not match classifier authority".to_string(),
                });
            }
            if provider_decision.session_id != session_id {
                return Err(VoiceContractError::Invalid {
                    field_name: "provider_session_id",
                    message: "belongs to a different session".to_string(),
                });
            }
            if provider_decision.occurred_at_ms > occurred_at_ms {
                return Err(VoiceContractError::Invalid {
                    field_name: "provider_interruption_decision",
                    message: "occurred after interruption classification".to_string(),
                });
            }
            if provider_decision.kind == InterruptionKind::Interrupt {
                let interrupted_playback_ids = playback
                    .entries
                    .iter()
                    .filter(|entry| {
                        entry.status == PlaybackStatus::Started
                            && entry.started_at_ms.is_some_and(|started_at_ms| {
                                started_at_ms <= provider_decision.occurred_at_ms
                            })
                    })
                    .map(|entry| entry.playback_id.clone())
                    .collect();
                return Ok(InterruptionDecision {
                    classifier_id: self.classifier_id.clone(),
                    session_id,
                    kind: InterruptionKind::Interrupt,
                    occurred_at_ms: provider_decision.occurred_at_ms,
                    interrupted_playback_ids,
                    reason: Some(
                        provider_decision
                            .reason
                            .clone()
                            .unwrap_or_else(|| "provider_confirmed_interruption".to_string()),
                    ),
                });
            }
            return Ok(InterruptionDecision {
                classifier_id: self.classifier_id.clone(),
                session_id,
                kind: InterruptionKind::Continue,
                occurred_at_ms: provider_decision.occurred_at_ms,
                interrupted_playback_ids: Vec::new(),
                reason: provider_decision.reason.clone(),
            });
        }
        if !active_ids.is_empty()
            && matches!(
                vad_decision.kind,
                VadDecisionKind::SpeechStart | VadDecisionKind::Speech
            )
        {
            return Ok(InterruptionDecision {
                classifier_id: self.classifier_id.clone(),
                session_id,
                kind: InterruptionKind::Interrupt,
                occurred_at_ms,
                interrupted_playback_ids: active_ids,
                reason: Some("user_speech_during_playback".to_string()),
            });
        }
        Ok(InterruptionDecision {
            classifier_id: self.classifier_id.clone(),
            session_id,
            kind: InterruptionKind::Continue,
            occurred_at_ms,
            interrupted_playback_ids: Vec::new(),
            reason: None,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RealtimeSessionRequest {
    pub session: DuplexSession,
    pub model: String,
    pub instructions: String,
    pub modalities: Vec<String>,
    pub tools: Vec<String>,
}

impl RealtimeSessionRequest {
    pub fn new(
        session: DuplexSession,
        model: impl Into<String>,
        instructions: impl Into<String>,
    ) -> Result<Self, VoiceContractError> {
        let request = Self {
            session,
            model: model.into(),
            instructions: instructions.into(),
            modalities: vec!["audio".to_string()],
            tools: Vec::new(),
        };
        request.validated()
    }

    pub fn with_modalities<I, S>(mut self, modalities: I) -> Result<Self, VoiceContractError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.modalities = modalities.into_iter().map(Into::into).collect();
        self.validated()
    }

    pub fn with_tools<I, S>(mut self, tools: I) -> Result<Self, VoiceContractError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.tools = tools.into_iter().map(Into::into).collect();
        self.validated()
    }

    pub fn with_tool(mut self, tool_name: impl Into<String>) -> Result<Self, VoiceContractError> {
        let tool_name = tool_name.into();
        require_non_empty("tool_name", &tool_name)?;
        self.tools.push(tool_name);
        self.validated()
    }

    fn validated(mut self) -> Result<Self, VoiceContractError> {
        require_non_empty("model", &self.model)?;
        require_non_empty("instructions", &self.instructions)?;
        for modality in &self.modalities {
            require_non_empty("modality", modality)?;
        }
        for tool in &self.tools {
            require_non_empty("tool_name", tool)?;
        }
        self.modalities = sorted_unique(self.modalities);
        self.tools = sorted_unique(self.tools);
        Ok(self)
    }

    pub fn provider_contract(&self) -> Value {
        json!({
            "sessionId": self.session.session_id,
            "model": self.model,
            "instructions": self.instructions,
            "modalities": self.modalities,
            "transport": self.session.transport.contract(),
            "tools": self.tools,
            "turnId": self.session.current_turn_id,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RealtimeProviderAdapter {
    pub adapter_id: String,
    pub endpoint: String,
    pub auth_secret_ref: String,
    pub default_model: Option<String>,
    pub default_instructions: Option<String>,
    pub options: BTreeMap<String, Value>,
}

impl RealtimeProviderAdapter {
    pub fn new(
        adapter_id: impl Into<String>,
        endpoint: impl Into<String>,
        auth_secret_ref: impl Into<String>,
    ) -> Result<Self, VoiceContractError> {
        let adapter = Self {
            adapter_id: adapter_id.into(),
            endpoint: endpoint.into(),
            auth_secret_ref: auth_secret_ref.into(),
            default_model: None,
            default_instructions: None,
            options: BTreeMap::new(),
        };
        adapter.validate()?;
        Ok(adapter)
    }

    pub fn with_default_model(mut self, model: impl Into<String>) -> Self {
        self.default_model = Some(model.into());
        self
    }

    pub fn with_default_instructions(
        mut self,
        instructions: impl Into<String>,
    ) -> Result<Self, VoiceContractError> {
        let instructions = instructions.into();
        require_non_empty("default_instructions", &instructions)?;
        self.default_instructions = Some(instructions);
        Ok(self)
    }

    pub fn with_option(
        mut self,
        key: impl Into<String>,
        value: Value,
    ) -> Result<Self, VoiceContractError> {
        let key = key.into();
        require_non_empty("adapter option", &key)?;
        self.options.insert(key, value);
        Ok(self)
    }

    pub fn build_session_request<I, S, J, T>(
        &self,
        session: DuplexSession,
        model: Option<String>,
        instructions: Option<String>,
        modalities: I,
        tools: J,
    ) -> Result<RealtimeProviderSessionRequest, VoiceContractError>
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
        J: IntoIterator<Item = T>,
        T: Into<String>,
    {
        self.validate()?;
        let model = model
            .or_else(|| self.default_model.clone())
            .ok_or_else(|| VoiceContractError::Invalid {
                field_name: "model",
                message: "must be provided by request or adapter default".to_string(),
            })?;
        let instructions = instructions
            .or_else(|| self.default_instructions.clone())
            .ok_or_else(|| VoiceContractError::Invalid {
                field_name: "instructions",
                message: "must be provided by request or adapter default".to_string(),
            })?;
        let request = RealtimeSessionRequest::new(session, model, instructions)?
            .with_modalities(modalities)?
            .with_tools(tools)?;
        RealtimeProviderSessionRequest::new(
            self.adapter_id.clone(),
            self.endpoint.clone(),
            self.auth_secret_ref.clone(),
            request,
            self.options.clone(),
        )
    }

    fn validate(&self) -> Result<(), VoiceContractError> {
        require_non_empty("adapter_id", &self.adapter_id)?;
        require_non_empty("endpoint", &self.endpoint)?;
        require_non_empty("auth_secret_ref", &self.auth_secret_ref)?;
        if let Some(model) = &self.default_model {
            require_non_empty("default_model", model)?;
        }
        if let Some(instructions) = &self.default_instructions {
            require_non_empty("default_instructions", instructions)?;
        }
        for key in self.options.keys() {
            require_non_empty("adapter option", key)?;
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RealtimeProviderSessionRequest {
    pub adapter_id: String,
    pub endpoint: String,
    pub auth_secret_ref: String,
    pub request: RealtimeSessionRequest,
    pub options: BTreeMap<String, Value>,
}

impl RealtimeProviderSessionRequest {
    pub fn new(
        adapter_id: impl Into<String>,
        endpoint: impl Into<String>,
        auth_secret_ref: impl Into<String>,
        request: RealtimeSessionRequest,
        options: BTreeMap<String, Value>,
    ) -> Result<Self, VoiceContractError> {
        let provider_request = Self {
            adapter_id: adapter_id.into(),
            endpoint: endpoint.into(),
            auth_secret_ref: auth_secret_ref.into(),
            request,
            options,
        };
        provider_request.validate()?;
        Ok(provider_request)
    }

    fn validate(&self) -> Result<(), VoiceContractError> {
        require_non_empty("adapter_id", &self.adapter_id)?;
        require_non_empty("endpoint", &self.endpoint)?;
        require_non_empty("auth_secret_ref", &self.auth_secret_ref)?;
        for key in self.options.keys() {
            require_non_empty("adapter option", key)?;
        }
        Ok(())
    }

    pub fn provider_envelope(&self) -> Value {
        json!({
            "provider": self.adapter_id,
            "endpoint": self.endpoint,
            "authSecretRef": self.auth_secret_ref,
            "request": self.request.provider_contract(),
            "options": self.options,
        })
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&self.provider_envelope())
    }
}
