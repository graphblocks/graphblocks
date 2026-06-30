use std::collections::{BTreeMap, BTreeSet};

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::json;

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

    pub fn validate(&self) -> Result<(), GenerationChunkError> {
        for (field, value) in [
            ("stream_id", self.stream_id.as_str()),
            ("response_id", self.response_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(GenerationChunkError::EmptyIdentityField { field });
            }
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GenerationChunkError {
    EmptyIdentityField { field: &'static str },
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

impl OutputDisposition {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Hold => "hold",
            Self::Redact => "redact",
            Self::Replace => "replace",
            Self::AbortResponse => "abort_response",
            Self::AbortTurn => "abort_turn",
            Self::DenyCommit => "deny_commit",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProviderCancellation {
    None,
    Request,
    RequiredIfSupported,
}

impl ProviderCancellation {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Request => "request",
            Self::RequiredIfSupported => "required_if_supported",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DraftDisposition {
    Keep,
    MarkIncomplete,
    Retract,
}

impl DraftDisposition {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Keep => "keep",
            Self::MarkIncomplete => "mark_incomplete",
            Self::Retract => "retract",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PendingToolCallsDisposition {
    Keep,
    Deny,
    CancelAdmitted,
}

impl PendingToolCallsDisposition {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Keep => "keep",
            Self::Deny => "deny",
            Self::CancelAdmitted => "cancel_admitted",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeliveryMode {
    BufferUntilCommit,
    BoundedHoldback,
    ImmediateDraft,
}

impl DeliveryMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BufferUntilCommit => "buffer_until_commit",
            Self::BoundedHoldback => "bounded_holdback",
            Self::ImmediateDraft => "immediate_draft",
        }
    }
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

impl FlushBoundary {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Token => "token",
            Self::Sentence => "sentence",
            Self::Paragraph => "paragraph",
            Self::ContentPart => "content_part",
            Self::ToolCall => "tool_call",
            Self::Response => "response",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ViolationAction {
    AbortResponse,
    AbortTurn,
    Redact,
    Replace,
}

impl ViolationAction {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AbortResponse => "abort_response",
            Self::AbortTurn => "abort_turn",
            Self::Redact => "redact",
            Self::Replace => "replace",
        }
    }
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
pub struct RedactionInstruction {
    pub path: String,
    pub start: u64,
    pub end: u64,
    pub replacement: String,
}

impl RedactionInstruction {
    pub fn text_range(
        path: impl Into<String>,
        start: u64,
        end: u64,
        replacement: impl Into<String>,
    ) -> Self {
        Self {
            path: path.into(),
            start,
            end,
            replacement: replacement.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutputPolicyDecision {
    pub decision_id: String,
    pub disposition: OutputDisposition,
    pub accepted_through_sequence: Option<u64>,
    pub replacement_chunks: Vec<GenerationChunk>,
    pub redactions: Vec<RedactionInstruction>,
    pub reason_codes: Vec<String>,
    pub policy_refs: Vec<String>,
    pub provider_cancellation: ProviderCancellation,
    pub draft_disposition: DraftDisposition,
    pub pending_tool_calls: PendingToolCallsDisposition,
    pub evaluated_at_unix_ms: Option<u64>,
    pub input_digest: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum OutputPolicyDecisionError {
    MissingDecisionId,
    MissingInputDigest { decision_id: String },
    ReplacementContentMissing { decision_id: String },
    InvalidReplacementChunk { source: GenerationChunkError },
    InvalidRedactionInstruction { path: String },
    InvalidReasonCode { reason_code: String },
    InvalidPolicyRef { policy_ref: String },
}

impl OutputPolicyDecision {
    pub fn validate(&self) -> Result<(), OutputPolicyDecisionError> {
        if self.decision_id.trim().is_empty() {
            return Err(OutputPolicyDecisionError::MissingDecisionId);
        }
        if self.input_digest.trim().is_empty() {
            return Err(OutputPolicyDecisionError::MissingInputDigest {
                decision_id: self.decision_id.clone(),
            });
        }
        if self.disposition == OutputDisposition::Replace && self.replacement_chunks.is_empty() {
            return Err(OutputPolicyDecisionError::ReplacementContentMissing {
                decision_id: self.decision_id.clone(),
            });
        }
        for chunk in &self.replacement_chunks {
            chunk
                .validate()
                .map_err(|source| OutputPolicyDecisionError::InvalidReplacementChunk { source })?;
        }
        for redaction in &self.redactions {
            if redaction.path.trim().is_empty() || redaction.start > redaction.end {
                return Err(OutputPolicyDecisionError::InvalidRedactionInstruction {
                    path: redaction.path.clone(),
                });
            }
        }
        for reason_code in &self.reason_codes {
            if reason_code.trim().is_empty() {
                return Err(OutputPolicyDecisionError::InvalidReasonCode {
                    reason_code: reason_code.clone(),
                });
            }
        }
        for policy_ref in &self.policy_refs {
            if policy_ref.trim().is_empty() {
                return Err(OutputPolicyDecisionError::InvalidPolicyRef {
                    policy_ref: policy_ref.clone(),
                });
            }
        }
        Ok(())
    }

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
            redactions: Vec::new(),
            reason_codes: Vec::new(),
            policy_refs: Vec::new(),
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
            redactions: Vec::new(),
            reason_codes: Vec::new(),
            policy_refs: Vec::new(),
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
            redactions: Vec::new(),
            reason_codes: Vec::new(),
            policy_refs: Vec::new(),
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
            redactions: Vec::new(),
            reason_codes: Vec::new(),
            policy_refs: Vec::new(),
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
            redactions: Vec::new(),
            reason_codes: Vec::new(),
            policy_refs: Vec::new(),
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
            redactions: Vec::new(),
            reason_codes: Vec::new(),
            policy_refs: Vec::new(),
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
            redactions: Vec::new(),
            reason_codes: Vec::new(),
            policy_refs: Vec::new(),
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

    pub fn with_redactions<I>(mut self, redactions: I) -> Self
    where
        I: IntoIterator<Item = RedactionInstruction>,
    {
        self.redactions = redactions.into_iter().collect();
        self
    }

    pub fn with_reason_codes<I, S>(mut self, reason_codes: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.reason_codes = reason_codes.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_policy_refs<I, S>(mut self, policy_refs: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.policy_refs = policy_refs.into_iter().map(Into::into).collect();
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

    pub fn with_accepted_through_sequence(mut self, accepted_through_sequence: u64) -> Self {
        self.accepted_through_sequence = Some(accepted_through_sequence);
        self
    }

    pub fn evaluated_at_unix_ms(mut self, evaluated_at_unix_ms: u64) -> Self {
        self.evaluated_at_unix_ms = Some(evaluated_at_unix_ms);
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeclarativeOutputPolicyRule {
    pub rule_id: String,
    pub literal: String,
    pub disposition: OutputDisposition,
    pub replacement: Option<String>,
    pub reason_codes: Vec<String>,
    pub policy_refs: Vec<String>,
    pub priority: i64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DeclarativeOutputPolicyRuleError {
    EmptyRuleId,
    EmptyLiteral {
        rule_id: String,
    },
    ReplacementRequired {
        rule_id: String,
        disposition: OutputDisposition,
    },
    InvalidReasonCode {
        rule_id: String,
        reason_code: String,
    },
    InvalidPolicyRef {
        rule_id: String,
        policy_ref: String,
    },
}

impl DeclarativeOutputPolicyRule {
    pub fn new(
        rule_id: impl Into<String>,
        literal: impl Into<String>,
        disposition: OutputDisposition,
    ) -> Self {
        Self {
            rule_id: rule_id.into(),
            literal: literal.into(),
            disposition,
            replacement: None,
            reason_codes: Vec::new(),
            policy_refs: Vec::new(),
            priority: 0,
        }
    }

    pub fn validate(&self) -> Result<(), DeclarativeOutputPolicyRuleError> {
        if self.rule_id.trim().is_empty() {
            return Err(DeclarativeOutputPolicyRuleError::EmptyRuleId);
        }
        if self.literal.is_empty() {
            return Err(DeclarativeOutputPolicyRuleError::EmptyLiteral {
                rule_id: self.rule_id.clone(),
            });
        }
        if matches!(
            self.disposition,
            OutputDisposition::Redact | OutputDisposition::Replace
        ) && self.replacement.is_none()
        {
            return Err(DeclarativeOutputPolicyRuleError::ReplacementRequired {
                rule_id: self.rule_id.clone(),
                disposition: self.disposition,
            });
        }
        for reason_code in &self.reason_codes {
            if reason_code.trim().is_empty() {
                return Err(DeclarativeOutputPolicyRuleError::InvalidReasonCode {
                    rule_id: self.rule_id.clone(),
                    reason_code: reason_code.clone(),
                });
            }
        }
        for policy_ref in &self.policy_refs {
            if policy_ref.trim().is_empty() {
                return Err(DeclarativeOutputPolicyRuleError::InvalidPolicyRef {
                    rule_id: self.rule_id.clone(),
                    policy_ref: policy_ref.clone(),
                });
            }
        }
        Ok(())
    }

    pub fn with_replacement(mut self, replacement: impl Into<String>) -> Self {
        self.replacement = Some(replacement.into());
        self
    }

    pub fn with_reason_codes<I, S>(mut self, reason_codes: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.reason_codes = reason_codes.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_policy_refs<I, S>(mut self, policy_refs: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.policy_refs = policy_refs.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_priority(mut self, priority: i64) -> Self {
        self.priority = priority;
        self
    }

    fn effective_policy_refs(&self) -> Vec<String> {
        if self.policy_refs.is_empty() {
            vec![self.rule_id.clone()]
        } else {
            self.policy_refs.clone()
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct DeclarativeOutputPolicyEvaluator {
    pub rules: Vec<DeclarativeOutputPolicyRule>,
}

impl DeclarativeOutputPolicyEvaluator {
    pub fn new<I>(rules: I) -> Self
    where
        I: IntoIterator<Item = DeclarativeOutputPolicyRule>,
    {
        Self {
            rules: rules.into_iter().collect(),
        }
    }

    pub fn validate(&self) -> Result<(), DeclarativeOutputPolicyRuleError> {
        for rule in &self.rules {
            rule.validate()?;
        }
        Ok(())
    }

    pub fn evaluate_chunk(
        &self,
        chunk: &GenerationChunk,
        evaluated_at_unix_ms: u64,
    ) -> OutputPolicyDecision {
        self.evaluate_chunk_checked(chunk, evaluated_at_unix_ms)
            .expect("declarative output policy rules must be valid")
    }

    pub fn evaluate_chunk_checked(
        &self,
        chunk: &GenerationChunk,
        evaluated_at_unix_ms: u64,
    ) -> Result<OutputPolicyDecision, DeclarativeOutputPolicyRuleError> {
        self.validate()?;
        Ok(self.evaluate_chunk_unchecked(chunk, evaluated_at_unix_ms))
    }

    fn evaluate_chunk_unchecked(
        &self,
        chunk: &GenerationChunk,
        evaluated_at_unix_ms: u64,
    ) -> OutputPolicyDecision {
        let input_digest = self.input_digest(chunk);
        let mut rules = self.rules.iter().collect::<Vec<_>>();
        rules.sort_by(|left, right| {
            right
                .priority
                .cmp(&left.priority)
                .then_with(|| left.rule_id.cmp(&right.rule_id))
        });
        for rule in rules {
            if chunk.text.contains(&rule.literal) {
                return self
                    .decision_for_rule(rule, chunk, &input_digest)
                    .evaluated_at_unix_ms(evaluated_at_unix_ms);
            }
        }

        OutputPolicyDecision::allow(
            Self::decision_id(&input_digest, OutputDisposition::Allow, None),
            Some(chunk.sequence),
            input_digest,
        )
        .evaluated_at_unix_ms(evaluated_at_unix_ms)
    }

    fn decision_for_rule(
        &self,
        rule: &DeclarativeOutputPolicyRule,
        chunk: &GenerationChunk,
        input_digest: &str,
    ) -> OutputPolicyDecision {
        let decision_id = Self::decision_id(input_digest, rule.disposition, Some(&rule.rule_id));
        let decision = match rule.disposition {
            OutputDisposition::Allow => {
                OutputPolicyDecision::allow(decision_id, Some(chunk.sequence), input_digest)
            }
            OutputDisposition::Hold => OutputPolicyDecision::hold(decision_id, input_digest),
            OutputDisposition::Redact => OutputPolicyDecision::redact(
                decision_id,
                Some(chunk.sequence),
                Vec::<GenerationChunk>::new(),
                input_digest,
            )
            .with_redactions(self.redactions_for_rule(rule, chunk)),
            OutputDisposition::Replace => OutputPolicyDecision::replace(
                decision_id,
                Some(chunk.sequence),
                [GenerationChunk::text(
                    chunk.stream_id.clone(),
                    chunk.response_id.clone(),
                    chunk.sequence,
                    rule.replacement.clone().unwrap_or_default(),
                )],
                input_digest,
            ),
            OutputDisposition::AbortResponse => {
                OutputPolicyDecision::abort_response(decision_id, input_digest)
            }
            OutputDisposition::AbortTurn => {
                OutputPolicyDecision::abort_turn(decision_id, input_digest)
            }
            OutputDisposition::DenyCommit => {
                OutputPolicyDecision::deny_commit(decision_id, input_digest)
            }
        };

        decision
            .with_reason_codes(rule.reason_codes.clone())
            .with_policy_refs(rule.effective_policy_refs())
    }

    fn redactions_for_rule(
        &self,
        rule: &DeclarativeOutputPolicyRule,
        chunk: &GenerationChunk,
    ) -> Vec<RedactionInstruction> {
        let mut redactions = Vec::new();
        let mut search_start = 0;
        while let Some(offset) = chunk.text[search_start..].find(&rule.literal) {
            let start_byte = search_start + offset;
            let end_byte = start_byte + rule.literal.len();
            let start = chunk.text[..start_byte].chars().count();
            let end = start + rule.literal.chars().count();
            redactions.push(RedactionInstruction::text_range(
                format!("/chunks/{}/text", chunk.sequence),
                start as u64,
                end as u64,
                rule.replacement.clone().unwrap_or_default(),
            ));
            search_start = end_byte;
        }
        redactions
    }

    fn input_digest(&self, chunk: &GenerationChunk) -> String {
        canonical_hash(&json!({
            "chunk": {
                "stream_id": chunk.stream_id,
                "response_id": chunk.response_id,
                "sequence": chunk.sequence,
                "text": chunk.text,
            },
            "rules": self.rules.iter().map(|rule| json!({
                "rule_id": rule.rule_id,
                "literal": rule.literal,
                "disposition": disposition_name(rule.disposition),
                "replacement": rule.replacement,
                "reason_codes": rule.reason_codes,
                "policy_refs": rule.policy_refs,
                "priority": rule.priority,
            })).collect::<Vec<_>>(),
        }))
    }

    fn decision_id(
        input_digest: &str,
        disposition: OutputDisposition,
        rule_id: Option<&str>,
    ) -> String {
        "output-decision:".to_owned()
            + &canonical_hash(&json!({
                "input_digest": input_digest,
                "disposition": disposition_name(disposition),
                "rule_id": rule_id,
            }))
    }
}

fn disposition_name(disposition: OutputDisposition) -> &'static str {
    disposition.as_str()
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TerminalReason {
    PolicyDenied,
    BudgetExhausted,
    Cancelled,
    ClientDisconnected,
}

impl TerminalReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::PolicyDenied => "policy_denied",
            Self::BudgetExhausted => "budget_exhausted",
            Self::Cancelled => "cancelled",
            Self::ClientDisconnected => "client_disconnected",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DurableResult {
    None,
    Incomplete,
    Partial,
}

impl DurableResult {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Incomplete => "incomplete",
            Self::Partial => "partial",
        }
    }
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutputCutoffError {
    MissingOccurredAtUnixMs,
    EmptyIdentityField {
        field: &'static str,
    },
    PolicyAcceptedSequenceBeyondGenerated {
        last_generated_sequence: u64,
        last_policy_accepted_sequence: u64,
    },
    ClientDeliveredSequenceBeyondGenerated {
        last_generated_sequence: u64,
        last_client_delivered_sequence: u64,
    },
}

impl OutputCutoff {
    pub fn validate(&self) -> Result<(), OutputCutoffError> {
        for (field, value) in [
            ("stream_id", self.stream_id.as_str()),
            ("response_id", self.response_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(OutputCutoffError::EmptyIdentityField { field });
            }
        }
        if self
            .turn_id
            .as_ref()
            .is_some_and(|turn_id| turn_id.trim().is_empty())
        {
            return Err(OutputCutoffError::EmptyIdentityField { field: "turn_id" });
        }
        if self
            .policy_decision_id
            .as_ref()
            .is_some_and(|decision_id| decision_id.trim().is_empty())
        {
            return Err(OutputCutoffError::EmptyIdentityField {
                field: "policy_decision_id",
            });
        }
        if self.last_policy_accepted_sequence > self.last_generated_sequence {
            return Err(OutputCutoffError::PolicyAcceptedSequenceBeyondGenerated {
                last_generated_sequence: self.last_generated_sequence,
                last_policy_accepted_sequence: self.last_policy_accepted_sequence,
            });
        }
        if self.last_client_delivered_sequence > self.last_generated_sequence {
            return Err(OutputCutoffError::ClientDeliveredSequenceBeyondGenerated {
                last_generated_sequence: self.last_generated_sequence,
                last_client_delivered_sequence: self.last_client_delivered_sequence,
            });
        }
        if self.occurred_at_unix_ms == 0 {
            return Err(OutputCutoffError::MissingOccurredAtUnixMs);
        }
        Ok(())
    }

    pub fn accepts(&self, chunk: &GenerationChunk) -> bool {
        chunk.stream_id == self.stream_id
            && chunk.response_id == self.response_id
            && self.accepts_sequence(chunk.sequence)
    }

    pub fn accepts_sequence(&self, sequence: u64) -> bool {
        sequence <= self.last_client_delivered_sequence
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum OutputGateError {
    EmptyIdentityField {
        field: &'static str,
    },
    InvalidGenerationChunk {
        source: GenerationChunkError,
    },
    InvalidCutoff {
        source: OutputCutoffError,
    },
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
    NonContiguousSequence {
        last_generated_sequence: u64,
        attempted_sequence: u64,
    },
    AcceptedSequenceBeyondGenerated {
        last_generated_sequence: u64,
        accepted_through_sequence: u64,
    },
    ClientDeliveredSequenceBeyondGenerated {
        last_generated_sequence: u64,
        last_client_delivered_sequence: u64,
    },
    PendingChunkAlreadyDelivered {
        sequence: u64,
        last_client_delivered_sequence: u64,
    },
    PendingChunkBeyondGenerated {
        sequence: u64,
        last_generated_sequence: u64,
    },
    DuplicatePendingChunk {
        sequence: u64,
    },
    MissingPendingChunk {
        sequence: u64,
    },
    BoundedHoldbackExceeded {
        max_bytes: u64,
    },
    BoundedHoldbackTokensExceeded {
        max_tokens: u64,
    },
    InvalidRedactionInstruction {
        path: String,
    },
    MissingDecisionId,
    MissingInputDigest {
        decision_id: String,
    },
    ReplacementContentMissing {
        decision_id: String,
    },
    InvalidReasonCode {
        reason_code: String,
    },
    InvalidPolicyRef {
        policy_ref: String,
    },
    MissingOccurredAtUnixMs,
    PolicyStopped,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OutputGateUpdate {
    pub deliverable: Vec<GenerationChunk>,
    pub cutoff: Option<OutputCutoff>,
    pub provider_cancellation: Option<ProviderCancellation>,
    pub pending_tool_calls: Option<PendingToolCallsDisposition>,
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum FlushContext {
    PolicyDecision,
    ExplicitCommit,
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
            .with_holdback_max_tokens(48),
            pending: BTreeMap::new(),
            last_generated_sequence: 0,
            last_policy_accepted_sequence: 0,
            last_client_delivered_sequence: 0,
            stopped: None,
        }
    }

    pub fn from_state<I>(
        stream_id: impl Into<String>,
        response_id: impl Into<String>,
        pending: I,
        last_generated_sequence: u64,
        last_policy_accepted_sequence: u64,
        last_client_delivered_sequence: u64,
    ) -> Result<Self, OutputGateError>
    where
        I: IntoIterator<Item = GenerationChunk>,
    {
        let mut gate = Self::new(stream_id, response_id);
        gate.validate_identity()?;
        if last_policy_accepted_sequence > last_generated_sequence {
            return Err(OutputGateError::AcceptedSequenceBeyondGenerated {
                last_generated_sequence,
                accepted_through_sequence: last_policy_accepted_sequence,
            });
        }
        if last_client_delivered_sequence > last_generated_sequence {
            return Err(OutputGateError::ClientDeliveredSequenceBeyondGenerated {
                last_generated_sequence,
                last_client_delivered_sequence,
            });
        }

        let mut pending_chunks = BTreeMap::new();
        for chunk in pending {
            chunk
                .validate()
                .map_err(|source| OutputGateError::InvalidGenerationChunk { source })?;
            if chunk.stream_id != gate.stream_id {
                return Err(OutputGateError::StreamMismatch {
                    expected_stream_id: gate.stream_id.clone(),
                    actual_stream_id: chunk.stream_id,
                });
            }
            if chunk.response_id != gate.response_id {
                return Err(OutputGateError::ResponseMismatch {
                    expected_response_id: gate.response_id.clone(),
                    actual_response_id: chunk.response_id,
                });
            }
            if chunk.sequence <= last_client_delivered_sequence {
                return Err(OutputGateError::PendingChunkAlreadyDelivered {
                    sequence: chunk.sequence,
                    last_client_delivered_sequence,
                });
            }
            if chunk.sequence > last_generated_sequence {
                return Err(OutputGateError::PendingChunkBeyondGenerated {
                    sequence: chunk.sequence,
                    last_generated_sequence,
                });
            }
            let sequence = chunk.sequence;
            if pending_chunks.insert(sequence, chunk).is_some() {
                return Err(OutputGateError::DuplicatePendingChunk { sequence });
            }
        }

        if last_client_delivered_sequence < last_generated_sequence {
            for sequence in (last_client_delivered_sequence + 1)..=last_generated_sequence {
                if !pending_chunks.contains_key(&sequence) {
                    return Err(OutputGateError::MissingPendingChunk { sequence });
                }
            }
        }

        gate.pending = pending_chunks;
        gate.last_generated_sequence = last_generated_sequence;
        gate.last_policy_accepted_sequence = last_policy_accepted_sequence;
        gate.last_client_delivered_sequence = last_client_delivered_sequence;
        Ok(gate)
    }

    pub fn from_cutoff(cutoff: OutputCutoff) -> Result<Self, OutputGateError> {
        cutoff
            .validate()
            .map_err(|source| OutputGateError::InvalidCutoff { source })?;
        let mut gate = Self::new(cutoff.stream_id.clone(), cutoff.response_id.clone());
        gate.turn_id = cutoff.turn_id.clone();
        gate.last_generated_sequence = cutoff.last_generated_sequence;
        gate.last_policy_accepted_sequence = cutoff.last_policy_accepted_sequence;
        gate.last_client_delivered_sequence = cutoff.last_client_delivered_sequence;
        gate.stopped = Some(cutoff);
        Ok(gate)
    }

    pub fn with_turn_id(mut self, turn_id: impl Into<String>) -> Self {
        let turn_id = turn_id.into();
        self.turn_id = Some(turn_id.clone());
        if let Some(cutoff) = self.stopped.as_mut() {
            cutoff.turn_id = Some(turn_id);
        }
        self
    }

    pub fn with_delivery_policy(
        mut self,
        delivery_policy: OutputDeliveryPolicy,
    ) -> Result<Self, OutputGateError> {
        self.validate_identity()?;
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

    pub fn pending_chunks(&self) -> impl Iterator<Item = &GenerationChunk> {
        self.pending.values()
    }

    pub fn commit_accepted_output(&mut self) -> Vec<GenerationChunk> {
        self.release_accepted_output(FlushContext::ExplicitCommit)
    }

    fn release_accepted_output(&mut self, flush_context: FlushContext) -> Vec<GenerationChunk> {
        if self.stopped.is_some() {
            return Vec::new();
        }

        let delivered_after = self.last_client_delivered_sequence + 1;
        let accepted_through = self.flushable_accepted_sequence(flush_context);
        if delivered_after > accepted_through {
            return Vec::new();
        }

        let mut ready_sequences = Vec::new();
        let mut sequence = delivered_after;
        while sequence <= accepted_through && self.pending.contains_key(&sequence) {
            ready_sequences.push(sequence);
            sequence += 1;
        }
        let mut deliverable = Vec::new();
        for sequence in ready_sequences {
            if let Some(chunk) = self.pending.remove(&sequence) {
                self.last_client_delivered_sequence = sequence;
                deliverable.push(chunk);
            }
        }
        deliverable
    }

    fn flushable_accepted_sequence(&self, flush_context: FlushContext) -> u64 {
        if flush_context == FlushContext::ExplicitCommit
            || self.delivery_policy.flush_boundaries.is_empty()
        {
            return self.last_policy_accepted_sequence;
        }

        let mut flushable_sequence = self.last_client_delivered_sequence;
        let mut accumulated_text = String::new();
        for sequence in
            (self.last_client_delivered_sequence + 1)..=self.last_policy_accepted_sequence
        {
            let Some(chunk) = self.pending.get(&sequence) else {
                break;
            };
            accumulated_text.push_str(&chunk.text);
            if self.chunk_satisfies_flush_boundary(chunk, &accumulated_text, flush_context) {
                flushable_sequence = sequence;
            }
        }
        flushable_sequence
    }

    fn chunk_satisfies_flush_boundary(
        &self,
        chunk: &GenerationChunk,
        accumulated_text: &str,
        flush_context: FlushContext,
    ) -> bool {
        self.delivery_policy
            .flush_boundaries
            .iter()
            .any(|boundary| match boundary {
                FlushBoundary::Token => !chunk.text.is_empty(),
                FlushBoundary::Sentence => ends_at_sentence_boundary(accumulated_text),
                FlushBoundary::Paragraph => ends_at_paragraph_boundary(accumulated_text),
                FlushBoundary::ContentPart | FlushBoundary::ToolCall => true,
                FlushBoundary::Response => flush_context == FlushContext::ExplicitCommit,
            })
    }

    pub fn record_chunk(
        &mut self,
        chunk: GenerationChunk,
    ) -> Result<Vec<GenerationChunk>, OutputGateError> {
        self.validate_identity()?;
        chunk
            .validate()
            .map_err(|source| OutputGateError::InvalidGenerationChunk { source })?;
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
        if chunk.sequence != self.last_generated_sequence + 1 {
            return Err(OutputGateError::NonContiguousSequence {
                last_generated_sequence: self.last_generated_sequence,
                attempted_sequence: chunk.sequence,
            });
        }
        if self.delivery_policy.mode == DeliveryMode::BoundedHoldback
            && let Some(max_tokens) = self.delivery_policy.holdback_max_tokens
        {
            let mut pending_tokens = chunk.text.split_whitespace().count() as u64;
            for pending in self.pending.values() {
                pending_tokens =
                    pending_tokens.saturating_add(pending.text.split_whitespace().count() as u64);
                if pending_tokens > max_tokens {
                    return Err(OutputGateError::BoundedHoldbackTokensExceeded { max_tokens });
                }
            }
            if pending_tokens > max_tokens {
                return Err(OutputGateError::BoundedHoldbackTokensExceeded { max_tokens });
            }
        }
        if self.delivery_policy.mode == DeliveryMode::BoundedHoldback
            && let Some(max_bytes) = self.delivery_policy.holdback_max_bytes
        {
            let mut pending_bytes = chunk.text.len() as u64;
            for pending in self.pending.values() {
                pending_bytes = pending_bytes.saturating_add(pending.text.len() as u64);
                if pending_bytes > max_bytes {
                    return Err(OutputGateError::BoundedHoldbackExceeded { max_bytes });
                }
            }
            if pending_bytes > max_bytes {
                return Err(OutputGateError::BoundedHoldbackExceeded { max_bytes });
            }
        }

        self.last_generated_sequence = chunk.sequence;
        self.pending.insert(chunk.sequence, chunk);
        if self.delivery_policy.mode != DeliveryMode::ImmediateDraft {
            return Ok(Vec::new());
        }

        let Some(deliverable) = self.pending.remove(&self.last_generated_sequence) else {
            return Ok(Vec::new());
        };
        self.last_client_delivered_sequence = deliverable.sequence;
        Ok(vec![deliverable])
    }

    pub fn apply_decision(
        &mut self,
        decision: OutputPolicyDecision,
        occurred_at_unix_ms: u64,
    ) -> Result<OutputGateUpdate, OutputGateError> {
        self.validate_identity()?;
        if self.stopped.is_some() {
            return Err(OutputGateError::PolicyStopped);
        }
        if occurred_at_unix_ms == 0 {
            return Err(OutputGateError::MissingOccurredAtUnixMs);
        }
        if let Err(source) = decision.validate() {
            return match source {
                OutputPolicyDecisionError::MissingDecisionId => {
                    Err(OutputGateError::MissingDecisionId)
                }
                OutputPolicyDecisionError::MissingInputDigest { decision_id } => {
                    Err(OutputGateError::MissingInputDigest { decision_id })
                }
                OutputPolicyDecisionError::ReplacementContentMissing { decision_id } => {
                    Err(OutputGateError::ReplacementContentMissing { decision_id })
                }
                OutputPolicyDecisionError::InvalidReplacementChunk { source } => {
                    Err(OutputGateError::InvalidGenerationChunk { source })
                }
                OutputPolicyDecisionError::InvalidRedactionInstruction { path } => {
                    Err(OutputGateError::InvalidRedactionInstruction { path })
                }
                OutputPolicyDecisionError::InvalidReasonCode { reason_code } => {
                    Err(OutputGateError::InvalidReasonCode { reason_code })
                }
                OutputPolicyDecisionError::InvalidPolicyRef { policy_ref } => {
                    Err(OutputGateError::InvalidPolicyRef { policy_ref })
                }
            };
        }

        match decision.disposition {
            OutputDisposition::Allow => {
                if let Some(accepted_through_sequence) = decision.accepted_through_sequence {
                    if accepted_through_sequence > self.last_generated_sequence {
                        return Err(OutputGateError::AcceptedSequenceBeyondGenerated {
                            last_generated_sequence: self.last_generated_sequence,
                            accepted_through_sequence,
                        });
                    }
                    if accepted_through_sequence > self.last_policy_accepted_sequence {
                        self.last_policy_accepted_sequence = accepted_through_sequence;
                    }
                }

                let deliverable = match self.delivery_policy.mode {
                    DeliveryMode::BufferUntilCommit => Vec::new(),
                    DeliveryMode::BoundedHoldback | DeliveryMode::ImmediateDraft => {
                        self.release_accepted_output(FlushContext::PolicyDecision)
                    }
                };

                Ok(OutputGateUpdate {
                    deliverable,
                    cutoff: None,
                    provider_cancellation: None,
                    pending_tool_calls: None,
                })
            }
            OutputDisposition::Hold => Ok(OutputGateUpdate {
                deliverable: Vec::new(),
                cutoff: None,
                provider_cancellation: None,
                pending_tool_calls: None,
            }),
            OutputDisposition::Redact | OutputDisposition::Replace => {
                if decision.disposition == OutputDisposition::Redact
                    && decision.replacement_chunks.is_empty()
                    && decision.redactions.is_empty()
                {
                    return Ok(OutputGateUpdate {
                        deliverable: Vec::new(),
                        cutoff: None,
                        provider_cancellation: None,
                        pending_tool_calls: None,
                    });
                }

                if let Some(accepted_through_sequence) = decision.accepted_through_sequence
                    && accepted_through_sequence > self.last_generated_sequence
                {
                    return Err(OutputGateError::AcceptedSequenceBeyondGenerated {
                        last_generated_sequence: self.last_generated_sequence,
                        accepted_through_sequence,
                    });
                }

                if !decision.replacement_chunks.is_empty() {
                    let mut expected_sequence = decision
                        .accepted_through_sequence
                        .unwrap_or(self.last_generated_sequence);
                    let mut previous_sequence = expected_sequence.saturating_sub(1);
                    for chunk in &decision.replacement_chunks {
                        chunk
                            .validate()
                            .map_err(|source| OutputGateError::InvalidGenerationChunk { source })?;
                        if chunk.stream_id != self.stream_id {
                            return Err(OutputGateError::StreamMismatch {
                                expected_stream_id: self.stream_id.clone(),
                                actual_stream_id: chunk.stream_id.clone(),
                            });
                        }
                        if chunk.response_id != self.response_id {
                            return Err(OutputGateError::ResponseMismatch {
                                expected_response_id: self.response_id.clone(),
                                actual_response_id: chunk.response_id.clone(),
                            });
                        }
                        if chunk.sequence != expected_sequence {
                            return Err(OutputGateError::NonContiguousSequence {
                                last_generated_sequence: previous_sequence,
                                attempted_sequence: chunk.sequence,
                            });
                        }
                        previous_sequence = expected_sequence;
                        expected_sequence = expected_sequence.checked_add(1).ok_or(
                            OutputGateError::NonContiguousSequence {
                                last_generated_sequence: previous_sequence,
                                attempted_sequence: chunk.sequence,
                            },
                        )?;
                    }
                }

                if decision.disposition == OutputDisposition::Redact
                    && !decision.redactions.is_empty()
                {
                    let mut redactions_by_sequence: BTreeMap<u64, Vec<RedactionInstruction>> =
                        BTreeMap::new();
                    for redaction in decision.redactions {
                        let Some(sequence_text) = redaction
                            .path
                            .strip_prefix("/chunks/")
                            .and_then(|suffix| suffix.strip_suffix("/text"))
                        else {
                            return Err(OutputGateError::InvalidRedactionInstruction {
                                path: redaction.path,
                            });
                        };
                        if sequence_text.is_empty()
                            || !sequence_text.bytes().all(|byte| byte.is_ascii_digit())
                            || (sequence_text != "0" && sequence_text.starts_with('0'))
                        {
                            return Err(OutputGateError::InvalidRedactionInstruction {
                                path: redaction.path,
                            });
                        }
                        let Ok(sequence) = sequence_text.parse::<u64>() else {
                            return Err(OutputGateError::InvalidRedactionInstruction {
                                path: redaction.path,
                            });
                        };
                        redactions_by_sequence
                            .entry(sequence)
                            .or_default()
                            .push(redaction);
                    }

                    for (sequence, mut redactions) in redactions_by_sequence {
                        if sequence > self.last_generated_sequence {
                            return Err(OutputGateError::PendingChunkBeyondGenerated {
                                sequence,
                                last_generated_sequence: self.last_generated_sequence,
                            });
                        }
                        if sequence <= self.last_client_delivered_sequence {
                            return Err(OutputGateError::PendingChunkAlreadyDelivered {
                                sequence,
                                last_client_delivered_sequence: self.last_client_delivered_sequence,
                            });
                        }
                        let Some(chunk) = self.pending.get_mut(&sequence) else {
                            return Err(OutputGateError::MissingPendingChunk { sequence });
                        };
                        redactions.sort_by(|left, right| right.start.cmp(&left.start));
                        for redaction in redactions {
                            let Ok(start) = usize::try_from(redaction.start) else {
                                return Err(OutputGateError::InvalidRedactionInstruction {
                                    path: redaction.path,
                                });
                            };
                            let Ok(end) = usize::try_from(redaction.end) else {
                                return Err(OutputGateError::InvalidRedactionInstruction {
                                    path: redaction.path,
                                });
                            };
                            let char_count = chunk.text.chars().count();
                            if start > end || end > char_count {
                                return Err(OutputGateError::InvalidRedactionInstruction {
                                    path: redaction.path,
                                });
                            }
                            let start_byte = if start == char_count {
                                chunk.text.len()
                            } else {
                                chunk
                                    .text
                                    .char_indices()
                                    .nth(start)
                                    .map(|(index, _)| index)
                                    .unwrap_or(chunk.text.len())
                            };
                            let end_byte = if end == char_count {
                                chunk.text.len()
                            } else {
                                chunk
                                    .text
                                    .char_indices()
                                    .nth(end)
                                    .map(|(index, _)| index)
                                    .unwrap_or(chunk.text.len())
                            };
                            chunk
                                .text
                                .replace_range(start_byte..end_byte, &redaction.replacement);
                        }
                    }
                }

                if decision.disposition == OutputDisposition::Replace
                    && let Some(accepted_through_sequence) = decision.accepted_through_sequence
                    && accepted_through_sequence > self.last_client_delivered_sequence
                {
                    self.pending.remove(&accepted_through_sequence);
                }

                let mut replacement_accepted_through = decision.accepted_through_sequence;
                for chunk in decision.replacement_chunks {
                    chunk
                        .validate()
                        .map_err(|source| OutputGateError::InvalidGenerationChunk { source })?;
                    let chunk_sequence = chunk.sequence;
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
                    if chunk_sequence > self.last_client_delivered_sequence {
                        replacement_accepted_through = Some(
                            replacement_accepted_through
                                .map_or(chunk_sequence, |accepted| accepted.max(chunk_sequence)),
                        );
                        self.last_generated_sequence =
                            self.last_generated_sequence.max(chunk_sequence);
                        self.pending.insert(chunk_sequence, chunk);
                    }
                }

                if let Some(accepted_through_sequence) = replacement_accepted_through
                    && accepted_through_sequence > self.last_policy_accepted_sequence
                {
                    self.last_policy_accepted_sequence = accepted_through_sequence;
                }

                let deliverable = match self.delivery_policy.mode {
                    DeliveryMode::BufferUntilCommit => Vec::new(),
                    DeliveryMode::BoundedHoldback | DeliveryMode::ImmediateDraft => {
                        self.release_accepted_output(FlushContext::PolicyDecision)
                    }
                };

                Ok(OutputGateUpdate {
                    deliverable,
                    cutoff: None,
                    provider_cancellation: None,
                    pending_tool_calls: None,
                })
            }
            OutputDisposition::AbortResponse
            | OutputDisposition::AbortTurn
            | OutputDisposition::DenyCommit => {
                if let Some(accepted_through_sequence) = decision.accepted_through_sequence {
                    if accepted_through_sequence > self.last_generated_sequence {
                        return Err(OutputGateError::AcceptedSequenceBeyondGenerated {
                            last_generated_sequence: self.last_generated_sequence,
                            accepted_through_sequence,
                        });
                    }
                    if accepted_through_sequence > self.last_policy_accepted_sequence {
                        self.last_policy_accepted_sequence = accepted_through_sequence;
                    }
                }
                let terminal_disposition = decision.disposition;
                let pending_tool_calls = match (terminal_disposition, decision.pending_tool_calls) {
                    (
                        OutputDisposition::AbortResponse | OutputDisposition::AbortTurn,
                        PendingToolCallsDisposition::Keep,
                    ) => PendingToolCallsDisposition::Deny,
                    (_, disposition) => disposition,
                };
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
                    provider_cancellation: Some(decision.provider_cancellation),
                    pending_tool_calls: Some(pending_tool_calls),
                })
            }
        }
    }

    fn validate_identity(&self) -> Result<(), OutputGateError> {
        for (field, value) in [
            ("stream_id", self.stream_id.as_str()),
            ("response_id", self.response_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(OutputGateError::EmptyIdentityField { field });
            }
        }
        if self
            .turn_id
            .as_ref()
            .is_some_and(|turn_id| turn_id.trim().is_empty())
        {
            return Err(OutputGateError::EmptyIdentityField { field: "turn_id" });
        }
        Ok(())
    }
}

fn ends_at_sentence_boundary(text: &str) -> bool {
    text.trim_end_matches([' ', '\t', '\r', '\n'])
        .chars()
        .last()
        .is_some_and(|character| matches!(character, '.' | '!' | '?'))
}

fn ends_at_paragraph_boundary(text: &str) -> bool {
    let text = text.trim_end_matches([' ', '\t']);
    text.ends_with("\n\n") || text.ends_with("\r\n\r\n")
}
