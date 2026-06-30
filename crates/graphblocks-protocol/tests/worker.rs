use std::collections::BTreeMap;

use graphblocks_protocol::{
    BlockCapability, RunOwnershipLease, WORKER_PROTOCOL_VERSION, WorkerAdmissionPolicy,
    WorkerAdvertisement, WorkerInvocationContext, WorkerInvocationContextError,
    WorkerInvokeRequest, WorkerInvokeRequestError, WorkerInvokeResult, WorkerInvokeResultError,
    WorkerProtocolError, WorkerResultError, WorkerSelectionError, WorkerState, admit_worker,
    admit_worker_with_policy, evaluate_worker_admission, select_worker_for_block,
    validate_worker_result,
};
use serde_json::json;

#[test]
fn worker_advertisement_round_trips_and_admits_current_protocol() -> Result<(), serde_json::Error> {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [
            BlockCapability::new("prompt.render@1"),
            BlockCapability::new("model.generate@1"),
        ],
    );

    let encoded = serde_json::to_value(&advertisement)?;
    let decoded = serde_json::from_value::<WorkerAdvertisement>(encoded.clone())?;

    assert_eq!(encoded["targetId"], json!("doc-cpu"));
    assert_eq!(encoded["packageLockHash"], json!("sha256:package-lock"));
    assert_eq!(encoded["imageDigest"], json!("sha256:image"));
    assert_eq!(encoded["state"], json!("ready"));
    assert_eq!(decoded.protocol_version, WORKER_PROTOCOL_VERSION);
    assert_eq!(decoded.worker_id, "worker-local-1");
    assert_eq!(decoded.target_id, "doc-cpu");
    assert_eq!(decoded.supported_blocks.len(), 2);
    assert_eq!(admit_worker(&decoded), Ok(()));
    Ok(())
}

#[test]
fn worker_admission_rejects_incompatible_protocol_version() {
    let mut advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    );
    advertisement.protocol_version = WORKER_PROTOCOL_VERSION + 1;

    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::IncompatibleVersion {
            expected: WORKER_PROTOCOL_VERSION,
            actual: WORKER_PROTOCOL_VERSION + 1,
        }),
    );
}

#[test]
fn worker_admission_rejects_incompatible_package_lock() {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:actual-package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    );
    let policy = WorkerAdmissionPolicy::current().require_package_lock_hash("sha256:expected-lock");

    assert_eq!(
        admit_worker_with_policy(&policy, &advertisement),
        Err(WorkerProtocolError::IncompatiblePackageLock {
            expected: "sha256:expected-lock".to_owned(),
            actual: "sha256:actual-package-lock".to_owned(),
        }),
    );
}

#[test]
fn worker_admission_rejects_whitespace_advertisement_identity_fields() {
    let mut advertisement = WorkerAdvertisement::new(
        " ",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    );
    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyWorkerId),
    );

    advertisement.worker_id = "worker-local-1".to_owned();
    advertisement.target_id = " ".to_owned();
    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyTargetId),
    );

    advertisement.target_id = "doc-cpu".to_owned();
    advertisement.package_lock_hash = " ".to_owned();
    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyPackageLockHash),
    );

    advertisement.package_lock_hash = "sha256:package-lock".to_owned();
    advertisement.image_digest = " ".to_owned();
    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyImageDigest),
    );
}

#[test]
fn worker_admission_rejects_blank_block_capability() {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new(" ")],
    );

    assert_eq!(
        admit_worker(&advertisement),
        Err(WorkerProtocolError::EmptyBlockCapability),
    );
}

#[test]
fn worker_admission_decision_reports_drain_and_missing_capability() -> Result<(), serde_json::Error>
{
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    )
    .with_state(WorkerState::Draining);
    let policy = WorkerAdmissionPolicy::current()
        .require_package_lock_hash("sha256:package-lock")
        .require_block("model.generate@1");

    let decision = evaluate_worker_admission(&policy, &advertisement);

    assert!(!decision.admitted);
    assert_eq!(decision.worker_id, "worker-local-1");
    assert_eq!(decision.target_id, "doc-cpu");
    assert_eq!(decision.required_block.as_deref(), Some("model.generate@1"));
    assert_eq!(
        decision.reason_codes,
        vec![
            "worker.not_ready".to_owned(),
            "worker.missing_required_block".to_owned(),
        ],
    );
    let encoded = serde_json::to_value(&decision)?;
    assert_eq!(
        encoded["reasonCodes"],
        json!(["worker.not_ready", "worker.missing_required_block"]),
    );
    assert_eq!(
        serde_json::from_value::<graphblocks_protocol::WorkerAdmissionDecision>(encoded)?,
        decision,
    );
    Ok(())
}

