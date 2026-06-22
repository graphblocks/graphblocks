use graphblocks_runtime_core::lifecycle::{LifecycleError, NodeLifecycle, NodeStatus};
use graphblocks_runtime_core::outcome::{BlockError, CancelCode, CancelReason, ErrorCategory};
use serde_json::json;

#[test]
fn terminal_state_is_recorded_once() -> Result<(), LifecycleError> {
    let mut lifecycle = NodeLifecycle::new();

    lifecycle.transition(NodeStatus::Running)?;
    assert!(lifecycle.complete()?);
    assert_eq!(lifecycle.status(), NodeStatus::Completed);
    assert_eq!(
        lifecycle.fail(BlockError::new(
            "provider.error",
            ErrorCategory::Provider,
            "provider failed after completion",
            false,
        )),
        Err(LifecycleError::AlreadyTerminal {
            current: NodeStatus::Completed
        }),
    );
    Ok(())
}

#[test]
fn terminal_state_rejects_late_output_and_state_patch() -> Result<(), LifecycleError> {
    let mut lifecycle = NodeLifecycle::new();

    lifecycle.transition(NodeStatus::Running)?;
    lifecycle.record_output("value", json!("before-terminal"))?;
    lifecycle.apply_state_patch(json!({"seen": true}))?;
    assert!(lifecycle.complete()?);

    assert_eq!(
        lifecycle.record_output("value", json!("after-terminal")),
        Err(LifecycleError::OutputAfterTerminal {
            current: NodeStatus::Completed
        }),
    );
    assert_eq!(
        lifecycle.apply_state_patch(json!({"late": true})),
        Err(LifecycleError::PatchAfterTerminal {
            current: NodeStatus::Completed
        }),
    );
    Ok(())
}

#[test]
fn cancellation_is_idempotent() -> Result<(), LifecycleError> {
    let mut lifecycle = NodeLifecycle::new();
    let reason = CancelReason::new(CancelCode::UserCancel);

    lifecycle.transition(NodeStatus::Running)?;
    assert!(lifecycle.cancel(reason.clone())?);
    assert!(!lifecycle.cancel(reason)?);
    assert_eq!(lifecycle.status(), NodeStatus::Cancelled);
    Ok(())
}

#[test]
fn failed_terminal_requires_and_retains_canonical_error() -> Result<(), LifecycleError> {
    let mut lifecycle = NodeLifecycle::new();
    let error = BlockError::new(
        "provider.timeout",
        ErrorCategory::Timeout,
        "provider timed out",
        true,
    );

    lifecycle.transition(NodeStatus::Running)?;
    assert!(lifecycle.fail(error.clone())?);

    assert_eq!(lifecycle.status(), NodeStatus::Failed);
    assert_eq!(lifecycle.terminal_error(), Some(&error));
    Ok(())
}
