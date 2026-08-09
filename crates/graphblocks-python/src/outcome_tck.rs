use std::collections::{BTreeMap, BTreeSet};

use graphblocks_runtime_core::outcome::{
    BlockError, BudgetExhaustion, CancelCode, CancelReason, ErrorCategory, Outcome,
    OutcomeTextRole, OutcomeValidationError, PauseReason, PolicyDecisionRef, SkipReason,
};
use graphblocks_runtime_core::readiness::{
    InputDependency, PortRef, Readiness, ReadinessTracker, ReadinessValidationError, ResolvedInput,
};
use graphblocks_runtime_core::scheduler::{ScheduledNode, StartedNode};
use graphblocks_runtime_core::test_runtime::{
    InProcessTestRuntime, OutcomeNodeExecutor, OutcomeRunStatus,
};
use serde_json::{Map, Value, json};

const CONTRACT_VERSION: &str = "graphblocks.outcome.tck.v1";
const MAX_JSON_DEPTH: usize = 64;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DecodeCategory {
    UnknownField,
    InvalidOutcome,
    InvalidReadiness,
    MissingField,
    InvalidIdentifier,
    NoncanonicalAlias,
    ForbiddenField,
    DuplicateDependency,
    DuplicateSignal,
    UnsupportedScenario,
}

impl DecodeCategory {
    fn as_str(self) -> &'static str {
        match self {
            Self::UnknownField => "unknown_field",
            Self::InvalidOutcome => "invalid_outcome",
            Self::InvalidReadiness => "invalid_readiness",
            Self::MissingField => "missing_field",
            Self::InvalidIdentifier => "invalid_identifier",
            Self::NoncanonicalAlias => "noncanonical_alias",
            Self::ForbiddenField => "forbidden_field",
            Self::DuplicateDependency => "duplicate_dependency",
            Self::DuplicateSignal => "duplicate_signal",
            Self::UnsupportedScenario => "unsupported_scenario",
        }
    }
}

pub(crate) fn evaluate_case(case: &Value) -> Value {
    let Some(case) = case.as_object() else {
        return rejection("", DecodeCategory::InvalidOutcome);
    };
    let scenario = match case.get("scenario") {
        Some(Value::String(scenario)) => scenario.as_str(),
        Some(_) => return rejection("", DecodeCategory::InvalidOutcome),
        None => return rejection("", DecodeCategory::MissingField),
    };

    let result = match scenario {
        "normalize_outcome" => evaluate_normalize_outcome(case),
        "evaluate_readiness" => evaluate_readiness(case),
        "execute_local_terminal" => evaluate_local_terminal(case),
        _ => Err(DecodeCategory::UnsupportedScenario),
    };
    match result {
        Ok(result) => result,
        Err(category) => rejection(scenario, category),
    }
}

#[derive(Clone, Debug)]
struct FixedOutcomeExecutor {
    outcome: Outcome<Vec<(PortRef, Outcome<Value>)>>,
}

impl OutcomeNodeExecutor for FixedOutcomeExecutor {
    fn execute(&mut self, _node: StartedNode) -> Outcome<Vec<(PortRef, Outcome<Value>)>> {
        self.outcome.clone()
    }
}

