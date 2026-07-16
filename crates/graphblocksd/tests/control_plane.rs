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
use graphblocks_runtime_core::async_operation::{
    AsyncOperation, AsyncOperationEvent, AsyncOperationKind, AsyncOperationState,
    SqliteAsyncOperationStore,
};
use graphblocks_runtime_core::run_store::{RunInvocationMode, RunStatus, SqliteRunStore};
use graphblocks_runtime_durable::{
    CheckpointBarrier, SchemaRef, SourceCursor, SqliteCheckpointStore,
};
use graphblocksd::{DaemonConfig, DaemonConfigError, WorkerRegistry, WorkerRegistryError};
use rusqlite::Connection;
use serde_json::json;

const VALID_RESUME_TOKEN_HASH: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn sqlite_async_operation_path(label: &str) -> std::path::PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time should be after epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "graphblocksd-async-operation-{label}-{unique}.sqlite3"
    ))
}

fn sqlite_callback_delivery_path(label: &str) -> std::path::PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time should be after epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "graphblocksd-callback-delivery-{label}-{unique}.sqlite3"
    ))
}

fn sqlite_callback_dead_letter_path(label: &str) -> std::path::PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time should be after epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "graphblocksd-callback-dead-letter-{label}-{unique}.sqlite3"
    ))
}

fn waiting_daemon_async_operation() -> AsyncOperation {
    AsyncOperation::new(
        "op-1",
        "run-1",
        "node-ci",
        "attempt-1",
        AsyncOperationKind::CiJob,
        VALID_RESUME_TOKEN_HASH,
        "idem-op-1",
        "schemas/CICallback@1",
        1_000,
    )
    .submitted("gha-run-1", 1_050)
    .waiting_callback(2_000)
}

fn enqueue_daemon_callback_delivery(
    path_text: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "enqueue-callback-delivery",
            "--callback-delivery-store",
            path_text,
            "--delivery-id",
            "del-sub-1-event-1",
            "--subscription-id",
            "sub-1",
            "--event-id",
            "event-1",
            "--run-id",
            "run-1",
            "--sequence",
            "1",
            "--cursor",
            "cursor-1",
            "--idempotency-key",
            "sub-1:event-1",
            "--failure-policy",
            "retry_then_dead_letter",
        ])
        .output()?;
    assert!(output.status.success());
    Ok(serde_json::from_slice::<serde_json::Value>(&output.stdout)?)
}

fn claim_daemon_callback_deliveries(
    path_text: &str,
    now_unix_ms: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "claim-callback-deliveries",
            "--callback-delivery-store",
            path_text,
            "--now-unix-ms",
            now_unix_ms,
            "--claim-lease-ms",
            "5000",
            "--limit",
            "10",
        ])
        .output()?;
    assert!(output.status.success());
    Ok(serde_json::from_slice::<serde_json::Value>(&output.stdout)?)
}

fn complete_daemon_callback_delivery(
    path_text: &str,
    claim: &serde_json::Value,
    response: &[&str],
    now_unix_ms: &str,
    retry_max_attempts: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let delivery_id = claim
        .pointer("/delivery/deliveryId")
        .and_then(|value| value.as_str())
        .ok_or("claimed delivery id is missing")?;
    let claim_generation = claim
        .pointer("/claimGeneration")
        .and_then(|value| value.as_u64())
        .ok_or("claim generation is missing")?
        .to_string();
    let claim_started_at_unix_ms = claim
        .pointer("/claimStartedAtUnixMs")
        .and_then(|value| value.as_u64())
        .ok_or("claim start is missing")?
        .to_string();
    let claim_expires_at_unix_ms = claim
        .pointer("/claimExpiresAtUnixMs")
        .and_then(|value| value.as_u64())
        .ok_or("claim expiration is missing")?
        .to_string();
    let mut args = vec![
        "complete-callback-delivery",
        "--callback-delivery-store",
        path_text,
        "--delivery-id",
        delivery_id,
        "--claim-generation",
        &claim_generation,
        "--claim-started-at-unix-ms",
        &claim_started_at_unix_ms,
        "--claim-expires-at-unix-ms",
        &claim_expires_at_unix_ms,
        "--now-unix-ms",
        now_unix_ms,
        "--retry-max-attempts",
        retry_max_attempts,
        "--retry-base-delay-ms",
        "100",
        "--retry-max-delay-ms",
        "250",
    ];
    args.extend_from_slice(response);

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args(args)
        .output()?;
    assert!(output.status.success());
    Ok(serde_json::from_slice::<serde_json::Value>(&output.stdout)?)
}

fn submit_daemon_ci_callback(
    path_text: &str,
    callback_id: &str,
    idempotency_key: &str,
    received_at_unix_ms: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    submit_daemon_ci_callback_with_resume_args(
        path_text,
        callback_id,
        idempotency_key,
        received_at_unix_ms,
        &[],
    )
}

