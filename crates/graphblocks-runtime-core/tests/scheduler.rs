use std::collections::BTreeMap;

use graphblocks_runtime_core::outcome::{
    BlockError, CancelCode, CancelReason, ErrorCategory, Outcome,
};
use graphblocks_runtime_core::readiness::{InputDependency, PortRef, ResolvedInput};
use graphblocks_runtime_core::scheduler::{
    LocalScheduler, NodeExecutionState, ScheduledNode, SchedulerError,
};
use serde_json::{Value, json};

#[test]
fn scheduler_rejects_node_start_before_run_admission() -> Result<(), SchedulerError> {
    let mut scheduler = LocalScheduler::new([ScheduledNode::new("render", [])])?;

    assert_eq!(
        scheduler.start_node("render"),
        Err(SchedulerError::RunNotAdmitted),
    );
    assert_eq!(
        scheduler.node_state("render"),
        Some(NodeExecutionState::Pending)
    );
    Ok(())
}

#[test]
fn scheduler_admits_roots_and_releases_dependents_deterministically() -> Result<(), SchedulerError>
{
    let mut scheduler = LocalScheduler::new([
        ScheduledNode::new("render", []),
        ScheduledNode::new(
            "model",
            [InputDependency::value(
                "prompt",
                PortRef::new("render", "prompt"),
            )],
        ),
        ScheduledNode::new(
            "answer",
            [InputDependency::value(
                "response",
                PortRef::new("model", "response"),
            )],
        ),
    ])?;

    assert_eq!(scheduler.admit_run()?, vec!["render".to_owned()]);
    scheduler.start_node("render")?;
    assert_eq!(
        scheduler.complete_node(
            "render",
            [(
                PortRef::new("render", "prompt"),
                Outcome::Value(json!("hi"))
            )],
        )?,
        vec!["model".to_owned()],
    );
    assert_eq!(scheduler.ready_nodes(), vec!["model".to_owned()]);
    scheduler.start_node("model")?;
    assert_eq!(
        scheduler.complete_node(
            "model",
            [(
                PortRef::new("model", "response"),
                Outcome::Value(json!("ok"))
            )],
        )?,
        vec!["answer".to_owned()],
    );
    Ok(())
}

#[test]
fn scheduler_blocks_required_value_input_on_terminal_outcome() -> Result<(), SchedulerError> {
    let mut scheduler = LocalScheduler::new([
        ScheduledNode::new("branch", []),
        ScheduledNode::new(
            "answer",
            [InputDependency::value(
                "value",
                PortRef::new("branch", "value"),
            )],
        ),
    ])?;
    let error = BlockError::new(
        "branch.failed",
        ErrorCategory::Permanent,
        "branch failed",
        false,
    );

    scheduler.admit_run()?;
    scheduler.start_node("branch")?;
    assert_eq!(
        scheduler.complete_node(
            "branch",
            [(
                PortRef::new("branch", "value"),
                Outcome::<Value>::Failed(error.clone())
            )],
        )?,
        Vec::<String>::new(),
    );

    assert_eq!(
        scheduler.node_state("answer"),
        Some(NodeExecutionState::Blocked),
    );
    assert_eq!(scheduler.ready_nodes(), Vec::<String>::new());
    Ok(())
}

#[test]
fn scheduler_rejects_duplicate_and_unknown_nodes() -> Result<(), SchedulerError> {
    assert!(matches!(
        LocalScheduler::new([
            ScheduledNode::new("render", []),
            ScheduledNode::new("render", []),
        ]),
        Err(SchedulerError::DuplicateNode { node_id }) if node_id == "render"
    ));

    let mut scheduler = LocalScheduler::new([ScheduledNode::new("render", [])])?;
    assert_eq!(
        scheduler.start_node("missing"),
        Err(SchedulerError::UnknownNode {
            node_id: "missing".to_owned(),
        }),
    );
    Ok(())
}

#[test]
fn scheduler_rejects_outputs_claiming_another_node_without_partial_publication()
-> Result<(), SchedulerError> {
    let mut scheduler = LocalScheduler::new([
        ScheduledNode::new("source", []),
        ScheduledNode::new(
            "consumer",
            [InputDependency::value(
                "value",
                PortRef::new("victim", "result"),
            )],
        ),
        ScheduledNode::new("victim", []),
    ])?;
    scheduler.admit_run()?;
    scheduler.start_node("source")?;

    assert_eq!(
        scheduler.complete_node(
            "source",
            [
                (
                    PortRef::new("source", "result"),
                    Outcome::Value(json!("legitimate")),
                ),
                (
                    PortRef::new("victim", "result"),
                    Outcome::Value(json!("forged")),
                ),
            ],
        ),
        Err(SchedulerError::OutputOwnerMismatch {
            node_id: "source".to_owned(),
            output_node_id: "victim".to_owned(),
        })
    );
    assert_eq!(
        scheduler.node_state("source"),
        Some(NodeExecutionState::Running)
    );
    assert_eq!(
        scheduler.node_state("consumer"),
        Some(NodeExecutionState::Pending)
    );

    assert_eq!(
        scheduler.cancel_node(
            "source",
            [PortRef::new("victim", "result")],
            CancelReason::new(CancelCode::UserCancel),
        ),
        Err(SchedulerError::OutputOwnerMismatch {
            node_id: "source".to_owned(),
            output_node_id: "victim".to_owned(),
        })
    );
    assert_eq!(
        scheduler.node_state("source"),
        Some(NodeExecutionState::Running)
    );
    assert_eq!(
        scheduler.node_state("consumer"),
        Some(NodeExecutionState::Pending)
    );
    Ok(())
}