fn evaluate_local_terminal(case: &Map<String, Value>) -> Result<Value, DecodeCategory> {
    ensure_closed_object(case, &["name", "scenario", "outcome"], &[])?;
    validate_case_name(case)?;
    let outcome = decode_outcome(required(case, "outcome")?)?;
    let execution_outcome = match outcome {
        Outcome::Value(value) => {
            Outcome::Value(vec![(PortRef::new("node", "value"), Outcome::Value(value))])
        }
        Outcome::Absent => Outcome::Absent,
        Outcome::Skipped(reason) => Outcome::Skipped(reason),
        Outcome::Denied(decision) => Outcome::Denied(decision),
        Outcome::BudgetExhausted(reason) => Outcome::BudgetExhausted(reason),
        Outcome::Paused(reason) => Outcome::Paused(reason),
        Outcome::Failed(error) => Outcome::Failed(error),
        Outcome::Cancelled(reason) => Outcome::Cancelled(reason),
    };
    let mut runtime =
        InProcessTestRuntime::new("run-outcome-tck", [ScheduledNode::new("node", [])])
            .map_err(|_| DecodeCategory::InvalidOutcome)?;
    let mut executor = FixedOutcomeExecutor {
        outcome: execution_outcome,
    };
    let result = runtime
        .run_with_outcomes(&mut executor)
        .map_err(|_| DecodeCategory::InvalidOutcome)?;
    let status = match result.status {
        OutcomeRunStatus::Succeeded => "succeeded",
        OutcomeRunStatus::Failed => "failed",
        OutcomeRunStatus::Cancelled => "cancelled",
        OutcomeRunStatus::Rejected => "rejected",
        OutcomeRunStatus::Paused => "paused",
        OutcomeRunStatus::Exhausted => "exhausted",
    };
    let terminal_kind = result
        .journal
        .terminal_kind()
        .ok_or(DecodeCategory::InvalidOutcome)?;
    let journal_kinds = result
        .journal
        .records()
        .iter()
        .map(|record| record.kind.as_str())
        .collect::<Vec<_>>();
    let terminal_count = result
        .journal
        .records()
        .iter()
        .filter(|record| record.terminal)
        .count();

    Ok(json!({
        "contractVersion": CONTRACT_VERSION,
        "ok": true,
        "scenario": "execute_local_terminal",
        "run": {
            "status": status,
            "terminalKind": terminal_kind,
            "terminalCount": terminal_count,
            "journalKinds": journal_kinds,
        },
    }))
}

fn validate_case_name(case: &Map<String, Value>) -> Result<(), DecodeCategory> {
    let name = required(case, "name")?
        .as_str()
        .ok_or(DecodeCategory::InvalidIdentifier)?;
    validate_identifier_text(name)
}

fn evaluate_normalize_outcome(case: &Map<String, Value>) -> Result<Value, DecodeCategory> {
    ensure_closed_object(case, &["name", "scenario", "outcome"], &[])?;
    validate_case_name(case)?;
    let outcome = decode_outcome(required(case, "outcome")?)?;
    Ok(json!({
        "contractVersion": CONTRACT_VERSION,
        "ok": true,
        "scenario": "normalize_outcome",
        "outcome": encode_outcome(&outcome),
    }))
}

fn evaluate_readiness(case: &Map<String, Value>) -> Result<Value, DecodeCategory> {
    ensure_closed_object(case, &["name", "scenario", "signals", "dependencies"], &[])?;
    validate_case_name(case)?;
    let signals = required(case, "signals")?
        .as_array()
        .ok_or(DecodeCategory::InvalidReadiness)?;
    let dependencies = required(case, "dependencies")?
        .as_array()
        .ok_or(DecodeCategory::InvalidReadiness)?;
    let mut tracker = ReadinessTracker::new();
    let mut signal_ports = BTreeSet::new();

    for signal in signals {
        let signal = signal.as_object().ok_or(DecodeCategory::InvalidReadiness)?;
        ensure_closed_object(signal, &["portRef", "outcome"], &["port_ref"])?;
        let port = decode_port_ref(required(signal, "portRef")?)?;
        if !signal_ports.insert(port.clone()) {
            return Err(DecodeCategory::DuplicateSignal);
        }
        let outcome = decode_outcome(required(signal, "outcome")?)?;
        tracker
            .try_publish(port, outcome)
            .map_err(readiness_error_category)?;
    }

    let mut decoded_dependencies = Vec::with_capacity(dependencies.len());
    let mut dependency_inputs = BTreeSet::new();
    for dependency in dependencies {
        decoded_dependencies.push(decode_dependency(dependency, &mut dependency_inputs)?);
    }
    let readiness = tracker
        .try_readiness(decoded_dependencies)
        .map_err(readiness_error_category)?;

    Ok(json!({
        "contractVersion": CONTRACT_VERSION,
        "ok": true,
        "scenario": "evaluate_readiness",
        "readiness": encode_readiness(&readiness),
    }))
}

