use std::collections::BTreeMap;

use graphblocks_protocol::{
    BlockCapability, RunOwnershipLease, WORKER_PROTOCOL_VERSION, WorkerAdmissionPolicy,
    WorkerAdvertisement, WorkerInvokeRequest, WorkerInvokeResult, WorkerProtocolError,
    WorkerResultError, WorkerSelectionError, WorkerState, admit_worker, admit_worker_with_policy,
    select_worker_for_block, validate_worker_result,
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
    assert_eq!(
        serde_json::from_str::<WorkerInvokeResult>(&serde_json::to_string(&result)?)?,
        result,
    );
    assert_eq!(validate_worker_result(&request, &result), Ok(()));
    Ok(())
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
