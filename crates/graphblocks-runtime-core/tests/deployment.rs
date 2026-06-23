use graphblocks_runtime_core::deployment::{
    DeploymentEvent, DeploymentEventKind, DeploymentObservabilityContext, DeploymentRevision,
    ExecutionTarget, ExecutionTargetKind, GraphRelease, GraphReleaseError, GraphReleaseGraph,
    ImageRef, KnowledgeBinding, PhysicalExecutionPlan, PlacementError, PlacementRule,
    PlacementSelector, PromptLock, RevisionDecision, UpgradePolicy, WorkloadKind,
};
use serde_json::json;

#[test]
fn deployment_revision_digest_is_stable_without_record_identity() {
    let left = DeploymentRevision::new(
        "rev-1",
        "sha256:release",
        "sha256:deployment",
        "sha256:physical",
        "sha256:binding",
        "sha256:target",
        "2026-06-23T00:00:00Z",
    );
    let right = DeploymentRevision::new(
        "rev-2",
        "sha256:release",
        "sha256:deployment",
        "sha256:physical",
        "sha256:binding",
        "sha256:target",
        "2026-06-23T00:01:00Z",
    );

    assert_eq!(left.content_digest(), right.content_digest());
    assert_eq!(left.revision_id, "rev-1");
    assert_eq!(left.release_digest, "sha256:release");
}

#[test]
fn physical_plan_hash_is_stable_for_target_and_rule_order() {
    let control = ExecutionTarget::new("control", ExecutionTargetKind::Service, "rust")
        .with_capabilities(["graph.coordinator", "model.remote_call"]);
    let doc_cpu = ExecutionTarget::new("doc-cpu", ExecutionTargetKind::WorkerPool, "python_worker")
        .with_capabilities(["document.parse.pdf"]);
    let left = PhysicalExecutionPlan::new("sha256:release", "rev-1", "sha256:graph")
        .with_package_lock_hash("sha256:package")
        .with_target(doc_cpu.clone())
        .with_target(control.clone())
        .with_placement(PlacementRule::new(
            "docs",
            PlacementSelector::capabilities(["document.parse.pdf"]),
            "doc-cpu",
        ))
        .with_placement(PlacementRule::new(
            "generate",
            PlacementSelector::nodes(["generate"]),
            "control",
        ));
    let right = PhysicalExecutionPlan::new("sha256:release", "rev-1", "sha256:graph")
        .with_package_lock_hash("sha256:package")
        .with_target(control)
        .with_target(doc_cpu)
        .with_placement(PlacementRule::new(
            "generate",
            PlacementSelector::nodes(["generate"]),
            "control",
        ))
        .with_placement(PlacementRule::new(
            "docs",
            PlacementSelector::capabilities(["document.parse.pdf"]),
            "doc-cpu",
        ));

    assert_eq!(left.plan_hash(), right.plan_hash());
}

#[test]
fn placement_resolution_applies_priority_from_node_to_default() -> Result<(), PlacementError> {
    let plan = PhysicalExecutionPlan::new("sha256:release", "rev-1", "sha256:graph")
        .with_target(
            ExecutionTarget::new("control", ExecutionTargetKind::Service, "rust")
                .with_capabilities(["graph.coordinator"]),
        )
        .with_target(
            ExecutionTarget::new("doc-cpu", ExecutionTargetKind::WorkerPool, "python_worker")
                .with_capabilities(["document.parse.pdf"]),
        )
        .with_target(
            ExecutionTarget::new("sandbox", ExecutionTargetKind::SandboxPool, "python_worker")
                .with_effects(["process_execution"]),
        )
        .with_default_target("control")
        .with_placement(PlacementRule::new(
            "group-doc",
            PlacementSelector::execution_groups(["per-document"]),
            "doc-cpu",
        ))
        .with_placement(PlacementRule::new(
            "block-doc",
            PlacementSelector::blocks(["document.parse"]),
            "doc-cpu",
        ))
        .with_placement(PlacementRule::new(
            "effect-sandbox",
            PlacementSelector::effects(["process_execution"]),
            "sandbox",
        ))
        .with_placement(PlacementRule::new(
            "node-control",
            PlacementSelector::nodes(["parse-one"]),
            "control",
        ));

    assert_eq!(
        plan.resolve_target(
            "parse-one",
            Some("per-document"),
            "document.parse",
            ["document.parse.pdf"],
            ["process_execution"],
            Some("batch"),
        )?
        .target_id,
        "control",
    );
    assert_eq!(
        plan.resolve_target(
            "parse-two",
            Some("per-document"),
            "document.parse",
            ["document.parse.pdf"],
            std::iter::empty::<&str>(),
            None,
        )?
        .target_id,
        "doc-cpu",
    );
    assert_eq!(
        plan.resolve_target(
            "other",
            None,
            "value.const",
            std::iter::empty::<&str>(),
            std::iter::empty::<&str>(),
            None,
        )?
        .target_id,
        "control",
    );
    Ok(())
}