fn decode_outcome(value: &Value) -> Result<Outcome<Value>, DecodeCategory> {
    let object = value.as_object().ok_or(DecodeCategory::InvalidOutcome)?;
    if !object.contains_key("status") {
        return Err(if object.contains_key("kind") {
            DecodeCategory::NoncanonicalAlias
        } else {
            DecodeCategory::MissingField
        });
    }
    let status = required_str(object, "status")?;
    let outcome = match status {
        "value" => {
            ensure_outcome_object(object, &["status", "value"], &["kind", "payload"])?;
            let value = required(object, "value")?;
            validate_json_value(value, 0)?;
            Outcome::Value(value.clone())
        }
        "absent" => {
            ensure_outcome_object(object, &["status"], &["kind"])?;
            Outcome::Absent
        }
        "skipped" => {
            ensure_outcome_object(object, &["status", "reason"], &["kind"])?;
            let reason_object = required(object, "reason")?
                .as_object()
                .ok_or(DecodeCategory::InvalidOutcome)?;
            ensure_closed_object(reason_object, &["code", "message"], &[])?;
            let mut reason = SkipReason::new(required_identifier_str(reason_object, "code")?);
            reason.message =
                required_nullable_human_text_str(reason_object, "message")?.map(ToOwned::to_owned);
            Outcome::Skipped(reason)
        }
        "denied" => {
            ensure_outcome_object(object, &["status", "decisionId"], &["kind", "decision_id"])?;
            Outcome::Denied(PolicyDecisionRef::new(required_identifier_str(
                object,
                "decisionId",
            )?))
        }
        "budget_exhausted" => {
            ensure_outcome_object(object, &["status", "code", "message"], &["kind"])?;
            let mut reason = BudgetExhaustion::new(required_identifier_str(object, "code")?);
            reason.message =
                required_nullable_human_text_str(object, "message")?.map(ToOwned::to_owned);
            Outcome::BudgetExhausted(reason)
        }
        "paused" => {
            ensure_outcome_object(object, &["status", "code", "message"], &["kind"])?;
            let mut reason = PauseReason::new(required_identifier_str(object, "code")?);
            reason.message =
                required_nullable_human_text_str(object, "message")?.map(ToOwned::to_owned);
            Outcome::Paused(reason)
        }
        "failed" => {
            ensure_outcome_object(object, &["status", "error"], &["kind"])?;
            Outcome::Failed(decode_block_error(required(object, "error")?)?)
        }
        "cancelled" => {
            ensure_outcome_object(object, &["status", "reason"], &["kind"])?;
            Outcome::Cancelled(decode_cancel_reason(required(object, "reason")?)?)
        }
        _ => return Err(DecodeCategory::InvalidOutcome),
    };
    outcome.validate().map_err(outcome_error_category)?;
    Ok(outcome)
}

fn decode_block_error(value: &Value) -> Result<BlockError, DecodeCategory> {
    let object = value.as_object().ok_or(DecodeCategory::InvalidOutcome)?;
    ensure_closed_object(
        object,
        &[
            "code",
            "category",
            "message",
            "retryable",
            "details",
            "causeChain",
        ],
        &["cause_chain"],
    )?;
    let code = required_identifier_str(object, "code")?;
    let category = decode_error_category(required_str(object, "category")?)?;
    let message = required_human_text_str(object, "message")?;
    let retryable = required(object, "retryable")?
        .as_bool()
        .ok_or(DecodeCategory::InvalidOutcome)?;
    let details_value = required(object, "details")?;
    validate_json_value(details_value, 0)?;
    let details = details_value
        .as_object()
        .ok_or(DecodeCategory::InvalidOutcome)?
        .iter()
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect::<BTreeMap<_, _>>();
    let cause_chain = required(object, "causeChain")?
        .as_array()
        .ok_or(DecodeCategory::InvalidOutcome)?
        .iter()
        .map(|cause| {
            cause
                .as_str()
                .map(ToOwned::to_owned)
                .ok_or(DecodeCategory::InvalidOutcome)
        })
        .collect::<Result<Vec<_>, _>>()?;
    for cause in &cause_chain {
        validate_human_text(cause)?;
    }
    let mut error = BlockError::new(code, category, message, retryable);
    error.details = details;
    error.cause_chain = cause_chain;
    Ok(error)
}

fn decode_cancel_reason(value: &Value) -> Result<CancelReason, DecodeCategory> {
    let object = value.as_object().ok_or(DecodeCategory::InvalidOutcome)?;
    ensure_closed_object(
        object,
        &["code", "message", "requestedBy", "policyDecisionRef"],
        &["requested_by", "policy_decision_ref"],
    )?;
    let mut reason = CancelReason::new(decode_cancel_code(required_identifier_str(
        object, "code",
    )?)?);
    reason.message = required_nullable_human_text_str(object, "message")?.map(ToOwned::to_owned);
    reason.requested_by =
        required_nullable_identifier_str(object, "requestedBy")?.map(ToOwned::to_owned);
    reason.policy_decision_ref =
        required_nullable_identifier_str(object, "policyDecisionRef")?.map(ToOwned::to_owned);
    Ok(reason)
}

