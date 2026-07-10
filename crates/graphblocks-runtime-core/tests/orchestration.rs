use std::collections::BTreeMap;
use std::error::Error;

use graphblocks_runtime_core::budget::{BudgetPermit, UsageAmount};
use graphblocks_runtime_core::orchestration::{
    ChildBudgetDelegation, LeasePool, LeasePoolError, LeaseRequest, ModelPool, ModelProfile,
    ModelSelectionError, ModelSelectionRequest, TaskContextAccess, TaskContextAccessEdge,
    TaskContextAccessErrorReason, TaskContextConflictKind, TaskPlan, TaskPlanError, TaskPlanLimits,
    TaskPlanPatch, TaskStep, WorkerProfile,
};
use serde_json::json;

fn tokens(amount: i64) -> UsageAmount {
    UsageAmount::new("tokens", amount, "tokens")
}

#[test]
fn task_plan_patch_revises_steps_and_preserves_noop_digest() -> Result<(), Box<dyn Error>> {
    let base = TaskPlan::new("plan-1", "answer support request")?
        .with_steps([TaskStep::new("draft", "Draft response")])?;
    let patch = TaskPlanPatch::new("patch-1", "plan-1", 1)
        .with_upsert_steps([
            TaskStep::new("verify", "Verify answer").with_depends_on(["draft"]),
            TaskStep::new("draft", "Draft response with citations"),
        ])
        .with_remove_step_ids(["missing"]);

    let updated = base.apply_patch(patch)?;

    assert_eq!(updated.revision, 2);
    assert_eq!(
        updated
            .steps
            .iter()
            .map(|step| step.step_id.as_str())
            .collect::<Vec<_>>(),
        vec!["draft", "verify"]
    );
    assert_eq!(
        updated.step("draft")?.description,
        "Draft response with citations"
    );
    assert_eq!(
        updated.content_digest(),
        updated
            .apply_patch(TaskPlanPatch::new("noop", "plan-1", 2))?
            .content_digest()
    );
    Ok(())
}

#[test]
fn task_plan_patch_rejects_duplicate_upsert_steps() -> Result<(), Box<dyn Error>> {
    let base = TaskPlan::new("plan-1", "answer support request")?
        .with_steps([TaskStep::new("draft", "Draft response")])?;
    let duplicate = base
        .apply_patch(
            TaskPlanPatch::new("patch-duplicate", "plan-1", 1).with_upsert_steps([
                TaskStep::new("verify", "Verify citations"),
                TaskStep::new("verify", "Verify policy"),
            ]),
        )
        .expect_err("duplicate upserts should be rejected before last-write-wins");

    assert_eq!(
        duplicate,
        TaskPlanError::DuplicateStep {
            step_id: "verify".to_owned(),
        }
    );
    Ok(())
}

#[test]
fn task_plan_reports_missing_dependencies_cycles_and_context_errors() -> Result<(), Box<dyn Error>>
{
    let missing = TaskPlan::new("plan-1", "answer support request")?
        .with_steps([TaskStep::new("verify", "Verify answer").with_depends_on(["draft"])])
        .expect_err("missing dependency should be rejected");
    assert_eq!(
        missing,
        TaskPlanError::DependencyMissing {
            step_id: "verify".to_string(),
            dependency_id: "draft".to_string(),
        }
    );

    let base = TaskPlan::new("plan-1", "answer support request")?.with_steps([
        TaskStep::new("draft", "Draft response"),
        TaskStep::new("verify", "Verify answer").with_depends_on(["draft"]),
    ])?;
    let cycle = base
        .apply_patch(
            TaskPlanPatch::new("patch-cycle", "plan-1", 1).with_upsert_steps([TaskStep::new(
                "draft",
                "Draft response",
            )
            .with_depends_on(["verify"])]),
        )
        .expect_err("cycle should be rejected");
    assert_eq!(
        cycle,
        TaskPlanError::Cycle {
            cycle: vec![
                "draft".to_string(),
                "verify".to_string(),
                "draft".to_string()
            ],
        }
    );

    let context_error = TaskPlan::from_parts(
        "plan-2",
        "answer support request",
        1,
        vec![TaskStep::new("draft", "Draft response")],
        BTreeMap::new(),
        TaskPlanLimits::default(),
        vec!["policy-doc".to_string()],
        vec![TaskContextAccess::new("draft", "secret-vault", "read")],
    )
    .expect_err("unknown resource should be rejected");
    assert_eq!(
        context_error,
        TaskPlanError::ContextAccess {
            step_id: "draft".to_string(),
            resource_id: "secret-vault".to_string(),
            mode: "read".to_string(),
            reason: TaskContextAccessErrorReason::UnknownResource,
        }
    );
    Ok(())
}

