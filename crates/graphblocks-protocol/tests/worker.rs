use std::collections::BTreeMap;

use graphblocks_protocol::{
    BlockCapability, WORKER_PROTOCOL_VERSION, WorkerAdvertisement, WorkerInvokeRequest,
    WorkerInvokeResult, WorkerProtocolError, admit_worker,
};
use serde_json::json;

#[test]
fn worker_advertisement_round_trips_and_admits_current_protocol() -> Result<(), serde_json::Error> {
    let advertisement = WorkerAdvertisement::new(
        "worker-local-1",
        [
            BlockCapability::new("prompt.render@1"),
            BlockCapability::new("model.generate@1"),
        ],
    );

    let encoded = serde_json::to_string(&advertisement)?;
    let decoded = serde_json::from_str::<WorkerAdvertisement>(&encoded)?;

    assert_eq!(decoded.protocol_version, WORKER_PROTOCOL_VERSION);
    assert_eq!(decoded.worker_id, "worker-local-1");
    assert_eq!(decoded.supported_blocks.len(), 2);
    assert_eq!(admit_worker(&decoded), Ok(()));
    Ok(())
}

#[test]
fn worker_admission_rejects_incompatible_protocol_version() {
    let mut advertisement =
        WorkerAdvertisement::new("worker-local-1", [BlockCapability::new("prompt.render@1")]);
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
