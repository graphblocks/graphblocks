use graphblocks_runtime_core::evaluation::{
    ChangeSet, CheckResult, CheckStatus, ConstraintOperator, GateConstraint, GateDecision,
    MetricDirection, MetricObservation, ResourceSnapshotRef, ResultBundle, ReviewDecision,
    ReviewRecord, RunProvenance, SloMeasurement, SloObjective, SloReportStatus, TrialResult,
    WorkspaceCommitError, WorkspaceCommitRequest, WorkspaceHead, WorkspaceMutationPolicy,
    WorkspaceTrialError, WorkspaceTrialPlan, evaluate_gate,
};
use graphblocks_runtime_core::orchestration::{LeasePool, LeaseRequest};
use graphblocks_runtime_core::policy::PrincipalRef;
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ToolBinding, ToolCatalog, ToolDefinition, ToolImplementation,
    ToolResolutionScope,
};
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
fn workspace_mutation_policy_protects_declared_read_only_inputs() {
    let policy = WorkspaceMutationPolicy::new("policy-1", ["file", "source", "test_oracle"])
        .with_read_only_resource_id("rtl/top.sv")
        .with_read_only_resource_kind("test_oracle");
    let principal = PrincipalRef::new("optimizer-1");
    let change_set = ChangeSet {
        change_set_id: "change-1".to_string(),
        base: ResourceSnapshotRef::new("workspace", "sha256:base").with_resource_kind("workspace"),
        candidate: ResourceSnapshotRef::new("workspace", "sha256:candidate")
            .with_resource_kind("workspace"),
        operations: vec![
            json!({"op": "file.read", "resource_id": "rtl/top.sv", "resource_kind": "source"}),
            json!({"op": "file.write", "resource_id": "rtl/top.sv", "resource_kind": "source"}),
            json!({"op": "file.write", "resource_id": "golden.json", "resource_kind": "test_oracle"}),
        ],
        summary: None,
    };

    let decision = policy.evaluate(&change_set, &principal, &[], &[], &[]);

    assert!(!decision.allowed);
    assert_eq!(
        decision.reason_codes,
        vec![
            "workspace.read_only_resource_changed",
            "workspace.read_only_resource_kind_changed",
        ]
    );
}

#[test]
fn workspace_mutation_policy_rejects_protected_snapshot_digest_change_without_operation_log() {
    let policy = WorkspaceMutationPolicy::new("policy-1", ["file", "source"])
        .with_read_only_resource_kind("source");
    let principal = PrincipalRef::new("optimizer-1");
    let change_set = ChangeSet {
        change_set_id: "change-1".to_string(),
        base: ResourceSnapshotRef::new("workspace", "sha256:base").with_resource_kind("workspace"),
        candidate: ResourceSnapshotRef::new("workspace", "sha256:candidate")
            .with_resource_kind("workspace"),
        operations: Vec::new(),
        summary: None,
    };
    let base_resources = vec![
        ResourceSnapshotRef::new("rtl/top.sv", "sha256:source-v1").with_resource_kind("source"),
        ResourceSnapshotRef::new("candidate/out.sv", "sha256:candidate-v1")
            .with_resource_kind("file"),
    ];
    let candidate_resources = vec![
        ResourceSnapshotRef::new("rtl/top.sv", "sha256:source-v2").with_resource_kind("source"),
        ResourceSnapshotRef::new("candidate/out.sv", "sha256:candidate-v2")
            .with_resource_kind("file"),
    ];

    let decision = policy.evaluate(
        &change_set,
        &principal,
        &[],
        &base_resources,
        &candidate_resources,
    );

    assert!(!decision.allowed);
    assert_eq!(
        decision.reason_codes,
        vec!["workspace.read_only_resource_changed"]
    );
}