fn submit_daemon_ci_callback_with_resume_args(
    path_text: &str,
    callback_id: &str,
    idempotency_key: &str,
    received_at_unix_ms: &str,
    resume_args: &[&str],
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "submit-async-callback",
            "--async-operation-store",
            path_text,
            "--callback-id",
            callback_id,
            "--operation-id",
            "op-1",
            "--run-id",
            "run-1",
            "--node-id",
            "node-ci",
            "--attempt-id",
            "attempt-1",
            "--provider-operation-id",
            "gha-run-1",
            "--idempotency-key",
            idempotency_key,
            "--received-at-unix-ms",
            received_at_unix_ms,
            "--verified-by",
            "hmac:callback-endpoint-1",
            "--policy-snapshot-id",
            "policy-snapshot-1",
            "--schema-id",
            "schemas/CICallback@1",
            "--schema-json",
            r#"{"type":"object","required":["status","workflow_run_id"],"properties":{"status":{"type":"string"},"workflow_run_id":{"type":"string"}}}"#,
        ])
        .args(resume_args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or("graphblocksd stdin pipe was not available")?;
    stdin.write_all(
        serde_json::to_string(&json!({"status": "completed", "workflow_run_id": "gha-run-1"}))?
            .as_bytes(),
    )?;

    let output = child.wait_with_output()?;
    assert!(output.status.success());
    Ok(serde_json::from_slice::<serde_json::Value>(&output.stdout)?)
}

fn quarantine_daemon_ci_callback(
    path_text: &str,
    callback_id: &str,
    idempotency_key: &str,
    quarantine_expires_at_unix_ms: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "quarantine-async-callback",
            "--async-operation-store",
            path_text,
            "--callback-id",
            callback_id,
            "--operation-id",
            "op-1",
            "--run-id",
            "run-1",
            "--node-id",
            "node-ci",
            "--attempt-id",
            "attempt-1",
            "--provider-operation-id",
            "gha-run-1",
            "--idempotency-key",
            idempotency_key,
            "--received-at-unix-ms",
            "1200",
            "--verified-by",
            "hmac:callback-endpoint-1",
            "--policy-snapshot-id",
            "policy-snapshot-1",
            "--quarantine-expires-at-unix-ms",
            quarantine_expires_at_unix_ms,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or("graphblocksd stdin pipe was not available")?;
    stdin.write_all(
        serde_json::to_string(&json!({"status": "completed", "workflow_run_id": "gha-run-1"}))?
            .as_bytes(),
    )?;

    let output = child.wait_with_output()?;
    assert!(output.status.success());
    Ok(serde_json::from_slice::<serde_json::Value>(&output.stdout)?)
}

fn register_daemon_waiting_operation(
    path_text: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "register-async-operation",
            "--async-operation-store",
            path_text,
            "--operation-id",
            "op-1",
            "--run-id",
            "run-1",
            "--node-id",
            "node-ci",
            "--attempt-id",
            "attempt-1",
            "--kind",
            "ci_job",
            "--resume-token-hash",
            VALID_RESUME_TOKEN_HASH,
            "--idempotency-key",
            "idem-op-1",
            "--expected-schema",
            "schemas/CICallback@1",
            "--created-at-unix-ms",
            "1000",
            "--provider-operation-id",
            "gha-run-1",
            "--submitted-at-unix-ms",
            "1050",
            "--waiting-callback-expires-at-unix-ms",
            "2000",
        ])
        .output()?;
    assert!(output.status.success());
    Ok(serde_json::from_slice::<serde_json::Value>(&output.stdout)?)
}

fn accept_quarantined_daemon_callbacks(
    path_text: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "accept-quarantined-async-callbacks",
            "--async-operation-store",
            path_text,
            "--operation-id",
            "op-1",
            "--schema-id",
            "schemas/CICallback@1",
            "--schema-json",
            r#"{"type":"object","required":["status","workflow_run_id"],"properties":{"status":{"type":"string"},"workflow_run_id":{"type":"string"}}}"#,
        ])
        .output()?;
    assert!(output.status.success());
    Ok(serde_json::from_slice::<serde_json::Value>(&output.stdout)?)
}

#[test]
fn graphblocksd_claims_and_completes_callback_delivery() -> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_callback_delivery_path("claim-complete");
    let path_text = path
        .to_str()
        .ok_or("temporary sqlite path is not valid utf-8")?;

    let enqueued = enqueue_daemon_callback_delivery(path_text)?;
    assert_eq!(
        enqueued
            .pointer("/delivery/status")
            .and_then(|value| value.as_str()),
        Some("pending")
    );

    let claimed = claim_daemon_callback_deliveries(path_text, "1000")?;
    assert_eq!(
        claimed
            .pointer("/claimedCount")
            .and_then(|value| value.as_u64()),
        Some(1)
    );
    let claim = claimed
        .pointer("/claimed/0")
        .ok_or("claimed delivery should be returned")?;
    assert_eq!(
        claim
            .pointer("/delivery/status")
            .and_then(|value| value.as_str()),
        Some("delivering")
    );
    assert_eq!(
        claim
            .pointer("/claimGeneration")
            .and_then(|value| value.as_u64()),
        Some(1)
    );

    let completed = complete_daemon_callback_delivery(
        path_text,
        claim,
        &["--response", "success"],
        "1100",
        "3",
    )?;
    assert_eq!(
        completed
            .pointer("/delivery/status")
            .and_then(|value| value.as_str()),
        Some("delivered")
    );
    assert_eq!(
        completed
            .pointer("/delivery/deliveredAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(1100)
    );

    let empty = claim_daemon_callback_deliveries(path_text, "1200")?;
    assert_eq!(
        empty
            .pointer("/claimedCount")
            .and_then(|value| value.as_u64()),
        Some(0)
    );

    Ok(())
}