fn decode_port_ref(value: &Value) -> Result<PortRef, DecodeCategory> {
    let object = value.as_object().ok_or(DecodeCategory::InvalidReadiness)?;
    ensure_closed_object(object, &["node", "port"], &[])?;
    let node = required_identifier_str(object, "node")?;
    let port = required_identifier_str(object, "port")?;
    PortRef::try_new(node, port).map_err(|_| DecodeCategory::InvalidIdentifier)
}

fn decode_dependency(
    value: &Value,
    dependency_inputs: &mut BTreeSet<String>,
) -> Result<InputDependency, DecodeCategory> {
    let object = value.as_object().ok_or(DecodeCategory::InvalidReadiness)?;
    ensure_closed_object(object, &["input", "source", "mode"], &[])?;
    let input = required_identifier_str(object, "input")?;
    if !dependency_inputs.insert(input.to_owned()) {
        return Err(DecodeCategory::DuplicateDependency);
    }
    let source = decode_port_ref(required(object, "source")?)?;
    let mode = required(object, "mode")?
        .as_str()
        .ok_or(DecodeCategory::InvalidReadiness)?;
    let dependency = match mode {
        "value" => InputDependency::try_value(input, source),
        "outcome" => InputDependency::try_outcome(input, source),
        _ => return Err(DecodeCategory::InvalidReadiness),
    }
    .map_err(|_| DecodeCategory::InvalidIdentifier)?;
    Ok(dependency)
}

fn encode_outcome(outcome: &Outcome<Value>) -> Value {
    match outcome {
        Outcome::Value(value) => json!({"status": "value", "value": value}),
        Outcome::Absent => json!({"status": "absent"}),
        Outcome::Skipped(reason) => json!({
            "status": "skipped",
            "reason": {"code": reason.code, "message": reason.message},
        }),
        Outcome::Denied(decision) => {
            json!({"status": "denied", "decisionId": decision.decision_id})
        }
        Outcome::BudgetExhausted(reason) => json!({
            "status": "budget_exhausted",
            "code": reason.code,
            "message": reason.message,
        }),
        Outcome::Paused(reason) => json!({
            "status": "paused",
            "code": reason.code,
            "message": reason.message,
        }),
        Outcome::Failed(error) => json!({
            "status": "failed",
            "error": {
                "code": error.code,
                "category": encode_error_category(error.category),
                "message": error.message,
                "retryable": error.retryable,
                "details": error.details,
                "causeChain": error.cause_chain,
            },
        }),
        Outcome::Cancelled(reason) => json!({
            "status": "cancelled",
            "reason": {
                "code": encode_cancel_code(reason.code),
                "message": reason.message,
                "requestedBy": reason.requested_by,
                "policyDecisionRef": reason.policy_decision_ref,
            },
        }),
    }
}

fn encode_readiness(readiness: &Readiness) -> Value {
    match readiness {
        Readiness::Ready(inputs) => json!({
            "status": "ready",
            "inputs": inputs
                .iter()
                .map(|(input, value)| (input.clone(), encode_resolved_input(value)))
                .collect::<Map<_, _>>(),
        }),
        Readiness::Waiting { missing } => json!({
            "status": "waiting",
            "missing": missing.iter().map(encode_port_ref).collect::<Vec<_>>(),
        }),
        Readiness::Blocked {
            input,
            source,
            outcome,
        } => json!({
            "status": "blocked",
            "input": input,
            "source": encode_port_ref(source),
            "outcome": encode_outcome(outcome),
        }),
    }
}

fn encode_resolved_input(input: &ResolvedInput) -> Value {
    match input {
        ResolvedInput::Value(value) => json!({"mode": "value", "value": value}),
        ResolvedInput::Outcome(outcome) => {
            json!({"mode": "outcome", "outcome": encode_outcome(outcome)})
        }
    }
}

fn encode_port_ref(port: &PortRef) -> Value {
    json!({"node": port.node, "port": port.port})
}

