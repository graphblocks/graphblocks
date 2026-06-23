use graphblocks_runtime_core::evaluation::{
    CheckResult, CheckStatus, ConstraintOperator, GateConstraint, GateDecision, MetricDirection,
    MetricObservation, ResourceSnapshotRef, ResultBundle, ReviewDecision, ReviewRecord,
    RunProvenance, TrialResult, evaluate_gate,
};
use graphblocks_runtime_core::policy::PrincipalRef;
use graphblocks_runtime_core::tool_result::ArtifactRef;
use serde_json::json;

#[test]
fn evaluate_gate_fails_when_required_check_failed() {
    let subject = ResourceSnapshotRef::new("candidate-1", "sha256:candidate");
    let checks = [
        CheckResult::new("lint", subject.clone(), CheckStatus::Passed)
            .with_tool("processor_id", json!("lint"))
            .with_tool("version", json!("1")),
        CheckResult::new("formal", subject.clone(), CheckStatus::Failed)
            .with_diagnostic(json!({"code": "GBE1001", "message": "assertion failed"}))
            .with_tool("processor_id", json!("formal"))
            .with_tool("version", json!("1")),
    ];

    let gate = evaluate_gate(
        "quality",
        subject,
        &checks,
        &[],
        Some(["lint", "formal"]),
        &[],
        None,
    );

    assert_eq!(gate.decision, GateDecision::Fail);
    assert_eq!(gate.check_ids, vec!["lint", "formal"]);
    assert_eq!(gate.violated_constraints, vec!["check:formal"]);
}

#[test]
fn evaluate_gate_uses_metric_thresholds() {
    let subject = ResourceSnapshotRef::new("candidate-1", "sha256:candidate");
    let metrics = [
        MetricObservation::new("accuracy", json!(0.91)).with_direction(MetricDirection::Maximize),
        MetricObservation::new("latency_ms", json!(125))
            .with_unit("ms")
            .with_direction(MetricDirection::Minimize),
    ];

    let passing = evaluate_gate(
        "quality",
        subject.clone(),
        &[],
        &metrics,
        None::<[&str; 0]>,
        &[
            GateConstraint::new("accuracy", ConstraintOperator::AtLeast, json!(0.9)),
            GateConstraint::new("latency_ms", ConstraintOperator::AtMost, json!(150)),
        ],
        None,
    );
    let failing = evaluate_gate(
        "quality",
        subject,
        &[],
        &metrics,
        None::<[&str; 0]>,
        &[GateConstraint::new(
            "latency_ms",
            ConstraintOperator::AtMost,
            json!(100),
        )],
        None,
    );

    assert_eq!(passing.decision, GateDecision::Pass);
    assert_eq!(failing.decision, GateDecision::Fail);
    assert_eq!(failing.violated_constraints, vec!["metric:latency_ms"]);
}

#[test]
fn evaluate_gate_supports_rollout_regression_for_minimized_metrics() {
    let subject = ResourceSnapshotRef::new("candidate-1", "sha256:candidate");
    let small_regression = [
        MetricObservation::new("p95_time_to_first_draft_ms", json!(1_120))
            .with_baseline_value(json!(1_000))
            .with_direction(MetricDirection::Minimize),
    ];
    let large_regression = [
        MetricObservation::new("p95_time_to_first_draft_ms", json!(1_250))
            .with_baseline_value(json!(1_000))
            .with_direction(MetricDirection::Minimize),
    ];
    let constraint = [GateConstraint::new(
        "p95_time_to_first_draft_ms",
        ConstraintOperator::MaxRegression,
        json!(0.15),
    )];

    let passing = evaluate_gate(
        "rollout-quality",
        subject.clone(),
        &[],
        &small_regression,
        None::<[&str; 0]>,
        &constraint,
        None,
    );
    let failing = evaluate_gate(
        "rollout-quality",
        subject,
        &[],
        &large_regression,
        None::<[&str; 0]>,
        &constraint,
        None,
    );

    assert_eq!(passing.decision, GateDecision::Pass);
    assert_eq!(failing.decision, GateDecision::Fail);
    assert_eq!(
        failing.violated_constraints,
        vec!["metric:p95_time_to_first_draft_ms"]
    );
}

