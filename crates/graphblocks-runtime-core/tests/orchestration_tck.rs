use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use graphblocks_runtime_core::budget::{BudgetPermit, UsageAmount};
use graphblocks_runtime_core::orchestration::{
    ChildBudgetDelegation, LeasePool, LeasePoolError, LeaseRequest, ModelPool, ModelProfile,
    ModelSelectionError, ModelSelectionRequest, TaskContextAccess, TaskContextAccessErrorReason,
    TaskPlan, TaskPlanError, TaskPlanLimits, TaskPlanPatch, TaskStep, WorkerProfile,
};
use serde_json::{Map, Value, json};

#[test]
fn orchestration_tck_cases_match_runtime_core() {
    let mut fixture_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    fixture_path.push("../../tck/orchestration/cases.json");
    let raw_fixture = fs::read_to_string(&fixture_path).expect("orchestration fixture is readable");
    let cases: Vec<Value> =
        serde_json::from_str(&raw_fixture).expect("orchestration fixture is valid");

    for raw_case in cases {
        let case = raw_case.as_object().expect("orchestration case is object");
        let name = required_str(case, &["name", "caseId", "case_id"]);
        let kind = required_str(case, &["kind"]);
        let observed = match kind.as_str() {
            "task_plan_patch" => {
                let base = plan_from(
                    case.get("base")
                        .and_then(Value::as_object)
                        .expect("base plan"),
                )
                .expect("base plan is valid");
                let patch = patch_from(
                    case.get("patch")
                        .and_then(Value::as_object)
                        .expect("plan patch"),
                );
                let updated = base.apply_patch(patch).expect("patch applies");
                let noop = updated
                    .apply_patch(TaskPlanPatch::new(
                        "noop",
                        &updated.plan_id,
                        updated.revision,
                    ))
                    .expect("noop patch applies");
                json!({
                    "revision": updated.revision,
                    "stepIds": updated.steps.iter().map(|step| step.step_id.as_str()).collect::<Vec<_>>(),
                    "draftDescription": updated.step("draft").expect("draft step").description,
                    "noopDigestStable": updated.content_digest() == noop.content_digest(),
                })
            }
            "task_plan_errors" => {
                let missing_error = plan_from(
                    case.get("missingDependencyPlan")
                        .and_then(Value::as_object)
                        .expect("missing dependency plan"),
                )
                .expect_err("missing dependency plan fails");
                let cycle_base = plan_from(
                    case.get("cycleBase")
                        .and_then(Value::as_object)
                        .expect("cycle base"),
                )
                .expect("cycle base is valid");
                let cycle_error = cycle_base
                    .apply_patch(patch_from(
                        case.get("cyclePatch")
                            .and_then(Value::as_object)
                            .expect("cycle patch"),
                    ))
                    .expect_err("cycle patch fails");
                let (missing_step, missing_dependency) = match &missing_error {
                    TaskPlanError::DependencyMissing {
                        step_id,
                        dependency_id,
                    } => (step_id.clone(), dependency_id.clone()),
                    other => panic!("unexpected missing dependency error {other:?}"),
                };
                let cycle = match &cycle_error {
                    TaskPlanError::Cycle { cycle } => cycle.clone(),
                    other => panic!("unexpected cycle error {other:?}"),
                };
                json!({
                    "missingDependencyError": task_plan_error_code(&missing_error),
                    "missingDependencyStep": missing_step,
                    "missingDependencyId": missing_dependency,
                    "cycleError": task_plan_error_code(&cycle_error),
                    "cycle": cycle,
                })
            }
            "context_access" => {
                let left = plan_from(
                    case.get("left")
                        .and_then(Value::as_object)
                        .expect("left plan"),
                )
                .expect("left plan is valid");
                let right = plan_from(
                    case.get("right")
                        .and_then(Value::as_object)
                        .expect("right plan"),
                )
                .expect("right plan is valid");
                let invalid_error = plan_from(
                    case.get("invalid")
                        .and_then(Value::as_object)
                        .expect("invalid plan"),
                )
                .expect_err("invalid context access fails");
                json!({
                    "sameDigest": left.content_digest() == right.content_digest(),
                    "orderedAccess": left.context_access.iter().map(|access| {
                        format!("{}:{}:{}", access.step_id, access.resource_id, access.mode)
                    }).collect::<Vec<_>>(),
                    "invalidError": task_plan_error_code(&invalid_error),
                })
            }
            "model_pool" => {
                let pool = model_pool_from(
                    case.get("pool")
                        .and_then(Value::as_object)
                        .expect("model pool"),
                );
                let worker = worker_profile_from(
                    case.get("worker")
                        .and_then(Value::as_object)
                        .expect("worker profile"),
                );
                let request = model_request_from(
                    worker.clone(),
                    case.get("request")
                        .and_then(Value::as_object)
                        .expect("model request"),
                );
                let selected = pool.select_model(&request).expect("model is selected");
                let invalid_error = pool
                    .select_model(&model_request_from(
                        worker,
                        case.get("invalidRequest")
                            .and_then(Value::as_object)
                            .expect("invalid model request"),
                    ))
                    .expect_err("invalid request fails");
                let invalid_tool = match &invalid_error {
                    ModelSelectionError::ToolNotAllowed { tool_name } => tool_name.clone(),
                    other => panic!("unexpected model error {other:?}"),
                };
                json!({
                    "selectedModel": selected.profile_id,
                    "selectedConnection": selected.connection,
                    "supportsUsageReport": selected.supports_usage_report,
                    "supportsCancellation": selected.supports_cancellation,
                    "invalidError": model_selection_error_code(&invalid_error),
                    "invalidTool": invalid_tool,
                })
            }
            "lease_pool" => {
                let pool = lease_pool_from(
                    case.get("pool")
                        .and_then(Value::as_object)
                        .expect("lease pool"),
                )
                .expect("lease pool is valid");
                let requests = case
                    .get("requests")
                    .and_then(Value::as_array)
                    .expect("lease requests");
                let first_request = requests[0].as_object().expect("first lease request");
                let second_request = requests[1].as_object().expect("second lease request");
                let (leased, first_grant) = pool
                    .acquire(
                        &lease_request_from(first_request),
                        required_str(first_request, &["leaseId", "lease_id"]),
                        required_str(first_request, &["acquiredAt", "acquired_at"]),
                        required_str(first_request, &["expiresAt", "expires_at"]),
                    )
                    .expect("first lease succeeds");
                let second_error = leased
                    .acquire(
                        &lease_request_from(second_request),
                        required_str(second_request, &["leaseId", "lease_id"]),
                        required_str(second_request, &["acquiredAt", "acquired_at"]),
                        required_str(second_request, &["expiresAt", "expires_at"]),
                    )
                    .expect_err("second lease fails");
                let release = case
                    .get("release")
                    .and_then(Value::as_object)
                    .expect("release request");
                let release_error = leased
                    .release(
                        required_str(release, &["leaseId", "lease_id"]),
                        required_u64(release, &["fencingEpoch", "fencing_epoch"]),
                    )
                    .expect_err("release fails with wrong fencing epoch");
                let (expected_epoch, actual_epoch) = match &release_error {
                    LeasePoolError::EpochMismatch {
                        expected_epoch,
                        actual_epoch,
                        ..
                    } => (*expected_epoch, *actual_epoch),
                    other => panic!("unexpected lease release error {other:?}"),
                };
                json!({
                    "firstLeaseEpoch": first_grant.fencing_epoch,
                    "secondError": lease_pool_error_code(&second_error),
                    "availableAfterFirst": leased.available_units(),
                    "releaseError": lease_pool_error_code(&release_error),
                    "expectedEpoch": expected_epoch,
                    "actualEpoch": actual_epoch,
                })
            }
            "child_budget_delegation" => {
                let parent_permit = budget_permit_from(
                    case.get("parentPermit")
                        .and_then(Value::as_object)
                        .expect("parent permit"),
                );
                let delegation = child_budget_delegation_from(
                    parent_permit,
                    case.get("delegation")
                        .and_then(Value::as_object)
                        .expect("delegation"),
                );
                let permit = delegation
                    .create_child_permit(required_str(case, &["childPermitId", "child_permit_id"]))
                    .expect("child permit is created");
                json!({
                    "permitId": permit.permit_id,
                    "owner": permit.owner,
                    "authorizedAmounts": permit.authorized_amounts.iter().map(usage_amount_value).collect::<Vec<_>>(),
                    "continuationProfile": permit.continuation_profile,
                    "reservationRefs": permit.reservation_refs,
                    "fencingTokens": permit.fencing_tokens,
                })
            }
            other => panic!("unsupported orchestration case kind {other:?}"),
        };

        let expected = case.get("expected").expect("expected result");
        assert_eq!(observed, *expected, "case {name} failed");
    }
}

