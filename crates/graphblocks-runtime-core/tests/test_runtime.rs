use graphblocks_runtime_core::cancellation::{
    CancellationGuarantee, CancellationScope, CancellationToken,
};
use graphblocks_runtime_core::outcome::{
    BlockError, CancelCode, CancelReason, ErrorCategory, Outcome,
};
use graphblocks_runtime_core::readiness::{InputDependency, PortRef, ResolvedInput};
use graphblocks_runtime_core::run_store::{InMemoryRunStore, RunStatus};
use graphblocks_runtime_core::scheduler::{ScheduledNode, StartedNode};
use graphblocks_runtime_core::test_runtime::{InProcessTestRuntime, NodeExecutor, TestRunStatus};
use serde_json::{Value, json};

#[derive(Default)]
struct RecordingExecutor {
    starts: Vec<StartedNode>,
}

impl NodeExecutor for RecordingExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        let outputs = match node.node_id.as_str() {
            "render" => vec![(
                PortRef::new("render", "prompt"),
                Outcome::Value(json!("rendered")),
            )],
            "model" => vec![(
                PortRef::new("model", "response"),
                Outcome::Value(json!("generated")),
            )],
            "answer" => vec![(
                PortRef::new("answer", "value"),
                Outcome::Value(json!("done")),
            )],
            node_id => {
                return Err(BlockError::new(
                    format!("{node_id}.unknown"),
                    ErrorCategory::Configuration,
                    "unknown node",
                    false,
                ));
            }
        };
        self.starts.push(node);
        Ok(outputs)
    }
}

#[test]
fn in_process_test_runtime_executes_nodes_in_dependency_order() {
    let mut runtime = InProcessTestRuntime::new(
        "run-000001",
        [
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
            ScheduledNode::new("render", []),
        ],
    )
    .expect("runtime should be created");
    let mut executor = RecordingExecutor::default();

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Succeeded);
    assert_eq!(
        executor
            .starts
            .iter()
            .map(|node| node.node_id.as_str())
            .collect::<Vec<_>>(),
        vec!["render", "model", "answer"],
    );
    assert_eq!(
        executor.starts[1].inputs.get("prompt"),
        Some(&ResolvedInput::Value(json!("rendered"))),
    );
    assert_eq!(
        executor.starts[2].inputs.get("response"),
        Some(&ResolvedInput::Value(json!("generated"))),
    );
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .map(|record| record.kind.as_str())
            .collect::<Vec<_>>(),
        vec![
            "run_started",
            "node_started",
            "node_completed",
            "node_started",
            "node_completed",
            "node_started",
            "node_completed",
            "run_succeeded",
        ],
    );
}

struct FailingExecutor {
    starts: Vec<String>,
}

impl NodeExecutor for FailingExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.starts.push(node.node_id.clone());
        match node.node_id.as_str() {
            "render" => Ok(vec![(
                PortRef::new("render", "prompt"),
                Outcome::Value(json!("rendered")),
            )]),
            "model" => Err(BlockError::new(
                "model.failed",
                ErrorCategory::Permanent,
                "model failed",
                false,
            )),
            node_id => Err(BlockError::new(
                format!("{node_id}.unexpected"),
                ErrorCategory::Internal,
                "unexpected node",
                false,
            )),
        }
    }
}

#[test]
fn in_process_test_runtime_records_single_terminal_failure() {
    let mut runtime = InProcessTestRuntime::new(
        "run-000001",
        [
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
        ],
    )
    .expect("runtime should be created");
    let mut executor = FailingExecutor { starts: Vec::new() };

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Failed);
    assert_eq!(
        executor.starts,
        vec!["render".to_owned(), "model".to_owned()]
    );
    assert_eq!(result.journal.terminal_kind(), Some("run_failed"));
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .filter(|record| record.terminal)
            .count(),
        1,
    );
    assert_eq!(
        result
            .journal
            .records()
            .last()
            .map(|record| record.kind.as_str()),
        Some("run_failed"),
    );
}