#[test]
fn workspace_commit_applies_changeset_with_compare_and_swap_gate_and_review() {
    let base = ResourceSnapshotRef::new("workspace", "sha256:base").with_resource_kind("workspace");
    let candidate =
        ResourceSnapshotRef::new("workspace", "sha256:candidate").with_resource_kind("workspace");
    let head = WorkspaceHead::new("workspace", base.clone(), 7);
    let change_set = ChangeSet {
        change_set_id: "change-1".to_string(),
        base: base.clone(),
        candidate: candidate.clone(),
        operations: vec![
            json!({"op": "file.write", "resource_id": "src/lib.rs", "resource_kind": "file"}),
        ],
        summary: Some("update implementation".to_string()),
    };
    let mutation = WorkspaceMutationPolicy::new("policy-1", ["file"]).evaluate(
        &change_set,
        &PrincipalRef::new("agent-1"),
        &[],
        &[],
        &[],
    );
    let gate = evaluate_gate(
        "quality",
        candidate.clone(),
        &[CheckResult::new(
            "unit",
            candidate.clone(),
            CheckStatus::Passed,
        )],
        &[],
        Some(["unit"]),
        &[],
        None,
    );
    let review = ReviewRecord::new(
        "review-1",
        candidate.clone(),
        "sha256:candidate",
        "quality",
        PrincipalRef::new("reviewer-1"),
        ReviewDecision::Accept,
    );
    let request = WorkspaceCommitRequest::new("commit-1", change_set, 7)
        .with_mutation_decision(mutation)
        .with_gate(gate)
        .with_review(review);

    let (updated, record) = head.commit(request).expect("commit should pass");

    assert_eq!(updated.revision, 8);
    assert_eq!(updated.current, candidate);
    assert_eq!(record.previous_revision, 7);
    assert_eq!(record.new_revision, 8);
    assert_eq!(record.change_set_id, "change-1");
    assert!(record.content_digest().starts_with("sha256:"));
}

#[test]
fn workspace_commit_rejects_stale_head_failed_gate_or_invalid_review() {
    let base = ResourceSnapshotRef::new("workspace", "sha256:base").with_resource_kind("workspace");
    let candidate =
        ResourceSnapshotRef::new("workspace", "sha256:candidate").with_resource_kind("workspace");
    let head = WorkspaceHead::new("workspace", base.clone(), 7);
    let change_set = ChangeSet {
        change_set_id: "change-1".to_string(),
        base: base.clone(),
        candidate: candidate.clone(),
        operations: Vec::new(),
        summary: None,
    };

    assert_eq!(
        head.commit(WorkspaceCommitRequest::new(
            "commit-stale",
            change_set.clone(),
            6
        ))
        .expect_err("stale revision should fail"),
        WorkspaceCommitError::StaleHead {
            expected_revision: 6,
            actual_revision: 7,
            expected_digest: "sha256:base".to_string(),
            actual_digest: "sha256:base".to_string(),
        }
    );

    let denied = WorkspaceMutationPolicy::new("policy-1", ["file"])
        .with_denied_operation("file.delete")
        .evaluate(
            &ChangeSet {
                operations: vec![json!({"op": "file.delete", "resource_id": "src/lib.rs", "resource_kind": "file"})],
                ..change_set.clone()
            },
            &PrincipalRef::new("agent-1"),
            &[],
            &[],
            &[],
        );
    assert_eq!(
        head.commit(
            WorkspaceCommitRequest::new("commit-denied", change_set.clone(), 7)
                .with_mutation_decision(denied)
        )
        .expect_err("denied mutation should fail"),
        WorkspaceCommitError::MutationDenied {
            reason_codes: vec!["workspace.operation_denied".to_string()],
        }
    );

    let failed_gate = evaluate_gate(
        "quality",
        candidate.clone(),
        &[CheckResult::new(
            "unit",
            candidate.clone(),
            CheckStatus::Failed,
        )],
        &[],
        Some(["unit"]),
        &[],
        None,
    );
    assert_eq!(
        head.commit(
            WorkspaceCommitRequest::new("commit-failed-gate", change_set.clone(), 7)
                .with_gate(failed_gate)
        )
        .expect_err("failed gate should fail"),
        WorkspaceCommitError::GateNotPassed {
            gate_id: "quality".to_string(),
            decision: GateDecision::Fail,
        }
    );

    let stale_review = ReviewRecord::new(
        "review-1",
        candidate.clone(),
        "sha256:old",
        "quality",
        PrincipalRef::new("reviewer-1"),
        ReviewDecision::Accept,
    );
    assert_eq!(
        head.commit(
            WorkspaceCommitRequest::new("commit-stale-review", change_set, 7)
                .with_review(stale_review)
        )
        .expect_err("stale review should fail"),
        WorkspaceCommitError::ReviewInvalid {
            review_id: "review-1".to_string(),
        }
    );
}

