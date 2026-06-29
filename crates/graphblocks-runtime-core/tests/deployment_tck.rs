#![allow(clippy::panic)]

use std::fs;
use std::path::PathBuf;

use graphblocks_runtime_core::deployment::{
    DeploymentRevision, DeploymentSloProfile, DeploymentSloReport, GraphRelease, GraphReleaseError,
    GraphReleaseGraph, ImageRef, KnowledgeBinding, PromptLock, ReleaseLockRef, RevisionDecision,
    RolloutAnalysisResult, RolloutPlan, RolloutStep, SupplyChainLock, UpgradePolicy, WorkloadKind,
};
use serde_json::{Map, Value, json};

#[test]
fn deployment_tck_cases_match_runtime_core() {
    let mut fixture_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    fixture_path.push("../../tck/deployment/cases.json");
    let raw_fixture = fs::read_to_string(&fixture_path).expect("deployment fixture is readable");
    let cases: Vec<Value> =
        serde_json::from_str(&raw_fixture).expect("deployment fixture is valid");

    let required_str = |mapping: &Map<String, Value>, keys: &[&str]| -> String {
        for key in keys {
            if let Some(value) = mapping.get(*key).and_then(Value::as_str) {
                return value.to_owned();
            }
        }
        panic!("missing required string field {keys:?}");
    };
    let optional_str = |mapping: &Map<String, Value>, keys: &[&str]| -> Option<String> {
        keys.iter()
            .find_map(|key| mapping.get(*key).and_then(Value::as_str))
            .map(str::to_owned)
    };
    let string_list = |value: Option<&Value>| -> Vec<String> {
        match value {
            Some(Value::Array(items)) => items
                .iter()
                .map(|item| item.as_str().expect("string list item").to_owned())
                .collect(),
            Some(Value::String(item)) => vec![item.to_owned()],
            _ => Vec::new(),
        }
    };
    let revision_from = |mapping: &Map<String, Value>| -> DeploymentRevision {
        DeploymentRevision::new(
            required_str(mapping, &["revisionId", "revision_id"]),
            required_str(mapping, &["releaseDigest", "release_digest"]),
            required_str(mapping, &["deploymentSpecHash", "deployment_spec_hash"]),
            required_str(mapping, &["physicalPlanHash", "physical_plan_hash"]),
            required_str(mapping, &["resolvedBindingHash", "resolved_binding_hash"]),
            required_str(mapping, &["targetCapabilityHash", "target_capability_hash"]),
            required_str(mapping, &["createdAt", "created_at"]),
        )
    };
    let workload_from = |value: &str| -> WorkloadKind {
        match value {
            "new_request" => WorkloadKind::NewRequest,
            "existing_request" => WorkloadKind::ExistingRequest,
            "conversation" => WorkloadKind::Conversation,
            "durable_job" => WorkloadKind::DurableJob,
            "realtime_session" => WorkloadKind::RealtimeSession,
            other => panic!("unsupported workload kind {other:?}"),
        }
    };
    let revision_decision_contract = |decision: RevisionDecision| -> Value {
        match decision {
            RevisionDecision::AdmitOnNew { revision_id } => json!({
                "kind": "admit_on_new",
                "revisionId": revision_id,
                "fromRevisionId": null,
                "toRevisionId": null,
            }),
            RevisionDecision::FinishOnOld { revision_id } => json!({
                "kind": "finish_on_old",
                "revisionId": revision_id,
                "fromRevisionId": null,
                "toRevisionId": null,
            }),
            RevisionDecision::KeepAffinity { revision_id } => json!({
                "kind": "keep_affinity",
                "revisionId": revision_id,
                "fromRevisionId": null,
                "toRevisionId": null,
            }),
            RevisionDecision::CheckpointAndMigrate {
                from_revision_id,
                to_revision_id,
            } => json!({
                "kind": "checkpoint_and_migrate",
                "revisionId": null,
                "fromRevisionId": from_revision_id,
                "toRevisionId": to_revision_id,
            }),
            RevisionDecision::DrainOnOld { revision_id } => json!({
                "kind": "drain_on_old",
                "revisionId": revision_id,
                "fromRevisionId": null,
                "toRevisionId": null,
            }),
        }
    };

    for raw_case in cases {
        let case = raw_case.as_object().expect("deployment case is object");
        let name = required_str(case, &["name", "case_id", "caseId"]);
        let kind = required_str(case, &["kind"]);
        let observed = match kind.as_str() {
            "deployment_revision_digest" => {
                let left = revision_from(
                    case.get("left")
                        .and_then(Value::as_object)
                        .expect("left revision"),
                );
                let right = revision_from(
                    case.get("right")
                        .and_then(Value::as_object)
                        .expect("right revision"),
                );
                let changed = revision_from(
                    case.get("changed")
                        .and_then(Value::as_object)
                        .expect("changed revision"),
                );
                json!({
                    "sameDigest": left.content_digest() == right.content_digest(),
                    "changedDigestDifferent": left.content_digest() != changed.content_digest(),
                })
            }
            "release_pins" => {
                let raw_release = case
                    .get("release")
                    .and_then(Value::as_object)
                    .expect("release mapping");
                let mut release = GraphRelease::new(
                    required_str(raw_release, &["name"]),
                    required_str(raw_release, &["version"]),
                );
                if let (Some(bundle_digest), Some(bundle_media_type)) = (
                    optional_str(raw_release, &["bundleDigest", "bundle_digest"]),
                    optional_str(raw_release, &["bundleMediaType", "bundle_media_type"]),
                ) {
                    release = release.with_bundle(bundle_digest, bundle_media_type);
                }
                if let Some(application_hash) =
                    optional_str(raw_release, &["applicationHash", "application_hash"])
                {
                    release = release.with_application_hash(application_hash);
                }
                if let Some(graphs) = raw_release.get("graphs").and_then(Value::as_object) {
                    for (graph_name, raw_graph) in graphs {
                        let raw_graph = raw_graph.as_object().expect("graph release graph");
                        release = release.with_graph(
                            graph_name,
                            GraphReleaseGraph::new(
                                required_str(raw_graph, &["graphHash", "graph_hash"]),
                                required_str(
                                    raw_graph,
                                    &["normalizedPlanHash", "normalized_plan_hash"],
                                ),
                            ),
                        );
                    }
                }
                if let Some(images) = raw_release.get("images").and_then(Value::as_object) {
                    for (image_name, image) in images {
                        release = release.with_image(
                            image_name,
                            ImageRef::new(image.as_str().expect("image ref")),
                        );
                    }
                }
                if let Some(locks) = raw_release.get("locks").and_then(Value::as_object) {
                    for (lock_name, raw_lock) in locks {
                        let raw_lock = raw_lock.as_object().expect("release lock");
                        let mut lock =
                            ReleaseLockRef::new(required_str(raw_lock, &["ref", "reference"]));
                        if let Some(digest) = optional_str(raw_lock, &["digest"]) {
                            lock = lock.with_digest(digest);
                        }
                        if let Some(lock_type) = optional_str(raw_lock, &["lockType", "lock_type"])
                        {
                            lock = lock.with_lock_type(lock_type);
                        }
                        release = release.with_lock(lock_name, lock);
                    }
                }
                if let Some(knowledge) = raw_release.get("knowledge").and_then(Value::as_object) {
                    for (index_id, raw_binding) in knowledge {
                        let raw_binding = raw_binding.as_object().expect("knowledge binding");
                        release = release.with_knowledge(KnowledgeBinding::new(
                            index_id,
                            required_str(raw_binding, &["indexRevision", "index_revision"]),
                        ));
                    }
                }
                if let Some(prompt_locks) = raw_release
                    .get("promptLocks")
                    .or_else(|| raw_release.get("prompt_locks"))
                    .and_then(Value::as_object)
                {
                    for (prompt_name, raw_prompt) in prompt_locks {
                        let raw_prompt = raw_prompt.as_object().expect("prompt lock");
                        let prompt_lock = match required_str(raw_prompt, &["kind"]).as_str() {
                            "versioned" => PromptLock::versioned(
                                raw_prompt
                                    .get("name")
                                    .and_then(Value::as_str)
                                    .unwrap_or(prompt_name),
                                required_str(raw_prompt, &["version"]),
                            ),
                            "label" => PromptLock::label(
                                raw_prompt
                                    .get("name")
                                    .and_then(Value::as_str)
                                    .unwrap_or(prompt_name),
                                required_str(raw_prompt, &["label", "lockLabel", "lock_label"]),
                            ),
                            other => panic!("unsupported prompt lock kind {other:?}"),
                        };
                        release = release.with_prompt_lock(prompt_name, prompt_lock);
                    }
                }
                if let Some(supply_chain) = raw_release
                    .get("supplyChain")
                    .or_else(|| raw_release.get("supply_chain"))
                    .and_then(Value::as_object)
                {
                    release = release.with_supply_chain(SupplyChainLock {
                        sbom_ref: optional_str(supply_chain, &["sbomRef", "sbom_ref"]),
                        provenance_ref: optional_str(
                            supply_chain,
                            &["provenanceRef", "provenance_ref"],
                        ),
                        signature_policy: optional_str(
                            supply_chain,
                            &["signaturePolicy", "signature_policy"],
                        ),
                    });
                }
                match release.validate_production_pins() {
                    Ok(()) => json!({"error": null, "references": []}),
                    Err(GraphReleaseError::MutableReferences { references }) => {
                        json!({"error": "mutable_references", "references": references})
                    }
                }
            }
            "upgrade_policy" => {
                let policy = UpgradePolicy::workload_aware(
                    required_str(case, &["oldRevisionId", "old_revision_id"]),
                    required_str(case, &["newRevisionId", "new_revision_id"]),
                );
                let decisions = case
                    .get("decisions")
                    .and_then(Value::as_array)
                    .expect("upgrade decisions")
                    .iter()
                    .map(|raw_decision| {
                        let raw_decision = raw_decision.as_object().expect("upgrade decision");
                        revision_decision_contract(
                            policy.decide(
                                workload_from(&required_str(raw_decision, &["workload"])),
                                optional_str(
                                    raw_decision,
                                    &["affinityRevisionId", "affinity_revision_id"],
                                )
                                .as_deref(),
                                raw_decision
                                    .get("checkpointCompatible")
                                    .or_else(|| raw_decision.get("checkpoint_compatible"))
                                    .and_then(Value::as_bool)
                                    .unwrap_or(false),
                            ),
                        )
                    })
                    .collect::<Vec<_>>();
                json!({ "decisions": decisions })
            }
            "rollout_gate" => {
                let canary_steps = case
                    .get("canarySteps")
                    .or_else(|| case.get("canary_steps"))
                    .and_then(Value::as_array)
                    .expect("canary steps")
                    .iter()
                    .map(|raw_step| {
                        let raw_step = raw_step.as_object().expect("canary step");
                        let mut step = RolloutStep::canary(
                            required_str(raw_step, &["stepId", "step_id"]),
                            raw_step
                                .get("trafficPercent")
                                .or_else(|| raw_step.get("traffic_percent"))
                                .and_then(Value::as_u64)
                                .expect("trafficPercent") as u8,
                        );
                        if let Some(minimum_samples) = raw_step
                            .get("minimumSamples")
                            .or_else(|| raw_step.get("minimum_samples"))
                            .and_then(Value::as_u64)
                        {
                            step = step.with_minimum_samples(minimum_samples);
                        }
                        if let Some(minimum_duration_seconds) = raw_step
                            .get("minimumDurationSeconds")
                            .or_else(|| raw_step.get("minimum_duration_seconds"))
                            .and_then(Value::as_u64)
                        {
                            step = step.with_minimum_duration_seconds(minimum_duration_seconds);
                        }
                        step
                    })
                    .collect::<Vec<_>>();
                let plan = RolloutPlan::canary(
                    required_str(case, &["rolloutId", "rollout_id"]),
                    required_str(case, &["stableRevisionId", "stable_revision_id"]),
                    required_str(case, &["candidateRevisionId", "candidate_revision_id"]),
                    canary_steps,
                );
                let decisions = case
                    .get("evaluations")
                    .and_then(Value::as_array)
                    .expect("rollout evaluations")
                    .iter()
                    .map(|raw_evaluation| {
                        let raw_evaluation =
                            raw_evaluation.as_object().expect("rollout evaluation");
                        let state = plan
                            .initial_state()
                            .advance_for_test(
                                raw_evaluation
                                    .get("currentStepIndex")
                                    .or_else(|| raw_evaluation.get("current_step_index"))
                                    .and_then(Value::as_u64)
                                    .expect("currentStepIndex")
                                    as usize,
                            )
                            .expect("current step is valid");
                        let mut result = if raw_evaluation
                            .get("passed")
                            .and_then(Value::as_bool)
                            .unwrap_or(false)
                        {
                            RolloutAnalysisResult::passed(required_str(
                                raw_evaluation,
                                &["stepId", "step_id"],
                            ))
                        } else {
                            RolloutAnalysisResult::failed(
                                required_str(raw_evaluation, &["stepId", "step_id"]),
                                raw_evaluation
                                    .get("reason")
                                    .and_then(Value::as_str)
                                    .unwrap_or("analysis_failed"),
                            )
                        };
                        result = result
                            .with_sample_count(
                                raw_evaluation
                                    .get("sampleCount")
                                    .or_else(|| raw_evaluation.get("sample_count"))
                                    .and_then(Value::as_u64)
                                    .unwrap_or(0),
                            )
                            .with_duration_seconds(
                                raw_evaluation
                                    .get("durationSeconds")
                                    .or_else(|| raw_evaluation.get("duration_seconds"))
                                    .and_then(Value::as_u64)
                                    .unwrap_or(0),
                            )
                            .with_non_reversible_effect_observed(
                                raw_evaluation
                                    .get("nonReversibleEffectObserved")
                                    .or_else(|| {
                                        raw_evaluation.get("non_reversible_effect_observed")
                                    })
                                    .and_then(Value::as_bool)
                                    .unwrap_or(false),
                            );
                        let decision = state.evaluate_gate(result).expect("rollout gate evaluates");
                        json!({
                            "decision": decision.decision,
                            "reason": decision.reason,
                            "nextStepIndex": decision.next_state.current_step_index,
                            "nextStatus": decision.next_state.status,
                            "automaticRollbackAllowed": decision.automatic_rollback_allowed,
                        })
                    })
                    .collect::<Vec<_>>();
                json!({ "decisions": decisions })
            }
            "slo_condition" => {
                let profile = DeploymentSloProfile::new(
                    required_str(case, &["profileId", "profile_id"]),
                    string_list(case.get("objectives")),
                );
                let conditions = case
                    .get("evaluations")
                    .and_then(Value::as_array)
                    .expect("SLO evaluations")
                    .iter()
                    .map(|raw_evaluation| {
                        let raw_evaluation = raw_evaluation.as_object().expect("SLO evaluation");
                        let reports = raw_evaluation
                            .get("reports")
                            .and_then(Value::as_array)
                            .expect("SLO reports")
                            .iter()
                            .map(|raw_report| {
                                let raw_report = raw_report.as_object().expect("SLO report");
                                DeploymentSloReport {
                                    slo_id: required_str(raw_report, &["sloId", "slo_id"]),
                                    status: required_str(raw_report, &["status"]),
                                }
                            })
                            .collect::<Vec<_>>();
                        profile.evaluate_slo_reports(reports).condition_contract()
                    })
                    .collect::<Vec<_>>();
                json!({ "conditions": conditions })
            }
            other => panic!("unsupported deployment TCK case kind {other:?}"),
        };

        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .expect("expected object");
        for (key, expected_value) in expected {
            assert_eq!(
                observed.get(key),
                Some(expected_value),
                "{name} expected {key}"
            );
        }
    }
}
