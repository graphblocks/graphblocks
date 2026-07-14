use graphblocks_runtime_core::cancellation::{
    CancellationGuarantee, CancellationScope, CancellationToken,
};
use graphblocks_runtime_core::outcome::{
    BlockError, CancelCode, CancelReason, ErrorCategory, Outcome,
};
use graphblocks_runtime_core::readiness::{InputDependency, PortRef, ResolvedInput};
use graphblocks_runtime_core::retry::{EffectKind, RetryPolicy, RetryPolicyError};
use graphblocks_runtime_core::run_store::{InMemoryRunStore, RunStatus, RunStoreError};
use graphblocks_runtime_core::scheduler::{ScheduledNode, StartedNode};
use graphblocks_runtime_core::test_runtime::{
    InProcessTestRuntime, NodeExecutor, NodeRetryBoundary, TestRunStatus, TestRuntimeError,
};
use graphblocks_runtime_core::timeout::TimeoutPolicy;
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

#[test]
fn in_process_test_runtime_seeds_external_inputs() {
    let mut runtime = InProcessTestRuntime::new(
        "run-000001",
        [ScheduledNode::new(
            "render",
            [InputDependency::value(
                "message",
                PortRef::new("$input", "message"),
            )],
        )],
    )
    .expect("runtime should be created")
    .with_initial_value(PortRef::new("$input", "message"), json!("hello"));
    let mut executor = RecordingExecutor::default();

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Succeeded);
    assert_eq!(
        executor.starts[0].inputs.get("message"),
        Some(&ResolvedInput::Value(json!("hello"))),
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
fn in_process_test_runtime_rejects_excessive_retries_before_mutating_store() {
    let mut runtime = InProcessTestRuntime::new("placeholder", [ScheduledNode::new("model", [])])
        .expect("runtime should be created")
        .with_retry_policy(
            "model",
            RetryPolicy::new(101).retry_on([ErrorCategory::Timeout]),
        );
    let mut executor = FlakyExecutor { attempts: 0 };
    let mut store = InMemoryRunStore::new();
    let store_before_run = store.clone();

    let error = runtime
        .run_with_store(
            &mut store,
            "sha256:graph",
            json!({"message": "hello"}),
            &mut executor,
        )
        .expect_err("an excessive retry policy should fail before run creation");

    assert_eq!(
        error,
        TestRuntimeError::RetryPolicy(RetryPolicyError::MaxAttemptsExceeded { max_attempts: 101 }),
    );
    assert_eq!(store, store_before_run);
    assert_eq!(
        store.get_run("run-000001"),
        Err(RunStoreError::NotFound {
            run_id: "run-000001".to_owned(),
        }),
    );
    assert_eq!(executor.attempts, 0);
    assert!(runtime.journal().records().is_empty());
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

struct FlakyExecutor {
    attempts: usize,
}

impl NodeExecutor for FlakyExecutor {
    fn execute(
        &mut self,
        _node: StartedNode,
    ) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.attempts += 1;
        if self.attempts == 1 {
            return Err(BlockError::new(
                "model.timeout",
                ErrorCategory::Timeout,
                "temporary timeout",
                true,
            ));
        }
        Ok(vec![(
            PortRef::new("model", "response"),
            Outcome::Value(json!("ok")),
        )])
    }
}

#[test]
fn in_process_test_runtime_retries_retryable_node_failure() {
    let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("model", [])])
        .expect("runtime should be created")
        .with_retry_policy("model", RetryPolicy::default_model_read());
    let mut executor = FlakyExecutor { attempts: 0 };

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Succeeded);
    assert_eq!(executor.attempts, 2);
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
            "node_retry",
            "node_started",
            "node_completed",
            "run_succeeded",
        ],
    );
}

#[test]
fn in_process_test_runtime_rejects_excessive_retry_attempts_before_execution() {
    let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("model", [])])
        .expect("runtime should be created")
        .with_retry_policy(
            "model",
            RetryPolicy::new(101).retry_on([ErrorCategory::Timeout]),
        );
    let mut executor = FlakyExecutor { attempts: 0 };

    let error = runtime
        .run(&mut executor)
        .expect_err("an excessive retry policy should fail before execution");

    assert_eq!(
        error,
        TestRuntimeError::RetryPolicy(RetryPolicyError::MaxAttemptsExceeded { max_attempts: 101 }),
    );
    assert_eq!(executor.attempts, 0);
    assert!(runtime.journal().records().is_empty());
}

struct NonRetryableExecutor {
    attempts: usize,
}

impl NodeExecutor for NonRetryableExecutor {
    fn execute(
        &mut self,
        _node: StartedNode,
    ) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.attempts += 1;
        Err(BlockError::new(
            "model.validation",
            ErrorCategory::Validation,
            "invalid request",
            true,
        ))
    }
}

