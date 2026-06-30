use std::io::Write;
use std::process::{Command, Stdio};

use graphblocks_protocol::{
    BlockCapability, WORKER_PROTOCOL_VERSION, WorkerAdvertisement, WorkerDrainDisposition,
    WorkerDrainPlan, WorkerDrainPolicy, WorkerDrainTask, WorkerDrainWorkloadKind,
    WorkerInvocationContext, WorkerInvokeRequest, WorkerProtocolErrorPayload,
    WorkerProtocolMessage, WorkerProtocolMessageKind, WorkerProtocolMessagePayload, WorkerState,
};
use graphblocksd::{DaemonConfig, DaemonConfigError, WorkerRegistry, WorkerRegistryError};
use serde_json::json;

#[test]
fn daemon_config_validates_identity_protocol_and_capacity() {
    assert_eq!(
        DaemonConfig::new(" ", "127.0.0.1:8080").validate(),
        Err(DaemonConfigError::EmptyDaemonId)
    );
    assert_eq!(
        DaemonConfig::new("daemon-1", " ").validate(),
        Err(DaemonConfigError::EmptyBindAddress)
    );
    assert_eq!(
        DaemonConfig::new("daemon-1", "127.0.0.1:8080")
            .with_max_workers(0)
            .validate(),
        Err(DaemonConfigError::ZeroMaxWorkers)
    );
    assert_eq!(
        DaemonConfig::new("daemon-1", "127.0.0.1:8080")
            .with_protocol_version(WORKER_PROTOCOL_VERSION + 1)
            .validate(),
        Err(DaemonConfigError::UnsupportedProtocolVersion {
            expected: WORKER_PROTOCOL_VERSION,
            actual: WORKER_PROTOCOL_VERSION + 1,
        })
    );
}

#[test]
fn worker_registry_admits_ready_workers_and_reports_status() -> Result<(), DaemonConfigError> {
    let config = DaemonConfig::new("daemon-1", "127.0.0.1:8080")
        .require_package_lock_hash("sha256:package-lock")
        .with_max_workers(4);
    let mut registry = WorkerRegistry::new(config)?;
    let advertisement = WorkerAdvertisement::new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    );

    let decision = registry.admit_worker(advertisement);
    let status = registry.status();

    assert!(decision.admitted);
    assert_eq!(registry.ready_worker_ids(), vec!["worker-1"]);
    assert_eq!(status.daemon_id, "daemon-1");
    assert_eq!(status.ready_workers, 1);
    assert_eq!(status.saturated_workers, 0);
    assert_eq!(status.draining_workers, 0);
    assert_eq!(status.admitted_workers, 1);
    assert_eq!(status.rejected_workers, 0);
    assert_eq!(status.protocol_version, WORKER_PROTOCOL_VERSION);
    Ok(())
}

#[test]
fn worker_registry_admits_worker_advertisement_messages() -> Result<(), WorkerRegistryError> {
    let config = DaemonConfig::new("daemon-1", "127.0.0.1:8080")
        .require_package_lock_hash("sha256:package-lock");
    let mut registry = WorkerRegistry::new(config).expect("daemon config should be valid");
    let advertisement = WorkerAdvertisement::new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    );
    let message = WorkerProtocolMessage::advertisement("message-worker-1", 1, advertisement)
        .with_correlation_id("worker-1");

    let response = registry.admit_worker_message(message, "message-daemon-1", 2)?;

    assert_eq!(response.kind, WorkerProtocolMessageKind::AdmissionDecision);
    assert_eq!(response.correlation_id.as_deref(), Some("worker-1"));
    assert_eq!(response.causation_id.as_deref(), Some("message-worker-1"));
    assert_eq!(registry.ready_worker_ids(), vec!["worker-1"]);
    assert!(matches!(
        response.payload,
        WorkerProtocolMessagePayload::AdmissionDecision(_)
    ));
    if let WorkerProtocolMessagePayload::AdmissionDecision(decision) = response.payload {
        assert!(decision.admitted);
        assert_eq!(decision.worker_id, "worker-1");
    }
    Ok(())
}