fn ensure_closed_object(
    object: &Map<String, Value>,
    fields: &[&str],
    aliases: &[&str],
) -> Result<(), DecodeCategory> {
    let allowed = fields.iter().copied().collect::<BTreeSet<_>>();
    let noncanonical = aliases.iter().copied().collect::<BTreeSet<_>>();
    if object
        .keys()
        .any(|field| noncanonical.contains(field.as_str()))
    {
        return Err(DecodeCategory::NoncanonicalAlias);
    }
    if object.keys().any(|field| !allowed.contains(field.as_str())) {
        return Err(DecodeCategory::UnknownField);
    }
    if fields.iter().any(|field| !object.contains_key(*field)) {
        return Err(DecodeCategory::MissingField);
    }
    Ok(())
}

fn ensure_outcome_object(
    object: &Map<String, Value>,
    fields: &[&str],
    aliases: &[&str],
) -> Result<(), DecodeCategory> {
    const ROOT_FIELDS: &[&str] = &[
        "status",
        "value",
        "reason",
        "decisionId",
        "code",
        "message",
        "error",
    ];
    const ROOT_ALIASES: &[&str] = &["kind", "payload", "decision_id"];
    let allowed = fields.iter().copied().collect::<BTreeSet<_>>();
    let noncanonical = aliases.iter().copied().collect::<BTreeSet<_>>();
    if object
        .keys()
        .any(|field| noncanonical.contains(field.as_str()))
    {
        return Err(DecodeCategory::NoncanonicalAlias);
    }
    if object.keys().any(|field| {
        !allowed.contains(field.as_str())
            && (ROOT_FIELDS.contains(&field.as_str()) || ROOT_ALIASES.contains(&field.as_str()))
    }) {
        return Err(DecodeCategory::ForbiddenField);
    }
    if object.keys().any(|field| !allowed.contains(field.as_str())) {
        return Err(DecodeCategory::UnknownField);
    }
    if fields.iter().any(|field| !object.contains_key(*field)) {
        return Err(DecodeCategory::MissingField);
    }
    Ok(())
}

fn required<'a>(object: &'a Map<String, Value>, field: &str) -> Result<&'a Value, DecodeCategory> {
    object.get(field).ok_or(DecodeCategory::MissingField)
}

fn required_str<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<&'a str, DecodeCategory> {
    required(object, field)?
        .as_str()
        .ok_or(DecodeCategory::InvalidOutcome)
}

fn required_nullable_str<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<Option<&'a str>, DecodeCategory> {
    let value = required(object, field)?;
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_str()
        .map(Some)
        .ok_or(DecodeCategory::InvalidOutcome)
}

fn required_identifier_str<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<&'a str, DecodeCategory> {
    let value = required(object, field)?
        .as_str()
        .ok_or(DecodeCategory::InvalidIdentifier)?;
    validate_identifier_text(value)?;
    Ok(value)
}

fn required_nullable_identifier_str<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<Option<&'a str>, DecodeCategory> {
    let value = required(object, field)?;
    if value.is_null() {
        return Ok(None);
    }
    let value = value.as_str().ok_or(DecodeCategory::InvalidIdentifier)?;
    validate_identifier_text(value)?;
    Ok(Some(value))
}

fn required_human_text_str<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<&'a str, DecodeCategory> {
    let value = required_str(object, field)?;
    validate_human_text(value)?;
    Ok(value)
}

fn required_nullable_human_text_str<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<Option<&'a str>, DecodeCategory> {
    let value = required_nullable_str(object, field)?;
    if let Some(value) = value {
        validate_human_text(value)?;
    }
    Ok(value)
}

fn validate_identifier_text(value: &str) -> Result<(), DecodeCategory> {
    if value.trim().is_empty()
        || value != value.trim()
        || value
            .chars()
            .any(|character| character <= '\u{001f}' || character == '\u{007f}')
    {
        return Err(DecodeCategory::InvalidIdentifier);
    }
    Ok(())
}

fn validate_human_text(value: &str) -> Result<(), DecodeCategory> {
    if value.trim().is_empty()
        || value
            .chars()
            .any(|character| character <= '\u{001f}' || character == '\u{007f}')
    {
        return Err(DecodeCategory::InvalidOutcome);
    }
    Ok(())
}