#[test]
fn task_plan_context_access_graph_serializes_write_conflicts() -> Result<(), Box<dyn Error>> {
    let plan = TaskPlan::new("plan-1", "verify workspace")?
        .with_steps([
            TaskStep::new("check", "Run checks").with_depends_on(["patch"]),
            TaskStep::new("index", "Read docs"),
            TaskStep::new("patch", "Apply patch"),
            TaskStep::new("summarize", "Summarize docs"),
        ])?
        .with_context_resources(["workspace", "docs"])?
        .with_context_access([
            TaskContextAccess::new("patch", "workspace", "write"),
            TaskContextAccess::new("check", "workspace", "read"),
            TaskContextAccess::new("index", "docs", "read"),
            TaskContextAccess::new("summarize", "docs", "read"),
        ])?;

    let graph = plan.context_access_graph();

    assert_eq!(
        graph.edges,
        vec![TaskContextAccessEdge {
            from_step_id: "patch".to_owned(),
            to_step_id: "check".to_owned(),
            resource_id: "workspace".to_owned(),
            conflict: TaskContextConflictKind::WriteRead,
        }]
    );
    assert_eq!(
        graph.edge_contracts(),
        vec![json!({
            "from_step_id": "patch",
            "to_step_id": "check",
            "resource_id": "workspace",
            "conflict": "write_read"
        })]
    );
    assert!(graph.content_digest().starts_with("sha256:"));
    Ok(())
}

#[test]
fn task_plan_context_access_graph_orders_independent_writes_deterministically()
-> Result<(), Box<dyn Error>> {
    let plan = TaskPlan::new("plan-1", "merge workspace")?
        .with_steps([
            TaskStep::new("write-b", "Write generated file B"),
            TaskStep::new("write-a", "Write generated file A"),
        ])?
        .with_context_resources(["workspace"])?
        .with_context_access([
            TaskContextAccess::new("write-b", "workspace", "write"),
            TaskContextAccess::new("write-a", "workspace", "write"),
        ])?;

    assert_eq!(
        plan.context_access_graph().edges,
        vec![TaskContextAccessEdge {
            from_step_id: "write-a".to_owned(),
            to_step_id: "write-b".to_owned(),
            resource_id: "workspace".to_owned(),
            conflict: TaskContextConflictKind::WriteWrite,
        }]
    );
    Ok(())
}

#[test]
fn model_pool_selects_first_eligible_model_and_rejects_disallowed_tool() {
    let pool = ModelPool::new("support-pool", "policy-1").with_models([
        ModelProfile::new("public-only", "models.public")
            .with_capabilities(["chat", "tool_use"])
            .with_allowed_sensitivity(["public"])
            .with_regions(["us-east-1"]),
        ModelProfile::new("support-internal", "models.support")
            .with_capabilities(["chat", "tool_use", "json"])
            .with_allowed_sensitivity(["public", "internal"])
            .with_regions(["us-east-1", "eu-west-1"])
            .with_usage_report(true)
            .with_cancellation(true),
    ]);
    let worker = WorkerProfile::new("support-worker")
        .with_required_capabilities(["chat", "tool_use"])
        .with_allowed_tools(["knowledge.search"])
        .with_model_pool_ref("support-pool")
        .with_sensitivity_ceiling("internal");
    let request = ModelSelectionRequest::new(worker.clone())
        .with_required_tools(["knowledge.search"])
        .with_required_capabilities(["json"])
        .with_sensitivity("internal")
        .with_region("us-east-1");

    let selected = pool.select_model(&request).expect("model is eligible");

    assert_eq!(selected.profile_id, "support-internal");
    assert_eq!(selected.connection, "models.support");
    assert!(selected.supports_usage_report);
    assert!(selected.supports_cancellation);

    assert_eq!(
        pool.select_model(
            &ModelSelectionRequest::new(worker).with_required_tools(["ticket.create"])
        )
        .expect_err("tool should be denied"),
        ModelSelectionError::ToolNotAllowed {
            tool_name: "ticket.create".to_string(),
        }
    );
}