fn required_str(mapping: &Map<String, Value>, keys: &[&str]) -> String {
    for key in keys {
        if let Some(value) = mapping.get(*key).and_then(Value::as_str) {
            return value.to_owned();
        }
    }
    panic!("missing required string field {keys:?}");
}

fn optional_str(mapping: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| mapping.get(*key).and_then(Value::as_str))
        .map(str::to_owned)
}

fn required_u64(mapping: &Map<String, Value>, keys: &[&str]) -> u64 {
    for key in keys {
        if let Some(value) = mapping.get(*key).and_then(Value::as_u64) {
            return value;
        }
    }
    panic!("missing required u64 field {keys:?}");
}

fn optional_u64(mapping: &Map<String, Value>, keys: &[&str], default: u64) -> u64 {
    keys.iter()
        .find_map(|key| mapping.get(*key).and_then(Value::as_u64))
        .unwrap_or(default)
}

fn optional_bool(mapping: &Map<String, Value>, keys: &[&str]) -> bool {
    keys.iter()
        .find_map(|key| mapping.get(*key).and_then(Value::as_bool))
        .unwrap_or(false)
}

fn string_list(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| item.as_str().expect("string list item").to_owned())
            .collect(),
        Some(Value::String(item)) => vec![item.to_owned()],
        _ => Vec::new(),
    }
}

