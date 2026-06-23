use graphblocks_runtime_core::agent::{
    AgentLoopController, AgentLoopDecision, AgentSpec, AgentState, AgentStateError,
    AgentStatePatch, AgentStateSchema, ToolFailurePolicy,
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
