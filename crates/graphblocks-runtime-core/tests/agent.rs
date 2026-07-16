use graphblocks_runtime_core::agent::{
    AgentLoopController, AgentLoopDecision, AgentSpec, AgentState, AgentStateError,
    AgentStatePatch, AgentStateSchema, ModelPool, ModelProfile, ModelSelectionError,
    ModelSelectionRequest, ToolFailurePolicy, WorkerProfile,
};
use serde_json::json;

#[test]
fn agent_spec_defaults_match_conversation_profile() {
    let spec = AgentSpec::new("support-models");

    assert_eq!(spec.model_pool, "support-models");
    assert_eq!(spec.max_steps, 12);
    assert_eq!(spec.exit_conditions, vec!["final_message"]);
    assert_eq!(spec.tool_failure, ToolFailurePolicy::ReturnToModel);
    assert!(spec.parallel_tool_calls);
    assert!(spec.tools.is_empty());
    assert_eq!(spec.output_policy_profile_ref, None);
}

#[test]
fn agent_state_patch_increments_revision_and_preserves_json_null()
-> Result<(), Box<dyn std::error::Error>> {
    let schema = AgentStateSchema::new(["profile", "note"]);
    let mut state = AgentState::new();
    state.apply_patch(
        0,
        AgentStatePatch::new()
            .set("profile", json!(null))
            .set("note", json!("keep")),
        Some(&schema),
    )?;

    assert_eq!(state.revision, 1);
    assert!(state.values.contains_key("profile"));
    assert_eq!(state.values["profile"], json!(null));
    assert_eq!(state.values["note"], json!("keep"));

    state.apply_patch(1, AgentStatePatch::new().delete("note"), Some(&schema))?;

    assert_eq!(state.revision, 2);
    assert!(state.values.contains_key("profile"));
    assert!(!state.values.contains_key("note"));
    Ok(())
}

#[test]
fn agent_state_patch_rejects_unknown_schema_keys() {
    let schema = AgentStateSchema::new(["profile"]);
    let mut state = AgentState::new();

    let error = state
        .apply_patch(
            0,
            AgentStatePatch::new().set("unapproved", json!("value")),
            Some(&schema),
        )
        .expect_err("schema rejects unknown keys");

    assert_eq!(
        error,
        AgentStateError::UnknownStateKey {
            key: "unapproved".to_owned()
        }
    );
    assert_eq!(state.revision, 0);
    assert!(state.values.is_empty());
}

#[test]
fn agent_loop_forces_finalization_at_completion_reserve() {
    let spec = AgentSpec::new("support-models")
        .with_completion_reserve_units(100)
        .with_max_steps(12);
    let controller = AgentLoopController::new(spec);

    let decision = controller.decide_next_step(3, 100);

    assert_eq!(
        decision,
        AgentLoopDecision::Finalize {
            reason: "completion_reserve_reached".to_owned()
        }
    );
}

#[test]
fn agent_spec_records_output_policy_profile_ref_for_agent_run() {
    let spec = AgentSpec::new("support-models")
        .with_tools(["knowledge.search"])
        .with_output_policy_profile_ref("assistant-output-standard");

    assert_eq!(
        spec.output_policy_profile_ref.as_deref(),
        Some("assistant-output-standard")
    );
    assert_eq!(spec.tools, vec!["knowledge.search"]);
}

#[test]
fn agent_loop_stops_at_max_steps() {
    let spec = AgentSpec::new("support-models").with_max_steps(4);
    let controller = AgentLoopController::new(spec);

    let decision = controller.decide_next_step(4, 1_000);

    assert_eq!(
        decision,
        AgentLoopDecision::Stop {
            reason: "max_steps_reached".to_owned()
        }
    );
}

#[test]
fn agent_loop_continues_when_step_and_budget_boundaries_allow_work() {
    let spec = AgentSpec::new("support-models")
        .with_completion_reserve_units(100)
        .with_max_steps(12);
    let controller = AgentLoopController::new(spec);

    let decision = controller.decide_next_step(3, 101);

    assert_eq!(
        decision,
        AgentLoopDecision::Continue {
            reason: "admitted".to_owned()
        }
    );
}

#[test]
fn model_pool_selects_first_profile_matching_worker_policy_and_request()
-> Result<(), Box<dyn std::error::Error>> {
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
    let request = ModelSelectionRequest::new(worker)
        .with_required_tools(["knowledge.search"])
        .with_sensitivity("internal")
        .with_region("us-east-1");

    let selected = pool.select_model(&request)?;

    assert_eq!(selected.profile_id, "support-internal");
    assert_eq!(selected.connection, "models.support");
    assert!(selected.supports_usage_report);
    assert!(selected.supports_cancellation);
    Ok(())
}

#[test]
fn model_pool_rejects_tool_not_allowed_by_worker_profile() {
    let pool = ModelPool::new("support-pool", "policy-1").with_models([ModelProfile::new(
        "support-internal",
        "models.support",
    )
    .with_capabilities(["chat"])
    .with_allowed_sensitivity(["internal"])
    .with_regions(["us-east-1"])]);
    let worker = WorkerProfile::new("support-worker")
        .with_required_capabilities(["chat"])
        .with_allowed_tools(["knowledge.search"])
        .with_model_pool_ref("support-pool");
    let request = ModelSelectionRequest::new(worker)
        .with_required_tools(["ticket.create"])
        .with_sensitivity("internal")
        .with_region("us-east-1");

    let error = pool
        .select_model(&request)
        .expect_err("worker policy denies ticket.create");

    assert_eq!(
        error,
        ModelSelectionError::ToolNotAllowed {
            tool_name: "ticket.create".to_owned()
        }
    );
}

#[test]
fn model_pool_rejects_sensitivity_above_worker_ceiling() {
    let pool = ModelPool::new("support-pool", "policy-1");
    let worker = WorkerProfile::new("support-worker")
        .with_model_pool_ref("support-pool")
        .with_sensitivity_ceiling("internal");
    let request = ModelSelectionRequest::new(worker).with_sensitivity("restricted");

    let error = pool
        .select_model(&request)
        .expect_err("restricted data exceeds worker ceiling");

    assert_eq!(
        error,
        ModelSelectionError::SensitivityAboveCeiling {
            requested: "restricted".to_owned(),
            ceiling: "internal".to_owned()
        }
    );
}

#[test]
fn model_pool_rejects_unknown_worker_sensitivity_ceiling() {
    let pool = ModelPool::new("support-pool", "policy-1");
    let worker = WorkerProfile::new("support-worker")
        .with_model_pool_ref("support-pool")
        .with_sensitivity_ceiling("internl");
    let request = ModelSelectionRequest::new(worker).with_sensitivity("public");

    assert_eq!(
        pool.select_model(&request),
        Err(ModelSelectionError::SensitivityAboveCeiling {
            requested: "public".to_owned(),
            ceiling: "internl".to_owned(),
        })
    );
}

#[test]
fn model_pool_rejects_mismatched_worker_pool_ref() {
    let pool = ModelPool::new("support-pool", "policy-1");
    let worker = WorkerProfile::new("support-worker").with_model_pool_ref("other-pool");
    let request = ModelSelectionRequest::new(worker);

    let error = pool
        .select_model(&request)
        .expect_err("worker points at a different pool");

    assert_eq!(
        error,
        ModelSelectionError::PoolMismatch {
            expected: "other-pool".to_owned(),
            actual: "support-pool".to_owned()
        }
    );
}
