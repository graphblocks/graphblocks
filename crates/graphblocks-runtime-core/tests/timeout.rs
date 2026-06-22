use graphblocks_runtime_core::outcome::{CancelCode, ErrorCategory};
use graphblocks_runtime_core::timeout::{Deadline, TimeoutDecision, TimeoutError, TimeoutPolicy};

#[test]
fn timeout_policy_rejects_zero_duration() {
    assert_eq!(TimeoutPolicy::new(0), Err(TimeoutError::InvalidDuration));
}

#[test]
fn deadline_reports_remaining_time_until_boundary() -> Result<(), TimeoutError> {
    let policy = TimeoutPolicy::new(1_000)?;
    let deadline = Deadline::new("model", 5_000, policy)?;

    assert_eq!(deadline.node_id(), "model");
    assert_eq!(deadline.started_at_ms(), 5_000);
    assert_eq!(deadline.deadline_ms(), 6_000);
    assert_eq!(deadline.remaining_ms(5_250), 750);
    assert_eq!(deadline.remaining_ms(6_000), 0);
    assert_eq!(
        deadline.check(5_999),
        TimeoutDecision::Pending { remaining_ms: 1 }
    );
    Ok(())
}

#[test]
fn deadline_expires_at_boundary_with_canonical_error() -> Result<(), TimeoutError> {
    let policy = TimeoutPolicy::new(250)?;
    let deadline = Deadline::new("embed", 1_000, policy)?;

    let expired = deadline.check(1_250);
    assert_eq!(
        expired,
        TimeoutDecision::Expired {
            node_id: "embed".to_owned(),
            deadline_ms: 1_250,
            now_ms: 1_250,
        },
    );
    assert_eq!(expired.cancel_reason().code, CancelCode::Timeout);
    assert_eq!(expired.block_error().category, ErrorCategory::Timeout);
    assert_eq!(expired.block_error().code, "runtime.timeout");
    Ok(())
}

#[test]
fn deadline_uses_saturating_deadline_for_large_start_time() -> Result<(), TimeoutError> {
    let policy = TimeoutPolicy::new(100)?;
    let deadline = Deadline::new("large", u64::MAX - 50, policy)?;

    assert_eq!(deadline.deadline_ms(), u64::MAX);
    assert_eq!(
        deadline.check(u64::MAX - 1),
        TimeoutDecision::Pending { remaining_ms: 1 }
    );
    assert!(matches!(
        deadline.check(u64::MAX),
        TimeoutDecision::Expired { .. }
    ));
    Ok(())
}