fn workspace_commit_identity_inputs() -> (WorkspaceHead, ChangeSet) {
    let base = ResourceSnapshotRef::new("workspace", "sha256:base").with_resource_kind("workspace");
    let candidate =
        ResourceSnapshotRef::new("workspace", "sha256:candidate").with_resource_kind("workspace");
    let head = WorkspaceHead::new("workspace", base.clone(), 7);
    let change_set = ChangeSet {
        change_set_id: "change-1".to_owned(),
        base: base.clone(),
        candidate: candidate.clone(),
        operations: Vec::new(),
        summary: None,
    };
    (head, change_set)
}

#[test]
fn workspace_commit_rejects_cross_workspace_base_identity() {
    let (head, change_set) = workspace_commit_identity_inputs();

    let wrong_base = ChangeSet {
        base: ResourceSnapshotRef::new("other-workspace", "sha256:base")
            .with_resource_kind("workspace"),
        ..change_set.clone()
    };
    let error = head
        .commit(WorkspaceCommitRequest::new(
            "commit-wrong-base",
            wrong_base,
            7,
        ))
        .expect_err("base identity from another workspace must be rejected");
    assert!(error.to_string().contains("base targets workspace"));
}

#[test]
fn workspace_commit_rejects_cross_workspace_candidate_identity() {
    let (head, change_set) = workspace_commit_identity_inputs();

    let wrong_candidate = ChangeSet {
        candidate: ResourceSnapshotRef::new("other-workspace", "sha256:candidate")
            .with_resource_kind("workspace"),
        ..change_set.clone()
    };
    let error = head
        .commit(WorkspaceCommitRequest::new(
            "commit-wrong-candidate",
            wrong_candidate,
            7,
        ))
        .expect_err("candidate identity from another workspace must be rejected");
    assert!(error.to_string().contains("candidate targets workspace"));
}