fn validate_json_value(value: &Value, depth: usize) -> Result<(), DecodeCategory> {
    if depth > MAX_JSON_DEPTH {
        return Err(DecodeCategory::InvalidOutcome);
    }
    match value {
        Value::Array(items) => items
            .iter()
            .try_for_each(|item| validate_json_value(item, depth + 1)),
        Value::Object(object) => object
            .values()
            .try_for_each(|item| validate_json_value(item, depth + 1)),
        _ => Ok(()),
    }
}

fn readiness_error_category(error: ReadinessValidationError) -> DecodeCategory {
    match error {
        ReadinessValidationError::DuplicateDependencyInput { .. } => {
            DecodeCategory::DuplicateDependency
        }
        ReadinessValidationError::DuplicateSignal { .. } => DecodeCategory::DuplicateSignal,
        ReadinessValidationError::InvalidText { .. } => DecodeCategory::InvalidIdentifier,
        ReadinessValidationError::InvalidOutcome { error, .. } => outcome_error_category(error),
    }
}

fn outcome_error_category(error: OutcomeValidationError) -> DecodeCategory {
    match error.role {
        OutcomeTextRole::Identifier => DecodeCategory::InvalidIdentifier,
        OutcomeTextRole::HumanText => DecodeCategory::InvalidOutcome,
    }
}

fn decode_error_category(value: &str) -> Result<ErrorCategory, DecodeCategory> {
    match value {
        "validation" => Ok(ErrorCategory::Validation),
        "configuration" => Ok(ErrorCategory::Configuration),
        "authentication" => Ok(ErrorCategory::Authentication),
        "authorization" => Ok(ErrorCategory::Authorization),
        "not_found" => Ok(ErrorCategory::NotFound),
        "rate_limit" => Ok(ErrorCategory::RateLimit),
        "quota" => Ok(ErrorCategory::Quota),
        "budget" => Ok(ErrorCategory::Budget),
        "capacity" => Ok(ErrorCategory::Capacity),
        "timeout" => Ok(ErrorCategory::Timeout),
        "transient" => Ok(ErrorCategory::Transient),
        "permanent" => Ok(ErrorCategory::Permanent),
        "provider" => Ok(ErrorCategory::Provider),
        "policy" => Ok(ErrorCategory::Policy),
        "cancelled" => Ok(ErrorCategory::Cancelled),
        "conflict" => Ok(ErrorCategory::Conflict),
        "internal" => Ok(ErrorCategory::Internal),
        _ => Err(DecodeCategory::InvalidOutcome),
    }
}

fn encode_error_category(category: ErrorCategory) -> &'static str {
    match category {
        ErrorCategory::Validation => "validation",
        ErrorCategory::Configuration => "configuration",
        ErrorCategory::Authentication => "authentication",
        ErrorCategory::Authorization => "authorization",
        ErrorCategory::NotFound => "not_found",
        ErrorCategory::RateLimit => "rate_limit",
        ErrorCategory::Quota => "quota",
        ErrorCategory::Budget => "budget",
        ErrorCategory::Capacity => "capacity",
        ErrorCategory::Timeout => "timeout",
        ErrorCategory::Transient => "transient",
        ErrorCategory::Permanent => "permanent",
        ErrorCategory::Provider => "provider",
        ErrorCategory::Policy => "policy",
        ErrorCategory::Cancelled => "cancelled",
        ErrorCategory::Conflict => "conflict",
        ErrorCategory::Internal => "internal",
    }
}

fn decode_cancel_code(value: &str) -> Result<CancelCode, DecodeCategory> {
    match value {
        "client_disconnect" => Ok(CancelCode::ClientDisconnect),
        "user_cancel" => Ok(CancelCode::UserCancel),
        "timeout" => Ok(CancelCode::Timeout),
        "superseded" => Ok(CancelCode::Superseded),
        "policy_denied" => Ok(CancelCode::PolicyDenied),
        "budget_exhausted" => Ok(CancelCode::BudgetExhausted),
        "provider_quota_exhausted" => Ok(CancelCode::ProviderQuotaExhausted),
        "dependency_failed" => Ok(CancelCode::DependencyFailed),
        "shutdown" => Ok(CancelCode::Shutdown),
        "barge_in" => Ok(CancelCode::BargeIn),
        "rollout_drain" => Ok(CancelCode::RolloutDrain),
        "lease_lost" => Ok(CancelCode::LeaseLost),
        "entitlement_revoked" => Ok(CancelCode::EntitlementRevoked),
        _ => Err(DecodeCategory::InvalidOutcome),
    }
}