#[test]
fn worker_registry_returns_denial_message_for_incompatible_worker_advertisement_protocol() {
    let mut registry = WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080"))
        .expect("daemon config should be valid");
    let mut advertisement = WorkerAdvertisement::new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    );
    advertisement.protocol_version = WORKER_PROTOCOL_VERSION + 1;
    let message = WorkerProtocolMessage::advertisement("message-worker-1", 1, advertisement)
        .with_correlation_id("worker-1");

    let response = registry
        .admit_worker_message(message, "message-daemon-1", 2)
        .expect("advertisement protocol mismatch should produce an admission denial");
    let status = registry.status();

    assert_eq!(response.kind, WorkerProtocolMessageKind::AdmissionDecision);
    assert_eq!(response.causation_id.as_deref(), Some("message-worker-1"));
    assert!(matches!(
        response.payload,
        WorkerProtocolMessagePayload::AdmissionDecision(_)
    ));
    if let WorkerProtocolMessagePayload::AdmissionDecision(decision) = response.payload {
        assert!(!decision.admitted);
        assert_eq!(
            decision.reason_codes,
            vec!["worker.incompatible_protocol_version"]
        );
    }
    assert_eq!(status.admitted_workers, 0);
    assert_eq!(status.rejected_workers, 1);
}

#[test]
fn wire_admission_denies_incompatible_worker_protocol() -> Result<(), WorkerRegistryError> {
    let mut registry = WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080"))
        .expect("daemon config should be valid");
    let message = json!({
        "protocolVersion": WORKER_PROTOCOL_VERSION,
        "messageId": "message-worker-1",
        "kind": "advertisement",
        "sequence": 1,
        "correlationId": "worker-1",
        "payload": {
            "protocolVersion": WORKER_PROTOCOL_VERSION + 1,
            "workerId": "worker-1",
            "targetId": "doc-cpu",
            "packageLockHash": "sha256:package-lock",
            "imageDigest": "sha256:image",
            "supportedBlocks": [{"block": "document.parse@1"}],
            "state": "ready",
        },
    });

    let response = registry.admit_worker_message_wire_value(&message, "message-daemon-1", 2)?;
    let status = registry.status();

    assert_eq!(response.kind, WorkerProtocolMessageKind::AdmissionDecision);
    assert_eq!(response.correlation_id.as_deref(), Some("worker-1"));
    assert_eq!(response.causation_id.as_deref(), Some("message-worker-1"));
    assert!(matches!(
        response.payload,
        WorkerProtocolMessagePayload::AdmissionDecision(_)
    ));
    if let WorkerProtocolMessagePayload::AdmissionDecision(decision) = response.payload {
        assert!(!decision.admitted);
        assert_eq!(
            decision.reason_codes,
            vec!["worker.incompatible_protocol_version"]
        );
    }
    assert_eq!(status.admitted_workers, 0);
    assert_eq!(status.rejected_workers, 1);
    Ok(())
}

#[test]
fn worker_registry_rejects_non_advertisement_worker_messages() {
    let mut registry = WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080"))
        .expect("daemon config should be valid");
    let message = WorkerProtocolMessage::new(
        "message-error",
        1,
        WorkerProtocolMessagePayload::Error(WorkerProtocolErrorPayload::new(
            "worker.failed",
            "worker failed",
        )),
    );

    assert_eq!(
        registry.admit_worker_message(message, "message-daemon-1", 2),
        Err(WorkerRegistryError::UnexpectedWorkerMessageKind {
            kind: WorkerProtocolMessageKind::Error,
        }),
    );
}

#[test]
fn worker_registry_rejects_worker_message_kind_payload_mismatch() {
    let mut registry = WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080"))
        .expect("daemon config should be valid");
    let mut message = WorkerProtocolMessage::new(
        "message-error",
        1,
        WorkerProtocolMessagePayload::Error(WorkerProtocolErrorPayload::new(
            "worker.failed",
            "worker failed",
        )),
    );
    message.kind = WorkerProtocolMessageKind::Advertisement;

    assert_eq!(
        registry.admit_worker_message(message, "message-daemon-1", 2),
        Err(WorkerRegistryError::KindPayloadMismatch {
            kind: WorkerProtocolMessageKind::Advertisement,
            payload_kind: WorkerProtocolMessageKind::Error,
        }),
    );
}