#[test]
fn scheduler_uses_seeded_external_input_signals() -> Result<(), SchedulerError> {
    let mut scheduler = LocalScheduler::new([ScheduledNode::new(
        "render",
        [InputDependency::value(
            "message",
            PortRef::new("$input", "message"),
        )],
    )])?;

    assert_eq!(
        scheduler.publish_signal(
            PortRef::new("$input", "message"),
            Outcome::Value(json!("hello"))
        ),
        Vec::<String>::new(),
    );
    assert_eq!(scheduler.admit_run()?, vec!["render".to_owned()]);

    let render = scheduler.start_node("render")?;
    let mut expected_inputs = BTreeMap::new();
    expected_inputs.insert("message".to_owned(), ResolvedInput::Value(json!("hello")));
    assert_eq!(render.inputs, expected_inputs);

    Ok(())
}

#[test]
fn scheduler_cancels_node_as_idempotent_terminal_state() -> Result<(), SchedulerError> {
    let reason = CancelReason::new(CancelCode::UserCancel);
    let mut scheduler = LocalScheduler::new([
        ScheduledNode::new("render", []),
        ScheduledNode::new(
            "late_observer",
            [InputDependency::outcome(
                "outcome",
                PortRef::new("render", "late"),
            )],
        ),
    ])?;

    scheduler.admit_run()?;
    scheduler.start_node("render")?;
    assert_eq!(
        scheduler.cancel_node("render", [PortRef::new("render", "result")], reason.clone(),)?,
        Vec::<String>::new(),
    );

    assert_eq!(
        scheduler.node_state("render"),
        Some(NodeExecutionState::Cancelled),
    );
    assert_eq!(
        scheduler.cancel_node("render", [PortRef::new("render", "late")], reason)?,
        Vec::<String>::new(),
    );
    assert_eq!(
        scheduler.node_state("late_observer"),
        Some(NodeExecutionState::Pending),
    );
    assert!(matches!(
        scheduler.complete_node(
            "render",
            [(
                PortRef::new("render", "late"),
                Outcome::Value(json!("must not publish"))
            )],
        ),
        Err(SchedulerError::NodeNotRunning {
            state: NodeExecutionState::Cancelled,
            ..
        })
    ));
    Ok(())
}

#[test]
fn scheduler_releases_or_blocks_dependents_after_cancellation() -> Result<(), SchedulerError> {
    let reason = CancelReason::new(CancelCode::Timeout);
    let mut scheduler = LocalScheduler::new([
        ScheduledNode::new("worker", []),
        ScheduledNode::new(
            "answer",
            [InputDependency::value(
                "result",
                PortRef::new("worker", "result"),
            )],
        ),
        ScheduledNode::new(
            "audit",
            [InputDependency::outcome(
                "result",
                PortRef::new("worker", "result"),
            )],
        ),
    ])?;

    scheduler.admit_run()?;
    scheduler.start_node("worker")?;

    assert_eq!(
        scheduler.cancel_node("worker", [PortRef::new("worker", "result")], reason.clone(),)?,
        vec!["audit".to_owned()],
    );
    assert_eq!(
        scheduler.node_state("answer"),
        Some(NodeExecutionState::Blocked),
    );
    assert_eq!(
        scheduler.node_state("audit"),
        Some(NodeExecutionState::Ready),
    );

    Ok(())
}

#[test]
fn scheduler_hands_resolved_inputs_to_started_node() -> Result<(), SchedulerError> {
    let mut scheduler = LocalScheduler::new([
        ScheduledNode::new("source", []),
        ScheduledNode::new(
            "audit",
            [InputDependency::outcome(
                "published",
                PortRef::new("source", "result"),
            )],
        ),
        ScheduledNode::new(
            "value_consumer",
            [InputDependency::value(
                "value",
                PortRef::new("source", "result"),
            )],
        ),
    ])?;

    assert_eq!(scheduler.admit_run()?, vec!["source".to_owned()]);
    assert_eq!(scheduler.start_node("source")?.inputs, BTreeMap::new());
    assert_eq!(
        scheduler.complete_node(
            "source",
            [(
                PortRef::new("source", "result"),
                Outcome::Value(json!("payload"))
            )],
        )?,
        vec!["audit".to_owned(), "value_consumer".to_owned()],
    );

    let audit = scheduler.start_node("audit")?;
    let mut expected_audit_inputs = BTreeMap::new();
    expected_audit_inputs.insert(
        "published".to_owned(),
        ResolvedInput::Outcome(Outcome::Value(json!("payload"))),
    );
    assert_eq!(audit.node_id, "audit");
    assert_eq!(audit.inputs, expected_audit_inputs);

    let value_consumer = scheduler.start_node("value_consumer")?;
    let mut expected_value_inputs = BTreeMap::new();
    expected_value_inputs.insert("value".to_owned(), ResolvedInput::Value(json!("payload")));
    assert_eq!(value_consumer.node_id, "value_consumer");
    assert_eq!(value_consumer.inputs, expected_value_inputs);

    Ok(())
}
