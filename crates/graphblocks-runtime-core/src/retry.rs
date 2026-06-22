use std::collections::BTreeSet;

use crate::outcome::{BlockError, ErrorCategory};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum EffectKind {
    ExternalWrite,
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetryPolicy {
    max_attempts: u32,
    retry_on: BTreeSet<ErrorCategory>,
    backoff: Backoff,
    partial_output_policy: PartialOutputPolicy,
}

impl RetryPolicy {
    pub fn new(max_attempts: u32) -> Self {
        Self {
            max_attempts,
            retry_on: BTreeSet::new(),
            backoff: Backoff::None,
            partial_output_policy: PartialOutputPolicy::Fail,
        }
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
        if request.attempt >= self.max_attempts {
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
        if request.effect.is_some() && request.idempotency_key.is_none() {
            return RetryDecision::Stop {
                reason: "missing_idempotency_key",
            };
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