#[test]
fn graphblocksd_retries_callback_delivery_after_server_error()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_callback_delivery_path("server-error-retry");
    let path_text = path
        .to_str()
        .ok_or("temporary sqlite path is not valid utf-8")?;

    enqueue_daemon_callback_delivery(path_text)?;
    let claimed = claim_daemon_callback_deliveries(path_text, "1000")?;
    let claim = claimed
        .pointer("/claimed/0")
        .ok_or("claimed delivery should be returned")?;

    let retry = complete_daemon_callback_delivery(
        path_text,
        claim,
        &["--response", "server_error", "--status-code", "503"],
        "1100",
        "3",
    )?;

    assert_eq!(
        retry
            .pointer("/delivery/status")
            .and_then(|value| value.as_str()),
        Some("pending")
    );
    assert_eq!(
        retry
            .pointer("/delivery/attempt")
            .and_then(|value| value.as_u64()),
        Some(2)
    );
    let retry_at = retry
        .pointer("/delivery/nextRetryAtUnixMs")
        .and_then(|value| value.as_u64())
        .ok_or("retry timestamp should be present")?;
    assert!(retry_at > 1100 && retry_at <= 1350);

    let too_early = claim_daemon_callback_deliveries(path_text, "1100")?;
    assert_eq!(
        too_early
            .pointer("/claimedCount")
            .and_then(|value| value.as_u64()),
        Some(0)
    );
    let retried = claim_daemon_callback_deliveries(path_text, &retry_at.to_string())?;
    assert_eq!(
        retried
            .pointer("/claimed/0/delivery/attempt")
            .and_then(|value| value.as_u64()),
        Some(2)
    );

    Ok(())
}

