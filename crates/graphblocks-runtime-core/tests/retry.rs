use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::retry::{
    Backoff, EffectKind, PartialOutputPolicy, ProviderLimitDecision, ProviderLimitIncident,
    ProviderLimitKind, ProviderLimitPolicy, RetryDecision, RetryPolicy, RetryRequest,
};

fn error(category: ErrorCategory, retryable: bool) -> BlockError {
    BlockError::new("error.test", category, "test error", retryable)
}

#[test]
fn retry_policy_allows_retryable_categories_before_max_attempts() {
    let policy = RetryPolicy::new(3)
        .retry_on([ErrorCategory::RateLimit, ErrorCategory::Timeout])
        .with_backoff(Backoff::Fixed { delay_ms: 250 });

    assert_eq!(
        policy.decide(&RetryRequest::new(1, error(ErrorCategory::Timeout, true))),
        RetryDecision::Retry { delay_ms: 250 },
    );
}

#[test]
fn retry_policy_stops_at_max_attempts() {
    let policy = RetryPolicy::new(3).retry_on([ErrorCategory::Timeout]);

    assert_eq!(
        policy.decide(&RetryRequest::new(3, error(ErrorCategory::Timeout, true))),
        RetryDecision::Stop {
            reason: "max_attempts_exhausted",
        },
    );
}

#[test]
fn retry_policy_rejects_default_non_retry_categories() {
    let policy = RetryPolicy::default_model_read();

    for category in [
        ErrorCategory::Validation,
        ErrorCategory::Policy,
        ErrorCategory::Budget,
        ErrorCategory::Internal,
    ] {
        assert_eq!(
            policy.decide(&RetryRequest::new(1, error(category, true))),
            RetryDecision::Stop {
                reason: "category_not_retryable",
            },
        );
    }
}

#[test]
fn retry_policy_rejects_partial_output_by_default() {
    let policy = RetryPolicy::default_model_read();
    let request = RetryRequest::new(1, error(ErrorCategory::Timeout, true)).with_partial_output();

    assert_eq!(
        policy.decide(&request),
        RetryDecision::Stop {
            reason: "partial_output_not_retryable",
        },
    );
}

#[test]
fn retry_policy_allows_partial_output_only_when_policy_allows_resume() {
    let policy = RetryPolicy::default_model_read()
        .with_partial_output_policy(PartialOutputPolicy::ResumeWithCursor);
    let request = RetryRequest::new(1, error(ErrorCategory::Timeout, true))
        .with_partial_output()
        .with_resume_cursor("cursor-1");

    assert_eq!(
        policy.decide(&request),
        RetryDecision::Retry { delay_ms: 250 },
    );
}

#[test]
fn effect_retry_requires_idempotency_key() {
    let policy = RetryPolicy::new(3).retry_on([ErrorCategory::Transient]);
    let request = RetryRequest::new(1, error(ErrorCategory::Transient, true))
        .with_effect(EffectKind::ExternalWrite);

    assert_eq!(
        policy.decide(&request),
        RetryDecision::Stop {
            reason: "missing_idempotency_key",
        },
    );

    assert_eq!(
        policy.decide(&request.with_idempotency_key("request-1")),
        RetryDecision::Retry { delay_ms: 0 },
    );
}

#[test]
fn provider_quota_retry_respects_retry_after() {
    let policy = RetryPolicy::new(3).retry_on([ErrorCategory::Quota]);
    let request =
        RetryRequest::new(1, error(ErrorCategory::Quota, true)).with_retry_after_ms(1_500);

    assert_eq!(
        policy.decide(&request),
        RetryDecision::Retry { delay_ms: 1_500 },
    );
}

#[test]
fn provider_limit_policy_does_not_retry_internal_graphblocks_quota_denial() {
    let policy = ProviderLimitPolicy::new();
    let incident = ProviderLimitIncident::new(ProviderLimitKind::GraphBlocksQuotaExceeded)
        .with_retry_after_ms(1_000)
        .with_fallback("openai-compatible:gpt-economy");

    assert_eq!(
        policy.decide(&incident),
        ProviderLimitDecision::Fail {
            reason: "graphblocks_quota_exceeded",
        },
    );
}

#[test]
fn provider_limit_policy_honors_provider_retry_after_before_fallback() {
    let policy = ProviderLimitPolicy::new().with_fallback_enabled(true);
    let incident = ProviderLimitIncident::new(ProviderLimitKind::ProviderQuotaExceeded)
        .with_retry_after_ms(2_500)
        .with_fallback("openai-compatible:gpt-economy");

    assert_eq!(
        policy.decide(&incident),
        ProviderLimitDecision::RetryAfter { delay_ms: 2_500 },
    );
}

#[test]
fn provider_limit_policy_selects_compatible_fallback_with_policy_recheck() {
    let policy = ProviderLimitPolicy::new().with_fallback_enabled(true);
    let incident = ProviderLimitIncident::new(ProviderLimitKind::ProviderQuotaExceeded)
        .with_fallback("openai-compatible:gpt-economy");

    assert_eq!(
        policy.decide(&incident),
        ProviderLimitDecision::Fallback {
            target: "openai-compatible:gpt-economy".to_owned(),
            requires_policy_recheck: true,
        },
    );
}

#[test]
fn provider_limit_policy_pauses_for_capacity_when_queueing_is_allowed() {
    let policy = ProviderLimitPolicy::new().with_queue_enabled(true);
    let incident = ProviderLimitIncident::new(ProviderLimitKind::CapacityUnavailable);

    assert_eq!(
        policy.decide(&incident),
        ProviderLimitDecision::Pause {
            reason: "capacity_unavailable",
        },
    );
}
