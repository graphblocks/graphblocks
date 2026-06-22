use graphblocks_runtime_core::outcome::{
    BlockError, BudgetExhaustion, CancelCode, CancelReason, ErrorCategory, Outcome, PauseReason,
    PolicyDecisionRef, SkipReason,
};
use serde_json::Value;

#[test]
fn outcome_distinguishes_null_absence_and_terminal_reasons() {
    let null_value = Outcome::Value(Value::Null);

    assert_ne!(null_value, Outcome::Absent);
    assert_ne!(
        Outcome::<Value>::Denied(PolicyDecisionRef::new("decision-1")),
        Outcome::Failed(BlockError::new(
            "policy.denied",
            ErrorCategory::Policy,
            "denied by policy",
            false,
        )),
    );
    assert_ne!(
        Outcome::<Value>::BudgetExhausted(BudgetExhaustion::new("budget.hard_stop")),
        Outcome::Cancelled(CancelReason::new(CancelCode::BudgetExhausted)),
    );
}

#[test]
fn outcome_carries_explicit_branch_and_pause_reasons() {
    assert_eq!(
        Outcome::<Value>::Skipped(SkipReason::new("condition_false")),
        Outcome::Skipped(SkipReason::new("condition_false")),
    );
    assert_eq!(
        Outcome::<Value>::Paused(PauseReason::new("approval_required")),
        Outcome::Paused(PauseReason::new("approval_required")),
    );
}
