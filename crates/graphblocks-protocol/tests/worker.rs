use std::collections::BTreeMap;

use graphblocks_protocol::{
    BlockCapability, WORKER_PROTOCOL_VERSION, WorkerAdmissionPolicy, WorkerAdvertisement,
    WorkerInvokeRequest, WorkerInvokeResult, WorkerProtocolError, WorkerSelectionError,
    WorkerState, admit_worker, admit_worker_with_policy, select_worker_for_block,
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
        block: "prompt.render@1".to_owned(),
        inputs: json!({"message": {"text": "Hello"}}),
        config: json!({"template": "Echo {message.text}"}),
    };
    let mut outputs = BTreeMap::new();
    outputs.insert("prompt".to_owned(), json!("Echo Hello"));
    let result = WorkerInvokeResult {
        invocation_id: request.invocation_id.clone(),
        outputs,
    };

    assert_eq!(
        serde_json::from_str::<WorkerInvokeRequest>(&serde_json::to_string(&request)?)?,
        request,
    );
    assert_eq!(
        serde_json::from_str::<WorkerInvokeResult>(&serde_json::to_string(&result)?)?,
        result,
    );
    Ok(())
}
