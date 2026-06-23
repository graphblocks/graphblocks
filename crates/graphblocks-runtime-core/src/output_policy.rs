use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GenerationChunk {
    pub stream_id: String,
    pub response_id: String,
    pub sequence: u64,
    pub text: String,
}

impl GenerationChunk {
    pub fn text(
        stream_id: impl Into<String>,
        response_id: impl Into<String>,
        sequence: u64,
        text: impl Into<String>,
    ) -> Self {
        Self {
            stream_id: stream_id.into(),
            response_id: response_id.into(),
            sequence,
            text: text.into(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutputDisposition {
    Allow,
    Hold,
    Redact,
    Replace,
    AbortResponse,
    AbortTurn,
    DenyCommit,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProviderCancellation {
    None,
    Request,
    RequiredIfSupported,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DraftDisposition {
    Keep,
    MarkIncomplete,
    Retract,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PendingToolCallsDisposition {
    Keep,
    Deny,
    CancelAdmitted,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeliveryMode {
    BufferUntilCommit,
    BoundedHoldback,
    ImmediateDraft,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum FlushBoundary {
    Token,
    Sentence,
    Paragraph,
    ContentPart,
    ToolCall,
    Response,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ViolationAction {
    AbortResponse,
    AbortTurn,
    Redact,
    Replace,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutputDeliveryPolicy {
    pub mode: DeliveryMode,
    pub holdback_max_tokens: Option<u64>,
    pub holdback_max_bytes: Option<u64>,
    pub holdback_max_duration_ms: Option<u64>,
    pub flush_boundaries: BTreeSet<FlushBoundary>,
    pub on_violation: ViolationAction,
    pub delivered_draft_disposition: DraftDisposition,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutputDeliveryPolicyError {
    UnboundedPolicyHoldback,
    ImmediateDraftWithoutRetractionSupport,
    InvalidHoldbackMaxTokens,
    InvalidHoldbackMaxBytes,
    InvalidHoldbackMaxDuration,
}

impl OutputDeliveryPolicy {
    pub fn buffer_until_commit(on_violation: ViolationAction) -> Self {
        Self {
            mode: DeliveryMode::BufferUntilCommit,
            holdback_max_tokens: None,
            holdback_max_bytes: None,
            holdback_max_duration_ms: None,
            flush_boundaries: BTreeSet::new(),
            on_violation,
            delivered_draft_disposition: DraftDisposition::Retract,
        }
    }

    pub fn bounded_holdback(
        on_violation: ViolationAction,
        delivered_draft_disposition: DraftDisposition,
    ) -> Self {
        Self {
            mode: DeliveryMode::BoundedHoldback,
            holdback_max_tokens: None,
            holdback_max_bytes: None,
            holdback_max_duration_ms: None,
            flush_boundaries: BTreeSet::new(),
            on_violation,
            delivered_draft_disposition,
        }
    }

    pub fn immediate_draft(
        on_violation: ViolationAction,
        delivered_draft_disposition: DraftDisposition,
    ) -> Self {
        Self {
            mode: DeliveryMode::ImmediateDraft,
            holdback_max_tokens: None,
            holdback_max_bytes: None,
            holdback_max_duration_ms: None,
            flush_boundaries: BTreeSet::new(),
            on_violation,
            delivered_draft_disposition,
        }
    }

    pub fn with_holdback_max_tokens(mut self, holdback_max_tokens: u64) -> Self {
        self.holdback_max_tokens = Some(holdback_max_tokens);
        self
    }

    pub fn with_holdback_max_bytes(mut self, holdback_max_bytes: u64) -> Self {
        self.holdback_max_bytes = Some(holdback_max_bytes);
        self
    }

    pub fn with_holdback_max_duration_ms(mut self, holdback_max_duration_ms: u64) -> Self {
        self.holdback_max_duration_ms = Some(holdback_max_duration_ms);
        self
    }

    pub fn flush_on(mut self, boundaries: impl IntoIterator<Item = FlushBoundary>) -> Self {
        self.flush_boundaries = boundaries.into_iter().collect();
        self
    }

    pub fn validate(&self) -> Result<(), OutputDeliveryPolicyError> {
        if self.holdback_max_tokens == Some(0) {
            return Err(OutputDeliveryPolicyError::InvalidHoldbackMaxTokens);
        }
        if self.holdback_max_bytes == Some(0) {
            return Err(OutputDeliveryPolicyError::InvalidHoldbackMaxBytes);
        }
        if self.holdback_max_duration_ms == Some(0) {
            return Err(OutputDeliveryPolicyError::InvalidHoldbackMaxDuration);
        }

        match self.mode {
            DeliveryMode::BufferUntilCommit => Ok(()),
            DeliveryMode::BoundedHoldback => {
                if self.holdback_max_tokens.is_none()
                    && self.holdback_max_bytes.is_none()
                    && self.holdback_max_duration_ms.is_none()
                {
                    return Err(OutputDeliveryPolicyError::UnboundedPolicyHoldback);
                }
                Ok(())
            }
            DeliveryMode::ImmediateDraft => {
                if self.delivered_draft_disposition == DraftDisposition::Keep {
                    return Err(OutputDeliveryPolicyError::ImmediateDraftWithoutRetractionSupport);
                }
                Ok(())
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutputPolicyDecision {
    pub decision_id: String,
    pub disposition: OutputDisposition,
    pub accepted_through_sequence: Option<u64>,
    pub replacement_chunks: Vec<GenerationChunk>,
    pub provider_cancellation: ProviderCancellation,
    pub draft_disposition: DraftDisposition,
    pub pending_tool_calls: PendingToolCallsDisposition,
    pub evaluated_at_unix_ms: Option<u64>,
    pub input_digest: String,
}

impl OutputPolicyDecision {
    pub fn allow(
        decision_id: impl Into<String>,
        accepted_through_sequence: Option<u64>,
        input_digest: impl Into<String>,
    ) -> Self {
        Self {
            decision_id: decision_id.into(),
            disposition: OutputDisposition::Allow,
            accepted_through_sequence,
            replacement_chunks: Vec::new(),
            provider_cancellation: ProviderCancellation::Request,
            draft_disposition: DraftDisposition::Keep,
            pending_tool_calls: PendingToolCallsDisposition::Keep,
            evaluated_at_unix_ms: None,
            input_digest: input_digest.into(),
        }
    }

    pub fn hold(decision_id: impl Into<String>, input_digest: impl Into<String>) -> Self {
        Self {
            decision_id: decision_id.into(),
            disposition: OutputDisposition::Hold,
            accepted_through_sequence: None,
            replacement_chunks: Vec::new(),
            provider_cancellation: ProviderCancellation::Request,
            draft_disposition: DraftDisposition::Keep,
            pending_tool_calls: PendingToolCallsDisposition::Keep,
            evaluated_at_unix_ms: None,
            input_digest: input_digest.into(),
        }
    }

    pub fn redact(
        decision_id: impl Into<String>,
        accepted_through_sequence: Option<u64>,
        replacement_chunks: impl IntoIterator<Item = GenerationChunk>,
        input_digest: impl Into<String>,
    ) -> Self {
        Self {
            decision_id: decision_id.into(),
            disposition: OutputDisposition::Redact,
            accepted_through_sequence,
            replacement_chunks: replacement_chunks.into_iter().collect(),
            provider_cancellation: ProviderCancellation::Request,
            draft_disposition: DraftDisposition::Keep,
            pending_tool_calls: PendingToolCallsDisposition::Keep,
            evaluated_at_unix_ms: None,
            input_digest: input_digest.into(),
        }
    }

    pub fn replace(
        decision_id: impl Into<String>,
        accepted_through_sequence: Option<u64>,
        replacement_chunks: impl IntoIterator<Item = GenerationChunk>,
        input_digest: impl Into<String>,
    ) -> Self {
        Self {
            decision_id: decision_id.into(),
            disposition: OutputDisposition::Replace,
            accepted_through_sequence,
            replacement_chunks: replacement_chunks.into_iter().collect(),
            provider_cancellation: ProviderCancellation::Request,
            draft_disposition: DraftDisposition::Keep,
            pending_tool_calls: PendingToolCallsDisposition::Keep,
            evaluated_at_unix_ms: None,
            input_digest: input_digest.into(),
        }
    }

    pub fn abort_response(decision_id: impl Into<String>, input_digest: impl Into<String>) -> Self {
        Self {
            decision_id: decision_id.into(),
            disposition: OutputDisposition::AbortResponse,
            accepted_through_sequence: None,
            replacement_chunks: Vec::new(),
            provider_cancellation: ProviderCancellation::Request,
            draft_disposition: DraftDisposition::Retract,
            pending_tool_calls: PendingToolCallsDisposition::Deny,
            evaluated_at_unix_ms: None,
            input_digest: input_digest.into(),
        }
    }

    pub fn abort_turn(decision_id: impl Into<String>, input_digest: impl Into<String>) -> Self {
        Self {
            decision_id: decision_id.into(),
            disposition: OutputDisposition::AbortTurn,
            accepted_through_sequence: None,
            replacement_chunks: Vec::new(),
            provider_cancellation: ProviderCancellation::Request,
            draft_disposition: DraftDisposition::Retract,
            pending_tool_calls: PendingToolCallsDisposition::Deny,
            evaluated_at_unix_ms: None,
            input_digest: input_digest.into(),
        }
    }

    pub fn deny_commit(decision_id: impl Into<String>, input_digest: impl Into<String>) -> Self {
        Self {
            decision_id: decision_id.into(),
            disposition: OutputDisposition::DenyCommit,
            accepted_through_sequence: None,
            replacement_chunks: Vec::new(),
            provider_cancellation: ProviderCancellation::Request,
            draft_disposition: DraftDisposition::Retract,
            pending_tool_calls: PendingToolCallsDisposition::Deny,
            evaluated_at_unix_ms: None,
            input_digest: input_digest.into(),
        }
    }

    pub fn with_provider_cancellation(mut self, cancellation: ProviderCancellation) -> Self {
        self.provider_cancellation = cancellation;
        self
    }

    pub fn with_draft_disposition(mut self, disposition: DraftDisposition) -> Self {
        self.draft_disposition = disposition;
        self
    }

    pub fn with_pending_tool_calls(mut self, disposition: PendingToolCallsDisposition) -> Self {
        self.pending_tool_calls = disposition;
        self
    }

    pub fn evaluated_at_unix_ms(mut self, evaluated_at_unix_ms: u64) -> Self {
        self.evaluated_at_unix_ms = Some(evaluated_at_unix_ms);
        self
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TerminalReason {
    PolicyDenied,
    BudgetExhausted,
    Cancelled,
    ClientDisconnected,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DurableResult {
    None,
    Incomplete,
    Partial,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutputCutoff {
    pub stream_id: String,
    pub response_id: String,
    pub turn_id: Option<String>,
    pub last_generated_sequence: u64,
    pub last_policy_accepted_sequence: u64,
    pub last_client_delivered_sequence: u64,
    pub terminal_reason: TerminalReason,
    pub draft_disposition: DraftDisposition,
    pub durable_result: DurableResult,
    pub policy_decision_id: Option<String>,
    pub occurred_at_unix_ms: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum OutputGateError {
    InvalidDeliveryPolicy {
        source: OutputDeliveryPolicyError,
    },
    StreamMismatch {
        expected_stream_id: String,
        actual_stream_id: String,
    },
    ResponseMismatch {
        expected_response_id: String,
        actual_response_id: String,
    },
    NonMonotonicSequence {
        last_generated_sequence: u64,
        attempted_sequence: u64,
    },
    PolicyStopped,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutputGateUpdate {
    pub deliverable: Vec<GenerationChunk>,
    pub cutoff: Option<OutputCutoff>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutputDeliveryGate {
    stream_id: String,
    response_id: String,
    turn_id: Option<String>,
    delivery_policy: OutputDeliveryPolicy,
    pending: BTreeMap<u64, GenerationChunk>,
    last_generated_sequence: u64,
    last_policy_accepted_sequence: u64,
    last_client_delivered_sequence: u64,
    stopped: Option<OutputCutoff>,
}

impl OutputDeliveryGate {
    pub fn new(stream_id: impl Into<String>, response_id: impl Into<String>) -> Self {
        Self {
            stream_id: stream_id.into(),
            response_id: response_id.into(),
            turn_id: None,
            delivery_policy: OutputDeliveryPolicy::bounded_holdback(
                ViolationAction::AbortResponse,
                DraftDisposition::Retract,
            )
            .with_holdback_max_tokens(1),
            pending: BTreeMap::new(),
            last_generated_sequence: 0,
            last_policy_accepted_sequence: 0,
            last_client_delivered_sequence: 0,
            stopped: None,
        }
    }

    pub fn with_turn_id(mut self, turn_id: impl Into<String>) -> Self {
        self.turn_id = Some(turn_id.into());
        self
    }

    pub fn with_delivery_policy(
        mut self,
        delivery_policy: OutputDeliveryPolicy,
    ) -> Result<Self, OutputGateError> {
        delivery_policy
            .validate()
            .map_err(|source| OutputGateError::InvalidDeliveryPolicy { source })?;
        self.delivery_policy = delivery_policy;
        Ok(self)
    }

    pub fn last_generated_sequence(&self) -> u64 {
        self.last_generated_sequence
    }

    pub fn last_policy_accepted_sequence(&self) -> u64 {
        self.last_policy_accepted_sequence
    }

    pub fn last_client_delivered_sequence(&self) -> u64 {
        self.last_client_delivered_sequence
    }

    pub fn cutoff(&self) -> Option<&OutputCutoff> {
        self.stopped.as_ref()
    }

    pub fn commit_accepted_output(&mut self) -> Vec<GenerationChunk> {
        if self.stopped.is_some() {
            return Vec::new();
        }

        let delivered_after = self.last_client_delivered_sequence + 1;
        let accepted_through = self.last_policy_accepted_sequence;
        if delivered_after > accepted_through {
            return Vec::new();
        }

        let ready_sequences = self
            .pending
            .range(delivered_after..=accepted_through)
            .map(|(sequence, _)| *sequence)
            .collect::<Vec<_>>();
        let mut deliverable = Vec::new();
        for sequence in ready_sequences {
            if let Some(chunk) = self.pending.remove(&sequence) {
                self.last_client_delivered_sequence = sequence;
                deliverable.push(chunk);
            }
        }
        deliverable
    }

    pub fn record_chunk(&mut self, chunk: GenerationChunk) -> Result<(), OutputGateError> {
        if self.stopped.is_some() {
            return Err(OutputGateError::PolicyStopped);
        }
        if chunk.stream_id != self.stream_id {
            return Err(OutputGateError::StreamMismatch {
                expected_stream_id: self.stream_id.clone(),
                actual_stream_id: chunk.stream_id,
            });
        }
        if chunk.response_id != self.response_id {
            return Err(OutputGateError::ResponseMismatch {
                expected_response_id: self.response_id.clone(),
                actual_response_id: chunk.response_id,
            });
        }
        if chunk.sequence <= self.last_generated_sequence {
            return Err(OutputGateError::NonMonotonicSequence {
                last_generated_sequence: self.last_generated_sequence,
                attempted_sequence: chunk.sequence,
            });
        }

        self.last_generated_sequence = chunk.sequence;
        self.pending.insert(chunk.sequence, chunk);
        Ok(())
    }

    pub fn apply_decision(
        &mut self,
        decision: OutputPolicyDecision,
        occurred_at_unix_ms: u64,
    ) -> Result<OutputGateUpdate, OutputGateError> {
        if self.stopped.is_some() {
            return Err(OutputGateError::PolicyStopped);
        }

        match decision.disposition {
            OutputDisposition::Allow => {
                if let Some(accepted_through_sequence) = decision.accepted_through_sequence {
                    if accepted_through_sequence > self.last_policy_accepted_sequence {
                        self.last_policy_accepted_sequence = accepted_through_sequence;
                    }
                }

                let deliverable = match self.delivery_policy.mode {
                    DeliveryMode::BufferUntilCommit => Vec::new(),
                    DeliveryMode::BoundedHoldback | DeliveryMode::ImmediateDraft => {
                        self.commit_accepted_output()
                    }
                };

                Ok(OutputGateUpdate {
                    deliverable,
                    cutoff: None,
                })
            }
            OutputDisposition::Hold => Ok(OutputGateUpdate {
                deliverable: Vec::new(),
                cutoff: None,
            }),
            OutputDisposition::Redact | OutputDisposition::Replace => {
                if decision.disposition == OutputDisposition::Redact
                    && decision.replacement_chunks.is_empty()
                {
                    return Ok(OutputGateUpdate {
                        deliverable: Vec::new(),
                        cutoff: None,
                    });
                }

                if decision.disposition == OutputDisposition::Replace {
                    if let Some(accepted_through_sequence) = decision.accepted_through_sequence {
                        let delivered_after = self.last_client_delivered_sequence + 1;
                        let replaced_sequences = self
                            .pending
                            .range(delivered_after..=accepted_through_sequence)
                            .map(|(sequence, _)| *sequence)
                            .collect::<Vec<_>>();
                        for sequence in replaced_sequences {
                            self.pending.remove(&sequence);
                        }
                    }
                }

                for chunk in decision.replacement_chunks {
                    if chunk.stream_id != self.stream_id {
                        return Err(OutputGateError::StreamMismatch {
                            expected_stream_id: self.stream_id.clone(),
                            actual_stream_id: chunk.stream_id,
                        });
                    }
                    if chunk.response_id != self.response_id {
                        return Err(OutputGateError::ResponseMismatch {
                            expected_response_id: self.response_id.clone(),
                            actual_response_id: chunk.response_id,
                        });
                    }
                    if chunk.sequence > self.last_client_delivered_sequence {
                        self.pending.insert(chunk.sequence, chunk);
                    }
                }

                if let Some(accepted_through_sequence) = decision.accepted_through_sequence {
                    if accepted_through_sequence > self.last_policy_accepted_sequence {
                        self.last_policy_accepted_sequence = accepted_through_sequence;
                    }
                }

                let deliverable = match self.delivery_policy.mode {
                    DeliveryMode::BufferUntilCommit => Vec::new(),
                    DeliveryMode::BoundedHoldback | DeliveryMode::ImmediateDraft => {
                        self.commit_accepted_output()
                    }
                };

                Ok(OutputGateUpdate {
                    deliverable,
                    cutoff: None,
                })
            }
            OutputDisposition::AbortResponse
            | OutputDisposition::AbortTurn
            | OutputDisposition::DenyCommit => {
                let cutoff = OutputCutoff {
                    stream_id: self.stream_id.clone(),
                    response_id: self.response_id.clone(),
                    turn_id: self.turn_id.clone(),
                    last_generated_sequence: self.last_generated_sequence,
                    last_policy_accepted_sequence: self.last_policy_accepted_sequence,
                    last_client_delivered_sequence: self.last_client_delivered_sequence,
                    terminal_reason: TerminalReason::PolicyDenied,
                    draft_disposition: decision.draft_disposition,
                    durable_result: DurableResult::None,
                    policy_decision_id: Some(decision.decision_id),
                    occurred_at_unix_ms,
                };
                self.pending.clear();
                self.stopped = Some(cutoff.clone());
                Ok(OutputGateUpdate {
                    deliverable: Vec::new(),
                    cutoff: Some(cutoff),
                })
            }
        }
    }
}