#[test]
fn graphblocksd_moves_dead_letter_and_redrives_callback_delivery()
-> Result<(), Box<dyn std::error::Error>> {
    let delivery_path = sqlite_callback_delivery_path("dead-letter-redrive");
    let dead_letter_path = sqlite_callback_dead_letter_path("dead-letter-redrive");
    let delivery_path_text = delivery_path
        .to_str()
        .ok_or("temporary callback delivery path is not valid utf-8")?;
    let dead_letter_path_text = dead_letter_path
        .to_str()
        .ok_or("temporary callback dead-letter path is not valid utf-8")?;

    enqueue_daemon_callback_delivery(delivery_path_text)?;
    let claimed = claim_daemon_callback_deliveries(delivery_path_text, "1000")?;
    let claim = claimed
        .pointer("/claimed/0")
        .ok_or("claimed delivery should be returned")?;
    let dead_lettered = complete_daemon_callback_delivery(
        delivery_path_text,
        claim,
        &["--response", "server_error", "--status-code", "503"],
        "1100",
        "1",
    )?;
    assert_eq!(
        dead_lettered
            .pointer("/delivery/status")
            .and_then(|value| value.as_str()),
        Some("dead_lettered")
    );

    let moved_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "move-callback-to-dead-letter",
            "--callback-delivery-store",
            delivery_path_text,
            "--callback-dead-letter-store",
            dead_letter_path_text,
            "--delivery-id",
            "del-sub-1-event-1",
            "--dead-lettered-at-unix-ms",
            "1200",
        ])
        .output()?;
    assert!(moved_output.status.success());
    let moved = serde_json::from_slice::<serde_json::Value>(&moved_output.stdout)?;
    assert_eq!(
        moved
            .pointer("/deadLetter/originalDeliveryId")
            .and_then(|value| value.as_str()),
        Some("del-sub-1-event-1")
    );
    assert_eq!(
        moved
            .pointer("/deadLetter/attemptHistory")
            .and_then(|value| value.as_array())
            .map(Vec::len),
        Some(1)
    );

    let redriven_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "redrive-callback-delivery",
            "--callback-delivery-store",
            delivery_path_text,
            "--callback-dead-letter-store",
            dead_letter_path_text,
            "--delivery-id",
            "del-sub-1-event-1",
            "--operator",
            "operator:alice",
            "--reason",
            "receiver recovered",
            "--redriven-at-unix-ms",
            "1300",
        ])
        .output()?;
    assert!(redriven_output.status.success());
    let redriven = serde_json::from_slice::<serde_json::Value>(&redriven_output.stdout)?;
    assert_eq!(
        redriven
            .pointer("/delivery/status")
            .and_then(|value| value.as_str()),
        Some("pending")
    );
    assert_eq!(
        redriven
            .pointer("/delivery/attempt")
            .and_then(|value| value.as_u64()),
        Some(2)
    );
    assert_eq!(
        redriven
            .pointer("/delivery/redriveCount")
            .and_then(|value| value.as_u64()),
        Some(1)
    );
    assert_eq!(
        redriven
            .pointer("/delivery/lastRedriveOperator")
            .and_then(|value| value.as_str()),
        Some("operator:alice")
    );

    let claimed_again = claim_daemon_callback_deliveries(delivery_path_text, "1300")?;
    assert_eq!(
        claimed_again
            .pointer("/claimed/0/delivery/attempt")
            .and_then(|value| value.as_u64()),
        Some(2)
    );
    assert_eq!(
        claimed_again
            .pointer("/claimed/0/delivery/status")
            .and_then(|value| value.as_str()),
        Some("delivering")
    );

    Ok(())
}

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
fn graphblocksd_rejects_duplicate_worker_message_keys() -> Result<(), Box<dyn std::error::Error>> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .arg("admit-worker-message")
        .stdin(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    child
        .stdin
        .as_mut()
        .ok_or("graphblocksd stdin pipe was not available")?
        .write_all(br#"{"protocolVersion":1,"protocolVersion":1}"#)?;

    let output = child.wait_with_output()?;
    assert_eq!(output.status.code(), Some(2));
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stderr)?;
    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("json.parse_failed"),
    );
    assert!(
        payload
            .pointer("/error/message")
            .and_then(|value| value.as_str())
            .is_some_and(|message| message.contains("duplicate JSON object key")),
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

    let early_renew_output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
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
            "999",
            "--new-expires-at-unix-ms",
            "3000",
        ])
        .output()?;
    assert!(!early_renew_output.status.success());
    let early_renew_payload =
        serde_json::from_slice::<serde_json::Value>(&early_renew_output.stderr)?;
    assert_eq!(
        early_renew_payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.checkpoint.recovery_claim_not_yet_active"),
    );
    assert_eq!(
        early_renew_payload
            .pointer("/error/claimedAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(1_000),
    );
    assert_eq!(
        early_renew_payload
            .pointer("/error/nowUnixMs")
            .and_then(|value| value.as_u64()),
        Some(999),
    );

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
fn graphblocksd_sets_run_status_only_with_current_ownership_lease()
-> Result<(), Box<dyn std::error::Error>> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time should be after epoch")
        .as_nanos();
    let path = std::env::temp_dir().join(format!("graphblocksd-run-status-{unique}.sqlite3"));
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
        let run = store
            .create_run_with_invocation_mode(
                "sha256:graph",
                json!({"task": "background"}),
                RunInvocationMode::Background,
            )
            .map_err(|error| format!("{error:?}"))?;
        store
            .acquire_ownership_lease(&run.run_id, "coordinator-a", 1000, 2000)
            .map_err(|error| format!("{error:?}"))?;
    }

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "set-run-status-with-lease",
            "--run-store",
            path_text,
            "--run-id",
            "run-000001",
            "--status",
            "running",
            "--owner",
            "coordinator-a",
            "--lease-id",
            "run-000001:1",
            "--fencing-epoch",
            "1",
            "--now-unix-ms",
            "1200",
        ])
        .output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stdout)?;

    assert_eq!(
        payload
            .pointer("/run/runId")
            .and_then(|value| value.as_str()),
        Some("run-000001"),
    );
    assert_eq!(
        payload
            .pointer("/run/status")
            .and_then(|value| value.as_str()),
        Some("running"),
    );
    assert_eq!(
        payload
            .pointer("/lease/leaseId")
            .and_then(|value| value.as_str()),
        Some("run-000001:1"),
    );
    assert_eq!(
        SqliteRunStore::open(&path)
            .map_err(|error| format!("{error:?}"))?
            .get_run("run-000001")
            .map_err(|error| format!("{error:?}"))?
            .status,
        RunStatus::Running,
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_rejects_forged_run_status_lease_identity() -> Result<(), Box<dyn std::error::Error>>
{
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time should be after epoch")
        .as_nanos();
    let path =
        std::env::temp_dir().join(format!("graphblocksd-run-status-forged-{unique}.sqlite3"));
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let mut store = SqliteRunStore::open(&path).map_err(|error| format!("{error:?}"))?;
        let run = store
            .create_run_with_invocation_mode(
                "sha256:graph",
                json!({"task": "background"}),
                RunInvocationMode::Background,
            )
            .map_err(|error| format!("{error:?}"))?;
        store
            .acquire_ownership_lease(&run.run_id, "coordinator-a", 1000, 2000)
            .map_err(|error| format!("{error:?}"))?;
    }

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "set-run-status-with-lease",
            "--run-store",
            path_text,
            "--run-id",
            "run-000001",
            "--status",
            "running",
            "--owner",
            "coordinator-forged",
            "--lease-id",
            "run-000001:1",
            "--fencing-epoch",
            "1",
            "--now-unix-ms",
            "1200",
        ])
        .output()?;
    assert!(!output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stderr)?;

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
        SqliteRunStore::open(&path)
            .map_err(|error| format!("{error:?}"))?
            .get_run("run-000001")
            .map_err(|error| format!("{error:?}"))?
            .status,
        RunStatus::Created,
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_registers_async_operation_for_callback_wait()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("register-callback-wait");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "register-async-operation",
            "--async-operation-store",
            path_text,
            "--operation-id",
            "op-1",
            "--run-id",
            "run-1",
            "--node-id",
            "node-ci",
            "--attempt-id",
            "attempt-1",
            "--kind",
            "ci_job",
            "--resume-token-hash",
            VALID_RESUME_TOKEN_HASH,
            "--idempotency-key",
            "idem-op-1",
            "--expected-schema",
            "schemas/CICallback@1",
            "--created-at-unix-ms",
            "1000",
            "--provider-operation-id",
            "gha-run-1",
            "--submitted-at-unix-ms",
            "1050",
            "--waiting-callback-expires-at-unix-ms",
            "2000",
        ])
        .output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stdout)?;

    assert_eq!(
        payload.pointer("/ok").and_then(|value| value.as_bool()),
        Some(true),
    );
    assert_eq!(
        payload
            .pointer("/operation/operationId")
            .and_then(|value| value.as_str()),
        Some("op-1"),
    );
    assert_eq!(
        payload
            .pointer("/operation/kind")
            .and_then(|value| value.as_str()),
        Some("ci_job"),
    );
    assert_eq!(
        payload
            .pointer("/operation/state")
            .and_then(|value| value.as_str()),
        Some("waiting_callback"),
    );
    assert_eq!(
        payload
            .pointer("/operation/expiresAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(2000),
    );

    let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback),
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .into_iter()
            .filter(|event| matches!(event, AsyncOperationEvent::StateChanged { .. }))
            .count(),
        2,
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_rejects_waiting_callback_async_operation_without_timeout()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("register-callback-without-timeout");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "register-async-operation",
            "--async-operation-store",
            path_text,
            "--operation-id",
            "op-1",
            "--run-id",
            "run-1",
            "--node-id",
            "node-ci",
            "--attempt-id",
            "attempt-1",
            "--kind",
            "ci_job",
            "--resume-token-hash",
            VALID_RESUME_TOKEN_HASH,
            "--idempotency-key",
            "idem-op-1",
            "--expected-schema",
            "schemas/CICallback@1",
            "--created-at-unix-ms",
            "1000",
            "--provider-operation-id",
            "gha-run-1",
            "--submitted-at-unix-ms",
            "1050",
            "--waiting-callback",
        ])
        .output()?;
    assert!(!output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stderr)?;

    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.async_operation.invalid_operation"),
    );
    assert_eq!(
        payload
            .pointer("/error/reason")
            .and_then(|value| value.as_str()),
        Some("waiting callback operations require an expiration or infinite_wait_policy"),
    );
    assert_eq!(
        SqliteAsyncOperationStore::open(&path)
            .map_err(|error| format!("{error:?}"))?
            .operation_state("op-1"),
        None,
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_quarantines_early_async_callback_and_accepts_after_registration()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("quarantine-callback");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    let quarantined = quarantine_daemon_ci_callback(path_text, "cb-early", "idem-early", "5000")?;
    assert_eq!(
        quarantined.pointer("/ok").and_then(|value| value.as_bool()),
        Some(true),
    );
    assert_eq!(
        quarantined
            .pointer("/quarantined/duplicate")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        quarantined
            .pointer("/quarantined/expiresAtUnixMs")
            .and_then(|value| value.as_u64()),
        Some(5000),
    );

    let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(store.operation_state("op-1"), None);
    assert_eq!(store.quarantined_callback_count("op-1"), 1);

    let registered = register_daemon_waiting_operation(path_text)?;
    assert_eq!(
        registered
            .pointer("/operation/state")
            .and_then(|value| value.as_str()),
        Some("waiting_callback"),
    );

    let accepted = accept_quarantined_daemon_callbacks(path_text)?;
    assert_eq!(
        accepted
            .pointer("/acceptedCount")
            .and_then(|value| value.as_u64()),
        Some(1),
    );
    assert_eq!(
        accepted
            .pointer("/accepted/0/shouldResume")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        accepted
            .pointer("/accepted/0/receipt/callbackId")
            .and_then(|value| value.as_str()),
        Some("cb-early"),
    );

    let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived),
    );
    assert_eq!(store.quarantined_callback_count("op-1"), 0);

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_quarantined_duplicate_callback_replays_once()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("quarantine-callback-duplicate");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    let first = quarantine_daemon_ci_callback(path_text, "cb-early", "idem-early", "5000")?;
    let duplicate =
        quarantine_daemon_ci_callback(path_text, "cb-early-duplicate", "idem-early", "5001")?;
    assert_eq!(
        first
            .pointer("/quarantined/duplicate")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        duplicate
            .pointer("/quarantined/duplicate")
            .and_then(|value| value.as_bool()),
        Some(true),
    );
    assert_eq!(
        duplicate
            .pointer("/quarantined/callbackId")
            .and_then(|value| value.as_str()),
        Some("cb-early-duplicate"),
    );

    let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(store.quarantined_callback_count("op-1"), 1);

    register_daemon_waiting_operation(path_text)?;
    let accepted = accept_quarantined_daemon_callbacks(path_text)?;
    assert_eq!(
        accepted
            .pointer("/acceptedCount")
            .and_then(|value| value.as_u64()),
        Some(1),
    );
    assert_eq!(
        accepted
            .pointer("/accepted/0/receipt/callbackId")
            .and_then(|value| value.as_str()),
        Some("cb-early"),
    );

    let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(store.quarantined_callback_count("op-1"), 0);
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| matches!(event, AsyncOperationEvent::ExternalCallbackReceived { .. }))
            .count(),
        1,
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_cancels_async_operation_and_records_late_callback_without_resume()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("cancel-callback-wait");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "cancel-async-operation",
            "--async-operation-store",
            path_text,
            "--operation-id",
            "op-1",
            "--cancelled-at-unix-ms",
            "1300",
        ])
        .output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stdout)?;

    assert_eq!(
        payload.pointer("/ok").and_then(|value| value.as_bool()),
        Some(true),
    );
    assert_eq!(
        payload
            .pointer("/operation/state")
            .and_then(|value| value.as_str()),
        Some("cancelled"),
    );

    let callback_payload =
        submit_daemon_ci_callback(path_text, "cb-cancelled", "idem-cancelled", "1400")?;
    assert_eq!(
        callback_payload
            .pointer("/accepted/shouldResume")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        callback_payload
            .pointer("/accepted/duplicate")
            .and_then(|value| value.as_bool()),
        Some(false),
    );

    let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::Cancelled),
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    AsyncOperationEvent::LateExternalCallbackReceived {
                        terminal_state: AsyncOperationState::Cancelled,
                        ..
                    }
                )
            })
            .count(),
        1,
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_reports_storage_failure_when_terminal_response_reload_is_corrupt()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("cancel-corrupt-response-reload");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }
    {
        let connection = Connection::open(&path)?;
        connection.execute_batch(
            "
            CREATE TRIGGER corrupt_async_operation_after_insert
            AFTER INSERT ON async_operations
            BEGIN
                UPDATE async_operations
                SET operation_json = 'not-json'
                WHERE operation_id = NEW.operation_id;
            END;
            ",
        )?;
    }

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "cancel-async-operation",
            "--async-operation-store",
            path_text,
            "--operation-id",
            "op-1",
            "--cancelled-at-unix-ms",
            "1300",
        ])
        .output()?;

    assert!(!output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stderr)?;
    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.async_operation.storage"),
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_expires_async_operation_and_records_late_callback_without_resume()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("expire-callback-wait");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }
    assert_eq!(
        SqliteAsyncOperationStore::open(&path)
            .map_err(|error| format!("{error:?}"))?
            .operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback),
    );

    let output = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "expire-async-operation",
            "--async-operation-store",
            path_text,
            "--operation-id",
            "op-1",
            "--expired-at-unix-ms",
            "2001",
        ])
        .output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr),
    );
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stdout)?;

    assert_eq!(
        payload.pointer("/ok").and_then(|value| value.as_bool()),
        Some(true),
    );
    assert_eq!(
        payload
            .pointer("/operation/state")
            .and_then(|value| value.as_str()),
        Some("expired"),
    );

    let callback_payload =
        submit_daemon_ci_callback(path_text, "cb-expired", "idem-expired", "2100")?;
    assert_eq!(
        callback_payload
            .pointer("/accepted/shouldResume")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        callback_payload
            .pointer("/accepted/duplicate")
            .and_then(|value| value.as_bool()),
        Some(false),
    );

    let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
    assert_eq!(
        store.operation_state("op-1"),
        Some(AsyncOperationState::Expired),
    );
    assert_eq!(
        store
            .events_for_operation("op-1")
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    AsyncOperationEvent::LateExternalCallbackReceived {
                        terminal_state: AsyncOperationState::Expired,
                        ..
                    }
                )
            })
            .count(),
        1,
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_submits_async_callback_through_sqlite_store()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("submit-callback");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }

    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "submit-async-callback",
            "--async-operation-store",
            path_text,
            "--callback-id",
            "cb-1",
            "--operation-id",
            "op-1",
            "--run-id",
            "run-1",
            "--node-id",
            "node-ci",
            "--attempt-id",
            "attempt-1",
            "--provider-operation-id",
            "gha-run-1",
            "--idempotency-key",
            "idem-cb-1",
            "--received-at-unix-ms",
            "1200",
            "--verified-by",
            "hmac:callback-endpoint-1",
            "--policy-snapshot-id",
            "policy-snapshot-1",
            "--schema-id",
            "schemas/CICallback@1",
            "--schema-json",
            r#"{"type":"object","required":["status","workflow_run_id"],"properties":{"status":{"type":"string"},"workflow_run_id":{"type":"string"}}}"#,
            "--authentication-verified",
            "--resume-policy-decision-id",
            "policy-reevaluation-1",
            "--resume-budget-reservation-id",
            "budget-reservation-1",
            "--resume-compatible-release-id",
            "release-1",
            "--resume-ownership-fence-token",
            "lease-generation-7",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or("graphblocksd stdin pipe was not available")?;
    stdin.write_all(
        serde_json::to_string(&json!({"status": "completed", "workflow_run_id": "gha-run-1"}))?
            .as_bytes(),
    )?;

    let output = child.wait_with_output()?;
    assert!(output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stdout)?;

    assert_eq!(
        payload.pointer("/ok").and_then(|value| value.as_bool()),
        Some(true),
    );
    assert_eq!(
        payload
            .pointer("/accepted/shouldResume")
            .and_then(|value| value.as_bool()),
        Some(true),
    );
    assert_eq!(
        payload
            .pointer("/accepted/duplicate")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        payload
            .pointer("/receipt/operationId")
            .and_then(|value| value.as_str()),
        Some("op-1"),
    );
    assert_eq!(
        payload
            .pointer("/receipt/callbackId")
            .and_then(|value| value.as_str()),
        Some("cb-1"),
    );
    assert_eq!(
        SqliteAsyncOperationStore::open(&path)
            .map_err(|error| format!("{error:?}"))?
            .operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived),
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_verified_by_text_alone_cannot_authorize_callback_resume()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("submit-callback-fail-closed");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }

    let payload = submit_daemon_ci_callback(
        path_text,
        "cb-untrusted-provenance",
        "idem-untrusted-provenance",
        "1200",
    )?;

    assert_eq!(
        payload
            .pointer("/accepted/shouldResume")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        SqliteAsyncOperationStore::open(&path)
            .map_err(|error| format!("{error:?}"))?
            .operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived),
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_requires_authentication_and_every_resume_gate_for_resume()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("submit-callback-authorized");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }

    let payload = submit_daemon_ci_callback_with_resume_args(
        path_text,
        "cb-authorized",
        "idem-authorized",
        "1200",
        &[
            "--authentication-verified",
            "--resume-policy-decision-id",
            "policy-reevaluation-1",
            "--resume-budget-reservation-id",
            "budget-reservation-1",
            "--resume-compatible-release-id",
            "release-1",
            "--resume-ownership-fence-token",
            "lease-generation-7",
        ],
    )?;

    assert_eq!(
        payload
            .pointer("/accepted/shouldResume")
            .and_then(|value| value.as_bool()),
        Some(true),
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_holds_resume_when_gate_evidence_lacks_explicit_authentication()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("submit-callback-auth-held");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }

    let payload = submit_daemon_ci_callback_with_resume_args(
        path_text,
        "cb-auth-held",
        "idem-auth-held",
        "1200",
        &[
            "--resume-policy-decision-id",
            "policy-reevaluation-1",
            "--resume-budget-reservation-id",
            "budget-reservation-1",
            "--resume-compatible-release-id",
            "release-1",
            "--resume-ownership-fence-token",
            "lease-generation-7",
        ],
    )?;

    assert_eq!(
        payload
            .pointer("/accepted/shouldResume")
            .and_then(|value| value.as_bool()),
        Some(false),
    );
    assert_eq!(
        SqliteAsyncOperationStore::open(&path)
            .map_err(|error| format!("{error:?}"))?
            .operation_state("op-1"),
        Some(AsyncOperationState::CallbackReceived),
    );

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_submitted_async_callback_duplicate_does_not_resume_twice()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("submit-callback-duplicate");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }

    for callback_id in ["cb-1", "cb-duplicate"] {
        let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
            .args([
                "submit-async-callback",
                "--async-operation-store",
                path_text,
                "--callback-id",
                callback_id,
                "--operation-id",
                "op-1",
                "--run-id",
                "run-1",
                "--node-id",
                "node-ci",
                "--attempt-id",
                "attempt-1",
                "--provider-operation-id",
                "gha-run-1",
                "--idempotency-key",
                "idem-cb-1",
                "--received-at-unix-ms",
                "1200",
                "--verified-by",
                "hmac:callback-endpoint-1",
                "--policy-snapshot-id",
                "policy-snapshot-1",
                "--schema-id",
                "schemas/CICallback@1",
                "--schema-json",
                r#"{"type":"object","required":["status","workflow_run_id"],"properties":{"status":{"type":"string"},"workflow_run_id":{"type":"string"}}}"#,
                "--authentication-verified",
                "--resume-policy-decision-id",
                "policy-reevaluation-1",
                "--resume-budget-reservation-id",
                "budget-reservation-1",
                "--resume-compatible-release-id",
                "release-1",
                "--resume-ownership-fence-token",
                "lease-generation-7",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()?;
        let stdin = child
            .stdin
            .as_mut()
            .ok_or("graphblocksd stdin pipe was not available")?;
        stdin.write_all(
            serde_json::to_string(&json!({"status": "completed", "workflow_run_id": "gha-run-1"}))?
                .as_bytes(),
        )?;
        let output = child.wait_with_output()?;
        assert!(output.status.success());
        let payload = serde_json::from_slice::<serde_json::Value>(&output.stdout)?;

        if callback_id == "cb-1" {
            assert_eq!(
                payload
                    .pointer("/accepted/shouldResume")
                    .and_then(|value| value.as_bool()),
                Some(true),
            );
            assert_eq!(
                payload
                    .pointer("/accepted/duplicate")
                    .and_then(|value| value.as_bool()),
                Some(false),
            );
        } else {
            assert_eq!(
                payload
                    .pointer("/accepted/shouldResume")
                    .and_then(|value| value.as_bool()),
                Some(false),
            );
            assert_eq!(
                payload
                    .pointer("/accepted/duplicate")
                    .and_then(|value| value.as_bool()),
                Some(true),
            );
            assert_eq!(
                payload
                    .pointer("/receipt/callbackId")
                    .and_then(|value| value.as_str()),
                Some("cb-1"),
            );
        }
    }

    let _ = std::fs::remove_file(&path);
    Ok(())
}

#[test]
fn graphblocksd_rejects_schema_invalid_async_callback_without_resume()
-> Result<(), Box<dyn std::error::Error>> {
    let path = sqlite_async_operation_path("submit-callback-schema-invalid");
    let path_text = path.to_str().ok_or("temp path is not utf-8")?;
    let _ = std::fs::remove_file(&path);

    {
        let store = SqliteAsyncOperationStore::open(&path).map_err(|error| format!("{error:?}"))?;
        store
            .register(waiting_daemon_async_operation())
            .map_err(|error| format!("{error:?}"))?;
    }

    let mut child = Command::new(env!("CARGO_BIN_EXE_graphblocksd"))
        .args([
            "submit-async-callback",
            "--async-operation-store",
            path_text,
            "--callback-id",
            "cb-schema-invalid",
            "--operation-id",
            "op-1",
            "--run-id",
            "run-1",
            "--node-id",
            "node-ci",
            "--attempt-id",
            "attempt-1",
            "--provider-operation-id",
            "gha-run-1",
            "--idempotency-key",
            "idem-schema-invalid",
            "--received-at-unix-ms",
            "1200",
            "--verified-by",
            "hmac:callback-endpoint-1",
            "--policy-snapshot-id",
            "policy-snapshot-1",
            "--schema-id",
            "schemas/CICallback@1",
            "--schema-json",
            r#"{"type":"object","required":["status","workflow_run_id"],"properties":{"status":{"type":"string"},"workflow_run_id":{"type":"string"}}}"#,
        ])
        .stdin(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let stdin = child
        .stdin
        .as_mut()
        .ok_or("graphblocksd stdin pipe was not available")?;
    stdin.write_all(serde_json::to_string(&json!({"status": "completed"}))?.as_bytes())?;

    let output = child.wait_with_output()?;
    assert!(!output.status.success());
    let payload = serde_json::from_slice::<serde_json::Value>(&output.stderr)?;

    assert_eq!(
        payload
            .pointer("/error/code")
            .and_then(|value| value.as_str()),
        Some("daemon.async_operation.callback_schema_invalid"),
    );
    assert_eq!(
        payload
            .pointer("/error/expected")
            .and_then(|value| value.as_str()),
        Some("required property workflow_run_id"),
    );
    assert_eq!(
        SqliteAsyncOperationStore::open(&path)
            .map_err(|error| format!("{error:?}"))?
            .operation_state("op-1"),
        Some(AsyncOperationState::WaitingCallback),
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
fn worker_registry_evicts_known_worker_after_rejected_unready_refresh()
-> Result<(), DaemonConfigError> {
    let mut registry = WorkerRegistry::new(DaemonConfig::new("daemon-1", "127.0.0.1:8080"))?;
    let ready = WorkerAdvertisement::new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [BlockCapability::new("document.parse@1")],
    );
    assert!(registry.admit_worker(ready.clone()).admitted);

    let rejected = registry.admit_worker(ready.with_state(WorkerState::Unhealthy));

    assert!(!rejected.admitted);
    assert_eq!(rejected.reason_codes, vec!["worker.not_ready"]);
    assert!(registry.ready_worker_ids().is_empty());
    assert_eq!(registry.status().admitted_workers, 0);
    assert_eq!(registry.status().rejected_workers, 1);
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
