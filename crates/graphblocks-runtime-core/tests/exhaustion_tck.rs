use std::collections::BTreeMap;

use graphblocks_runtime_core::budget::{BudgetPermit, UsageAmount};
use graphblocks_runtime_core::exhaustion::{
    ContinuationEnvelope, ExhaustionController, ExhaustionPolicy, ExhaustionPolicyError,
    ExhaustionPreset, ExhaustionUnit, WorkKind, validate_exhaustion_policy,
};
use serde_json::Value;

#[test]
fn rust_exhaustion_controller_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("../../../tck/exhaustion/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "exhaustion TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let name = required_str(case, "name", "exhaustion TCK case")?;
    let policy = exhaustion_policy(
        case.get("policy")
            .ok_or_else(|| format!("exhaustion TCK case {name} is missing policy"))?,
        name,
    )?;

    if let Some(validation) = case.get("validate") {
        assert_policy_validation(name, validation, &policy)?;
    }

    let atomic_unit = optional_str(case, "atomicUnit").unwrap_or("turn:1");
    let admission_epoch = optional_u64(case, "admissionEpoch").unwrap_or(7);
    let profile = policy
        .preset
        .map(ExhaustionPreset::as_str)
        .unwrap_or("finish_current_turn");
    let stored_permit = case
        .get("continuationPermit")
        .map(|value| budget_permit(value, name, profile, atomic_unit, admission_epoch))
        .transpose()?;
    let mut controller = ExhaustionController::new(policy, atomic_unit, admission_epoch);
    if let Some(validation_time) = optional_str(case, "validationTime") {
        controller = controller.with_validation_time(validation_time);
    }
    if let Some(permit) = stored_permit.clone() {
        controller = controller.with_continuation_permit(permit);
    }

    for operation in case
        .get("admissions")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let explicit_permit = match operation.get("permit") {
            Some(value) if value.is_object() => Some(budget_permit(
                value,
                name,
                profile,
                atomic_unit,
                admission_epoch,
            )?),
            _ => None,
        };
        let permit = match operation.get("permit") {
            Some(value) if value.is_object() => explicit_permit.as_ref(),
            Some(Value::String(raw)) if raw == "stored" => stored_permit.as_ref(),
            Some(Value::String(raw)) if raw == "none" => None,
            Some(Value::String(raw)) => {
                return Err(format!(
                    "exhaustion TCK case {name} has unknown permit reference {raw}"
                ));
            }
            Some(_) => {
                return Err(format!(
                    "exhaustion TCK case {name} permit must be string or object"
                ));
            }
            None => None,
        };
        let usage = operation
            .get("usage")
            .map(|value| usage_amounts(value, name))
            .transpose()?
            .unwrap_or_default();
        let decision = if usage.is_empty() {
            controller.admit(
                work_kind(required_str(operation, "workKind", name)?, name)?,
                required_u64(operation, "workEpoch", name)?,
                permit,
            )
        } else {
            controller.admit_with_usage(
                work_kind(required_str(operation, "workKind", name)?, name)?,
                required_u64(operation, "workEpoch", name)?,
                permit,
                usage,
            )
        };
        assert_eq!(
            decision.allowed,
            required_bool(operation, "allowed", name)?,
            "{name}"
        );
        assert_eq!(
            decision.reason,
            required_str(operation, "reason", name)?,
            "{name}"
        );
    }

    if let Some(expected) = case.get("expected") {
        if let Some(steps) = optional_u64(expected, "usedAdditionalSteps") {
            assert_eq!(controller.used_additional_steps, steps as u32, "{name}");
        }
    }
    Ok(())
}