#[test]
fn in_process_test_runtime_admits_and_finalizes_run_store_record() {
    let mut runtime = InProcessTestRuntime::new(
        "placeholder",
        [
            ScheduledNode::new("render", []),
            ScheduledNode::new(
                "model",
                [InputDependency::value(
                    "prompt",
                    PortRef::new("render", "prompt"),
                )],
            ),
        ],
    )
    .expect("runtime should be created");
    let mut executor = RecordingExecutor::default();
    let mut store = InMemoryRunStore::new();

    let result = runtime
        .run_with_store(
            &mut store,
            "sha256:graph",
            json!({"message": "hello"}),
            &mut executor,
        )
        .expect("runtime should run");

    assert_eq!(result.run_id, "run-000001");
    assert_eq!(result.status, TestRunStatus::Succeeded);
    assert_eq!(result.journal.run_id(), "run-000001");
    let stored = store.get_run("run-000001").expect("run should be recorded");
    assert_eq!(stored.graph_hash, "sha256:graph");
    assert_eq!(stored.inputs, json!({"message": "hello"}));
    assert_eq!(stored.status, RunStatus::Completed);
}

#[test]
fn in_process_test_runtime_honors_precancelled_token() {
    let token = CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative);
    token.cancel(CancelReason::new(CancelCode::UserCancel));
    let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("render", [])])
        .expect("runtime should be created");
    let mut executor = RecordingExecutor::default();

    let result = runtime
        .run_with_cancellation(&token, &mut executor)
        .expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Cancelled);
    assert!(executor.starts.is_empty());
    assert_eq!(result.journal.terminal_kind(), Some("run_cancelled"));
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .map(|record| record.kind.as_str())
            .collect::<Vec<_>>(),
        vec!["run_started", "run_cancelled"],
    );
}

struct CancellingExecutor {
    token: CancellationToken,
    starts: Vec<String>,
}

impl NodeExecutor for CancellingExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.starts.push(node.node_id.clone());
        if node.node_id == "render" {
            self.token.cancel(CancelReason::new(CancelCode::Shutdown));
            return Ok(vec![(
                PortRef::new("render", "prompt"),
                Outcome::Value(json!("rendered")),
            )]);
        }
        Err(BlockError::new(
            "unexpected.node",
            ErrorCategory::Internal,
            "unexpected node execution after cancellation",
            false,
        ))
    }
}

#[test]
fn in_process_test_runtime_stops_before_dependent_work_after_cancellation() {
    let token = CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative);
    let mut runtime = InProcessTestRuntime::new(
        "run-000001",
        [
            ScheduledNode::new("render", []),
            ScheduledNode::new(
                "model",
                [InputDependency::value(
                    "prompt",
                    PortRef::new("render", "prompt"),
                )],
            ),
        ],
    )
    .expect("runtime should be created");
    let mut executor = CancellingExecutor {
        token: token.clone(),
        starts: Vec::new(),
    };

    let result = runtime
        .run_with_cancellation(&token, &mut executor)
        .expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Cancelled);
    assert_eq!(executor.starts, vec!["render".to_owned()]);
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .map(|record| record.kind.as_str())
            .collect::<Vec<_>>(),
        vec![
            "run_started",
            "node_started",
            "node_completed",
            "run_cancelled",
        ],
    );
}

#[test]
fn in_process_test_runtime_finalizes_store_record_on_cancellation() {
    let token = CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative);
    token.cancel(CancelReason::new(CancelCode::UserCancel));
    let mut runtime = InProcessTestRuntime::new("placeholder", [ScheduledNode::new("render", [])])
        .expect("runtime should be created");
    let mut executor = RecordingExecutor::default();
    let mut store = InMemoryRunStore::new();

    let result = runtime
        .run_with_store_and_cancellation(
            &mut store,
            "sha256:graph",
            json!({"message": "stop"}),
            &token,
            &mut executor,
        )
        .expect("runtime should run");

    assert_eq!(result.run_id, "run-000001");
    assert_eq!(result.status, TestRunStatus::Cancelled);
    assert!(executor.starts.is_empty());
    assert_eq!(
        store
            .get_run("run-000001")
            .expect("run should be recorded")
            .status,
        RunStatus::Cancelled,
    );
}
