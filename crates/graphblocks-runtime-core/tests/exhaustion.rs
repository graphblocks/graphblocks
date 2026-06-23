use std::collections::BTreeMap;

use graphblocks_runtime_core::budget::{BudgetPermit, UsageAmount};
use graphblocks_runtime_core::exhaustion::{
    ContinuationEnvelope, ExhaustionController, ExhaustionPolicy, ExhaustionPolicyError,
    ExhaustionPreset, ExhaustionUnit, WorkKind, validate_exhaustion_policy,
};

fn tokens(amount: i64) -> UsageAmount {
    UsageAmount::new("model_output_tokens", amount, "tokens")
}

fn permit() -> BudgetPermit {
    BudgetPermit {
        permit_id: "permit-1".to_string(),
        reservation_refs: vec!["reservation-1".to_string()],
        owner: "worker:1".to_string(),
        atomic_unit: "turn:1".to_string(),
        admission_epoch: 7,
        authorized_amounts: vec![tokens(100)],
        continuation_profile: "finish_current_turn".to_string(),
        policy_snapshot_digest: "sha256:policy".to_string(),
        expires_at: "2026-06-22T01:00:00Z".to_string(),
        low_watermark: Vec::new(),
        fencing_tokens: BTreeMap::from([("budget-1".to_string(), 1)]),
    }
}

#[test]
fn finish_current_turn_requires_bounded_continuation_in_production() {
    let policy = ExhaustionPolicy::from_preset(
        ExhaustionPreset::FinishCurrentTurn,
        ExhaustionUnit::Turn,
        None,
    );

    assert_eq!(
        validate_exhaustion_policy(&policy, true),
        Err(ExhaustionPolicyError::MissingExhaustionBoundary)
    );

    let bounded = ExhaustionPolicy::from_preset(
        ExhaustionPreset::FinishCurrentTurn,
        ExhaustionUnit::Turn,
        Some(
            ContinuationEnvelope::new()
                .with_max_additional_usage([tokens(4_000)])
                .with_max_additional_steps(2),
        ),
    );

    assert_eq!(validate_exhaustion_policy(&bounded, true), Ok(Vec::new()));
}

#[test]
fn finish_current_turn_allows_only_declared_continuation_work() {
    let policy = ExhaustionPolicy::from_preset(
        ExhaustionPreset::FinishCurrentTurn,
        ExhaustionUnit::Turn,
        Some(
            ContinuationEnvelope::new()
                .with_max_additional_usage([tokens(4_000)])
                .with_max_additional_steps(1),
        ),
    );
    let valid_permit = permit();
    let mut controller = ExhaustionController::new(policy, "turn:1", 7)
        .with_continuation_permit(valid_permit.clone());

    let already_admitted = controller.admit(WorkKind::AlreadyAdmittedChildWork, 7, None);
    let finalization = controller.admit(WorkKind::DeclaredFinalization, 8, Some(&valid_permit));
    let optional_task = controller.admit(WorkKind::OptionalTask, 8, Some(&valid_permit));
    let second_finalization =
        controller.admit(WorkKind::DeclaredFinalization, 8, Some(&valid_permit));

    assert!(already_admitted.allowed);
    assert_eq!(already_admitted.reason, "already_admitted");
    assert!(finalization.allowed);
    assert!(!optional_task.allowed);
    assert_eq!(optional_task.reason, "forbidden_work");
    assert!(!second_finalization.allowed);
    assert_eq!(second_finalization.reason, "max_additional_steps_exceeded");
}

#[test]
fn hard_stop_allows_cleanup_and_blocks_provider_work() {
    let policy = ExhaustionPolicy::from_preset(
        ExhaustionPreset::HardStop,
        ExhaustionUnit::ProviderCall,
        None,
    );
    let mut controller = ExhaustionController::new(policy, "call-1", 2);

    let cleanup = controller.admit(WorkKind::Cleanup, 2, None);
    let provider_call = controller.admit(WorkKind::CurrentProviderCall, 2, None);

    assert!(cleanup.allowed);
    assert!(!provider_call.allowed);
    assert_eq!(provider_call.reason, "new_work_denied");
}

#[test]
fn continuation_permit_must_match_atomic_unit_profile_and_epoch() {
    let policy = ExhaustionPolicy::from_preset(
        ExhaustionPreset::FinishCurrentTurn,
        ExhaustionUnit::Turn,
        Some(
            ContinuationEnvelope::new()
                .with_max_additional_usage([tokens(100)])
                .with_max_additional_steps(1),
        ),
    );
    let wrong_profile = BudgetPermit {
        permit_id: "permit-2".to_string(),
        continuation_profile: "hard_stop".to_string(),
        ..permit()
    };
    let wrong_unit = BudgetPermit {
        permit_id: "permit-3".to_string(),
        atomic_unit: "turn:other".to_string(),
        ..permit()
    };
    let wrong_epoch = BudgetPermit {
        permit_id: "permit-4".to_string(),
        admission_epoch: 8,
        ..permit()
    };
    let mut controller = ExhaustionController::new(policy, "turn:1", 7);

    assert_eq!(
        controller
            .admit(WorkKind::DeclaredFinalization, 8, Some(&wrong_profile))
            .reason,
        "invalid_permit"
    );
    assert_eq!(
        controller
            .admit(WorkKind::DeclaredFinalization, 8, Some(&wrong_unit))
            .reason,
        "invalid_permit"
    );
    assert_eq!(
        controller
            .admit(WorkKind::DeclaredFinalization, 8, Some(&wrong_epoch))
            .reason,
        "invalid_permit"
    );
}

#[test]
fn continuation_usage_must_fit_permit_authorized_amounts() {
    let policy = ExhaustionPolicy::from_preset(
        ExhaustionPreset::FinishCurrentTurn,
        ExhaustionUnit::Turn,
        Some(
            ContinuationEnvelope::new()
                .with_max_additional_usage([tokens(200)])
                .with_max_additional_steps(2),
        ),
    );
    let valid_permit = permit();
    let mut controller = ExhaustionController::new(policy, "turn:1", 7)
        .with_continuation_permit(valid_permit.clone());

    let denied =
        controller.admit_with_usage(WorkKind::DeclaredFinalization, 8, None, [tokens(101)]);
    let allowed = controller.admit_with_usage(
        WorkKind::DeclaredFinalization,
        8,
        Some(&valid_permit),
        [tokens(100)],
    );

    assert!(!denied.allowed);
    assert_eq!(denied.reason, "usage_exceeds_permit");
    assert!(allowed.allowed);
}

#[test]
fn continuation_usage_accumulates_against_envelope_bound() {
    let policy = ExhaustionPolicy::from_preset(
        ExhaustionPreset::FinishCurrentTurn,
        ExhaustionUnit::Turn,
        Some(
            ContinuationEnvelope::new()
                .with_max_additional_usage([tokens(100)])
                .with_max_additional_steps(3),
        ),
    );
    let mut controller =
        ExhaustionController::new(policy, "turn:1", 7).with_continuation_permit(permit());

    let first = controller.admit_with_usage(WorkKind::DeclaredFinalization, 8, None, [tokens(60)]);
    let denied = controller.admit_with_usage(WorkKind::Checkpoint, 8, None, [tokens(41)]);
    let second = controller.admit_with_usage(WorkKind::Cleanup, 8, None, [tokens(40)]);

    assert!(first.allowed);
    assert!(!denied.allowed);
    assert_eq!(denied.reason, "max_additional_usage_exceeded");
    assert!(second.allowed);
}
