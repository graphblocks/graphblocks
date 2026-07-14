use std::collections::BTreeSet;
use std::error::Error;
use std::fmt;

use graphblocks_compiler::compiler::MAX_NODE_RETRY_ATTEMPTS;

use crate::outcome::{BlockError, ErrorCategory};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum EffectKind {
    ExternalWrite,
    FilesystemWrite,
    Destructive,
    Process,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PartialOutputPolicy {
    Fail,
    ResumeWithCursor,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Backoff {
    None,
    Fixed { delay_ms: u64 },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RetryDecision {
    Retry { delay_ms: u64 },
    Stop { reason: &'static str },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProviderLimitKind {
    GraphBlocksQuotaExceeded,
    ProviderQuotaExceeded,
    CapacityUnavailable,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProviderLimitIncident {
    pub kind: ProviderLimitKind,
    pub retry_after_ms: Option<u64>,
    pub compatible_fallbacks: Vec<String>,
    pub credential_or_topup_available: bool,
}

impl ProviderLimitIncident {
    pub fn new(kind: ProviderLimitKind) -> Self {
        Self {
            kind,
            retry_after_ms: None,
            compatible_fallbacks: Vec::new(),
            credential_or_topup_available: false,
        }
    }

    pub fn with_retry_after_ms(mut self, retry_after_ms: u64) -> Self {
        self.retry_after_ms = Some(retry_after_ms);
        self
    }

    pub fn with_fallback(mut self, target: impl Into<String>) -> Self {
        self.compatible_fallbacks.push(target.into());
        self
    }

    pub fn with_credential_or_topup_available(mut self) -> Self {
        self.credential_or_topup_available = true;
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProviderLimitDecision {
    RetryAfter {
        delay_ms: u64,
    },
    Fallback {
        target: String,
        requires_policy_recheck: bool,
    },
    Pause {
        reason: &'static str,
    },
    RequestCredentialOrTopup,
    Fail {
        reason: &'static str,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProviderLimitPolicy {
    fallback_enabled: bool,
    queue_enabled: bool,
    credential_or_topup_enabled: bool,
}

impl ProviderLimitPolicy {
    pub fn new() -> Self {
        Self {
            fallback_enabled: false,
            queue_enabled: false,
            credential_or_topup_enabled: false,
        }
    }

    pub fn with_fallback_enabled(mut self, enabled: bool) -> Self {
        self.fallback_enabled = enabled;
        self
    }

    pub fn with_queue_enabled(mut self, enabled: bool) -> Self {
        self.queue_enabled = enabled;
        self
    }

    pub fn with_credential_or_topup_enabled(mut self, enabled: bool) -> Self {
        self.credential_or_topup_enabled = enabled;
        self
    }

    pub fn decide(&self, incident: &ProviderLimitIncident) -> ProviderLimitDecision {
        match incident.kind {
            ProviderLimitKind::GraphBlocksQuotaExceeded => ProviderLimitDecision::Fail {
                reason: "graphblocks_quota_exceeded",
            },
            ProviderLimitKind::ProviderQuotaExceeded => {
                if let Some(delay_ms) = incident.retry_after_ms {
                    return ProviderLimitDecision::RetryAfter { delay_ms };
                }
                if self.fallback_enabled
                    && let Some(target) = incident.compatible_fallbacks.first()
                {
                    return ProviderLimitDecision::Fallback {
                        target: target.clone(),
                        requires_policy_recheck: true,
                    };
                }
                if self.credential_or_topup_enabled && incident.credential_or_topup_available {
                    return ProviderLimitDecision::RequestCredentialOrTopup;
                }
                ProviderLimitDecision::Fail {
                    reason: "provider_quota_exceeded",
                }
            }
            ProviderLimitKind::CapacityUnavailable => {
                if self.queue_enabled {
                    ProviderLimitDecision::Pause {
                        reason: "capacity_unavailable",
                    }
                } else {
                    ProviderLimitDecision::Fail {
                        reason: "capacity_unavailable",
                    }
                }
            }
        }
    }
}

impl Default for ProviderLimitPolicy {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetryPolicy {
    max_attempts: u64,
    retry_on: BTreeSet<ErrorCategory>,
    backoff: Backoff,
    partial_output_policy: PartialOutputPolicy,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RetryPolicyError {
    MaxAttemptsExceeded { max_attempts: u64 },
}

impl fmt::Display for RetryPolicyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MaxAttemptsExceeded { .. } => write!(
                formatter,
                "node retry attempts must not exceed {MAX_NODE_RETRY_ATTEMPTS}"
            ),
        }
    }
}

impl Error for RetryPolicyError {}

impl RetryPolicy {
    pub fn new(max_attempts: u32) -> Self {
        Self {
            max_attempts: u64::from(max_attempts),
            retry_on: BTreeSet::new(),
            backoff: Backoff::None,
            partial_output_policy: PartialOutputPolicy::Fail,
        }
    }

    pub fn try_new(max_attempts: u64) -> Result<Self, RetryPolicyError> {
        let policy = Self {
            max_attempts,
            retry_on: BTreeSet::new(),
            backoff: Backoff::None,
            partial_output_policy: PartialOutputPolicy::Fail,
        };
        policy.validate()?;
        Ok(policy)
    }

    pub fn validate(&self) -> Result<(), RetryPolicyError> {
        if self.max_attempts > MAX_NODE_RETRY_ATTEMPTS {
            return Err(RetryPolicyError::MaxAttemptsExceeded {
                max_attempts: self.max_attempts,
            });
        }
        Ok(())
    }

    pub fn default_model_read() -> Self {
        Self::new(3)
            .retry_on([
                ErrorCategory::RateLimit,
                ErrorCategory::Timeout,
                ErrorCategory::Transient,
            ])
            .with_backoff(Backoff::Fixed { delay_ms: 250 })
    }

    pub fn retry_on(mut self, categories: impl IntoIterator<Item = ErrorCategory>) -> Self {
        self.retry_on = categories.into_iter().collect();
        self
    }

    pub fn with_backoff(mut self, backoff: Backoff) -> Self {
        self.backoff = backoff;
        self
    }

    pub fn with_partial_output_policy(mut self, policy: PartialOutputPolicy) -> Self {
        self.partial_output_policy = policy;
        self
    }

    pub fn decide(&self, request: &RetryRequest) -> RetryDecision {
        if self.validate().is_err() {
            return RetryDecision::Stop {
                reason: "max_attempts_exceeded",
            };
        }
        if u64::from(request.attempt) >= self.max_attempts {
            return RetryDecision::Stop {
                reason: "max_attempts_exhausted",
            };
        }
        if !request.error.retryable || !self.retry_on.contains(&request.error.category) {
            return RetryDecision::Stop {
                reason: "category_not_retryable",
            };
        }
        if request.has_partial_output {
            match self.partial_output_policy {
                PartialOutputPolicy::Fail => {
                    return RetryDecision::Stop {
                        reason: "partial_output_not_retryable",
                    };
                }
                PartialOutputPolicy::ResumeWithCursor => {
                    if request.resume_cursor.is_none() {
                        return RetryDecision::Stop {
                            reason: "missing_resume_cursor",
                        };
                    }
                }
            }
        }
        if request.effect.is_some() {
            match request.idempotency_key.as_deref() {
                None => {
                    return RetryDecision::Stop {
                        reason: "missing_idempotency_key",
                    };
                }
                Some(idempotency_key)
                    if idempotency_key.is_empty() || idempotency_key != idempotency_key.trim() =>
                {
                    return RetryDecision::Stop {
                        reason: "invalid_idempotency_key",
                    };
                }
                Some(_) => {}
            }
        }

        let delay_ms = match self.backoff {
            Backoff::None => 0,
            Backoff::Fixed { delay_ms } => delay_ms,
        };
        RetryDecision::Retry {
            delay_ms: request.retry_after_ms.unwrap_or(delay_ms),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RetryRequest {
    attempt: u32,
    error: BlockError,
    has_partial_output: bool,
    resume_cursor: Option<String>,
    effect: Option<EffectKind>,
    idempotency_key: Option<String>,
    retry_after_ms: Option<u64>,
}

impl RetryRequest {
    pub fn new(attempt: u32, error: BlockError) -> Self {
        Self {
            attempt,
            error,
            has_partial_output: false,
            resume_cursor: None,
            effect: None,
            idempotency_key: None,
            retry_after_ms: None,
        }
    }

    pub fn with_partial_output(mut self) -> Self {
        self.has_partial_output = true;
        self
    }

    pub fn with_resume_cursor(mut self, cursor: impl Into<String>) -> Self {
        self.resume_cursor = Some(cursor.into());
        self
    }

    pub fn with_effect(mut self, effect: EffectKind) -> Self {
        self.effect = Some(effect);
        self
    }

    pub fn with_idempotency_key(mut self, key: impl Into<String>) -> Self {
        self.idempotency_key = Some(key.into());
        self
    }

    pub fn with_retry_after_ms(mut self, retry_after_ms: u64) -> Self {
        self.retry_after_ms = Some(retry_after_ms);
        self
    }
}
