use std::collections::BTreeMap;
use std::io::Write;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use graphblocks_protocol::{
    BlockCapability, WORKER_PROTOCOL_VERSION, WorkerAdvertisement, WorkerDrainDisposition,
    WorkerDrainPlan, WorkerDrainPolicy, WorkerDrainTask, WorkerDrainWorkloadKind,
    WorkerInvocationContext, WorkerInvokeRequest, WorkerProtocolErrorPayload,
    WorkerProtocolMessage, WorkerProtocolMessageKind, WorkerProtocolMessagePayload, WorkerState,
};
use graphblocks_runtime_core::run_store::{RunInvocationMode, SqliteRunStore};
use graphblocks_runtime_durable::{
    CheckpointBarrier, SchemaRef, SourceCursor, SqliteCheckpointStore,
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
fn graphblocksd_claims_sqlite_checkpoint_for_worker_recovery()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!("graphblocksd-checkpoint-claim-{unique}.sqlite3"));
    let mut store = SqliteCheckpointStore::open(&path).expect("sqlite checkpoint store opens");
    store
        .put(CheckpointBarrier {
            checkpoint_id: "checkpoint-000001".to_owned(),
            run_id: "run-000001".to_owned(),
            release_id: "release-2026-07-11".to_owned(),
            deployment_revision_id: "deployment-rev-1".to_owned(),
            plan_hash: "sha256:plan".to_owned(),
            checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
            state_revision: 1,
            completed_nodes: vec!["begin".to_owned()],
            pending_nodes: vec!["resume".to_owned()],
            source_cursors: BTreeMap::from([(
                "events".to_owned(),
                SourceCursor::new("events", 0, 7),
            )]),
            operator_state: BTreeMap::new(),
            sink_commit_metadata: BTreeMap::new(),
            schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
            created_at_unix_ms: 1_820_000_000_000,
        })
        .expect("checkpoint should persist");
    drop(store);

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path.to_str().ok_or("checkpoint path was not utf-8")?,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--now-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "2000",
        ])
        .stdout(Stdio::piped())
        .output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stdout)?;

    assert_eq!(
        payload.pointer("/ok").and_then(|value| value.as_bool()),
        Some(true)
    );
    assert_eq!(
        payload
            .pointer("/checkpoint/checkpointId")
            .and_then(|value| value.as_str()),
        Some("checkpoint-000001"),
    );
    assert_eq!(
        payload
            .pointer("/claim/workerId")
            .and_then(|value| value.as_str()),
        Some("worker-1"),
    );
    assert_eq!(
        payload
            .pointer("/claim/fencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(1),
    );
    Ok(())
}

#[test]
fn graphblocksd_completes_sqlite_checkpoint_claim_for_worker_recovery()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    let path =
        std::env::temp_dir().join(format!("graphblocksd-checkpoint-complete-{unique}.sqlite3"));
    let mut store = SqliteCheckpointStore::open(&path).expect("sqlite checkpoint store opens");
    store
        .put(CheckpointBarrier {
            checkpoint_id: "checkpoint-000001".to_owned(),
            run_id: "run-000001".to_owned(),
            release_id: "release-2026-07-11".to_owned(),
            deployment_revision_id: "deployment-rev-1".to_owned(),
            plan_hash: "sha256:plan".to_owned(),
            checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
            state_revision: 1,
            completed_nodes: vec!["begin".to_owned()],
            pending_nodes: vec!["resume".to_owned()],
            source_cursors: BTreeMap::from([(
                "events".to_owned(),
                SourceCursor::new("events", 0, 7),
            )]),
            operator_state: BTreeMap::new(),
            sink_commit_metadata: BTreeMap::new(),
            schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
            created_at_unix_ms: 1_820_000_000_000,
        })
        .expect("checkpoint should persist");
    drop(store);

    let path_text = path.to_str().ok_or("checkpoint path was not utf-8")?;
    let claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--now-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "2000",
        ])
        .stdout(Stdio::piped())
        .output()?;
    assert!(claim_output.status.success());
    let claim_payload = serde_json::from_slice::<serde_json::Value>(&claim_output.stdout)?;
    assert_eq!(
        claim_payload
            .pointer("/claim/fencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(1),
    );

    let complete_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "complete-checkpoint-claim",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--checkpoint-id",
            "checkpoint-000001",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--fencing-epoch",
            "1",
            "--claimed-at-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "2000",
            "--now-unix-ms",
            "1500",
        ])
        .stdout(Stdio::piped())
        .output()?;
    assert!(complete_output.status.success());
    let complete_payload = serde_json::from_slice::<serde_json::Value>(&complete_output.stdout)?;
    assert_eq!(
        complete_payload
            .pointer("/ok")
            .and_then(|value| value.as_bool()),
        Some(true),
    );

    let next_claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-2",
            "--lease-id",
            "lease-2",
            "--now-unix-ms",
            "1600",
            "--expires-at-unix-ms",
            "2600",
        ])
        .stdout(Stdio::piped())
        .output()?;
    assert!(next_claim_output.status.success());
    let next_claim_payload =
        serde_json::from_slice::<serde_json::Value>(&next_claim_output.stdout)?;
    assert_eq!(
        next_claim_payload
            .pointer("/claim/fencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(2),
    );
    Ok(())
}