fn task_step_from(raw: &Map<String, Value>) -> TaskStep {
    TaskStep::new(
        required_str(raw, &["stepId", "step_id"]),
        required_str(raw, &["description"]),
    )
    .with_depends_on(string_list(
        raw.get("dependsOn").or_else(|| raw.get("depends_on")),
    ))
}

fn task_context_access_from(raw: &Map<String, Value>) -> TaskContextAccess {
    let mut access = TaskContextAccess::new(
        required_str(raw, &["stepId", "step_id"]),
        required_str(raw, &["resourceId", "resource_id"]),
        required_str(raw, &["mode"]),
    );
    if let Some(reason) = optional_str(raw, &["reason"]) {
        access = access.with_reason(reason);
    }
    access
}

fn plan_from(raw: &Map<String, Value>) -> Result<TaskPlan, TaskPlanError> {
    TaskPlan::from_parts(
        required_str(raw, &["planId", "plan_id"]),
        required_str(raw, &["objective"]),
        optional_u64(raw, &["revision"], 1),
        raw.get("steps")
            .and_then(Value::as_array)
            .map(|steps| {
                steps
                    .iter()
                    .map(|step| task_step_from(step.as_object().expect("task step")))
                    .collect()
            })
            .unwrap_or_default(),
        BTreeMap::new(),
        TaskPlanLimits::default(),
        string_list(
            raw.get("contextResources")
                .or_else(|| raw.get("context_resources")),
        ),
        raw.get("contextAccess")
            .or_else(|| raw.get("context_access"))
            .and_then(Value::as_array)
            .map(|accesses| {
                accesses
                    .iter()
                    .map(|access| {
                        task_context_access_from(access.as_object().expect("context access"))
                    })
                    .collect()
            })
            .unwrap_or_default(),
    )
}