#[test]
fn placement_resolution_rejects_same_priority_conflicts() {
    let plan = PhysicalExecutionPlan::new("sha256:release", "rev-1", "sha256:graph")
        .with_target(ExecutionTarget::new(
            "control",
            ExecutionTargetKind::Service,
            "rust",
        ))
        .with_target(ExecutionTarget::new(
            "doc-cpu",
            ExecutionTargetKind::WorkerPool,
            "python_worker",
        ))
        .with_placement(PlacementRule::new(
            "a",
            PlacementSelector::nodes(["generate"]),
            "control",
        ))
        .with_placement(PlacementRule::new(
            "b",
            PlacementSelector::nodes(["generate"]),
            "doc-cpu",
        ));

    assert_eq!(
        plan.resolve_target(
            "generate",
            None,
            "model.generate",
            std::iter::empty::<&str>(),
            std::iter::empty::<&str>(),
            None,
        ),
        Err(PlacementError::AmbiguousPlacement {
            node_id: "generate".to_owned(),
            priority: "node".to_owned(),
            target_ids: vec!["control".to_owned(), "doc-cpu".to_owned()],
        })
    );
}

#[test]
fn graph_release_digest_is_stable_for_artifact_order() {
    let left = GraphRelease::new("enterprise-rag", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_application_hash("sha256:app")
        .with_graph(
            "chat",
            GraphReleaseGraph::new("sha256:graph-chat", "sha256:plan-chat"),
        )
        .with_graph(
            "ingest",
            GraphReleaseGraph::new("sha256:graph-ingest", "sha256:plan-ingest"),
        )
        .with_image(
            "worker",
            ImageRef::new("registry.example.com/gb/worker@sha256:abc"),
        )
        .with_prompt_lock(
            "answer",
            PromptLock::versioned("support.answer", "2026-06-23"),
        );
    let right = GraphRelease::new("enterprise-rag-copy", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_application_hash("sha256:app")
        .with_graph(
            "ingest",
            GraphReleaseGraph::new("sha256:graph-ingest", "sha256:plan-ingest"),
        )
        .with_graph(
            "chat",
            GraphReleaseGraph::new("sha256:graph-chat", "sha256:plan-chat"),
        )
        .with_prompt_lock(
            "answer",
            PromptLock::versioned("support.answer", "2026-06-23"),
        )
        .with_image(
            "worker",
            ImageRef::new("registry.example.com/gb/worker@sha256:abc"),
        );

    assert_eq!(left.content_digest(), right.content_digest());
}

#[test]
fn graph_release_validation_rejects_mutable_production_references() {
    let release = GraphRelease::new("enterprise-rag", "2026.06.23.1")
        .with_bundle("latest", "application/vnd.graphblocks.release.v1")
        .with_graph("chat", GraphReleaseGraph::new("main", "sha256:plan-chat"))
        .with_image(
            "control",
            ImageRef::new("registry.example.com/gb/control:latest"),
        )
        .with_prompt_lock("answer", PromptLock::label("support.answer", "production"))
        .with_knowledge(KnowledgeBinding::new("intranet_docs", "current"));

    assert_eq!(
        release.validate_production_pins(),
        Err(GraphReleaseError::MutableReferences {
            references: vec![
                "bundle.digest".to_owned(),
                "graphs.chat.graph_hash".to_owned(),
                "images.control".to_owned(),
                "knowledge.intranet_docs.index_revision".to_owned(),
                "prompts.answer".to_owned(),
            ],
        })
    );
}

#[test]
fn deployment_event_exports_release_revision_and_cohort_attributes() {
    let context = DeploymentObservabilityContext::new("release-1", "rev-1")
        .with_release_digest("sha256:release")
        .with_rollout("rollout-1", "step-2", "canary");
    let event = DeploymentEvent::new(
        "event-1",
        DeploymentEventKind::RolloutGateFailed,
        context,
        "2026-06-23T00:00:00Z",
    )
    .with_metadata("reason", json!("latency_regression"));

    let attributes = event.telemetry_attributes();

    assert_eq!(
        DeploymentEventKind::RolloutGateFailed.as_str(),
        "rollout.gate.failed"
    );
    assert_eq!(
        attributes.get("deployment.event").map(String::as_str),
        Some("rollout.gate.failed")
    );
    assert_eq!(
        attributes.get("graphblocks.release.id").map(String::as_str),
        Some("release-1")
    );
    assert_eq!(
        attributes
            .get("graphblocks.deployment.revision")
            .map(String::as_str),
        Some("rev-1")
    );
    assert_eq!(
        attributes
            .get("graphblocks.rollout.cohort")
            .map(String::as_str),
        Some("canary")
    );
    assert_eq!(
        event.metadata.get("reason"),
        Some(&json!("latency_regression"))
    );
}

#[test]
fn deployment_observability_context_compares_stable_and_canary_rollout_step() {
    let stable = DeploymentObservabilityContext::new("release-stable", "rev-stable").with_rollout(
        "rollout-1",
        "step-2",
        "stable",
    );
    let canary = DeploymentObservabilityContext::new("release-canary", "rev-canary").with_rollout(
        "rollout-1",
        "step-2",
        "canary",
    );
    let later = DeploymentObservabilityContext::new("release-canary", "rev-canary").with_rollout(
        "rollout-1",
        "step-3",
        "canary",
    );

    assert!(stable.same_rollout_step(&canary));
    assert!(!stable.same_rollout_step(&later));
    assert_ne!(stable.cohort.as_deref(), canary.cohort.as_deref());
}

#[test]
fn upgrade_policy_finishes_existing_requests_on_old_revision() {
    let policy = UpgradePolicy::workload_aware("rev-old", "rev-new");

    assert_eq!(
        policy.decide(WorkloadKind::ExistingRequest, None, false),
        RevisionDecision::FinishOnOld {
            revision_id: "rev-old".to_owned(),
        }
    );
}

#[test]
fn upgrade_policy_preserves_conversation_affinity() {
    let policy = UpgradePolicy::workload_aware("rev-old", "rev-new");

    assert_eq!(
        policy.decide(
            WorkloadKind::Conversation,
            Some("rev-conversation-affinity"),
            false,
        ),
        RevisionDecision::KeepAffinity {
            revision_id: "rev-conversation-affinity".to_owned(),
        }
    );
    assert_eq!(
        policy.decide(WorkloadKind::Conversation, None, false),
        RevisionDecision::AdmitOnNew {
            revision_id: "rev-new".to_owned(),
        }
    );
}

#[test]
fn upgrade_policy_migrates_compatible_durable_jobs_and_drains_realtime_on_old() {
    let policy = UpgradePolicy::workload_aware("rev-old", "rev-new");

    assert_eq!(
        policy.decide(WorkloadKind::DurableJob, Some("rev-old"), true),
        RevisionDecision::CheckpointAndMigrate {
            from_revision_id: "rev-old".to_owned(),
            to_revision_id: "rev-new".to_owned(),
        }
    );
    assert_eq!(
        policy.decide(WorkloadKind::RealtimeSession, Some("rev-old"), true),
        RevisionDecision::DrainOnOld {
            revision_id: "rev-old".to_owned(),
        }
    );
}
