use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft};
use graphblocks_runtime_core::tool_execution::{
    ToolExecutionPlan, ToolExecutionPlanError, ToolExecutionState, ToolPlanCall,
};

fn tool_call(tool_call_id: &str, arguments: &str) -> ToolCall {
    let mut draft = ToolCallDraft::proposed("response-1", tool_call_id, "ticket.create");
    draft
        .append_argument_fragment(arguments)
        .expect("test argument fragment is accepted");
    draft
        .into_completed_tool_call("resolved-tool-1", 1_000)
        .expect("test arguments are valid JSON")
}

#[test]
fn independent_calls_are_ready_up_to_maximum_parallelism() -> Result<(), ToolExecutionPlanError> {
    let plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(tool_call("call-b", "{\"resource_id\":\"b\"}")),
        ],
        2,
    )?;

    assert_eq!(
        plan.ready_call_ids(),
        vec!["call-a".to_owned(), "call-b".to_owned()]
    );
    Ok(())
}

#[test]
fn dependent_calls_wait_for_completed_dependencies() -> Result<(), ToolExecutionPlanError> {
    let mut dependent = tool_call("call-b", "{\"resource_id\":\"b\"}");
    dependent.depends_on = vec!["call-a".to_owned()];
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(dependent),
        ],
        2,
    )?;

    assert_eq!(plan.ready_call_ids(), vec!["call-a".to_owned()]);
    plan.record_started("call-a")?;
    plan.record_completed("call-a")?;
    assert_eq!(plan.ready_call_ids(), vec!["call-b".to_owned()]);
    Ok(())
}

#[test]
fn conflicting_effect_keys_are_serialized() -> Result<(), ToolExecutionPlanError> {
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"ticket-1\"}"))
                .with_effect_key("ticket:ticket-1"),
            ToolPlanCall::new(tool_call("call-b", "{\"resource_id\":\"ticket-1\"}"))
                .with_effect_key("ticket:ticket-1"),
        ],
        2,
    )?;

    assert_eq!(plan.ready_call_ids(), vec!["call-a".to_owned()]);
    plan.record_started("call-a")?;
    assert_eq!(plan.ready_call_ids(), Vec::<String>::new());
    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Running));

    plan.record_completed("call-a")?;
    assert_eq!(plan.ready_call_ids(), vec!["call-b".to_owned()]);
    Ok(())
}