#[test]
fn evaluate_gate_supports_rollout_regression_for_maximized_metrics() {
    let subject = ResourceSnapshotRef::new("candidate-1", "sha256:candidate");
    let metrics = [
        MetricObservation::new("citation_validation_rate", json!(0.90))
            .with_baseline_value(json!(0.99))
            .with_direction(MetricDirection::Maximize),
    ];

    let gate = evaluate_gate(
        "rollout-quality",
        subject,
        &[],
        &metrics,
        None::<[&str; 0]>,
        &[GateConstraint::new(
            "citation_validation_rate",
            ConstraintOperator::MaxRegression,
            json!(0.05),
        )],
        None,
    );

    assert_eq!(gate.decision, GateDecision::Fail);
    assert_eq!(
        gate.violated_constraints,
        vec!["metric:citation_validation_rate"]
    );
}

#[test]
fn review_record_is_invalid_for_changed_subject_digest() {
    let subject = ResourceSnapshotRef::new("candidate-1", "sha256:old");
    let review = ReviewRecord::new(
        "review-1",
        subject.clone(),
        "sha256:old",
        "quality",
        PrincipalRef::new("reviewer-1"),
        ReviewDecision::Accept,
    )
    .with_created_at("2026-06-22T00:00:00Z");

    assert!(review.is_valid_for(&subject));
    assert!(!review.is_valid_for(&ResourceSnapshotRef::new("candidate-1", "sha256:new")));
    assert!(
        !review
            .invalidate("2026-06-22T00:05:00Z")
            .is_valid_for(&subject)
    );
}

#[test]
fn result_bundle_digest_is_stable_without_record_identity() {
    let subject = ResourceSnapshotRef::new("candidate-1", "sha256:candidate");
    let gate = evaluate_gate(
        "quality",
        subject.clone(),
        &[],
        &[],
        None::<[&str; 0]>,
        &[],
        None,
    );
    let provenance = RunProvenance::new("sha256:graph", "2026-06-22T00:00:00Z");
    let bundle = ResultBundle::new("bundle-1", "run-1", "release-1")
        .with_input(ResourceSnapshotRef::new("input-1", "sha256:input"))
        .with_artifact(
            ArtifactRef::new("artifact-1", "file:///tmp/out.txt").with_checksum("sha256:out"),
        )
        .with_policy_decision_ref("decision-1")
        .with_provenance(provenance.clone());
    let same_payload = ResultBundle::new("bundle-2", "run-1", "release-1")
        .with_input(ResourceSnapshotRef::new("input-1", "sha256:input"))
        .with_artifact(
            ArtifactRef::new("artifact-1", "file:///tmp/out.txt").with_checksum("sha256:out"),
        )
        .with_policy_decision_ref("decision-1")
        .with_provenance(provenance);

    assert_eq!(gate.subject, subject);
    assert_eq!(bundle.content_digest(), same_payload.content_digest());
}

#[test]
fn trial_result_carries_gate_and_outcome() {
    let base = ResourceSnapshotRef::new("base", "sha256:base");
    let candidate = ResourceSnapshotRef::new("candidate", "sha256:candidate");
    let gate = evaluate_gate(
        "quality",
        candidate.clone(),
        &[],
        &[],
        None::<[&str; 0]>,
        &[],
        None,
    );

    let trial = TrialResult::new("trial-1", base, candidate)
        .with_gate(gate.clone())
        .with_outcome("accepted");

    assert_eq!(trial.gate, Some(gate));
    assert_eq!(trial.outcome, "accepted");
}