fn assert_policy_validation(
    name: &str,
    validation: &Value,
    policy: &ExhaustionPolicy,
) -> Result<(), String> {
    let production = validation
        .get("production")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let result = validate_exhaustion_policy(policy, production);
    if let Some(expected_error) = optional_str(validation, "expectError") {
        match result {
            Ok(_) => {
                return Err(format!(
                    "exhaustion TCK case {name} expected validation error {expected_error}"
                ));
            }
            Err(error) => assert_eq!(
                exhaustion_policy_error_name(error),
                expected_error,
                "{name}"
            ),
        }
    } else {
        result.map_err(|error| {
            format!("exhaustion TCK case {name} validation failed unexpectedly: {error:?}")
        })?;
    }
    Ok(())
}

fn exhaustion_policy(value: &Value, name: &str) -> Result<ExhaustionPolicy, String> {
    let preset = exhaustion_preset(required_str(value, "preset", name)?, name)?;
    let unit = exhaustion_unit(required_str(value, "unit", name)?, name)?;
    let continuation = value
        .get("continuation")
        .map(|value| continuation_envelope(value, name))
        .transpose()?;
    Ok(ExhaustionPolicy::from_preset(preset, unit, continuation))
}

fn continuation_envelope(value: &Value, name: &str) -> Result<ContinuationEnvelope, String> {
    let mut envelope = ContinuationEnvelope::new();
    if let Some(work) = value.get("allowedWork") {
        envelope = envelope.with_allowed_work(work_kinds(work, name)?);
    }
    if let Some(work) = value.get("forbiddenWork") {
        envelope = envelope.with_forbidden_work(work_kinds(work, name)?);
    }
    if let Some(usage) = value.get("maxAdditionalUsage") {
        envelope = envelope.with_max_additional_usage(usage_amounts(usage, name)?);
    }
    if let Some(steps) = optional_u64(value, "maxAdditionalSteps") {
        envelope = envelope.with_max_additional_steps(steps as u32);
    }
    if let Some(deadline) = optional_str(value, "deadline") {
        envelope = envelope.with_deadline(deadline);
    }
    Ok(envelope)
}

fn budget_permit(
    value: &Value,
    name: &str,
    default_profile: &str,
    default_atomic_unit: &str,
    default_epoch: u64,
) -> Result<BudgetPermit, String> {
    let authorized_amounts = value
        .get("authorizedUsage")
        .map(|value| usage_amounts(value, name))
        .transpose()?
        .unwrap_or_else(|| vec![UsageAmount::new("model_output_tokens", 100, "tokens")]);
    Ok(BudgetPermit {
        permit_id: optional_str(value, "permitId")
            .unwrap_or("permit-1")
            .to_owned(),
        reservation_refs: vec!["reservation-1".to_owned()],
        owner: optional_str(value, "owner")
            .unwrap_or("worker:1")
            .to_owned(),
        atomic_unit: optional_str(value, "atomicUnit")
            .unwrap_or(default_atomic_unit)
            .to_owned(),
        admission_epoch: optional_u64(value, "admissionEpoch").unwrap_or(default_epoch),
        authorized_amounts,
        continuation_profile: optional_str(value, "continuationProfile")
            .unwrap_or(default_profile)
            .to_owned(),
        policy_snapshot_digest: "sha256:policy".to_owned(),
        expires_at: optional_str(value, "expiresAt")
            .unwrap_or("2026-06-22T01:00:00Z")
            .to_owned(),
        low_watermark: Vec::new(),
        fencing_tokens: BTreeMap::from([("budget-1".to_owned(), 1)]),
    })
}

fn usage_amounts(value: &Value, owner: &str) -> Result<Vec<UsageAmount>, String> {
    let amounts = value
        .as_array()
        .ok_or_else(|| format!("{owner} usage amounts must be an array"))?;
    amounts
        .iter()
        .map(|amount| {
            Ok(UsageAmount::new(
                required_str(amount, "kind", owner)?,
                required_i64(amount, "amount", owner)?,
                required_str(amount, "unit", owner)?,
            ))
        })
        .collect()
}

