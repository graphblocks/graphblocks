use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory, Outcome};
use graphblocks_runtime_core::readiness::{InputDependency, PortRef};
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