#[test]
fn worker_admission_decision_reports_blank_capabilities_and_trimmed_identity_fields() {
    let advertisement = WorkerAdvertisement::new(" ", " ", " ", " ", [BlockCapability::new(" ")]);

    let decision = evaluate_worker_admission(&WorkerAdmissionPolicy::current(), &advertisement);

    assert!(!decision.admitted);
    assert_eq!(
        decision.reason_codes,
        vec![
            "worker.empty_worker_id".to_owned(),
            "worker.empty_target_id".to_owned(),
            "worker.empty_package_lock_hash".to_owned(),
            "worker.empty_image_digest".to_owned(),
            "worker.empty_block_capability".to_owned(),
        ],
    );
}

#[test]
fn worker_admission_decision_allows_ready_matching_worker() {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("prompt.render@1")],
    );
    let policy = WorkerAdmissionPolicy::current().require_block("prompt.render@1");

    let decision = evaluate_worker_admission(&policy, &advertisement);

    assert!(decision.admitted);
    assert!(decision.reason_codes.is_empty());
}

#[test]
fn worker_selection_skips_draining_and_saturated_workers() {
    let ready_late = WorkerAdvertisement::new(
        "worker-z",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-z",
        [BlockCapability::new("model.generate@1")],
    );
    let draining = WorkerAdvertisement::new(
        "worker-a",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability::new("model.generate@1")],
    )
    .with_state(WorkerState::Draining);
    let saturated = WorkerAdvertisement::new(
        "worker-b",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-b",
        [BlockCapability::new("model.generate@1")],
    )
    .with_state(WorkerState::Saturated);
    let ready_early = WorkerAdvertisement::new(
        "worker-c",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-c",
        [BlockCapability::new("model.generate@1")],
    );
    let workers = [ready_late, draining, saturated, ready_early];

    let selected = select_worker_for_block(workers.iter(), "model.generate@1")
        .expect("a ready worker should be eligible");

    assert_eq!(selected.worker_id, "worker-c");
}

#[test]
fn worker_selection_skips_invalid_advertisements() {
    let blank_worker_id = WorkerAdvertisement::new(
        " ",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability::new("model.generate@1")],
    );
    let blank_capability = WorkerAdvertisement::new(
        "worker-a",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-b",
        [BlockCapability::new(" ")],
    );
    let valid = WorkerAdvertisement::new(
        "worker-b",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image-c",
        [BlockCapability::new("model.generate@1")],
    );
    let workers = [blank_worker_id, blank_capability, valid];

    let selected = select_worker_for_block(workers.iter(), "model.generate@1")
        .expect("the valid worker should be eligible");

    assert_eq!(selected.worker_id, "worker-b");
}

#[test]
fn worker_selection_reports_when_no_ready_worker_supports_block() {
    let workers = [
        WorkerAdvertisement::new(
            "worker-a",
            "model-cpu",
            "sha256:package-lock",
            "sha256:image-a",
            [BlockCapability::new("model.generate@1")],
        )
        .with_state(WorkerState::Draining),
        WorkerAdvertisement::new(
            "worker-b",
            "model-cpu",
            "sha256:package-lock",
            "sha256:image-b",
            [BlockCapability::new("prompt.render@1")],
        ),
    ];

    assert_eq!(
        select_worker_for_block(workers.iter(), "model.generate@1"),
        Err(WorkerSelectionError::NoEligibleWorker {
            block: "model.generate@1".to_owned(),
        }),
    );
}

#[test]
fn worker_invocation_envelopes_preserve_json_payloads() -> Result<(), serde_json::Error> {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };
    let mut outputs = BTreeMap::new();
    outputs.insert("prompt".to_owned(), json!("Echo Hello"));
    let result = WorkerInvokeResult {
        invocation_id: request.invocation_id.clone(),
        node_attempt_id: request.node_attempt_id.clone(),
        lease_epoch: request.lease_epoch,
        outputs,
    };

    let encoded_request = serde_json::to_value(&request)?;
    assert_eq!(encoded_request["nodeAttemptId"], json!("render-attempt-1"));
    assert_eq!(encoded_request["leaseEpoch"], json!(7));
    assert_eq!(
        serde_json::from_value::<WorkerInvokeRequest>(encoded_request)?,
        request,
    );
    assert_eq!(request.validate(), Ok(()));
    assert_eq!(
        serde_json::from_str::<WorkerInvokeResult>(&serde_json::to_string(&result)?)?,
        result,
    );
    assert_eq!(result.validate(), Ok(()));
    assert_eq!(validate_worker_result(&request, &result), Ok(()));
    Ok(())
}

