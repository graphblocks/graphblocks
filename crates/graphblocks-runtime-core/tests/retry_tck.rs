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
use serde_json::{Value, json};

struct FixtureExecutor {
    attempts: usize,
    failures_before_success: usize,
    cancel_on_attempt: Option<usize>,
    token: Option<CancellationToken>,
    output_value: Value,
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
        Ok(vec![(
            PortRef::new(node.node_id, "value"),
            Outcome::Value(self.output_value.clone()),
        )])
    }
}

#[test]
fn rust_retry_matches_shared_tck_cases() {
    let cases: Value = serde_json::from_str(include_str!("../../../tck/retry/cases.json"))
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

        let policy = RetryPolicy::new(max_attempts).retry_on([ErrorCategory::Transient]);
        let mut boundary = NodeRetryBoundary::new(policy);
        if effects
            .iter()
            .any(|effect| effect.as_str() == Some("external_write"))
        {
            boundary = boundary.with_effect(EffectKind::ExternalWrite);
        }
        if let Some(idempotency_key) = idempotency_key {
            boundary = boundary.with_idempotency_key(idempotency_key);
        }

        let mut runtime =
            InProcessTestRuntime::new("run-000001", [ScheduledNode::new(node_id, [])])
                .expect("runtime should be created")
                .with_retry_boundary(node_id, boundary);
        let token = cancel_on_attempt.map(|_| {
            CancellationToken::new(CancellationScope::Run, CancellationGuarantee::Cooperative)
        });
        let mut executor = FixtureExecutor {
            attempts: 0,
            failures_before_success,
            cancel_on_attempt,
            token: token.clone(),
            output_value,
        };
        let result = if let Some(token) = &token {
            runtime
                .run_with_cancellation(token, &mut executor)
                .expect("runtime should run")
        } else {
            runtime.run(&mut executor).expect("runtime should run")
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
            .filter_map(|record| {
                record
                    .payload
                    .as_ref()
                    .and_then(|payload| payload.get("idempotencyKey"))
                    .cloned()
            })
            .collect::<Vec<_>>();
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
                unsupported => panic!("{case_name}: unsupported retry expectation {unsupported}"),
            };
            assert_eq!(
                observed, *expected_value,
                "{case_name}: expected {key} to match"
            );
        }
    }
}