#[test]
fn graphblocksd_renews_sqlite_checkpoint_claim_for_worker_recovery()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!("graphblocksd-checkpoint-renew-{unique}.sqlite3"));
    let mut store = SqliteCheckpointStore::open(&path).expect("sqlite checkpoint store opens");
    store
        .put(CheckpointBarrier {
            checkpoint_id: "checkpoint-000001".to_owned(),
            run_id: "run-000001".to_owned(),
            release_id: "release-2026-07-11".to_owned(),
            deployment_revision_id: "deployment-rev-1".to_owned(),
            plan_hash: "sha256:plan".to_owned(),
            checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
            state_revision: 1,
            completed_nodes: vec!["begin".to_owned()],
            pending_nodes: vec!["resume".to_owned()],
            source_cursors: BTreeMap::from([(
                "events".to_owned(),
                SourceCursor::new("events", 0, 7),
            )]),
            operator_state: BTreeMap::new(),
            sink_commit_metadata: BTreeMap::new(),
            schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
            created_at_unix_ms: 1_820_000_000_000,
        })
        .expect("checkpoint should persist");
    drop(store);

    let path_text = path.to_str().ok_or("checkpoint path was not utf-8")?;
    let claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--now-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "1500",
        ])
        .output()?;
    assert!(claim_output.status.success());

    let renew_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "renew-checkpoint-claim",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--checkpoint-id",
            "checkpoint-000001",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--fencing-epoch",
            "1",
            "--claimed-at-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "1500",
            "--now-unix-ms",
            "1200",
            "--new-expires-at-unix-ms",
            "3000",
        ])
        .output()?;
    assert!(renew_output.status.success());
    let renew_payload = serde_json::from_slice::<serde_json::Value>(&renew_output.stdout)?;
    assert_eq!(
        renew_payload
            .pointer("/claim/fencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(1),
    );
    assert_eq!(
        renew_payload
            .pointer("/claim/expiresAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(3000),
    );
    assert_eq!(
        renew_payload
            .pointer("/claim/renewedAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(1200),
    );

    let blocked_claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-2",
            "--lease-id",
            "lease-2",
            "--now-unix-ms",
            "2500",
            "--expires-at-unix-ms",
            "3500",
        ])
        .output()?;
    assert!(!blocked_claim_output.status.success());
    let blocked_payload =
        serde_json::from_slice::<serde_json::Value>(&blocked_claim_output.stderr)?;
    assert_eq!(
        blocked_payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.checkpoint.active_recovery_claim"),
    );
    assert_eq!(
        blocked_payload
            .pointer("/error/expiresAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(3000),
    );
    Ok(())
}

#[test]
fn graphblocksd_rejects_checkpoint_claim_renewal_that_shortens_lease()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    let path =
        std::env::temp_dir().join(format!("graphblocksd-checkpoint-shorten-{unique}.sqlite3"));
    let mut store = SqliteCheckpointStore::open(&path).expect("sqlite checkpoint store opens");
    store
        .put(CheckpointBarrier {
            checkpoint_id: "checkpoint-000001".to_owned(),
            run_id: "run-000001".to_owned(),
            release_id: "release-2026-07-11".to_owned(),
            deployment_revision_id: "deployment-rev-1".to_owned(),
            plan_hash: "sha256:plan".to_owned(),
            checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
            state_revision: 1,
            completed_nodes: vec!["begin".to_owned()],
            pending_nodes: vec!["resume".to_owned()],
            source_cursors: BTreeMap::from([(
                "events".to_owned(),
                SourceCursor::new("events", 0, 7),
            )]),
            operator_state: BTreeMap::new(),
            sink_commit_metadata: BTreeMap::new(),
            schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
            created_at_unix_ms: 1_820_000_000_000,
        })
        .expect("checkpoint should persist");
    drop(store);

    let path_text = path.to_str().ok_or("checkpoint path was not utf-8")?;
    let claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--now-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "3000",
        ])
        .output()?;
    assert!(claim_output.status.success());

    let renew_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "renew-checkpoint-claim",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--checkpoint-id",
            "checkpoint-000001",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--fencing-epoch",
            "1",
            "--claimed-at-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "3000",
            "--now-unix-ms",
            "1500",
            "--new-expires-at-unix-ms",
            "2000",
        ])
        .output()?;
    assert!(!renew_output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&renew_output.stderr)?;
    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.checkpoint.invalid_recovery_claim"),
    );
    assert_eq!(
        payload
            .pointer("/error/field")
            .and_then(|value| value.as_str()),
        Some("expires_at_unix_ms"),
    );
    Ok(())
}