#[test]
fn in_process_test_runtime_stops_retry_on_policy_decision() {
    let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("model", [])])
        .expect("runtime should be created")
        .with_retry_policy("model", RetryPolicy::default_model_read());
    let mut executor = NonRetryableExecutor { attempts: 0 };

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Failed);
    assert_eq!(executor.attempts, 1);
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .map(|record| record.kind.as_str())
            .collect::<Vec<_>>(),
        vec!["run_started", "node_started", "node_failed", "run_failed"],
    );
}

struct RetryCancellingExecutor {
    token: CancellationToken,
    attempts: usize,
}

impl NodeExecutor for RetryCancellingExecutor {
    fn execute(
        &mut self,
        _node: StartedNode,
    ) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.attempts += 1;
        self.token.cancel(CancelReason::new(CancelCode::Shutdown));
        Err(BlockError::new(
            "model.timeout",
            ErrorCategory::Timeout,
            "timeout after cancellation",
            true,
        ))
    }
}

#[test]
fn in_process_test_runtime_cancels_before_retrying_failed_attempt() {
    let token = CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative);
    let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("model", [])])
        .expect("runtime should be created")
        .with_retry_policy("model", RetryPolicy::default_model_read());
    let mut executor = RetryCancellingExecutor {
        token: token.clone(),
        attempts: 0,
    };

    let result = runtime
        .run_with_cancellation(&token, &mut executor)
        .expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Cancelled);
    assert_eq!(executor.attempts, 1);
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .map(|record| record.kind.as_str())
            .collect::<Vec<_>>(),
        vec!["run_started", "node_started", "run_cancelled"],
    );
}

struct ExternalWriteFlakyExecutor {
    attempts: usize,
    failures_before_success: usize,
}

impl NodeExecutor for ExternalWriteFlakyExecutor {
    fn execute(
        &mut self,
        _node: StartedNode,
    ) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.attempts += 1;
        if self.attempts <= self.failures_before_success {
            return Err(BlockError::new(
                "tool.transient",
                ErrorCategory::Transient,
                "temporary tool failure",
                true,
            ));
        }
        Ok(vec![(
            PortRef::new("tool", "result"),
            Outcome::Value(json!("ok")),
        )])
    }
}

#[test]
fn in_process_test_runtime_allows_the_retry_attempt_limit() {
    let policy = RetryPolicy::try_new(100)
        .expect("the node retry attempt limit should be accepted")
        .retry_on([ErrorCategory::Transient]);
    let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("tool", [])])
        .expect("runtime should be created")
        .with_retry_policy("tool", policy);
    let mut executor = ExternalWriteFlakyExecutor {
        attempts: 0,
        failures_before_success: 99,
    };

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Succeeded);
    assert_eq!(executor.attempts, 100);
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .filter(|record| record.kind == "node_retry")
            .count(),
        99,
    );
}

#[test]
fn in_process_test_runtime_rejects_effect_retry_without_idempotency_key() {
    for effect in [EffectKind::ExternalWrite, EffectKind::FilesystemWrite] {
        let policy = RetryPolicy::new(3).retry_on([ErrorCategory::Transient]);
        let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("tool", [])])
            .expect("runtime should be created")
            .with_retry_boundary("tool", NodeRetryBoundary::new(policy).with_effect(effect));
        let mut executor = ExternalWriteFlakyExecutor {
            attempts: 0,
            failures_before_success: 1,
        };

        let result = runtime.run(&mut executor).expect("runtime should run");

        assert_eq!(result.status, TestRunStatus::Failed);
        assert_eq!(executor.attempts, 1);
        assert_eq!(
            result
                .journal
                .records()
                .last()
                .and_then(|record| record.payload.as_ref())
                .and_then(|payload| payload.get("retryStopReason"))
                .and_then(Value::as_str),
            Some("missing_idempotency_key"),
        );
    }
}

#[test]
fn in_process_test_runtime_retries_effect_with_idempotency_key() {
    for effect in [EffectKind::ExternalWrite, EffectKind::FilesystemWrite] {
        let policy = RetryPolicy::new(3).retry_on([ErrorCategory::Transient]);
        let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("tool", [])])
            .expect("runtime should be created")
            .with_retry_boundary(
                "tool",
                NodeRetryBoundary::new(policy)
                    .with_effect(effect)
                    .with_idempotency_key("tool-call-1"),
            );
        let mut executor = ExternalWriteFlakyExecutor {
            attempts: 0,
            failures_before_success: 1,
        };

        let result = runtime.run(&mut executor).expect("runtime should run");

        assert_eq!(result.status, TestRunStatus::Succeeded);
        assert_eq!(executor.attempts, 2);
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
                "node_retry",
                "node_started",
                "node_completed",
                "run_succeeded",
            ],
        );
    }
}

