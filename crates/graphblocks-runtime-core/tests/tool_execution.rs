use graphblocks_runtime_core::output_policy::PendingToolCallsDisposition;
use graphblocks_runtime_core::tool::{ToolCancellation, ToolEffect};
use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft, ToolCallError};
use graphblocks_runtime_core::tool_execution::{
    ToolExecutionCancellationPolicy, ToolExecutionFailurePolicy, ToolExecutionPlan,
    ToolExecutionPlanError, ToolExecutionState, ToolPlanCall,
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
fn plan_rejects_empty_identity_fields() {
    assert_eq!(
        ToolExecutionPlan::new(
            " ",
            "response-1",
            [ToolPlanCall::new(tool_call(
                "call-a",
                "{\"resource_id\":\"a\"}"
            ))],
            1,
        ),
        Err(ToolExecutionPlanError::EmptyField { field: "plan_id" }),
    );
    assert_eq!(
        ToolExecutionPlan::new(
            "plan-1",
            "",
            [ToolPlanCall::new(tool_call(
                "call-a",
                "{\"resource_id\":\"a\"}"
            ))],
            1,
        ),
        Err(ToolExecutionPlanError::EmptyField {
            field: "response_id",
        }),
    );
}

#[test]
fn plan_rejects_tool_calls_from_different_response() {
    let mut mismatched = tool_call("call-b", "{\"resource_id\":\"b\"}");
    mismatched.response_id = "response-2".to_owned();

    assert_eq!(
        ToolExecutionPlan::new(
            "plan-1",
            "response-1",
            [
                ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
                ToolPlanCall::new(mismatched),
            ],
            2,
        ),
        Err(ToolExecutionPlanError::ResponseMismatch {
            tool_call_id: "call-b".to_owned(),
            expected_response_id: "response-1".to_owned(),
            actual_response_id: "response-2".to_owned(),
        }),
    );
}

#[test]
fn plan_rejects_invalid_tool_call_model() {
    let mut invalid = tool_call("call-a", "{\"resource_id\":\"a\"}");
    invalid.revision = 0;

    assert_eq!(
        ToolExecutionPlan::new("plan-1", "response-1", [ToolPlanCall::new(invalid)], 1,),
        Err(ToolExecutionPlanError::InvalidToolCall {
            source: ToolCallError::InvalidRevision { revision: 0 },
        }),
    );
}

#[test]
fn plan_rejects_unknown_dependency() {
    let mut dependent = tool_call("call-b", "{\"resource_id\":\"b\"}");
    dependent.depends_on = vec!["call-missing".to_owned()];

    assert_eq!(
        ToolExecutionPlan::new(
            "plan-1",
            "response-1",
            [
                ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
                ToolPlanCall::new(dependent),
            ],
            2,
        ),
        Err(ToolExecutionPlanError::UnknownDependency {
            tool_call_id: "call-b".to_owned(),
            dependency_id: "call-missing".to_owned(),
        }),
    );
}

#[test]
fn plan_rejects_dependency_cycle() {
    let mut first = tool_call("call-a", "{\"resource_id\":\"a\"}");
    first.depends_on = vec!["call-b".to_owned()];
    let mut second = tool_call("call-b", "{\"resource_id\":\"b\"}");
    second.depends_on = vec!["call-a".to_owned()];

    assert_eq!(
        ToolExecutionPlan::new(
            "plan-1",
            "response-1",
            [ToolPlanCall::new(first), ToolPlanCall::new(second)],
            2,
        ),
        Err(ToolExecutionPlanError::DependencyCycle {
            tool_call_id: "call-a".to_owned(),
        }),
    );
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
fn denied_pending_call_skips_dependents() -> Result<(), ToolExecutionPlanError> {
    let mut dependent = tool_call("call-b", "{\"resource_id\":\"b\"}");
    dependent.depends_on = vec!["call-a".to_owned()];
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(dependent),
            ToolPlanCall::new(tool_call("call-c", "{\"resource_id\":\"c\"}")),
        ],
        3,
    )?;

    plan.record_denied("call-a")?;

    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Denied));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Skipped));
    assert_eq!(plan.state("call-c"), Some(ToolExecutionState::Pending));
    assert_eq!(plan.ready_call_ids(), vec!["call-c".to_owned()]);
    Ok(())
}

#[test]
fn expired_pending_call_skips_dependents() -> Result<(), ToolExecutionPlanError> {
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

    plan.record_expired("call-a")?;

    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Expired));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Skipped));
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
fn parallel_state_changing_calls_require_effect_keys() {
    assert_eq!(
        ToolExecutionPlan::new(
            "plan-1",
            "response-1",
            [
                ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"ticket-1\"}"))
                    .with_effects([ToolEffect::ExternalWrite]),
                ToolPlanCall::new(tool_call("call-b", "{\"resource_id\":\"ticket-2\"}"))
                    .with_effects([ToolEffect::ExternalWrite]),
            ],
            2,
        ),
        Err(ToolExecutionPlanError::UnsafeParallelEffects {
            tool_call_id: "call-a".to_owned(),
        }),
    );
}

#[test]
fn plan_rejects_empty_effect_key() {
    assert_eq!(
        ToolExecutionPlan::new(
            "plan-1",
            "response-1",
            [
                ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"ticket-1\"}"))
                    .with_effect_key(" ")
            ],
            1,
        ),
        Err(ToolExecutionPlanError::EmptyEffectKey {
            tool_call_id: "call-a".to_owned(),
        }),
    );
}

