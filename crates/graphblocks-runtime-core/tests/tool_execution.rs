use graphblocks_runtime_core::output_policy::PendingToolCallsDisposition;
use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft};
use graphblocks_runtime_core::tool_execution::{
    ToolExecutionFailurePolicy, ToolExecutionPlan, ToolExecutionPlanError, ToolExecutionState,
    ToolPlanCall,
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
fn failed_dependencies_skip_dependent_calls() -> Result<(), ToolExecutionPlanError> {
    let mut dependent = tool_call("call-b", "{\"resource_id\":\"b\"}");
    dependent.depends_on = vec!["call-a".to_owned()];
    let mut transitive = tool_call("call-c", "{\"resource_id\":\"c\"}");
    transitive.depends_on = vec!["call-b".to_owned()];
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(dependent),
            ToolPlanCall::new(transitive),
        ],
        3,
    )?;

    assert_eq!(plan.ready_call_ids(), vec!["call-a".to_owned()]);
    plan.record_started("call-a")?;
    plan.record_failed("call-a")?;

    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Failed));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Skipped));
    assert_eq!(plan.state("call-c"), Some(ToolExecutionState::Skipped));
    assert_eq!(plan.ready_call_ids(), Vec::<String>::new());
    Ok(())
}

#[test]
fn fail_fast_policy_cancels_pending_calls_after_failure() -> Result<(), ToolExecutionPlanError> {
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(tool_call("call-b", "{\"resource_id\":\"b\"}")),
            ToolPlanCall::new(tool_call("call-c", "{\"resource_id\":\"c\"}")),
        ],
        3,
    )?
    .with_failure_policy(ToolExecutionFailurePolicy::FailFast);

    plan.record_started("call-a")?;
    plan.record_started("call-b")?;
    plan.record_failed("call-a")?;

    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Failed));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Running));
    assert_eq!(plan.state("call-c"), Some(ToolExecutionState::Cancelled));
    assert_eq!(plan.ready_call_ids(), Vec::<String>::new());
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

#[test]
fn policy_stop_denies_pending_tool_calls() -> Result<(), ToolExecutionPlanError> {
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(tool_call("call-b", "{\"resource_id\":\"b\"}")),
        ],
        2,
    )?;

    plan.record_started("call-a")?;
    assert_eq!(
        plan.apply_policy_stop(PendingToolCallsDisposition::Deny),
        vec!["call-b".to_owned()],
    );
    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Running));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Denied));
    assert_eq!(plan.ready_call_ids(), Vec::<String>::new());
    assert_eq!(
        plan.record_started("call-b"),
        Err(ToolExecutionPlanError::ToolCallNotPending {
            tool_call_id: "call-b".to_owned(),
            current: ToolExecutionState::Denied
        }),
    );
    Ok(())
}

#[test]
fn policy_stop_can_cancel_admitted_tool_calls() -> Result<(), ToolExecutionPlanError> {
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(tool_call("call-b", "{\"resource_id\":\"b\"}")),
        ],
        2,
    )?;

    plan.record_started("call-a")?;
    assert_eq!(
        plan.apply_policy_stop(PendingToolCallsDisposition::CancelAdmitted),
        vec!["call-a".to_owned(), "call-b".to_owned()],
    );
    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Cancelled));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Denied));
    assert_eq!(
        plan.record_completed("call-a"),
        Err(ToolExecutionPlanError::ToolCallNotRunning {
            tool_call_id: "call-a".to_owned(),
            current: ToolExecutionState::Cancelled
        }),
    );
    Ok(())
}