#[test]
fn workspace_commit_rejects_stale_passing_gate_subject() {
    let (head, change_set) = workspace_commit_identity_inputs();

    let stale_gate = evaluate_gate(
        "quality",
        ResourceSnapshotRef::new("workspace", "sha256:stale-candidate")
            .with_resource_kind("workspace"),
        &[],
        &[],
        None::<[&str; 0]>,
        &[],
        None,
    );
    assert_eq!(stale_gate.decision, GateDecision::Pass);
    let error = head
        .commit(
            WorkspaceCommitRequest::new("commit-stale-gate", change_set, 7).with_gate(stale_gate),
        )
        .expect_err("passing gate for another candidate must be rejected");
    assert!(error.to_string().contains("gate subject"));
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
fn slo_objective_passes_when_ratio_meets_objective() {
    let objective = SloObjective::at_least(
        "chat-availability",
        "successful_committed_turns / admitted_turns",
        0.995,
        "30d",
    );
    let measurement =
        SloMeasurement::new("successful_committed_turns / admitted_turns", 0.996, "30d")
            .with_sample_count(10_000);

    let report = objective.evaluate(&measurement);

    assert_eq!(report.status, SloReportStatus::Pass);
    assert_eq!(report.slo_id, "chat-availability");
    assert_eq!(report.observed_value, Some(0.996));
    assert_eq!(report.violated_by, None);
}

#[test]
fn slo_objective_fails_when_latency_exceeds_maximum() {
    let objective =
        SloObjective::at_most("first-draft", "p95(turn_first_draft_ms)", 1_500.0, "30d")
            .with_unit("ms");
    let measurement = SloMeasurement::new("p95(turn_first_draft_ms)", 1_700.0, "30d")
        .with_unit("ms")
        .with_sample_count(500);

    let report = objective.evaluate(&measurement);

    assert_eq!(report.status, SloReportStatus::Fail);
    assert_eq!(report.observed_value, Some(1_700.0));
    assert_eq!(report.violated_by, Some(200.0));
}

#[test]
fn slo_objective_is_no_data_for_mismatched_indicator_or_window() {
    let objective =
        SloObjective::at_least("citation-validity", "validated / returned", 0.99, "30d");
    let measurement = SloMeasurement::new("validated / returned", 0.995, "7d");

    let report = objective.evaluate(&measurement);

    assert_eq!(report.status, SloReportStatus::NoData);
    assert_eq!(report.observed_value, None);
    assert_eq!(report.reason.as_deref(), Some("window_mismatch"));
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
fn result_bundle_digest_includes_release_plan_and_signature_provenance() {
    let base = RunProvenance::new("sha256:graph", "2026-06-22T00:00:00Z")
        .with_release("release-1", "rev-1")
        .with_physical_plan_hash("sha256:plan-1")
        .with_release_signature_digest("sha256:signature-1");
    let changed_signature = RunProvenance::new("sha256:graph", "2026-06-22T00:00:00Z")
        .with_release("release-1", "rev-1")
        .with_physical_plan_hash("sha256:plan-1")
        .with_release_signature_digest("sha256:signature-2");
    let changed_plan = RunProvenance::new("sha256:graph", "2026-06-22T00:00:00Z")
        .with_release("release-1", "rev-1")
        .with_physical_plan_hash("sha256:plan-2")
        .with_release_signature_digest("sha256:signature-1");

    let base_digest = ResultBundle::new("bundle-1", "run-1", "release-1")
        .with_provenance(base)
        .content_digest();
    let changed_signature_digest = ResultBundle::new("bundle-2", "run-1", "release-1")
        .with_provenance(changed_signature)
        .content_digest();
    let changed_plan_digest = ResultBundle::new("bundle-3", "run-1", "release-1")
        .with_provenance(changed_plan)
        .content_digest();

    assert_ne!(base_digest, changed_signature_digest);
    assert_ne!(base_digest, changed_plan_digest);
}

#[test]
fn result_bundle_digest_records_model_visible_tool_set_deterministically() {
    let catalog = ToolCatalog::new(
        [
            ToolDefinition::new(
                "knowledge.search",
                "Search support articles.",
                "schemas/Search@1",
            ),
            ToolDefinition::new("ticket.create", "Create a ticket.", "schemas/Ticket@1"),
        ],
        [
            ToolBinding::new(
                "binding-search",
                "knowledge.search",
                ToolImplementation::Block(BlockToolImplementation::new("blocks.search")),
            ),
            ToolBinding::new(
                "binding-ticket",
                "ticket.create",
                ToolImplementation::Block(BlockToolImplementation::new("blocks.ticket.create")),
            ),
        ],
    )
    .expect("catalog should be valid");
    let resolved = catalog
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")
        .expect("tools should resolve");
    let reversed = resolved.iter().rev().collect::<Vec<_>>();

    let stable = RunProvenance::new("sha256:graph", "2026-06-22T00:00:00Z")
        .with_model_visible_tools(&resolved);
    let same_tools_reversed = RunProvenance::new("sha256:graph", "2026-06-22T00:00:00Z")
        .with_model_visible_tools(reversed);
    let missing_tool = RunProvenance::new("sha256:graph", "2026-06-22T00:00:00Z")
        .with_model_visible_tools(resolved.iter().take(1).collect::<Vec<_>>());

    assert_eq!(stable.model_visible_tools.len(), 2);
    assert_eq!(stable.model_visible_tools[0].tool_name, "knowledge.search");
    assert_eq!(
        stable.model_visible_tools[0].definition_digest,
        resolved[0].definition_digest
    );
    assert_eq!(
        ResultBundle::new("bundle-1", "run-1", "release-1")
            .with_provenance(stable)
            .content_digest(),
        ResultBundle::new("bundle-2", "run-1", "release-1")
            .with_provenance(same_tools_reversed)
            .content_digest()
    );
    assert_ne!(
        ResultBundle::new("bundle-3", "run-1", "release-1")
            .with_provenance(missing_tool)
            .content_digest(),
        ResultBundle::new("bundle-4", "run-1", "release-1")
            .with_provenance(
                RunProvenance::new("sha256:graph", "2026-06-22T00:00:00Z")
                    .with_model_visible_tools(&resolved)
            )
            .content_digest()
    );
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

fn rtl_trial_inputs() -> (
    ChangeSet,
    CheckResult,
    CheckResult,
    graphblocks_runtime_core::orchestration::LeaseGrant,
    ReviewRecord,
) {
    let base =
        ResourceSnapshotRef::new("workspace", "sha256:rtl-base").with_resource_kind("workspace");
    let candidate = ResourceSnapshotRef::new("workspace", "sha256:rtl-candidate")
        .with_resource_kind("workspace");
    let change_set = ChangeSet {
        change_set_id: "changeset-rtl-1".to_owned(),
        base,
        candidate: candidate.clone(),
        operations: vec![json!({
            "op": "file.patch",
            "resource_id": "rtl/pipeline.sv",
            "resource_kind": "file",
        })],
        summary: Some("retime pipeline stage".to_owned()),
    };
    let lint = CheckResult::new("lint", candidate.clone(), CheckStatus::Passed)
        .with_tool("processor_id", json!("verilator"));
    let formal = CheckResult::new("formal", candidate.clone(), CheckStatus::Passed)
        .with_tool("processor_id", json!("sby"));
    let (_, lease) = LeasePool::new("formal-license", "eda.formal", 1)
        .expect("lease pool is valid")
        .acquire(
            &LeaseRequest::new("formal-check", "trial:trial-rtl-1", "eda.formal"),
            "lease-formal-1",
            "2026-07-02T00:00:00Z",
            "2026-07-02T00:30:00Z",
        )
        .expect("lease is granted");
    let review = ReviewRecord::new(
        "review-rtl-1",
        candidate.clone(),
        "sha256:rtl-candidate",
        "rtl.owner",
        PrincipalRef::new("reviewer-1"),
        ReviewDecision::Accept,
    )
    .with_created_at("2026-07-02T00:20:00Z");

    (change_set, lint, formal, lease, review)
}

#[test]
fn workspace_trial_plan_builds_commit_request_from_verified_rtl_trial() {
    let (change_set, lint, formal, lease, review) = rtl_trial_inputs();
    let gate = evaluate_gate(
        "rtl-quality",
        change_set.candidate.clone(),
        &[lint.clone(), formal.clone()],
        &[],
        Some(["lint", "formal"]),
        &[],
        Some("policy:rtl-quality".to_owned()),
    );
    let mutation = WorkspaceMutationPolicy::new("policy-1", ["file", "workspace"]).evaluate(
        &change_set,
        &PrincipalRef::new("optimizer-1"),
        &["rtl.owner"],
        &[],
        &[],
    );

    let plan = WorkspaceTrialPlan::new("trial-rtl-1", change_set.clone(), 7)
        .require_checks(["lint", "formal"])
        .require_lease_kinds(["eda.formal"])
        .require_review_scopes(["rtl.owner"])
        .with_check(lint)
        .with_check(formal)
        .with_gate(gate.clone())
        .with_mutation_decision(mutation.clone())
        .with_lease(lease.clone())
        .with_review(review.clone());
    let request = plan
        .to_commit_request("commit-rtl-1", "2026-07-02T00:25:00Z")
        .expect("verified trial can create commit request");

    assert_eq!(request.expected_base_revision, 7);
    assert_eq!(request.change_set, change_set);
    assert_eq!(request.gate, Some(gate));
    assert_eq!(request.mutation_decision, Some(mutation));
    assert_eq!(request.reviews, vec![review]);
    assert_eq!(request.trial_id.as_deref(), Some("trial-rtl-1"));
    assert_eq!(request.required_check_ids, vec!["formal", "lint"]);
    assert_eq!(request.required_lease_kinds, vec!["eda.formal"]);
    assert_eq!(request.required_review_scopes, vec!["rtl.owner"]);
    assert_eq!(request.leases, vec![lease]);
    assert_eq!(request.metadata["trial_id"], json!("trial-rtl-1"));
    assert_eq!(request.metadata["lease_ids"], json!(["lease-formal-1"]));

    let head = WorkspaceHead::new("workspace", change_set.base.clone(), 7);
    assert_eq!(
        head.commit(request.clone())
            .expect_err("lease-bearing commit requires an evaluation time"),
        WorkspaceCommitError::CommitTimeRequired
    );
    head.commit_at(request.clone(), "2026-07-02T00:29:59Z")
        .expect("active trial lease can authorize commit");
    let error = head
        .commit_at(request.clone(), "2026-07-02T00:30:00Z")
        .expect_err("expired trial lease must not authorize commit");
    assert!(error.to_string().contains("active lease kind"));

    let mut missing_gate_check = request.clone();
    missing_gate_check
        .gate
        .as_mut()
        .expect("verified request has a gate")
        .check_ids
        .clear();
    let error = head
        .commit_at(missing_gate_check, "2026-07-02T00:29:59Z")
        .expect_err("required gate checks must remain bound at commit");
    assert!(error.to_string().contains("required gate check"));

    let mut missing_review = request;
    missing_review.reviews.clear();
    let error = head
        .commit_at(missing_review, "2026-07-02T00:29:59Z")
        .expect_err("required review scopes must remain bound at commit");
    assert!(error.to_string().contains("required review scope"));
}

#[test]
fn workspace_trial_plan_rejects_missing_proof_before_commit_request() {
    let (change_set, lint, formal, lease, review) = rtl_trial_inputs();
    let gate = evaluate_gate(
        "rtl-quality",
        change_set.candidate.clone(),
        &[lint.clone(), formal.clone()],
        &[],
        Some(["lint", "formal"]),
        &[],
        None,
    );

    let missing_lease = WorkspaceTrialPlan::new("trial-rtl-1", change_set.clone(), 7)
        .require_checks(["lint", "formal"])
        .require_lease_kinds(["eda.formal"])
        .require_review_scopes(["rtl.owner"])
        .with_check(lint.clone())
        .with_check(formal.clone())
        .with_gate(gate.clone())
        .with_review(review.clone());
    let failing_gate = WorkspaceTrialPlan::new("trial-rtl-1", change_set.clone(), 7)
        .require_checks(["lint", "formal"])
        .with_check(lint.clone())
        .with_check(formal.clone())
        .with_gate(evaluate_gate(
            "rtl-quality",
            change_set.candidate.clone(),
            &[lint.clone(), formal.clone()],
            &[MetricObservation::new("wns_ns", json!(-0.12))],
            Some(["lint", "formal"]),
            &[GateConstraint::new(
                "wns_ns",
                ConstraintOperator::AtLeast,
                json!(0.0),
            )],
            None,
        ))
        .with_lease(lease.clone());
    let stale_review = WorkspaceTrialPlan::new("trial-rtl-1", change_set.clone(), 7)
        .require_checks(["lint"])
        .require_review_scopes(["rtl.owner"])
        .with_check(lint.clone())
        .with_gate(evaluate_gate(
            "rtl-quality",
            change_set.candidate.clone(),
            std::slice::from_ref(&lint),
            &[],
            Some(["lint"]),
            &[],
            None,
        ))
        .with_review(ReviewRecord::new(
            "review-stale",
            ResourceSnapshotRef::new("workspace", "sha256:old-candidate"),
            "sha256:old-candidate",
            "rtl.owner",
            PrincipalRef::new("reviewer-1"),
            ReviewDecision::Accept,
        ));

    assert_eq!(
        missing_lease.to_commit_request("commit-rtl-1", "2026-07-02T00:25:00Z"),
        Err(WorkspaceTrialError::MissingLeaseKind {
            resource_kind: "eda.formal".to_owned(),
        })
    );
    assert_eq!(
        failing_gate.to_commit_request("commit-rtl-1", "2026-07-02T00:25:00Z"),
        Err(WorkspaceTrialError::GateNotPassed {
            gate_id: "rtl-quality".to_owned(),
            decision: GateDecision::Fail,
        })
    );
    assert_eq!(
        stale_review.to_commit_request("commit-rtl-1", "2026-07-02T00:25:00Z"),
        Err(WorkspaceTrialError::MissingReviewScope {
            scope: "rtl.owner".to_owned(),
        })
    );
}