#[test]
fn worker_invocation_context_round_trips_release_policy_budget_and_trace()
-> Result<(), serde_json::Error> {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000002".to_owned(),
        run_id: "run-000002".to_owned(),
        node_id: "generate".to_owned(),
        node_attempt_id: "generate-attempt-1".to_owned(),
        lease_epoch: 11,
        block: "model.generate@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1")
            .with_trace("trace-1", "span-parent")
            .with_policy_snapshot("policy-snapshot-1", "sha256:policy")
            .with_budget_permit("permit-1", "sha256:budget-permit")
            .with_attribute("tenant", "acme"),
        inputs: json!({"prompt": "Hello"}),
        config: json!({"model": "scripted"}),
    };

    let encoded = serde_json::to_value(&request)?;

    assert_eq!(encoded["context"]["releaseId"], json!("release-1"));
    assert_eq!(encoded["context"]["deploymentRevisionId"], json!("rev-1"));
    assert_eq!(encoded["context"]["traceId"], json!("trace-1"));
    assert_eq!(encoded["context"]["parentSpanId"], json!("span-parent"));
    assert_eq!(
        encoded["context"]["policySnapshotId"],
        json!("policy-snapshot-1")
    );
    assert_eq!(encoded["context"]["budgetPermitId"], json!("permit-1"));
    assert_eq!(encoded["context"]["attributes"]["tenant"], json!("acme"));
    assert_eq!(
        serde_json::from_value::<WorkerInvokeRequest>(encoded)?,
        request,
    );
    assert_eq!(request.context.validate(), Ok(()));
    Ok(())
}

#[test]
fn worker_invocation_context_validation_rejects_empty_required_fields() {
    let mut context = WorkerInvocationContext::new(" ", "rev-1");
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyRequiredField {
            field: "release_id".to_owned(),
        }),
    );

    context.release_id = "release-1".to_owned();
    context.deployment_revision_id.clear();
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyRequiredField {
            field: "deployment_revision_id".to_owned(),
        }),
    );
}

#[test]
fn worker_invocation_context_validation_rejects_empty_optional_fields() {
    let mut context = WorkerInvocationContext::new("release-1", "rev-1");
    context.trace_id = Some(" ".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyOptionalField {
            field: "trace_id".to_owned(),
        }),
    );

    context.trace_id = Some("trace-1".to_owned());
    context.policy_snapshot_digest = Some(String::new());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyOptionalField {
            field: "policy_snapshot_digest".to_owned(),
        }),
    );
}

#[test]
fn worker_invocation_context_validation_requires_bound_policy_and_budget_pairs() {
    let mut context = WorkerInvocationContext::new("release-1", "rev-1");
    context.policy_snapshot_id = Some("policy-snapshot-1".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::MissingPolicySnapshotDigest),
    );

    context.policy_snapshot_id = None;
    context.policy_snapshot_digest = Some("sha256:policy".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::MissingPolicySnapshotId),
    );

    context.policy_snapshot_id = Some("policy-snapshot-1".to_owned());
    context.budget_permit_id = Some("permit-1".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::MissingBudgetPermitDigest),
    );

    context.budget_permit_id = None;
    context.budget_permit_digest = Some("sha256:budget".to_owned());
    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::MissingBudgetPermitId),
    );
}

#[test]
fn worker_invocation_context_validation_rejects_empty_attribute_keys() {
    let context =
        WorkerInvocationContext::new("release-1", "rev-1").with_attribute(" ", "tenant-acme");

    assert_eq!(
        context.validate(),
        Err(WorkerInvocationContextError::EmptyAttributeKey),
    );
}

#[test]
fn worker_invoke_request_validation_rejects_blank_envelope_fields() {
    let mut request = WorkerInvokeRequest {
        invocation_id: " ".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };

    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "invocation_id".to_owned(),
        }),
    );

    request.invocation_id = "invoke-000001".to_owned();
    request.run_id.clear();
    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "run_id".to_owned(),
        }),
    );

    request.run_id = "run-000001".to_owned();
    request.node_id = " ".to_owned();
    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "node_id".to_owned(),
        }),
    );

    request.node_id = "render".to_owned();
    request.node_attempt_id.clear();
    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "node_attempt_id".to_owned(),
        }),
    );

    request.node_attempt_id = "render-attempt-1".to_owned();
    request.block = " ".to_owned();
    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::EmptyField {
            field: "block".to_owned(),
        }),
    );
}