#[test]
fn lease_pool_enforces_capacity_and_fencing_epoch() -> Result<(), Box<dyn Error>> {
    let pool = LeasePool::new("formal-license", "eda.formal", 1)?;
    let (leased, grant) = pool.acquire(
        &LeaseRequest::new("formal-check-1", "trial:formal-1", "eda.formal"),
        "lease-1",
        "2026-06-26T00:00:00Z",
        "2026-06-26T00:05:00Z",
    )?;

    assert_eq!(grant.fencing_epoch, 1);
    assert_eq!(leased.available_units(), 0);
    assert_eq!(
        leased
            .acquire(
                &LeaseRequest::new("formal-check-2", "trial:formal-2", "eda.formal"),
                "lease-2",
                "2026-06-26T00:01:00Z",
                "2026-06-26T00:06:00Z",
            )
            .expect_err("pool is exhausted"),
        LeasePoolError::Exhausted {
            pool_id: "formal-license".to_string(),
            requested_units: 1,
            available_units: 0,
        }
    );
    assert_eq!(
        leased
            .release("lease-1", 99)
            .expect_err("fencing epoch should match"),
        LeasePoolError::EpochMismatch {
            lease_id: "lease-1".to_string(),
            expected_epoch: 1,
            actual_epoch: 99,
        }
    );
    assert_eq!(
        leased
            .release("lease-1", grant.fencing_epoch)?
            .available_units(),
        1
    );
    Ok(())
}

#[test]
fn lease_pool_reap_expired_compares_expiration_as_datetime() -> Result<(), Box<dyn Error>> {
    let pool = LeasePool::new("formal-license", "eda.formal", 1)?;
    let (leased, grant) = pool.acquire(
        &LeaseRequest::new("formal-check", "trial:formal", "eda.formal"),
        "lease-1",
        "2026-06-23T00:00:00Z",
        "2026-06-23T19:05:00-05:00",
    )?;

    let early = leased.reap_expired("2026-06-24T00:04:59Z")?;
    let reaped = leased.reap_expired("2026-06-24T00:05:01Z")?;

    assert_eq!(early.active_leases, vec![grant]);
    assert_eq!(early.available_units(), 0);
    assert!(reaped.active_leases.is_empty());
    assert_eq!(reaped.available_units(), 1);
    Ok(())
}

#[test]
fn child_budget_delegation_creates_scoped_permit() -> Result<(), Box<dyn Error>> {
    let parent = BudgetPermit {
        permit_id: "permit-parent".to_string(),
        reservation_refs: vec!["reservation-parent".to_string()],
        owner: "task:parent".to_string(),
        atomic_unit: "turn:1".to_string(),
        admission_epoch: 3,
        authorized_amounts: vec![tokens(100)],
        continuation_profile: "default".to_string(),
        policy_snapshot_digest: "sha256:policy".to_string(),
        expires_at: "2026-06-26T01:00:00Z".to_string(),
        low_watermark: Vec::new(),
        fencing_tokens: BTreeMap::from([("reservation-parent".to_string(), 11)]),
    };
    let delegation = ChildBudgetDelegation::new(
        "delegation-1",
        parent.clone(),
        "task:child",
        [tokens(40)],
        "2026-06-26T00:30:00Z",
    )
    .with_continuation_profile("child");

    let permit = delegation.create_child_permit("permit-child")?;

    assert_eq!(permit.permit_id, "permit-child");
    assert_eq!(permit.owner, "task:child");
    assert_eq!(permit.atomic_unit, parent.atomic_unit);
    assert_eq!(permit.authorized_amounts, vec![tokens(40)]);
    assert_eq!(permit.continuation_profile, "child");
    assert_eq!(permit.reservation_refs, vec!["reservation-parent"]);
    assert_eq!(
        permit.fencing_tokens,
        BTreeMap::from([("reservation-parent".to_string(), 11)])
    );
    Ok(())
}