fn patch_from(raw: &Map<String, Value>) -> TaskPlanPatch {
    TaskPlanPatch::new(
        required_str(raw, &["patchId", "patch_id"]),
        required_str(raw, &["basePlanId", "base_plan_id"]),
        required_u64(raw, &["baseRevision", "base_revision"]),
    )
    .with_upsert_steps(
        raw.get("upsertSteps")
            .or_else(|| raw.get("upsert_steps"))
            .and_then(Value::as_array)
            .map(|steps| {
                steps
                    .iter()
                    .map(|step| task_step_from(step.as_object().expect("upsert task step")))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default(),
    )
    .with_remove_step_ids(string_list(
        raw.get("removeStepIds")
            .or_else(|| raw.get("remove_step_ids")),
    ))
    .with_created_at(optional_str(raw, &["createdAt", "created_at"]).unwrap_or_default())
}

fn task_plan_error_code(error: &TaskPlanError) -> &'static str {
    match error {
        TaskPlanError::DependencyMissing { .. } => "task_dependency_missing",
        TaskPlanError::Cycle { .. } => "task_cycle",
        TaskPlanError::ContextAccess {
            reason: TaskContextAccessErrorReason::InvalidMode,
            ..
        } => "context_access_invalid_mode",
        TaskPlanError::ContextAccess {
            reason: TaskContextAccessErrorReason::UnknownStep,
            ..
        } => "context_access_unknown_step",
        TaskPlanError::ContextAccess {
            reason: TaskContextAccessErrorReason::UnknownResource,
            ..
        } => "context_access_unknown_resource",
        TaskPlanError::DuplicateStep { .. } => "task_duplicate_step",
        TaskPlanError::Identity { .. } => "task_identity",
        TaskPlanError::Limit { .. } => "task_limit",
        TaskPlanError::PatchMismatch { .. } => "task_patch_mismatch",
        TaskPlanError::StepNotFound { .. } => "task_step_not_found",
    }
}

fn model_profile_from(raw: &Map<String, Value>) -> ModelProfile {
    ModelProfile::new(
        required_str(raw, &["profileId", "profile_id"]),
        required_str(raw, &["connection"]),
    )
    .with_capabilities(string_list(raw.get("capabilities")))
    .with_allowed_sensitivity(string_list(
        raw.get("allowedSensitivity")
            .or_else(|| raw.get("allowed_sensitivity")),
    ))
    .with_regions(string_list(raw.get("regions")))
    .with_usage_report(optional_bool(
        raw,
        &["supportsUsageReport", "supports_usage_report"],
    ))
    .with_cancellation(optional_bool(
        raw,
        &["supportsCancellation", "supports_cancellation"],
    ))
}

fn model_pool_from(raw: &Map<String, Value>) -> ModelPool {
    ModelPool::new(
        required_str(raw, &["poolId", "pool_id"]),
        required_str(raw, &["selectionPolicyRef", "selection_policy_ref"]),
    )
    .with_models(
        raw.get("models")
            .and_then(Value::as_array)
            .expect("model list")
            .iter()
            .map(|model| model_profile_from(model.as_object().expect("model profile")))
            .collect::<Vec<_>>(),
    )
}

fn worker_profile_from(raw: &Map<String, Value>) -> WorkerProfile {
    let mut worker = WorkerProfile::new(required_str(raw, &["profileId", "profile_id"]))
        .with_required_capabilities(string_list(
            raw.get("requiredCapabilities")
                .or_else(|| raw.get("required_capabilities")),
        ))
        .with_allowed_tools(string_list(
            raw.get("allowedTools").or_else(|| raw.get("allowed_tools")),
        ));
    if let Some(model_pool_ref) = optional_str(raw, &["modelPoolRef", "model_pool_ref"]) {
        worker = worker.with_model_pool_ref(model_pool_ref);
    }
    if let Some(sensitivity_ceiling) =
        optional_str(raw, &["sensitivityCeiling", "sensitivity_ceiling"])
    {
        worker = worker.with_sensitivity_ceiling(sensitivity_ceiling);
    }
    worker
}

fn model_request_from(worker: WorkerProfile, raw: &Map<String, Value>) -> ModelSelectionRequest {
    let mut request = ModelSelectionRequest::new(worker)
        .with_required_tools(string_list(
            raw.get("requiredTools")
                .or_else(|| raw.get("required_tools")),
        ))
        .with_required_capabilities(string_list(
            raw.get("requiredCapabilities")
                .or_else(|| raw.get("required_capabilities")),
        ));
    if let Some(sensitivity) = optional_str(raw, &["sensitivity"]) {
        request = request.with_sensitivity(sensitivity);
    }
    if let Some(region) = optional_str(raw, &["region"]) {
        request = request.with_region(region);
    }
    request
}

fn model_selection_error_code(error: &ModelSelectionError) -> &'static str {
    match error {
        ModelSelectionError::PoolMismatch { .. } => "pool_mismatch",
        ModelSelectionError::ToolNotAllowed { .. } => "tool_not_allowed",
        ModelSelectionError::SensitivityAboveCeiling { .. } => "sensitivity_above_ceiling",
        ModelSelectionError::NoEligibleModel { .. } => "no_eligible_model",
    }
}

