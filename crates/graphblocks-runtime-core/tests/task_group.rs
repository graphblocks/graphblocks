use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::task_group::{
    ChildTaskState, SiblingCancellationPolicy, TaskGroupDecision, TaskGroupError, TaskGroupFailure,
    TaskGroupFailurePolicy, TaskGroupPolicy, TaskGroupState,
};

fn provider_error(code: &str) -> BlockError {
    BlockError::new(code, ErrorCategory::Provider, "provider failed", false)
}

#[test]
fn task_group_collects_partial_failures_until_quorum_is_met() -> Result<(), TaskGroupError> {
    let mut group = TaskGroupState::new(
        ["dense", "keyword", "tickets"],
        TaskGroupPolicy::new(1)
            .with_failure(TaskGroupFailurePolicy::Collect)
            .with_cancellation(SiblingCancellationPolicy::CancelSiblingsOnFatal),
    )?;

    assert_eq!(
        group.record_failure("dense", provider_error("provider.down"))?,
        TaskGroupDecision::Pending
    );
    assert_eq!(
        group.record_success("keyword")?,
        TaskGroupDecision::Succeeded {
            successes: 1,
            failures: 1,
        },
    );
    assert_eq!(group.child_state("tickets"), Some(&ChildTaskState::Pending));
    Ok(())
}

#[test]
fn task_group_fail_fast_cancels_pending_siblings() -> Result<(), TaskGroupError> {
    let mut group = TaskGroupState::new(
        ["dense", "keyword", "tickets"],
        TaskGroupPolicy::new(2)
            .with_failure(TaskGroupFailurePolicy::FailFast)
            .with_cancellation(SiblingCancellationPolicy::CancelSiblingsOnFatal),
    )?;
    let error = provider_error("provider.timeout");

    assert_eq!(
        group.record_failure("dense", error.clone())?,
        TaskGroupDecision::Failed {
            failure: TaskGroupFailure::ChildFailed {
                child_id: "dense".to_owned(),
                error,
            },
            cancel_siblings: vec!["keyword".to_owned(), "tickets".to_owned()],
        },
    );
    assert_eq!(
        group.record_success("keyword"),
        Err(TaskGroupError::AlreadyTerminal),
    );
    Ok(())
}

#[test]
fn task_group_collect_failure_fails_when_quorum_is_impossible() -> Result<(), TaskGroupError> {
    let mut group = TaskGroupState::new(
        ["dense", "keyword", "tickets"],
        TaskGroupPolicy::new(3)
            .with_failure(TaskGroupFailurePolicy::Collect)
            .with_cancellation(SiblingCancellationPolicy::CancelSiblingsOnFatal),
    )?;

    assert_eq!(group.record_success("dense")?, TaskGroupDecision::Pending);
    assert_eq!(
        group.record_failure("keyword", provider_error("provider.down"))?,
        TaskGroupDecision::Failed {
            failure: TaskGroupFailure::InsufficientSuccesses {
                successes: 1,
                required: 3,
            },
            cancel_siblings: vec!["tickets".to_owned()],
        },
    );
    Ok(())
}

#[test]
fn task_group_deadline_cancels_unfinished_children() -> Result<(), TaskGroupError> {
    let mut group = TaskGroupState::new(
        ["dense", "keyword", "tickets"],
        TaskGroupPolicy::new(2)
            .with_deadline_ms(100)
            .with_cancellation(SiblingCancellationPolicy::CancelSiblingsOnFatal),
    )?;

    assert_eq!(group.record_success("dense")?, TaskGroupDecision::Pending);
    assert_eq!(group.check_deadline(99)?, TaskGroupDecision::Pending);
    assert_eq!(
        group.check_deadline(100)?,
        TaskGroupDecision::Failed {
            failure: TaskGroupFailure::DeadlineExceeded {
                deadline_ms: 100,
                now_ms: 100,
            },
            cancel_siblings: vec!["keyword".to_owned(), "tickets".to_owned()],
        },
    );
    Ok(())
}

#[test]
fn task_group_rejects_invalid_minimum_successes() {
    assert_eq!(
        TaskGroupState::new(["only"], TaskGroupPolicy::new(0)),
        Err(TaskGroupError::InvalidMinimumSuccesses {
            minimum_successes: 0,
            child_count: 1,
        }),
    );
    assert_eq!(
        TaskGroupState::new(["only"], TaskGroupPolicy::new(2)),
        Err(TaskGroupError::InvalidMinimumSuccesses {
            minimum_successes: 2,
            child_count: 1,
        }),
    );
}
