use std::collections::BTreeMap;

use graphblocks_runtime_core::outcome::{
    BlockError, CancelCode, CancelReason, ErrorCategory, Outcome, SkipReason,
};
use graphblocks_runtime_core::readiness::{
    InputDependency, PortRef, Readiness, ReadinessTracker, ReadinessValidationError, ResolvedInput,
};
use serde_json::Value;

#[test]
fn missing_dependency_waits_but_null_value_is_ready() {
    let source = PortRef::new("source", "value");
    let dependency = InputDependency::value("message", source.clone());
    let mut tracker = ReadinessTracker::new();

    assert_eq!(
        tracker.readiness([dependency.clone()]),
        Readiness::Waiting {
            missing: vec![source.clone()]
        },
    );

    tracker.publish(source, Outcome::Value(Value::Null));

    assert_eq!(
        tracker.readiness([dependency]),
        Readiness::Ready(BTreeMap::from([(
            "message".to_owned(),
            ResolvedInput::Value(Value::Null),
        )])),
    );
}

#[test]
fn absent_dependency_blocks_required_value_input() {
    let source = PortRef::new("branch", "maybe_value");
    let dependency = InputDependency::value("value", source.clone());
    let mut tracker = ReadinessTracker::new();

    tracker.publish(source.clone(), Outcome::Absent);

    assert_eq!(
        tracker.readiness([dependency]),
        Readiness::Blocked {
            input: "value".to_owned(),
            source,
            outcome: Outcome::Absent,
        },
    );
}

#[test]
fn failed_and_cancelled_dependencies_remain_distinct_terminal_outcomes() {
    let failed_source = PortRef::new("model", "answer");
    let cancelled_source = PortRef::new("tool", "result");
    let failed = BlockError::new(
        "provider.timeout",
        ErrorCategory::Timeout,
        "provider timed out",
        true,
    );
    let cancelled = CancelReason::new(CancelCode::UserCancel);
    let mut tracker = ReadinessTracker::new();

    tracker.publish(failed_source.clone(), Outcome::Failed(failed.clone()));
    tracker.publish(
        cancelled_source.clone(),
        Outcome::Cancelled(cancelled.clone()),
    );

    assert_eq!(
        tracker.readiness([InputDependency::value("answer", failed_source.clone())]),
        Readiness::Blocked {
            input: "answer".to_owned(),
            source: failed_source,
            outcome: Outcome::Failed(failed),
        },
    );
    assert_eq!(
        tracker.readiness([InputDependency::value("result", cancelled_source.clone())]),
        Readiness::Blocked {
            input: "result".to_owned(),
            source: cancelled_source,
            outcome: Outcome::Cancelled(cancelled),
        },
    );
}

#[test]
fn outcome_input_explicitly_accepts_terminal_outcome() {
    let source = PortRef::new("optional_branch", "value");
    let dependency = InputDependency::outcome("branch_outcome", source.clone());
    let mut tracker = ReadinessTracker::new();
    let skipped = Outcome::<Value>::Skipped(SkipReason::new("condition_false"));

    tracker.publish(source, skipped.clone());

    assert_eq!(
        tracker.readiness([dependency]),
        Readiness::Ready(BTreeMap::from([(
            "branch_outcome".to_owned(),
            ResolvedInput::Outcome(skipped),
        )])),
    );
}

#[test]
fn readiness_reports_all_missing_dependencies_in_input_order() {
    let first = PortRef::new("a", "value");
    let second = PortRef::new("b", "value");
    let tracker = ReadinessTracker::new();

    assert_eq!(
        tracker.readiness([
            InputDependency::value("first", first.clone()),
            InputDependency::value("second", second.clone()),
        ]),
        Readiness::Waiting {
            missing: vec![first, second]
        },
    );
}

#[test]
fn checked_readiness_rejects_invalid_identity_and_duplicate_inputs() {
    assert!(matches!(
        PortRef::try_new(" source", "value"),
        Err(ReadinessValidationError::InvalidText { field, .. })
            if field == "port ref node"
    ));
    assert!(matches!(
        InputDependency::try_value(" ", PortRef::new("source", "value")),
        Err(ReadinessValidationError::InvalidText { field, .. })
            if field == "input dependency input"
    ));

    let tracker = ReadinessTracker::new();
    let source = PortRef::new("source", "value");
    assert_eq!(
        tracker.try_readiness([
            InputDependency::value("value", source.clone()),
            InputDependency::outcome("value", source),
        ]),
        Err(ReadinessValidationError::DuplicateDependencyInput {
            input: "value".to_owned(),
        })
    );
}

#[test]
fn checked_signal_publication_is_closed_and_duplicate_safe() {
    let source = PortRef::new("source", "value");
    let mut tracker = ReadinessTracker::new();

    assert_eq!(
        tracker.try_publish(source.clone(), Outcome::Value(Value::Null)),
        Ok(())
    );
    assert_eq!(
        tracker.try_publish(source.clone(), Outcome::Absent),
        Err(ReadinessValidationError::DuplicateSignal { port: source })
    );

    let mut invalid_outcome_tracker = ReadinessTracker::new();
    let invalid_source = PortRef::new("policy", "decision");
    assert!(matches!(
        invalid_outcome_tracker.try_publish(
            invalid_source.clone(),
            Outcome::Denied(graphblocks_runtime_core::outcome::PolicyDecisionRef::new(" ")),
        ),
        Err(ReadinessValidationError::InvalidOutcome { port, .. })
            if port == invalid_source
    ));
}

#[test]
fn checked_readiness_preserves_nested_source_path_semantics() -> Result<(), ReadinessValidationError>
{
    let source = PortRef::new("source", "value");
    let mut tracker = ReadinessTracker::new();
    tracker.try_publish(
        source.clone(),
        Outcome::Value(serde_json::json!({"items": [{"name": "first"}]})),
    )?;
    let dependency = InputDependency::try_value("name", source.clone())
        .and_then(|dependency| dependency.try_with_source_path(["items", "0", "name"]))?;

    assert_eq!(
        tracker.try_readiness([dependency]),
        Ok(Readiness::Ready(BTreeMap::from([(
            "name".to_owned(),
            ResolvedInput::Value(serde_json::json!("first")),
        )])))
    );

    let missing_dependency = InputDependency::try_value("name", source)
        .and_then(|dependency| dependency.try_with_source_path(["items", "1", "name"]))?;
    assert!(matches!(
        tracker.try_readiness([missing_dependency]),
        Ok(Readiness::Blocked {
            outcome: Outcome::Failed(error),
            ..
        }) if error.code == "runtime.missing_source_path"
    ));
    Ok(())
}
