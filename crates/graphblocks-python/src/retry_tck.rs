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
        self.attempts = self.attempts.checked_add(1).ok_or_else(|| {
            BlockError::new(
                "retry.attempt_overflow",
                ErrorCategory::Permanent,
                "attempt overflow",
                false,
            )
        })?;
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

pub(crate) fn evaluate_case(case: &Value) -> Result<Value, String> {
    let case_object = case
        .as_object()
        .ok_or_else(|| "retry TCK case must be an object".to_owned())?;
    let case_name = case_object
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| "retry TCK case requires name".to_owned())?;
    let kind = case_object
        .get("kind")
        .and_then(Value::as_str)
        .ok_or_else(|| format!("retry TCK case {case_name} requires kind"))?;
    if !matches!(
        kind,
        "node_retry"
            | "cancelled_before_retry"
            | "cancelled_before_commit"
            | "cancelled_before_start"
            | "cancelled_after_terminal"
    ) {
        return Err(format!(
            "retry TCK case {case_name} has unknown kind {kind}"
        ));
    }
    let node_id = case_object
        .get("nodeId")
        .and_then(Value::as_str)
        .unwrap_or("write");
    let max_attempts = case_object
        .get("maxAttempts")
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("retry TCK case {case_name} requires maxAttempts"))?;
    let failures_before_success = case_object
        .get("failuresBeforeSuccess")
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("retry TCK case {case_name} requires failuresBeforeSuccess"))?
        .try_into()
        .map_err(|_| format!("retry TCK case {case_name} failuresBeforeSuccess is too large"))?;
    let cancel_on_attempt = case_object
        .get("cancelOnAttempt")
        .and_then(Value::as_u64)
        .map(usize::try_from)
        .transpose()
        .map_err(|_| format!("retry TCK case {case_name} cancelOnAttempt is too large"))?;
    let cancel_before_start = match case_object.get("cancelBeforeStart") {
        Some(value) => value.as_bool().ok_or_else(|| {
            format!("retry TCK case {case_name} cancelBeforeStart must be a boolean")
        })?,
        None => false,
    };
    let cancel_after_terminal = match case_object.get("cancelAfterTerminal") {
        Some(value) => value.as_bool().ok_or_else(|| {
            format!("retry TCK case {case_name} cancelAfterTerminal must be a boolean")
        })?,
        None => false,
    };
    let idempotency_key = match case_object.get("idempotencyKey") {
        Some(value) => Some(value.as_str().ok_or_else(|| {
            format!("retry TCK case {case_name} idempotencyKey must be a string")
        })?),
        None => None,
    };
    let effects = case_object
        .get("effects")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let effect = effects
        .iter()
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| format!("retry TCK case {case_name} effects must contain strings"))
        })
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .find_map(|value| match value {
            "external_write" => Some(EffectKind::ExternalWrite),
            "filesystem_write" => Some(EffectKind::FilesystemWrite),
            "destructive" => Some(EffectKind::Destructive),
            "process" => Some(EffectKind::Process),
            _ => None,
        });
    let output_value = case_object
        .get("outputValue")
        .cloned()
        .unwrap_or_else(|| json!("committed"));

    let policy = RetryPolicy::try_new(max_attempts)
        .map_err(|error| format!("retry TCK case {case_name}: {error}"))?
        .retry_on([ErrorCategory::Transient]);
    let mut boundary = NodeRetryBoundary::new(policy);
    if let Some(effect) = effect {
        boundary = boundary.with_effect(effect);
    }
    if let Some(idempotency_key) = idempotency_key {
        boundary = boundary.with_idempotency_key(idempotency_key);
    }

    let mut runtime = InProcessTestRuntime::new(
        format!("retry-tck-{case_name}"),
        [ScheduledNode::new(node_id, [])],
    )
    .map_err(|error| format!("retry TCK case {case_name}: {error:?}"))?
    .with_retry_boundary(node_id, boundary);
    let token =
        (cancel_on_attempt.is_some() || cancel_before_start || cancel_after_terminal).then(|| {
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
    };
    let result = if let Some(token) = &token {
        runtime.run_with_cancellation(token, &mut executor)
    } else {
        runtime.run(&mut executor)
    }
    .map_err(|error| format!("retry TCK case {case_name}: {error:?}"))?;
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

    let status = match result.status {
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
    let context_idempotency_keys = result
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
    let node_commit_count = result
        .journal
        .records()
        .iter()
        .filter(|record| record.kind == "node_completed")
        .count();
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
    Ok(json!({
        "status": status,
        "terminalKind": terminal_kind,
        "attempts": executor.attempts,
        "retryCount": retry_idempotency_keys.len(),
        "retryIdempotencyKeys": retry_idempotency_keys,
        "contextIdempotencyKeys": context_idempotency_keys,
        "nodeCommitCount": node_commit_count,
        "terminalCount": terminal_count,
        "postTerminalCancellation": post_terminal_cancellation,
    }))
}