#[test]
fn graphblocksd_reports_active_checkpoint_claim_as_structured_json()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "graphblocksd-checkpoint-active-claim-{unique}.sqlite3"
    ));
    let mut store = SqliteCheckpointStore::open(&path).expect("sqlite checkpoint store opens");
    store
        .put(CheckpointBarrier {
            checkpoint_id: "checkpoint-000001".to_owned(),
            run_id: "run-000001".to_owned(),
            release_id: "release-2026-07-11".to_owned(),
            deployment_revision_id: "deployment-rev-1".to_owned(),
            plan_hash: "sha256:plan".to_owned(),
            checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
            state_revision: 1,
            completed_nodes: vec!["begin".to_owned()],
            pending_nodes: vec!["resume".to_owned()],
            source_cursors: BTreeMap::from([(
                "events".to_owned(),
                SourceCursor::new("events", 0, 7),
            )]),
            operator_state: BTreeMap::new(),
            sink_commit_metadata: BTreeMap::new(),
            schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
            created_at_unix_ms: 1_820_000_000_000,
        })
        .expect("checkpoint should persist");
    drop(store);

    let path_text = path.to_str().ok_or("checkpoint path was not utf-8")?;
    let first_claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--now-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "2000",
        ])
        .output()?;
    assert!(first_claim_output.status.success());

    let blocked_claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-2",
            "--lease-id",
            "lease-2",
            "--now-unix-ms",
            "1500",
            "--expires-at-unix-ms",
            "2500",
        ])
        .output()?;
    assert!(!blocked_claim_output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&blocked_claim_output.stderr)?;

    assert_eq!(
        payload.pointer("/ok").and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.checkpoint.active_recovery_claim"),
    );
    assert_eq!(
        payload
            .pointer("/error/runId")
            .and_then(|value| value.as_str()),
        Some("run-000001"),
    );
    assert_eq!(
        payload
            .pointer("/error/workerId")
            .and_then(|value| value.as_str()),
        Some("worker-1"),
    );
    assert_eq!(
        payload
            .pointer("/error/leaseId")
            .and_then(|value| value.as_str()),
        Some("lease-1"),
    );
    assert_eq!(
        payload
            .pointer("/error/expiresAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(2000),
    );
    Ok(())
}

