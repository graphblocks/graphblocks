#![allow(clippy::panic)]

use graphblocks_runtime_core::cancellation::{
    CancellationGuarantee, CancellationScope, CancellationToken,
};
use graphblocks_runtime_core::outcome::{
    BlockError, CancelCode, CancelReason, ErrorCategory, Outcome,
};
use graphblocks_runtime_core::readiness::PortRef;
use graphblocks_runtime_core::retry::{EffectKind, RetryPolicy};
use graphblocks_runtime_core::scheduler::{ScheduledNode, StartedNode};
use graphblocks_runtime_core::test_runtime::{
    InProcessTestRuntime, NodeExecutor, NodeRetryBoundary, TestRunStatus,
};
use graphblocks_runtime_core::timeout::TimeoutPolicy;
use graphblocks_schema::parse_duration_milliseconds;
use serde_json::{Value, json};

struct FixtureExecutor {
    attempts: usize,
    failures_before_success: usize,
    cancel_on_attempt: Option<usize>,
    token: Option<CancellationToken>,
    output_value: Value,
    attempt_output_values: Vec<Value>,
}

impl NodeExecutor for FixtureExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        self.attempts += 1;
        if self.cancel_on_attempt == Some(self.attempts)
            && let Some(token) = &self.token
        {
            token.cancel(CancelReason::new(CancelCode::PolicyDenied));
        }
        if self.attempts <= self.failures_before_success {
            return Err(BlockError::new(
                "tool.transient",
                ErrorCategory::Transient,
                "temporary tool failure",
                true,
            ));
        }
        let output_value = self
            .attempt_output_values
            .get(self.attempts.saturating_sub(1))
            .unwrap_or(&self.output_value)
            .clone();
        Ok(vec![(
            PortRef::new(node.node_id, "value"),
            Outcome::Value(output_value),
        )])
    }
}

