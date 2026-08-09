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
    if matches!(kind, "timeout_retry" | "timeout_exhaustion") {
        let allowed_fields = [
            "contractVersion",
            "name",
            "kind",
            "block",
            "nodeId",
            "maxAttempts",
            "failuresBeforeSuccess",
            "timeout",
            "attemptDurationsMs",
            "attemptOutputValues",
            "idempotencyKey",
        ];
        if let Some(field) = case_object
            .keys()
            .find(|field| !allowed_fields.contains(&field.as_str()))
        {
            return Err(format!(
                "retry TCK case {case_name} has unknown field {field}"
            ));
        }
        if case_object.get("contractVersion").and_then(Value::as_str)
            != Some("graphblocks.retry-flow.tck.v1")
        {
            return Err(format!(
                "retry TCK case {case_name} requires contractVersion graphblocks.retry-flow.tck.v1"
            ));
        }
        for (field, max_bytes) in [
            ("name", 256_usize),
            ("block", 256_usize),
            ("nodeId", 256_usize),
            ("idempotencyKey", 1_024_usize),
        ] {
            let value = case_object
                .get(field)
                .and_then(Value::as_str)
                .ok_or_else(|| format!("retry TCK case {case_name} {field} must be a string"))?;
            if value.is_empty() || value.trim() != value || value.len() > max_bytes {
                return Err(format!(
                    "retry TCK case {case_name} {field} must be an exact non-empty string of at most {max_bytes} bytes"
                ));
            }
        }
        if !case_object.get("timeout").is_some_and(Value::is_string) {
            return Err(format!(
                "retry TCK case {case_name} timeout must be a duration string"
            ));
        }
    }
    if !matches!(
        kind,
        "node_retry"
            | "cancelled_before_retry"
            | "cancelled_before_commit"
            | "cancelled_before_start"
            | "cancelled_after_terminal"
            | "timeout_retry"
            | "timeout_exhaustion"
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
    let timeout_ms = case_object
        .get("timeout")
        .map(|value| {
            parse_duration_milliseconds(value)
                .ok_or_else(|| format!("retry TCK case {case_name} timeout is invalid"))
        })
        .transpose()?;
    let attempt_durations_ms = case_object
        .get("attemptDurationsMs")
        .map(|value| {
            let values = value
                .as_array()
                .ok_or_else(|| {
                    format!("retry TCK case {case_name} attemptDurationsMs must be an array")
                })?;
            if values.len() > 100 {
                return Err(format!(
                    "retry TCK case {case_name} attemptDurationsMs exceeds 100 entries"
                ));
            }
            values
                .iter()
                .enumerate()
                .map(|(index, duration)| {
                    duration.as_u64().ok_or_else(|| {
                        format!(
                            "retry TCK case {case_name} attemptDurationsMs[{index}] must be a non-negative integer"
                        )
                    })
                })
                .collect::<Result<Vec<_>, _>>()
        })
        .transpose()?
        .unwrap_or_default();
    let attempt_output_values = case_object
        .get("attemptOutputValues")
        .map(|value| {
            let values = value
                .as_array()
                .ok_or_else(|| {
                    format!("retry TCK case {case_name} attemptOutputValues must be an array")
                })?;
            if values.len() > 100 {
                return Err(format!(
                    "retry TCK case {case_name} attemptOutputValues exceeds 100 entries"
                ));
            }
            values
                .iter()
                .enumerate()
                .map(|(index, output)| {
                    let output = output.as_str().ok_or_else(|| {
                        format!(
                            "retry TCK case {case_name} attemptOutputValues[{index}] must be a string"
                        )
                    })?;
                    if output.is_empty() || output.len() > 4_096 {
                        return Err(format!(
                            "retry TCK case {case_name} attemptOutputValues[{index}] must be a non-empty string of at most 4096 bytes"
                        ));
                    }
                    Ok(Value::from(output))
                })
                .collect::<Result<Vec<_>, _>>()
        })
        .transpose()?
        .unwrap_or_default();
    if matches!(kind, "timeout_retry" | "timeout_exhaustion") {
        let max_attempts_usize = usize::try_from(max_attempts)
            .map_err(|_| format!("retry TCK case {case_name} maxAttempts is too large"))?;
        if max_attempts == 0 || max_attempts > 100 {
            return Err(format!(
                "retry TCK case {case_name} maxAttempts must be between 1 and 100"
            ));
        }
        if failures_before_success != 0 {
            return Err(format!(
                "retry TCK case {case_name} failuresBeforeSuccess must be zero for timeout cases"
            ));
        }
        if attempt_durations_ms.len() != max_attempts_usize
            || attempt_output_values.len() != max_attempts_usize
        {
            return Err(format!(
                "retry TCK case {case_name} attempt fixtures must match maxAttempts"
            ));
        }
        if attempt_durations_ms
            .iter()
            .any(|duration| *duration > 1_000)
        {
            return Err(format!(
                "retry TCK case {case_name} attempt duration exceeds 1000 milliseconds"
            ));
        }
        if attempt_durations_ms.iter().sum::<u64>() > 2_000 {
            return Err(format!(
                "retry TCK case {case_name} total attempt duration exceeds 2000 milliseconds"
            ));
        }
        let timeout_ms =
            timeout_ms.ok_or_else(|| format!("retry TCK case {case_name} requires timeout"))?;
        if timeout_ms > 1_000 {
            return Err(format!(
                "retry TCK case {case_name} timeout exceeds 1000 milliseconds"
            ));
        }
        let last_duration = attempt_durations_ms.last().copied().unwrap_or_default();
        if kind == "timeout_retry"
            && (attempt_durations_ms[..attempt_durations_ms.len() - 1]
                .iter()
                .any(|duration| *duration < timeout_ms)
                || last_duration >= timeout_ms)
        {
            return Err(format!(
                "retry TCK case {case_name} must time out before a final successful attempt"
            ));
        }
        if kind == "timeout_exhaustion"
            && attempt_durations_ms
                .iter()
                .any(|duration| *duration < timeout_ms)
        {
            return Err(format!(
                "retry TCK case {case_name} must time out every attempt"
            ));
        }
        if kind == "timeout_retry"
            && attempt_output_values
                .first()
                .zip(attempt_output_values.last())
                .is_some_and(|(first, last)| first == last)
        {
            return Err(format!(
                "retry TCK case {case_name} must use distinct stale and committed outputs"
            ));
        }
    }

    let policy = RetryPolicy::try_new(max_attempts)
        .map_err(|error| format!("retry TCK case {case_name}: {error}"))?
        .retry_on([
            ErrorCategory::Transient,
            ErrorCategory::Timeout,
            ErrorCategory::RateLimit,
        ]);
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
    if let Some(timeout_ms) = timeout_ms {
        let timeout = TimeoutPolicy::new(timeout_ms)
            .map_err(|error| format!("retry TCK case {case_name}: {error:?}"))?;
        runtime = runtime.with_timeout_policy(node_id, timeout);
    }
    if !attempt_durations_ms.is_empty() {
        runtime = runtime.with_node_attempt_durations_ms(node_id, attempt_durations_ms);
    }
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
        attempt_output_values,
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
    Ok(json!({
        "status": status,
        "terminalKind": terminal_kind,
        "attempts": executor.attempts,
        "retryCount": retry_idempotency_keys.len(),
        "retryIdempotencyKeys": retry_idempotency_keys,
        "startedIdempotencyKeys": started_idempotency_keys,
        "attemptIds": attempt_ids,
        "commitAttemptIds": commit_attempt_ids,
        "committedOutputValue": committed_output_value,
        "journalKinds": journal_kinds,
        "nodeCommitCount": node_commit_count,
        "terminalCount": terminal_count,
        "postTerminalCancellation": post_terminal_cancellation,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timeout_retry_tck_decoder_is_closed_and_bounded() {
        let valid = json!({
            "contractVersion": "graphblocks.retry-flow.tck.v1",
            "name": "closed-timeout-retry",
            "kind": "timeout_retry",
            "block": "test.slow@1",
            "nodeId": "worker",
            "maxAttempts": 2,
            "failuresBeforeSuccess": 0,
            "timeout": "1ms",
            "attemptDurationsMs": [2, 0],
            "attemptOutputValues": ["stale", "committed"],
            "idempotencyKey": "request-1",
        });
        assert!(evaluate_case(&valid).is_ok());

        let mut unknown = valid.clone();
        unknown
            .as_object_mut()
            .expect("fixture is an object")
            .insert("expected".to_owned(), json!({}));
        let mut boolean_duration = valid.clone();
        boolean_duration["attemptDurationsMs"] = json!([true, 0]);
        let mut oversized_output = valid.clone();
        oversized_output["attemptOutputValues"] = json!(["x".repeat(4_097), "ok"]);
        let mut oversized_attempts = valid.clone();
        oversized_attempts["maxAttempts"] = json!(101);
        let mut repeated_output = valid.clone();
        repeated_output["attemptOutputValues"] = json!(["same", "same"]);

        for (fixture, expected_error) in [
            (unknown, "unknown field expected"),
            (boolean_duration, "must be a non-negative integer"),
            (oversized_output, "at most 4096 bytes"),
            (oversized_attempts, "between 1 and 100"),
            (repeated_output, "distinct stale and committed outputs"),
        ] {
            let error = evaluate_case(&fixture).expect_err("fixture should be rejected");
            assert!(
                error.contains(expected_error),
                "unexpected rejection {error:?}"
            );
        }
    }
}
