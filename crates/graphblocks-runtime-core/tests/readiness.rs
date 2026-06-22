use std::collections::BTreeMap;

use graphblocks_runtime_core::outcome::{
    BlockError, CancelCode, CancelReason, ErrorCategory, Outcome, SkipReason,
};
use graphblocks_runtime_core::readiness::{
    InputDependency, PortRef, Readiness, ReadinessTracker, ResolvedInput,
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