fn lease_pool_from(raw: &Map<String, Value>) -> Result<LeasePool, LeasePoolError> {
    LeasePool::new(
        required_str(raw, &["poolId", "pool_id"]),
        required_str(raw, &["resourceKind", "resource_kind"]),
        required_u64(raw, &["capacityUnits", "capacity_units"]),
    )
}

fn lease_request_from(raw: &Map<String, Value>) -> LeaseRequest {
    LeaseRequest::new(
        required_str(raw, &["requestId", "request_id"]),
        required_str(raw, &["holder"]),
        required_str(raw, &["resourceKind", "resource_kind"]),
    )
    .with_units(optional_u64(raw, &["units"], 1))
}

fn lease_pool_error_code(error: &LeasePoolError) -> &'static str {
    match error {
        LeasePoolError::Capacity { .. } => "lease_pool_capacity",
        LeasePoolError::Exhausted { .. } => "lease_pool_exhausted",
        LeasePoolError::ResourceKindMismatch { .. } => "lease_resource_kind_mismatch",
        LeasePoolError::LeaseAlreadyExists { .. } => "lease_already_exists",
        LeasePoolError::LeaseNotFound { .. } => "lease_not_found",
        LeasePoolError::EpochMismatch { .. } => "lease_epoch_mismatch",
    }
}

fn usage_amount_from(raw: &Map<String, Value>) -> UsageAmount {
    let amount = required_str(raw, &["amount"])
        .parse::<i64>()
        .expect("usage amount is integer");
    UsageAmount::new(
        required_str(raw, &["kind"]),
        amount,
        required_str(raw, &["unit"]),
    )
}

fn usage_amount_value(amount: &UsageAmount) -> Value {
    json!({
        "kind": amount.kind,
        "amount": amount.amount.to_string(),
        "unit": amount.unit,
    })
}

fn usage_amounts_from(raw: &Map<String, Value>, keys: &[&str]) -> Vec<UsageAmount> {
    for key in keys {
        if let Some(items) = raw.get(*key).and_then(Value::as_array) {
            return items
                .iter()
                .map(|item| usage_amount_from(item.as_object().expect("usage amount")))
                .collect();
        }
    }
    Vec::new()
}

fn budget_permit_from(raw: &Map<String, Value>) -> BudgetPermit {
    BudgetPermit {
        permit_id: required_str(raw, &["permitId", "permit_id"]),
        reservation_refs: string_list(
            raw.get("reservationRefs")
                .or_else(|| raw.get("reservation_refs")),
        ),
        owner: required_str(raw, &["owner"]),
        atomic_unit: required_str(raw, &["atomicUnit", "atomic_unit"]),
        admission_epoch: required_u64(raw, &["admissionEpoch", "admission_epoch"]),
        authorized_amounts: usage_amounts_from(raw, &["authorizedAmounts", "authorized_amounts"]),
        continuation_profile: required_str(raw, &["continuationProfile", "continuation_profile"]),
        policy_snapshot_digest: required_str(
            raw,
            &["policySnapshotDigest", "policy_snapshot_digest"],
        ),
        expires_at: required_str(raw, &["expiresAt", "expires_at"]),
        low_watermark: Vec::new(),
        fencing_tokens: raw
            .get("fencingTokens")
            .or_else(|| raw.get("fencing_tokens"))
            .and_then(Value::as_object)
            .expect("fencing tokens")
            .iter()
            .map(|(key, value)| (key.clone(), value.as_u64().expect("fencing token")))
            .collect(),
    }
}

fn child_budget_delegation_from(
    parent_permit: BudgetPermit,
    raw: &Map<String, Value>,
) -> ChildBudgetDelegation {
    let mut delegation = ChildBudgetDelegation::new(
        required_str(raw, &["delegationId", "delegation_id"]),
        parent_permit,
        required_str(raw, &["childOwner", "child_owner"]),
        usage_amounts_from(raw, &["amounts"]),
        required_str(raw, &["expiresAt", "expires_at"]),
    );
    if let Some(continuation_profile) =
        optional_str(raw, &["continuationProfile", "continuation_profile"])
    {
        delegation = delegation.with_continuation_profile(continuation_profile);
    }
    delegation
}