fn work_kinds(value: &Value, owner: &str) -> Result<Vec<WorkKind>, String> {
    let values = value
        .as_array()
        .ok_or_else(|| format!("{owner} work kind list must be an array"))?;
    values
        .iter()
        .map(|value| {
            work_kind(
                value
                    .as_str()
                    .ok_or_else(|| format!("{owner} work kind must be a string"))?,
                owner,
            )
        })
        .collect()
}

fn exhaustion_preset(raw: &str, name: &str) -> Result<ExhaustionPreset, String> {
    match raw {
        "finish_current_turn" => Ok(ExhaustionPreset::FinishCurrentTurn),
        "finish_current_call" => Ok(ExhaustionPreset::FinishCurrentCall),
        "finish_current_step" => Ok(ExhaustionPreset::FinishCurrentStep),
        "checkpoint_and_pause" => Ok(ExhaustionPreset::CheckpointAndPause),
        "hard_stop" => Ok(ExhaustionPreset::HardStop),
        "degrade_then_finalize" => Ok(ExhaustionPreset::DegradeThenFinalize),
        "request_extension" => Ok(ExhaustionPreset::RequestExtension),
        other => Err(format!(
            "exhaustion TCK case {name} has unknown preset {other}"
        )),
    }
}

fn exhaustion_unit(raw: &str, name: &str) -> Result<ExhaustionUnit, String> {
    match raw {
        "provider_call" => Ok(ExhaustionUnit::ProviderCall),
        "node" => Ok(ExhaustionUnit::Node),
        "agent_step" => Ok(ExhaustionUnit::AgentStep),
        "turn" => Ok(ExhaustionUnit::Turn),
        "map_item" => Ok(ExhaustionUnit::MapItem),
        "task" => Ok(ExhaustionUnit::Task),
        "trial" => Ok(ExhaustionUnit::Trial),
        "run" => Ok(ExhaustionUnit::Run),
        other => Err(format!(
            "exhaustion TCK case {name} has unknown unit {other}"
        )),
    }
}

fn work_kind(raw: &str, name: &str) -> Result<WorkKind, String> {
    match raw {
        "current_provider_call" => Ok(WorkKind::CurrentProviderCall),
        "already_admitted_child_work" => Ok(WorkKind::AlreadyAdmittedChildWork),
        "declared_finalization" => Ok(WorkKind::DeclaredFinalization),
        "checkpoint" => Ok(WorkKind::Checkpoint),
        "cleanup" => Ok(WorkKind::Cleanup),
        "read_only_tool" => Ok(WorkKind::ReadOnlyTool),
        "new_turn" => Ok(WorkKind::NewTurn),
        "plan_expansion" => Ok(WorkKind::PlanExpansion),
        "optional_task" => Ok(WorkKind::OptionalTask),
        "new_trial" => Ok(WorkKind::NewTrial),
        "state_changing_effect" => Ok(WorkKind::StateChangingEffect),
        "unreserved_provider_call" => Ok(WorkKind::UnreservedProviderCall),
        other => Err(format!(
            "exhaustion TCK case {name} has unknown work kind {other}"
        )),
    }
}

fn exhaustion_policy_error_name(error: ExhaustionPolicyError) -> &'static str {
    match error {
        ExhaustionPolicyError::MissingExhaustionBoundary => "missing_exhaustion_boundary",
    }
}

fn required_str<'a>(value: &'a Value, key: &str, owner: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{owner} is missing string field {key}"))
}

fn optional_str<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn required_bool(value: &Value, key: &str, owner: &str) -> Result<bool, String> {
    value
        .get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| format!("{owner} is missing boolean field {key}"))
}

fn required_i64(value: &Value, key: &str, owner: &str) -> Result<i64, String> {
    value
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("{owner} is missing integer field {key}"))
}

fn required_u64(value: &Value, key: &str, owner: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("{owner} is missing integer field {key}"))
}

fn optional_u64(value: &Value, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}