#[test]
fn graphblocksd_admits_worker_message_from_stdin() -> Result<(), Box<dyn std::error::Error>> {
    let message = json!({
        "protocolVersion": WORKER_PROTOCOL_VERSION,
        "messageId": "message-worker-1",
        "kind": "advertisement",
        "sequence": 1,
        "correlationId": "worker-1",
        "payload": {
            "protocolVersion": WORKER_PROTOCOL_VERSION + 1,
            "workerId": "worker-1",
            "targetId": "doc-cpu",
            "packageLockHash": "sha256:package-lock",
            "imageDigest": "sha256:image",
            "supportedBlocks": [{"block": "document.parse@1"}],
            "state": "ready",
        },
    });
    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "admit-worker-message",
            "--daemon-id",
            "daemon-test",
            "--bind-address",
            "127.0.0.1:8080",
            "--response-message-id",
            "message-daemon-1",
            "--response-sequence",
            "2",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or("graphblocksd stdin pipe was not available")?;
    stdin.write_all(serde_json::to_string(&message)?.as_bytes())?;

    let output = child.wait_with_output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stdout)?;

    assert_eq!(
        payload.pointer("/ok").and_then(|value| value.as_bool()),
        Some(true)
    );
    assert_eq!(
        payload
            .pointer("/response/kind")
            .and_then(|value| value.as_str()),
        Some("admission_decision"),
    );
    assert_eq!(
        payload
            .pointer("/response/payload/admitted")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        payload
            .pointer("/response/payload/reasonCodes/0")
            .and_then(|value| value.as_str()),
        Some("worker.incompatible_protocol_version"),
    );
    assert_eq!(
        payload
            .pointer("/status/rejectedWorkers")
            .and_then(|value| value.as_u64()),
        Some(1),
    );
    Ok(())
}

#[test]
fn worker_registry_allows_admitted_worker_refresh_at_capacity() -> Result<(), DaemonConfigError> {
    let mut registry =
        WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080").with_max_workers(1))?;
    let initial = WorkerAdvertisement::new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image-a",
        [BlockCapability::new("document.parse@1")],
    );
    let refreshed = WorkerAdvertisement::new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image-b",
        [
            BlockCapability::new("document.parse@1"),
            BlockCapability::new("document.extract@1"),
        ],
    );
    let overflow = WorkerAdvertisement::new(
        "worker-2",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image-c",
        [BlockCapability::new("document.parse@1")],
    );

    let first_decision = registry.admit_worker(initial);
    let refresh_decision = registry.admit_worker(refreshed);
    let overflow_decision = registry.admit_worker(overflow);
    let status = registry.status();

    assert!(first_decision.admitted);
    assert!(refresh_decision.admitted);
    assert!(refresh_decision.reason_codes.is_empty());
    assert!(!overflow_decision.admitted);
    assert_eq!(
        overflow_decision.reason_codes,
        vec!["daemon.max_workers_exceeded"]
    );
    assert_eq!(registry.ready_worker_ids(), vec!["worker-1"]);
    assert_eq!(status.ready_workers, 1);
    assert_eq!(status.saturated_workers, 0);
    assert_eq!(status.draining_workers, 0);
    assert_eq!(status.admitted_workers, 1);
    assert_eq!(status.rejected_workers, 1);
    Ok(())
}

#[test]
fn worker_registry_tracks_saturated_workers_without_ready_capacity() -> Result<(), DaemonConfigError>
{
    let mut registry = WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080"))?;
    let advertisement = WorkerAdvertisement::new(
        "worker-saturated",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("model.generate@1")],
    )
    .with_state(WorkerState::Saturated);

    let decision = registry.admit_worker(advertisement);
    let status = registry.status();

    assert!(decision.admitted);
    assert_eq!(decision.state, WorkerState::Saturated);
    assert!(registry.ready_worker_ids().is_empty());
    assert_eq!(
        registry.worker_ids_by_state(WorkerState::Saturated),
        vec!["worker-saturated"]
    );
    assert_eq!(status.ready_workers, 0);
    assert_eq!(status.saturated_workers, 1);
    assert_eq!(status.draining_workers, 0);
    assert_eq!(status.admitted_workers, 1);
    assert_eq!(status.rejected_workers, 0);
    Ok(())
}