#[test]
fn rust_retry_matches_shared_tck_cases() {
    let cases: Value = serde_json::from_str(include_str!("fixtures/retry-cases.json"))
        .expect("retry TCK fixture should parse");
    let cases = cases
        .as_array()
        .expect("retry TCK fixture should be a list");

    for case in cases {
        let case_name = case
            .get("name")
            .and_then(Value::as_str)
            .expect("retry TCK case should have a name");
        let node_id = case
            .get("nodeId")
            .and_then(Value::as_str)
            .unwrap_or("write");
        let max_attempts = case
            .get("maxAttempts")
            .and_then(Value::as_u64)
            .expect("retry TCK case should have maxAttempts") as u32;
        let failures_before_success =
            case.get("failuresBeforeSuccess")
                .and_then(Value::as_u64)
                .expect("retry TCK case should have failuresBeforeSuccess") as usize;
        let cancel_on_attempt = case
            .get("cancelOnAttempt")
            .and_then(Value::as_u64)
            .map(|attempt| attempt as usize);
        let cancel_before_start = case
            .get("cancelBeforeStart")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let cancel_after_terminal = case
            .get("cancelAfterTerminal")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let idempotency_key = case.get("idempotencyKey").and_then(Value::as_str);
        let effects = case
            .get("effects")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let output_value = case
            .get("outputValue")
            .cloned()
            .unwrap_or_else(|| json!("committed"));
        let timeout_ms = case.get("timeout").map(|value| {
            parse_duration_milliseconds(value).expect("timeout should be a valid duration")
        });
        let attempt_durations_ms = case
            .get("attemptDurationsMs")
            .and_then(Value::as_array)
            .map(|durations| {
                durations
                    .iter()
                    .map(|duration| {
                        duration
                            .as_u64()
                            .expect("attempt duration should be a non-negative integer")
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let attempt_output_values = case
            .get("attemptOutputValues")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();

        let policy = RetryPolicy::new(max_attempts).retry_on([
            ErrorCategory::Transient,
            ErrorCategory::Timeout,
            ErrorCategory::RateLimit,
        ]);
        let mut boundary = NodeRetryBoundary::new(policy);
        if let Some(effect) =
            effects
                .iter()
                .filter_map(Value::as_str)
                .find_map(|effect| match effect {
                    "external_write" => Some(EffectKind::ExternalWrite),
                    "filesystem_write" => Some(EffectKind::FilesystemWrite),
                    "destructive" => Some(EffectKind::Destructive),
                    "process" => Some(EffectKind::Process),
                    _ => None,
                })
        {
            boundary = boundary.with_effect(effect);
        }
        if let Some(idempotency_key) = idempotency_key {
            boundary = boundary.with_idempotency_key(idempotency_key);
        }

        let mut runtime =
            InProcessTestRuntime::new("run-000001", [ScheduledNode::new(node_id, [])])
                .expect("runtime should be created")
                .with_retry_boundary(node_id, boundary);
        if let Some(timeout_ms) = timeout_ms {
            runtime = runtime.with_timeout_policy(
                node_id,
                TimeoutPolicy::new(timeout_ms).expect("timeout should be positive"),
            );
        }
        if !attempt_durations_ms.is_empty() {
            runtime = runtime.with_node_attempt_durations_ms(node_id, attempt_durations_ms);
        }
        let token = (cancel_on_attempt.is_some() || cancel_before_start || cancel_after_terminal)
            .then(|| {
                CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative)
            });
        if cancel_before_start && let Some(token) = &token {
            token.cancel(CancelReason::new(CancelCode::UserCancel));
        }
        let mut executor = FixtureExecutor {
            attempts: 0,
            failures_before_success,
            cancel_on_attempt,
            token: token.clone(),
            output_value,
            attempt_output_values,
        };
        let result = if let Some(token) = &token {
            runtime
                .run_with_cancellation(token, &mut executor)
                .expect("runtime should run")
        } else {
            runtime.run(&mut executor).expect("runtime should run")
        };
        let post_terminal_cancellation = if cancel_after_terminal {
            let snapshot = result.clone();
            if let Some(token) = &token {
                token.cancel(CancelReason::new(CancelCode::UserCancel));
            }
            if result == snapshot {
                "unchanged"
            } else {
                "changed"
            }
        } else {
            "not_requested"
        };

        let observed_status = match result.status {
            TestRunStatus::Succeeded => "succeeded",
            TestRunStatus::Failed => "failed",
            TestRunStatus::Cancelled => "cancelled",
        };
        let terminal_kind = result
            .journal
            .records()
            .last()
            .map(|record| record.kind.as_str())
            .unwrap_or("");
        let retry_idempotency_keys = result
            .journal
            .records()
            .iter()
            .filter(|record| record.kind == "node_retry")
            .map(|record| {
                record
                    .payload
                    .as_ref()
                    .and_then(|payload| payload.get("idempotencyKey"))
                    .cloned()
                    .unwrap_or(Value::Null)
            })
            .collect::<Vec<_>>();
        let started_idempotency_keys = result
            .journal
            .records()
            .iter()
            .filter(|record| record.kind == "node_started")
            .map(|record| {
                record
                    .payload
                    .as_ref()
                    .and_then(|payload| payload.get("idempotencyKey"))
                    .cloned()
                    .unwrap_or(Value::Null)
            })
            .collect::<Vec<_>>();
        let attempt_ids = result
            .journal
            .records()
            .iter()
            .filter(|record| record.kind == "node_started")
            .map(|record| {
                record
                    .attempt_id
                    .as_ref()
                    .map_or(Value::Null, |attempt_id| Value::from(attempt_id.clone()))
            })
            .collect::<Vec<_>>();
        let commit_attempt_ids = result
            .journal
            .records()
            .iter()
            .filter(|record| record.kind == "node_completed")
            .map(|record| {
                record
                    .attempt_id
                    .as_ref()
                    .map_or(Value::Null, |attempt_id| Value::from(attempt_id.clone()))
            })
            .collect::<Vec<_>>();
        let journal_kinds = result
            .journal
            .records()
            .iter()
            .map(|record| {
                Value::from(if record.kind == "node_completed" {
                    "node_succeeded"
                } else {
                    record.kind.as_str()
                })
            })
            .collect::<Vec<_>>();
        let node_commit_count = result
            .journal
            .records()
            .iter()
            .filter(|record| record.kind == "node_completed")
            .count();
        let committed_output_value = if node_commit_count == 1 {
            executor
                .attempt_output_values
                .get(executor.attempts.saturating_sub(1))
                .unwrap_or(&executor.output_value)
                .clone()
        } else {
            Value::Null
        };
        let terminal_count = result
            .journal
            .records()
            .iter()
            .filter(|record| {
                matches!(
                    record.kind.as_str(),
                    "run_succeeded" | "run_failed" | "run_cancelled"
                )
            })
            .count();
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .expect("retry TCK case should have expected object");

        for (key, expected_value) in expected {
            let observed = match key.as_str() {
                "status" => json!(observed_status),
                "terminalKind" => json!(terminal_kind),
                "attempts" => json!(executor.attempts),
                "retryCount" => json!(retry_idempotency_keys.len()),
                "retryIdempotencyKeys" => Value::Array(retry_idempotency_keys.clone()),
                "startedIdempotencyKeys" => Value::Array(started_idempotency_keys.clone()),
                "attemptIds" => Value::Array(attempt_ids.clone()),
                "commitAttemptIds" => Value::Array(commit_attempt_ids.clone()),
                "committedOutputValue" => committed_output_value.clone(),
                "journalKinds" => Value::Array(journal_kinds.clone()),
                "nodeCommitCount" => json!(node_commit_count),
                "terminalCount" => json!(terminal_count),
                "postTerminalCancellation" => json!(post_terminal_cancellation),
                unsupported => panic!("{case_name}: unsupported retry expectation {unsupported}"),
            };
            assert_eq!(
                observed, *expected_value,
                "{case_name}: expected {key} to match"
            );
        }
    }
}
