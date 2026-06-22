use graphblocks_runtime_core::cancellation::{
    CancellationGuarantee, CancellationScope, CancellationToken,
};
use graphblocks_runtime_core::outcome::{CancelCode, CancelReason};

#[test]
fn parent_cancellation_propagates_to_children() {
    let parent = CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative);
    let child = parent.child(
        CancellationScope::Node,
        CancellationGuarantee::BestEffortRemote,
    );
    let reason = CancelReason::new(CancelCode::UserCancel);

    assert!(parent.cancel(reason.clone()));

    assert!(parent.is_cancelled());
    assert!(child.is_cancelled());
    assert_eq!(child.reason(), Some(reason));
}

#[test]
fn child_cancellation_does_not_cancel_parent_by_default() {
    let parent = CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative);
    let child = parent.child(CancellationScope::Node, CancellationGuarantee::Cooperative);

    assert!(child.cancel(CancelReason::new(CancelCode::DependencyFailed)));

    assert!(child.is_cancelled());
    assert!(!parent.is_cancelled());
}

#[test]
fn cancellation_is_idempotent_and_keeps_original_reason() {
    let token = CancellationToken::new(
        CancellationScope::Task,
        CancellationGuarantee::ImmediateLocal,
    );
    let first = CancelReason::new(CancelCode::Timeout);
    let second = CancelReason::new(CancelCode::Shutdown);

    assert!(token.cancel(first.clone()));
    assert!(!token.cancel(second));
    assert_eq!(token.reason(), Some(first));
}

#[test]
fn child_created_after_parent_cancel_is_already_cancelled() {
    let parent = CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative);
    let reason = CancelReason::new(CancelCode::Shutdown);

    assert!(parent.cancel(reason.clone()));
    let child = parent.child(
        CancellationScope::ProviderCall,
        CancellationGuarantee::BestEffortRemote,
    );

    assert!(child.is_cancelled());
    assert_eq!(child.reason(), Some(reason));
}

#[test]
fn effective_guarantee_never_exceeds_provider_capability() {
    assert_eq!(
        CancellationGuarantee::effective(
            CancellationGuarantee::ImmediateLocal,
            CancellationGuarantee::BestEffortRemote,
        ),
        CancellationGuarantee::BestEffortRemote,
    );
    assert_eq!(
        CancellationGuarantee::effective(
            CancellationGuarantee::BestEffortRemote,
            CancellationGuarantee::Cooperative,
        ),
        CancellationGuarantee::BestEffortRemote,
    );
    assert_eq!(
        CancellationGuarantee::effective(
            CancellationGuarantee::ImmediateLocal,
            CancellationGuarantee::NonCancellableAtomicSection,
        ),
        CancellationGuarantee::NonCancellableAtomicSection,
    );
}
