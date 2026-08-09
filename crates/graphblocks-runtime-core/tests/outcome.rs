use graphblocks_runtime_core::outcome::{
    BlockError, BudgetExhaustion, CancelCode, CancelReason, ErrorCategory, Outcome,
    OutcomeTextErrorKind, OutcomeTextRole, OutcomeValidationError, PauseReason, PolicyDecisionRef,
    SkipReason,
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

#[test]
fn outcome_validation_accepts_all_closed_variants() {
    let mut skipped = SkipReason::new("condition_false");
    skipped.message = Some("condition resolved to false".to_owned());
    let mut exhausted = BudgetExhaustion::new("budget.hard_stop");
    exhausted.message = Some("budget exhausted".to_owned());
    let mut paused = PauseReason::new("approval_required");
    paused.message = Some("approval is required".to_owned());
    let mut failed = BlockError::new(
        "provider.timeout",
        ErrorCategory::Timeout,
        "provider timed out",
        true,
    );
    failed.cause_chain.push("upstream timeout".to_owned());
    let mut cancelled = CancelReason::new(CancelCode::UserCancel);
    cancelled.message = Some("cancelled by user".to_owned());
    cancelled.requested_by = Some("principal-1".to_owned());
    cancelled.policy_decision_ref = Some("decision-1".to_owned());

    let outcomes = [
        Outcome::Value(Value::Null),
        Outcome::Absent,
        Outcome::Skipped(skipped),
        Outcome::Denied(PolicyDecisionRef::new("decision-1")),
        Outcome::BudgetExhausted(exhausted),
        Outcome::Paused(paused),
        Outcome::Failed(failed),
        Outcome::Cancelled(cancelled),
    ];

    for outcome in outcomes {
        assert_eq!(outcome.validate(), Ok(()));
    }
}

#[test]
fn outcome_validation_rejects_noncanonical_identity_and_reason_text() {
    assert_eq!(
        Outcome::<Value>::Denied(PolicyDecisionRef::new(" ")).validate(),
        Err(OutcomeValidationError {
            field: "policy decision id".to_owned(),
            kind: OutcomeTextErrorKind::Empty,
            role: OutcomeTextRole::Identifier,
        })
    );
    assert_eq!(
        Outcome::<Value>::Skipped(SkipReason::new(" condition_false")).validate(),
        Err(OutcomeValidationError {
            field: "skip reason code".to_owned(),
            kind: OutcomeTextErrorKind::SurroundingWhitespace,
            role: OutcomeTextRole::Identifier,
        })
    );

    let mut preserved_message = SkipReason::new("condition_false");
    preserved_message.message = Some(" padded human message ".to_owned());
    assert_eq!(
        Outcome::<Value>::Skipped(preserved_message).validate(),
        Ok(())
    );
    let mut blank_message = SkipReason::new("condition_false");
    blank_message.message = Some(" \t ".to_owned());
    assert_eq!(
        Outcome::<Value>::Skipped(blank_message).validate(),
        Err(OutcomeValidationError {
            field: "skip reason message".to_owned(),
            kind: OutcomeTextErrorKind::Empty,
            role: OutcomeTextRole::HumanText,
        })
    );

    let mut cancelled = CancelReason::new(CancelCode::UserCancel);
    cancelled.requested_by = Some("principal\u{7f}1".to_owned());
    assert_eq!(
        Outcome::<Value>::Cancelled(cancelled).validate(),
        Err(OutcomeValidationError {
            field: "cancellation requested by".to_owned(),
            kind: OutcomeTextErrorKind::ControlCharacter,
            role: OutcomeTextRole::Identifier,
        })
    );

    let mut failed = BlockError::new(
        "provider.timeout",
        ErrorCategory::Timeout,
        "provider timed out",
        true,
    );
    failed.cause_chain.push(String::new());
    assert_eq!(
        Outcome::<Value>::Failed(failed).validate(),
        Err(OutcomeValidationError {
            field: "block error cause chain[0]".to_owned(),
            kind: OutcomeTextErrorKind::Empty,
            role: OutcomeTextRole::HumanText,
        })
    );

    let mut preserved_cause = BlockError::new(
        "provider.timeout",
        ErrorCategory::Timeout,
        " provider timed out ",
        true,
    );
    preserved_cause
        .cause_chain
        .push(" upstream timeout ".to_owned());
    assert_eq!(Outcome::<Value>::Failed(preserved_cause).validate(), Ok(()));
}