fn encode_cancel_code(code: CancelCode) -> &'static str {
    match code {
        CancelCode::ClientDisconnect => "client_disconnect",
        CancelCode::UserCancel => "user_cancel",
        CancelCode::Timeout => "timeout",
        CancelCode::Superseded => "superseded",
        CancelCode::PolicyDenied => "policy_denied",
        CancelCode::BudgetExhausted => "budget_exhausted",
        CancelCode::ProviderQuotaExhausted => "provider_quota_exhausted",
        CancelCode::DependencyFailed => "dependency_failed",
        CancelCode::Shutdown => "shutdown",
        CancelCode::BargeIn => "barge_in",
        CancelCode::RolloutDrain => "rollout_drain",
        CancelCode::LeaseLost => "lease_lost",
        CancelCode::EntitlementRevoked => "entitlement_revoked",
    }
}

fn rejection(scenario: &str, category: DecodeCategory) -> Value {
    json!({
        "contractVersion": CONTRACT_VERSION,
        "ok": false,
        "scenario": scenario,
        "errorCategory": category.as_str(),
    })
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::{DecodeCategory, evaluate_case, rejection};

    #[test]
    fn evaluator_exactly_matches_shared_fixture_requests() -> Result<(), String> {
        let crate_local =
            serde_json::from_str::<Value>(include_str!("fixtures/outcome-cases.json"))
                .map_err(|error| error.to_string())?;

        let cases = crate_local
            .as_array()
            .ok_or_else(|| "outcome TCK fixture must be an array".to_owned())?;
        for case in cases {
            let expected = case
                .get("expected")
                .ok_or_else(|| "outcome TCK case requires expected".to_owned())?;
            let observed = if let Some(request) = case.get("request") {
                evaluate_case(request)
            } else {
                let mut request = case
                    .as_object()
                    .cloned()
                    .ok_or_else(|| "outcome TCK case must be an object".to_owned())?;
                request.remove("expected");
                evaluate_case(&Value::Object(request))
            };
            assert_eq!(
                &observed,
                expected,
                "outcome TCK case {:?}",
                case.get("name")
            );
        }
        Ok(())
    }

    #[test]
    fn wrong_branch_fields_fail_closed_as_forbidden() -> Result<(), String> {
        for outcome in [
            json!({"status": "value", "value": null, "code": "not-allowed"}),
            json!({
                "status": "skipped",
                "reason": {"code": "condition_false", "message": null},
                "decisionId": "not-allowed"
            }),
            json!({
                "status": "denied",
                "decisionId": "decision-1",
                "reason": {"code": "not-allowed", "message": null}
            }),
            json!({
                "status": "budget_exhausted",
                "code": "budget.limit",
                "message": null,
                "value": 1
            }),
            json!({
                "status": "paused",
                "code": "approval.required",
                "message": null,
                "error": {}
            }),
            json!({
                "status": "failed",
                "error": {
                    "code": "provider.timeout",
                    "category": "timeout",
                    "message": "provider timed out",
                    "retryable": true,
                    "details": {},
                    "causeChain": []
                },
                "reason": {}
            }),
            json!({
                "status": "cancelled",
                "reason": {
                    "code": "user_cancel",
                    "message": null,
                    "requestedBy": null,
                    "policyDecisionRef": null
                },
                "message": "not-allowed"
            }),
        ] {
            let result = evaluate_case(&json!({
                "name": "wrong-branch",
                "scenario": "normalize_outcome",
                "outcome": outcome,
            }));
            assert_eq!(
                result.get("errorCategory").and_then(Value::as_str),
                Some("forbidden_field")
            );
        }
        Ok(())
    }

    #[test]
    fn aliases_and_unknown_fields_are_scoped_to_their_owner() {
        for (outcome, category) in [
            (
                json!({"status": "value", "value": null, "kind": "value"}),
                "noncanonical_alias",
            ),
            (
                json!({"status": "absent", "payload": null}),
                "forbidden_field",
            ),
            (
                json!({"status": "value", "value": null, "decision_id": "decision-1"}),
                "forbidden_field",
            ),
            (
                json!({"status": "value", "value": null, "requested_by": "principal-1"}),
                "unknown_field",
            ),
            (
                json!({"status": "denied", "decision_id": "decision-1", "metadata": {}}),
                "noncanonical_alias",
            ),
            (json!({"status": "value", "metadata": {}}), "unknown_field"),
        ] {
            let result = evaluate_case(&json!({
                "name": "owner-scoped-field",
                "scenario": "normalize_outcome",
                "outcome": outcome,
            }));
            assert_eq!(
                result.get("errorCategory").and_then(Value::as_str),
                Some(category)
            );
        }
    }

    #[test]
    fn case_shape_errors_are_closed_and_deterministic() {
        for (request, scenario, category) in [
            (json!(null), "", DecodeCategory::InvalidOutcome),
            (
                json!({"name": "missing-scenario"}),
                "",
                DecodeCategory::MissingField,
            ),
            (
                json!({"name": "bad-scenario", "scenario": 1}),
                "",
                DecodeCategory::InvalidOutcome,
            ),
            (
                json!({"name": "future", "scenario": "future_scenario"}),
                "future_scenario",
                DecodeCategory::UnsupportedScenario,
            ),
            (
                json!({
                    "name": 1,
                    "scenario": "normalize_outcome",
                    "outcome": {"status": "absent"},
                    "extra": true,
                }),
                "normalize_outcome",
                DecodeCategory::UnknownField,
            ),
            (
                json!({
                    "name": 1,
                    "scenario": "normalize_outcome",
                    "outcome": {"status": "absent"},
                }),
                "normalize_outcome",
                DecodeCategory::InvalidIdentifier,
            ),
        ] {
            assert_eq!(evaluate_case(&request), rejection(scenario, category));
        }
    }

    #[test]
    fn blank_message_and_cause_chain_text_is_rejected() -> Result<(), String> {
        for outcome in [
            json!({
                "status": "skipped",
                "reason": {"code": "condition_false", "message": " \t "}
            }),
            json!({
                "status": "failed",
                "error": {
                    "code": "provider.timeout",
                    "category": "timeout",
                    "message": "provider timed out",
                    "retryable": true,
                    "details": {},
                    "causeChain": [""]
                }
            }),
        ] {
            let result = evaluate_case(&json!({
                "name": "invalid-text",
                "scenario": "normalize_outcome",
                "outcome": outcome,
            }));
            assert_eq!(
                result.get("errorCategory").and_then(Value::as_str),
                Some("invalid_outcome")
            );
        }
        Ok(())
    }

    #[test]
    fn human_text_preserves_surrounding_whitespace() -> Result<(), String> {
        let outcome = json!({
            "status": "failed",
            "error": {
                "code": "provider.timeout",
                "category": "timeout",
                "message": " provider timed out ",
                "retryable": true,
                "details": {},
                "causeChain": [" upstream request "]
            }
        });
        let result = evaluate_case(&json!({
            "name": "preserved-human-text",
            "scenario": "normalize_outcome",
            "outcome": outcome,
        }));
        assert_eq!(result.get("ok").and_then(Value::as_bool), Some(true));
        assert_eq!(result.get("outcome"), Some(&outcome));
        Ok(())
    }

    #[test]
    fn value_payload_depth_matches_the_closed_reference_boundary() {
        let nested_array =
            |depth: usize| (0..depth).fold(Value::Null, |value, _| Value::Array(vec![value]));
        for (depth, expected_ok) in [(64, true), (65, false)] {
            let result = evaluate_case(&json!({
                "name": "value-depth-boundary",
                "scenario": "normalize_outcome",
                "outcome": {"status": "value", "value": nested_array(depth)},
            }));
            assert_eq!(result.get("ok").and_then(Value::as_bool), Some(expected_ok));
        }
    }

    #[test]
    fn execution_request_requires_a_canonical_case_name() -> Result<(), String> {
        let missing = evaluate_case(&json!({
            "scenario": "normalize_outcome",
            "outcome": {"status": "absent"},
        }));
        assert_eq!(
            missing.get("errorCategory").and_then(Value::as_str),
            Some("missing_field")
        );

        for name in ["", " padded", "bad\u{7f}name"] {
            let invalid = evaluate_case(&json!({
                "name": name,
                "scenario": "normalize_outcome",
                "outcome": {"status": "absent"},
            }));
            assert_eq!(
                invalid.get("errorCategory").and_then(Value::as_str),
                Some("invalid_identifier")
            );
        }
        Ok(())
    }
}