#[test]
fn in_process_test_runtime_records_same_idempotency_key_across_effect_retries() {
    let policy = RetryPolicy::new(4).retry_on([ErrorCategory::Transient]);
    let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("tool", [])])
        .expect("runtime should be created")
        .with_retry_boundary(
            "tool",
            NodeRetryBoundary::new(policy)
                .with_effect(EffectKind::ExternalWrite)
                .with_idempotency_key("tool-call-1"),
        );
    let mut executor = ExternalWriteFlakyExecutor {
        attempts: 0,
        failures_before_success: 2,
    };

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Succeeded);
    assert_eq!(executor.attempts, 3);
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .filter(|record| record.kind == "node_retry")
            .map(|record| {
                record
                    .payload
                    .as_ref()
                    .and_then(|payload| payload.get("idempotencyKey"))
                    .and_then(Value::as_str)
            })
            .collect::<Vec<_>>(),
        vec![Some("tool-call-1"), Some("tool-call-1")]
    );
}

struct TimeoutOutputExecutor {
    starts: Vec<String>,
}

impl NodeExecutor for TimeoutOutputExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.starts.push(node.node_id.clone());
        match node.node_id.as_str() {
            "model" => Ok(vec![(
                PortRef::new("model", "response"),
                Outcome::Value(json!("late output")),
            )]),
            "answer" => Ok(vec![(
                PortRef::new("answer", "value"),
                Outcome::Value(json!("done")),
            )]),
            node_id => Err(BlockError::new(
                format!("{node_id}.unexpected"),
                ErrorCategory::Internal,
                "unexpected node execution after timeout",
                false,
            )),
        }
    }
}

#[test]
fn in_process_test_runtime_fails_timed_out_node_without_publishing_outputs() {
    let mut runtime = InProcessTestRuntime::new(
        "run-000001",
        [
            ScheduledNode::new("model", []),
            ScheduledNode::new(
                "answer",
                [InputDependency::value(
                    "response",
                    PortRef::new("model", "response"),
                )],
            ),
        ],
    )
    .expect("runtime should be created")
    .with_timeout_policy("model", TimeoutPolicy::new(10).expect("valid timeout"))
    .with_node_duration_ms("model", 11);
    let mut executor = TimeoutOutputExecutor { starts: Vec::new() };

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Failed);
    assert_eq!(executor.starts, vec!["model".to_owned()]);
    assert_eq!(
        result
            .journal
            .records()
            .last()
            .and_then(|record| record.payload.as_ref())
            .and_then(|payload| payload.get("code"))
            .and_then(Value::as_str),
        Some("runtime.timeout"),
    );
    assert_eq!(
        result
            .journal
            .records()
            .iter()
            .map(|record| record.kind.as_str())
            .collect::<Vec<_>>(),
        vec!["run_started", "node_started", "node_failed", "run_failed"],
    );
}

#[test]
fn in_process_test_runtime_allows_node_before_timeout_deadline() {
    let mut runtime = InProcessTestRuntime::new("run-000001", [ScheduledNode::new("model", [])])
        .expect("runtime should be created")
        .with_timeout_policy("model", TimeoutPolicy::new(10).expect("valid timeout"))
        .with_node_duration_ms("model", 9);
    let mut executor = TimeoutOutputExecutor { starts: Vec::new() };

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Succeeded);
    assert_eq!(executor.starts, vec!["model".to_owned()]);
}

#[test]
fn in_process_test_runtime_retries_timeout_without_publishing_timed_out_outputs() {
    let mut runtime = InProcessTestRuntime::new(
        "run-000001",
        [
            ScheduledNode::new("model", []),
            ScheduledNode::new(
                "answer",
                [InputDependency::value(
                    "response",
                    PortRef::new("model", "response"),
                )],
            ),
        ],
    )
    .expect("runtime should be created")
    .with_retry_policy("model", RetryPolicy::default_model_read())
    .with_timeout_policy("model", TimeoutPolicy::new(10).expect("valid timeout"))
    .with_node_attempt_durations_ms("model", [11, 1]);
    let mut executor = TimeoutOutputExecutor { starts: Vec::new() };

    let result = runtime.run(&mut executor).expect("runtime should run");

    assert_eq!(result.status, TestRunStatus::Succeeded);
    assert_eq!(
        executor.starts,
        vec!["model".to_owned(), "model".to_owned(), "answer".to_owned()]
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
            "node_retry",
            "node_started",
            "node_completed",
            "node_started",
            "node_completed",
            "run_succeeded",
        ],
    );
}