#[test]
fn graphblocksd_reports_stale_checkpoint_completion_as_structured_json()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "graphblocksd-checkpoint-stale-claim-{unique}.sqlite3"
    ));
    let mut store = SqliteCheckpointStore::open(&path).expect("sqlite checkpoint store opens");
    store
        .put(CheckpointBarrier {
            checkpoint_id: "checkpoint-000001".to_owned(),
            run_id: "run-000001".to_owned(),
            release_id: "release-2026-07-11".to_owned(),
            deployment_revision_id: "deployment-rev-1".to_owned(),
            plan_hash: "sha256:plan".to_owned(),
            checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
            state_revision: 1,
            completed_nodes: vec!["begin".to_owned()],
            pending_nodes: vec!["resume".to_owned()],
            source_cursors: BTreeMap::from([(
                "events".to_owned(),
                SourceCursor::new("events", 0, 7),
            )]),
            operator_state: BTreeMap::new(),
            sink_commit_metadata: BTreeMap::new(),
            schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
            created_at_unix_ms: 1_820_000_000_000,
        })
        .expect("checkpoint should persist");
    drop(store);

    let path_text = path.to_str().ok_or("checkpoint path was not utf-8")?;
    let first_claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--now-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "2000",
        ])
        .output()?;
    assert!(first_claim_output.status.success());
    let replacement_claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-2",
            "--lease-id",
            "lease-2",
            "--now-unix-ms",
            "2001",
            "--expires-at-unix-ms",
            "3000",
        ])
        .output()?;
    assert!(replacement_claim_output.status.success());

    let stale_complete_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "complete-checkpoint-claim",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--checkpoint-id",
            "checkpoint-000001",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--fencing-epoch",
            "1",
            "--claimed-at-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "2000",
            "--now-unix-ms",
            "2100",
        ])
        .output()?;
    assert!(!stale_complete_output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&stale_complete_output.stderr)?;

    assert_eq!(
        payload.pointer("/ok").and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.checkpoint.recovery_claim_mismatch"),
    );
    assert_eq!(
        payload
            .pointer("/error/runId")
            .and_then(|value| value.as_str()),
        Some("run-000001"),
    );
    assert_eq!(
        payload
            .pointer("/error/expectedLeaseId")
            .and_then(|value| value.as_str()),
        Some("lease-1"),
    );
    assert_eq!(
        payload
            .pointer("/error/expectedFencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(1),
    );
    assert_eq!(
        payload
            .pointer("/error/actualLeaseId")
            .and_then(|value| value.as_str()),
        Some("lease-2"),
    );
    assert_eq!(
        payload
            .pointer("/error/actualFencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(2),
    );
    Ok(())
}

#[test]
fn graphblocksd_reports_forged_checkpoint_claim_identity_as_structured_json()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "graphblocksd-checkpoint-forged-claim-{unique}.sqlite3"
    ));
    let mut store = SqliteCheckpointStore::open(&path).expect("sqlite checkpoint store opens");
    store
        .put(CheckpointBarrier {
            checkpoint_id: "checkpoint-000001".to_owned(),
            run_id: "run-000001".to_owned(),
            release_id: "release-2026-07-11".to_owned(),
            deployment_revision_id: "deployment-rev-1".to_owned(),
            plan_hash: "sha256:plan".to_owned(),
            checkpoint_schema: SchemaRef::new("graphblocks.ai/Checkpoint", 1),
            state_revision: 1,
            completed_nodes: vec!["begin".to_owned()],
            pending_nodes: vec!["resume".to_owned()],
            source_cursors: BTreeMap::from([(
                "events".to_owned(),
                SourceCursor::new("events", 0, 7),
            )]),
            operator_state: BTreeMap::new(),
            sink_commit_metadata: BTreeMap::new(),
            schema_versions: BTreeMap::from([("checkpoint".to_owned(), 1)]),
            created_at_unix_ms: 1_820_000_000_000,
        })
        .expect("checkpoint should persist");
    drop(store);

    let path_text = path.to_str().ok_or("checkpoint path was not utf-8")?;
    let claim_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-checkpoint",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--release-id",
            "release-2026-07-11",
            "--deployment-revision-id",
            "deployment-rev-1",
            "--plan-hash",
            "sha256:plan",
            "--worker-id",
            "worker-1",
            "--lease-id",
            "lease-1",
            "--now-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "2000",
        ])
        .output()?;
    assert!(claim_output.status.success());

    let forged_complete_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "complete-checkpoint-claim",
            "--checkpoint-store",
            path_text,
            "--run-id",
            "run-000001",
            "--checkpoint-id",
            "checkpoint-forged",
            "--worker-id",
            "worker-forged",
            "--lease-id",
            "lease-1",
            "--fencing-epoch",
            "1",
            "--claimed-at-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "2000",
            "--now-unix-ms",
            "1500",
        ])
        .output()?;
    assert!(!forged_complete_output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&forged_complete_output.stderr)?;

    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.checkpoint.recovery_claim_mismatch"),
    );
    assert_eq!(
        payload
            .pointer("/error/expectedCheckpointId")
            .and_then(|value| value.as_str()),
        Some("checkpoint-forged"),
    );
    assert_eq!(
        payload
            .pointer("/error/expectedWorkerId")
            .and_then(|value| value.as_str()),
        Some("worker-forged"),
    );
    assert_eq!(
        payload
            .pointer("/error/actualCheckpointId")
            .and_then(|value| value.as_str()),
        Some("checkpoint-000001"),
    );
    assert_eq!(
        payload
            .pointer("/error/actualWorkerId")
            .and_then(|value| value.as_str()),
        Some("worker-1"),
    );
    assert_eq!(
        payload
            .pointer("/error/actualLeaseId")
            .and_then(|value| value.as_str()),
        Some("lease-1"),
    );
    assert_eq!(
        payload
            .pointer("/error/actualFencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(1),
    );
    Ok(())
}