#[test]
fn worker_registry_rejects_unready_or_mismatched_workers() -> Result<(), DaemonConfigError> {
    let config = DaemonConfig::new("daemon-1", "127.0.0.1:8080")
        .require_package_lock_hash("sha256:package-lock");
    let mut registry = WorkerRegistry::new(config)?;
    let mismatched = WorkerAdvertisement::new(
        "worker-mismatch",
        "doc-cpu",
        "sha256:other-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    );
    let draining = WorkerAdvertisement::new(
        "worker-draining",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    )
    .with_state(WorkerState::Draining);

    let mismatch_decision = registry.admit_worker(mismatched);
    let draining_decision = registry.admit_worker(draining);
    let status = registry.status();

    assert!(!mismatch_decision.admitted);
    assert_eq!(
        mismatch_decision.reason_codes,
        vec!["worker.incompatible_package_lock"]
    );
    assert!(!draining_decision.admitted);
    assert_eq!(draining_decision.reason_codes, vec!["worker.not_ready"]);
    assert!(registry.ready_worker_ids().is_empty());
    assert_eq!(status.admitted_workers, 0);
    assert_eq!(status.saturated_workers, 0);
    assert_eq!(status.draining_workers, 0);
    assert_eq!(status.rejected_workers, 2);
    Ok(())
}

#[test]
fn worker_registry_drains_admitted_worker_and_removes_it_from_ready_pool() {
    let mut registry =
        WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080").with_max_workers(4))
            .expect("daemon config should be valid");
    let advertisement = WorkerAdvertisement::new(
        "worker-1",
        "model-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("model.generate@1")],
    );
    let decision = registry.admit_worker(advertisement);
    assert!(decision.admitted);
    assert_eq!(registry.ready_worker_ids(), vec!["worker-1"]);

    let request = WorkerInvokeRequest {
        invocation_id: "invoke-1".to_owned(),
        run_id: "run-1".to_owned(),
        node_id: "model".to_owned(),
        node_attempt_id: "model-attempt-1".to_owned(),
        lease_epoch: 3,
        block: "model.generate@1".to_owned(),
        context: WorkerInvocationContext::new("release-1", "rev-old"),
        inputs: json!({"prompt": "hello"}),
        config: json!({}),
    };
    let plan = registry
        .drain_worker(
            "worker-1",
            &WorkerDrainPolicy::default(),
            [WorkerDrainTask {
                workload: WorkerDrainWorkloadKind::OnlineRequest,
                request,
                started_at_unix_ms: 1_000,
                checkpointable: false,
            }],
            2_000,
            2_100,
        )
        .expect("admitted worker should drain");
    let status = registry.status();

    assert_eq!(plan.worker_id, "worker-1");
    assert_eq!(plan.target_id, "model-cpu");
    assert_eq!(plan.decisions[0].run_id, "run-1");
    assert_eq!(
        plan.decisions[0].disposition,
        WorkerDrainDisposition::FinishInPlace
    );
    assert!(registry.ready_worker_ids().is_empty());
    assert_eq!(
        registry.worker_ids_by_state(WorkerState::Draining),
        vec!["worker-1"]
    );
    assert_eq!(status.ready_workers, 0);
    assert_eq!(status.saturated_workers, 0);
    assert_eq!(status.draining_workers, 1);
    assert_eq!(status.admitted_workers, 1);
    assert_eq!(
        serde_json::from_value::<WorkerDrainPlan>(
            serde_json::to_value(&plan).expect("plan should serialize")
        )
        .expect("plan should deserialize"),
        plan,
    );
}

#[test]
fn worker_registry_reports_unknown_worker_for_drain() -> Result<(), DaemonConfigError> {
    let mut registry = WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080"))?;

    assert_eq!(
        registry.drain_worker(
            "worker-missing",
            &WorkerDrainPolicy::default(),
            Vec::<WorkerDrainTask>::new(),
            2_000,
            2_100,
        ),
        Err(WorkerRegistryError::UnknownWorker {
            worker_id: "worker-missing".to_owned(),
        }),
    );
    Ok(())
}