#[test]
fn worker_invoke_request_validation_rejects_invalid_context() {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", " "),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };

    assert_eq!(
        request.validate(),
        Err(WorkerInvokeRequestError::InvalidContext {
            source: WorkerInvocationContextError::EmptyRequiredField {
                field: "deployment_revision_id".to_owned(),
            },
        }),
    );
}

#[test]
fn worker_invoke_result_validation_rejects_blank_envelope_fields_and_outputs() {
    let mut outputs = BTreeMap::new();
    outputs.insert("prompt".to_owned(), json!("Echo Hello"));
    let mut result = WorkerInvokeResult {
        invocation_id: " ".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        outputs,
    };

    assert_eq!(
        result.validate(),
        Err(WorkerInvokeResultError::EmptyField {
            field: "invocation_id".to_owned(),
        }),
    );

    result.invocation_id = "invoke-000001".to_owned();
    result.node_attempt_id.clear();
    assert_eq!(
        result.validate(),
        Err(WorkerInvokeResultError::EmptyField {
            field: "node_attempt_id".to_owned(),
        }),
    );

    result.node_attempt_id = "render-attempt-1".to_owned();
    result.outputs.clear();
    result.outputs.insert(" ".to_owned(), json!("Echo Hello"));
    assert_eq!(
        result.validate(),
        Err(WorkerInvokeResultError::EmptyOutputKey)
    );
}

#[test]
fn worker_result_validation_rejects_invalid_request_or_result_envelopes() {
    let mut request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: " ".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };
    let mut outputs = BTreeMap::new();
    outputs.insert("prompt".to_owned(), json!("Echo Hello"));
    let mut result = WorkerInvokeResult {
        invocation_id: "invoke-000001".to_owned(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: 7,
        outputs,
    };

    assert_eq!(
        validate_worker_result(&request, &result),
        Err(WorkerResultError::InvalidRequest {
            source: WorkerInvokeRequestError::EmptyField {
                field: "run_id".to_owned(),
            },
        }),
    );

    request.run_id = "run-000001".to_owned();
    result.outputs.clear();
    result.outputs.insert(" ".to_owned(), json!("Echo Hello"));
    assert_eq!(
        validate_worker_result(&request, &result),
        Err(WorkerResultError::InvalidResult {
            source: WorkerInvokeResultError::EmptyOutputKey,
        }),
    );
}

#[test]
fn run_ownership_lease_round_trips_with_fencing_epoch() -> Result<(), serde_json::Error> {
    let lease = RunOwnershipLease {
        run_id: "run-000001".to_owned(),
        owner_instance_id: "control-plane-a".to_owned(),
        lease_epoch: 42,
        expires_at_unix_ms: 1_820_000_000_000,
        last_checkpoint: Some("checkpoint-000004".to_owned()),
    };

    let encoded = serde_json::to_value(&lease)?;
    assert_eq!(encoded["leaseEpoch"], json!(42));
    assert_eq!(encoded["expiresAtUnixMs"], json!(1_820_000_000_000u64));

    assert_eq!(serde_json::from_value::<RunOwnershipLease>(encoded)?, lease);
    Ok(())
}

#[test]
fn worker_result_validation_rejects_mismatched_attempt_or_lease_epoch() {
    let request = WorkerInvokeRequest {
        invocation_id: "invoke-000001".to_owned(),
        run_id: "run-000001".to_owned(),
        node_id: "render".to_owned(),
        node_attempt_id: "render-attempt-2".to_owned(),
        lease_epoch: 9,
        block: "prompt.render@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-1"),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };
    let mut mismatched_attempt = WorkerInvokeResult {
        invocation_id: request.invocation_id.clone(),
        node_attempt_id: "render-attempt-1".to_owned(),
        lease_epoch: request.lease_epoch,
        outputs: BTreeMap::new(),
    };

    assert_eq!(
        validate_worker_result(&request, &mismatched_attempt),
        Err(WorkerResultError::MismatchedNodeAttempt {
            expected: "render-attempt-2".to_owned(),
            actual: "render-attempt-1".to_owned(),
        }),
    );

    mismatched_attempt.node_attempt_id = request.node_attempt_id.clone();
    mismatched_attempt.lease_epoch = 8;

    assert_eq!(
        validate_worker_result(&request, &mismatched_attempt),
        Err(WorkerResultError::StaleLeaseEpoch {
            expected: 9,
            actual: 8,
        }),
    );
}
