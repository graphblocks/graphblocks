use graphblocks_runtime_core::deployment::{
    DeploymentRevision, ExecutionTarget, ExecutionTargetKind, PhysicalExecutionPlan,
    PlacementError, PlacementRule, PlacementSelector,
};

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