#[test]
fn plan_rejects_none_effect_combined_with_side_effects() {
    assert_eq!(
        ToolExecutionPlan::new(
            "plan-1",
            "response-1",
            [
                ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"ticket-1\"}"))
                    .with_effects([ToolEffect::None, ToolEffect::ExternalWrite])
            ],
            1,
        ),
        Err(ToolExecutionPlanError::ConflictingToolEffects {
            tool_call_id: "call-a".to_owned(),
        }),
    );
}

#[test]
fn effect_key_template_derives_keys_from_tool_name_and_arguments()
-> Result<(), ToolExecutionPlanError> {
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"ticket-1\"}"))
                .with_effect_key_template("{tool.name}:{arguments.resource_id}")?,
            ToolPlanCall::new(tool_call("call-b", "{\"resource_id\":\"ticket-1\"}"))
                .with_effect_key_template("{tool.name}:{arguments.resource_id}")?,
            ToolPlanCall::new(tool_call("call-c", "{\"resource_id\":\"ticket-2\"}"))
                .with_effect_key_template("{tool.name}:{arguments.resource_id}")?,
        ],
        3,
    )?;

    assert_eq!(
        plan.ready_call_ids(),
        vec!["call-a".to_owned(), "call-c".to_owned()]
    );
    plan.record_started("call-a")?;
    assert_eq!(plan.ready_call_ids(), vec!["call-c".to_owned()]);
    plan.record_completed("call-a")?;
    assert_eq!(
        plan.ready_call_ids(),
        vec!["call-b".to_owned(), "call-c".to_owned()]
    );
    Ok(())
}

#[test]
fn effect_key_template_reports_missing_argument_path() {
    assert_eq!(
        ToolPlanCall::new(tool_call("call-a", "{}"))
            .with_effect_key_template("{tool.name}:{arguments.resource_id}"),
        Err(ToolExecutionPlanError::EffectKeyTemplateMissingValue {
            placeholder: "arguments.resource_id".to_owned(),
        }),
    );
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

#[test]
fn policy_stop_preserves_running_state_changing_calls_without_safe_cancellation()
-> Result<(), ToolExecutionPlanError> {
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"ticket-1\"}"))
                .with_effects([ToolEffect::ExternalWrite])
                .with_cancellation(ToolCancellation::Cooperative),
            ToolPlanCall::new(tool_call("call-b", "{\"resource_id\":\"ticket-2\"}")),
        ],
        2,
    )?;

    plan.record_started("call-a")?;

    assert_eq!(
        plan.apply_policy_stop(PendingToolCallsDisposition::CancelAdmitted),
        vec!["call-b".to_owned()],
    );
    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Running));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Denied));
    Ok(())
}

#[test]
fn policy_stop_can_cancel_running_state_changing_calls_when_force_terminable()
-> Result<(), ToolExecutionPlanError> {
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"ticket-1\"}"))
                .with_effects([ToolEffect::ExternalWrite])
                .with_cancellation(ToolCancellation::ForceTerminable),
        ],
        1,
    )?;

    plan.record_started("call-a")?;

    assert_eq!(
        plan.apply_policy_stop(PendingToolCallsDisposition::CancelAdmitted),
        vec!["call-a".to_owned()],
    );
    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Cancelled));
    Ok(())
}

#[test]
fn cancelled_call_cancels_dependents_by_default() -> Result<(), ToolExecutionPlanError> {
    let mut dependent = tool_call("call-b", "{\"resource_id\":\"b\"}");
    dependent.depends_on = vec!["call-a".to_owned()];
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(dependent),
            ToolPlanCall::new(tool_call("call-c", "{\"resource_id\":\"c\"}")),
        ],
        3,
    )?;

    plan.record_started("call-a")?;
    plan.record_cancelled("call-a")?;

    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Cancelled));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Cancelled));
    assert_eq!(plan.state("call-c"), Some(ToolExecutionState::Pending));
    assert_eq!(plan.ready_call_ids(), vec!["call-c".to_owned()]);
    Ok(())
}

#[test]
fn cancelled_call_can_skip_dependents_while_allowing_independent_calls()
-> Result<(), ToolExecutionPlanError> {
    let mut dependent = tool_call("call-b", "{\"resource_id\":\"b\"}");
    dependent.depends_on = vec!["call-a".to_owned()];
    let mut plan = ToolExecutionPlan::new(
        "plan-1",
        "response-1",
        [
            ToolPlanCall::new(tool_call("call-a", "{\"resource_id\":\"a\"}")),
            ToolPlanCall::new(dependent),
            ToolPlanCall::new(tool_call("call-c", "{\"resource_id\":\"c\"}")),
        ],
        3,
    )?
    .with_cancellation_policy(ToolExecutionCancellationPolicy::AllowIndependentCalls);

    plan.record_started("call-a")?;
    plan.record_cancelled("call-a")?;

    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Cancelled));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Skipped));
    assert_eq!(plan.state("call-c"), Some(ToolExecutionState::Pending));
    assert_eq!(plan.ready_call_ids(), vec!["call-c".to_owned()]);
    Ok(())
}

#[test]
fn cancel_all_policy_cancels_every_nonterminal_call() -> Result<(), ToolExecutionPlanError> {
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
    .with_cancellation_policy(ToolExecutionCancellationPolicy::CancelAll);

    plan.record_started("call-a")?;
    plan.record_started("call-c")?;
    plan.record_completed("call-c")?;
    plan.record_cancelled("call-a")?;

    assert_eq!(plan.state("call-a"), Some(ToolExecutionState::Cancelled));
    assert_eq!(plan.state("call-b"), Some(ToolExecutionState::Cancelled));
    assert_eq!(plan.state("call-c"), Some(ToolExecutionState::Completed));
    assert_eq!(plan.ready_call_ids(), Vec::<String>::new());
    Ok(())
}
