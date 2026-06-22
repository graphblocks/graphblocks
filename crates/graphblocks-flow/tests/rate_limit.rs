use graphblocks_flow::rate_limit::{LocalRateLimiter, RateLimitDecision, RateLimitError};

#[test]
fn rate_limiter_allows_until_limit_then_reports_retry_after() -> Result<(), RateLimitError> {
    let limiter = LocalRateLimiter::new("embedding-api", 3, 60_000)?;

    assert_eq!(limiter.check_at("run-1", 0, 1), RateLimitDecision::Allowed);
    assert_eq!(limiter.check_at("run-1", 0, 2), RateLimitDecision::Allowed);
    assert_eq!(
        limiter.check_at("run-1", 0, 1),
        RateLimitDecision::Limited {
            retry_after_ms: 60_000
        },
    );
    assert_eq!(limiter.available_at(0), 0);
    Ok(())
}

#[test]
fn rate_limiter_refills_at_window_boundary() -> Result<(), RateLimitError> {
    let limiter = LocalRateLimiter::new("embedding-api", 2, 1_000)?;

    assert_eq!(limiter.check_at("run-1", 0, 2), RateLimitDecision::Allowed);
    assert_eq!(
        limiter.check_at("run-1", 999, 1),
        RateLimitDecision::Limited { retry_after_ms: 1 },
    );
    assert_eq!(
        limiter.check_at("run-1", 1_000, 1),
        RateLimitDecision::Allowed
    );
    assert_eq!(limiter.available_at(1_000), 1);
    Ok(())
}

#[test]
fn rate_limiter_rejects_request_larger_than_limit() -> Result<(), RateLimitError> {
    let limiter = LocalRateLimiter::new("embedding-api", 2, 1_000)?;

    assert_eq!(
        limiter.check_at("run-1", 0, 3),
        RateLimitDecision::Limited {
            retry_after_ms: 1_000
        },
    );
    assert_eq!(limiter.available_at(0), 2);
    Ok(())
}

#[test]
fn rate_limiter_rejects_invalid_configuration_and_units() -> Result<(), RateLimitError> {
    assert!(matches!(
        LocalRateLimiter::new("bad", 0, 1_000),
        Err(RateLimitError::InvalidLimit),
    ));
    assert!(matches!(
        LocalRateLimiter::new("bad", 1, 0),
        Err(RateLimitError::InvalidWindow),
    ));

    let limiter = LocalRateLimiter::new("embedding-api", 1, 1_000)?;
    assert_eq!(
        limiter.check_at("run-1", 0, 0),
        RateLimitDecision::Rejected {
            reason: "invalid_units",
        },
    );
    Ok(())
}

#[test]
fn rate_limiter_tracks_identity_and_window() -> Result<(), RateLimitError> {
    let limiter = LocalRateLimiter::new("embedding-api", 600, 60_000)?;

    assert_eq!(limiter.id(), "embedding-api");
    assert_eq!(limiter.limit(), 600);
    assert_eq!(limiter.window_ms(), 60_000);
    Ok(())
}