#[test]
fn graphblocksd_acquires_and_renews_sqlite_run_ownership_lease()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time should be after epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!("graphblocksd-run-lease-{unique}.sqlite3"));
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .create_run_with_invocation_mode(
                "sha256:graph",
                json!({"task": "background"}),
                RunInvocationMode::Background,
            )
            .map_err(|error| format!("{error:?}"))?;
    }

    let acquire_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "acquire-run-lease",
            "--run-store",
            path_text,
            "--run-id",
            "run-000001",
            "--owner",
            "coordinator-a",
            "--acquired-at-unix-ms",
            "1000",
            "--expires-at-unix-ms",
            "1500",
        ])
        .output()?;
    assert!(acquire_output.status.success());
    let acquire_payload = serde_json::from_slice::<serde_json::Value>(&acquire_output.stdout)?;

    assert_eq!(
        acquire_payload
            .pointer("/lease/leaseId")
            .and_then(|value| value.as_str()),
        Some("run-000001:1"),
    );
    assert_eq!(
        acquire_payload
            .pointer("/lease/owner")
            .and_then(|value| value.as_str()),
        Some("coordinator-a"),
    );
    assert_eq!(
        acquire_payload
            .pointer("/lease/fencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(1),
    );

    let renew_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "renew-run-lease",
            "--run-store",
            path_text,
            "--run-id",
            "run-000001",
            "--owner",
            "coordinator-a",
            "--lease-id",
            "run-000001:1",
            "--fencing-epoch",
            "1",
            "--now-unix-ms",
            "1200",
            "--new-expires-at-unix-ms",
            "2500",
        ])
        .output()?;
    assert!(renew_output.status.success());
    let renew_payload = serde_json::from_slice::<serde_json::Value>(&renew_output.stdout)?;

    assert_eq!(
        renew_payload
            .pointer("/lease/leaseId")
            .and_then(|value| value.as_str()),
        Some("run-000001:1"),
    );
    assert_eq!(
        renew_payload
            .pointer("/lease/expiresAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(2500),
    );
    assert_eq!(
        renew_payload
            .pointer("/lease/renewedAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(1200),
    );

    let blocked_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "acquire-run-lease",
            "--run-store",
            path_text,
            "--run-id",
            "run-000001",
            "--owner",
            "coordinator-b",
            "--acquired-at-unix-ms",
            "2000",
            "--expires-at-unix-ms",
            "3000",
        ])
        .output()?;
    assert!(!blocked_output.status.success());
    let blocked_payload = serde_json::from_slice::<serde_json::Value>(&blocked_output.stderr)?;

    assert_eq!(
        blocked_payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.run_lease.active"),
    );
    assert_eq!(
        blocked_payload
            .pointer("/error/owner")
            .and_then(|value| value.as_str()),
        Some("coordinator-a"),
    );
    assert_eq!(
        blocked_payload
            .pointer("/error/expiresAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(2500),
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_reports_forged_run_lease_renewal_as_structured_json()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time should be after epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!("graphblocksd-run-lease-forged-{unique}.sqlite3"));
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
        let run = store
            .create_run_with_invocation_mode(
                "sha256:graph",
                json!({}),
                RunInvocationMode::Background,
            )
            .map_err(|error| format!("{error:?}"))?;
        store
            .acquire_ownership_lease(&run.run_id, "coordinator-a", 1000, 2000)
            .map_err(|error| format!("{error:?}"))?;
    }

    let forged_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "renew-run-lease",
            "--run-store",
            path_text,
            "--run-id",
            "run-000001",
            "--owner",
            "coordinator-forged",
            "--lease-id",
            "run-000001:1",
            "--fencing-epoch",
            "1",
            "--now-unix-ms",
            "1200",
            "--new-expires-at-unix-ms",
            "2500",
        ])
        .output()?;
    assert!(!forged_output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&forged_output.stderr)?;

    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.run_lease.mismatch"),
    );
    assert_eq!(
        payload
            .pointer("/error/expectedOwner")
            .and_then(|value| value.as_str()),
        Some("coordinator-a"),
    );
    assert_eq!(
        payload
            .pointer("/error/actualOwner")
            .and_then(|value| value.as_str()),
        Some("coordinator-forged"),
    );
    assert_eq!(
        payload
            .pointer("/error/actualLeaseId")
            .and_then(|value| value.as_str()),
        Some("run-000001:1"),
    );
    assert_eq!(
        payload
            .pointer("/error/actualFencingEpoch")
            .and_then(|value| value.as_u64()),
        Some(1),
    );

    let _ = std::fs::remove_file(&path);
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
