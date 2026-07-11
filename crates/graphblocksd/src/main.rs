use std::io::{self, Read};

use graphblocks_protocol::WorkerProtocolMessageKind;
use graphblocks_runtime_durable::{CheckpointRecoveryClaim, SqliteCheckpointStore};
use graphblocksd::{DaemonConfig, DaemonStatus, WorkerRegistry, WorkerRegistryError};
use serde_json::{Value, json};

#[derive(Clone, Debug, Eq, PartialEq)]
struct AdmitWorkerMessageOptions {
    daemon_id: String,
    bind_address: String,
    package_lock_hash: Option<String>,
    max_workers: usize,
    response_message_id: String,
    response_sequence: u64,
}

impl Default for AdmitWorkerMessageOptions {
    fn default() -> Self {
        Self {
            daemon_id: "daemon-1".to_owned(),
            bind_address: "127.0.0.1:0".to_owned(),
            package_lock_hash: None,
            max_workers: 1024,
            response_message_id: "message-daemon-1".to_owned(),
            response_sequence: 1,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum CliError {
    Usage(String),
    ReadStdin(String),
    ParseJson(String),
    Config(String),
    Registry(WorkerRegistryError),
    CheckpointStore(String),
    Render(String),
}

fn main() {
    let mut args = std::env::args().skip(1);
    let command = args.next();
    let result = match command.as_deref() {
        Some("admit-worker-message") => run_admit_worker_message(args.collect()),
        Some("claim-checkpoint") => run_claim_checkpoint(args.collect()),
        Some("complete-checkpoint-claim") => run_complete_checkpoint_claim(args.collect()),
        _ => Err(CliError::Usage(
            "usage: graphblocksd <admit-worker-message|claim-checkpoint|complete-checkpoint-claim> [options]".to_owned(),
        )),
    };

    match result {
        Ok(payload) => {
            if let Err(error) = print_json(&payload, false) {
                let _ = print_json(&error.to_json(), true);
                std::process::exit(error.exit_code());
            }
        }
        Err(error) => {
            let exit_code = error.exit_code();
            let _ = print_json(&error.to_json(), true);
            std::process::exit(exit_code);
        }
    }
}

fn run_admit_worker_message(args: Vec<String>) -> Result<Value, CliError> {
    let options = parse_admit_worker_message_options(args)?;
    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .map_err(|error| CliError::ReadStdin(error.to_string()))?;
    let message = serde_json::from_str::<Value>(&input)
        .map_err(|error| CliError::ParseJson(error.to_string()))?;

    let mut config = DaemonConfig::new(options.daemon_id, options.bind_address)
        .with_max_workers(options.max_workers);
    if let Some(package_lock_hash) = options.package_lock_hash {
        config = config.require_package_lock_hash(package_lock_hash);
    }
    let mut registry =
        WorkerRegistry::new(config).map_err(|error| CliError::Config(format!("{error:?}")))?;
    let response = registry
        .admit_worker_message_wire_value(
            &message,
            options.response_message_id,
            options.response_sequence,
        )
        .map_err(CliError::Registry)?;
    let status = registry.status();

    Ok(json!({
        "ok": true,
        "response": response,
        "status": daemon_status_json(&status),
    }))
}

fn run_claim_checkpoint(args: Vec<String>) -> Result<Value, CliError> {
    let mut checkpoint_store = None;
    let mut run_id = None;
    let mut release_id = None;
    let mut deployment_revision_id = None;
    let mut plan_hash = None;
    let mut worker_id = None;
    let mut lease_id = None;
    let mut now_unix_ms = None;
    let mut expires_at_unix_ms = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--checkpoint-store" => {
                checkpoint_store = Some(next_arg(&mut args, "--checkpoint-store")?);
            }
            "--run-id" => {
                run_id = Some(next_arg(&mut args, "--run-id")?);
            }
            "--release-id" => {
                release_id = Some(next_arg(&mut args, "--release-id")?);
            }
            "--deployment-revision-id" => {
                deployment_revision_id = Some(next_arg(&mut args, "--deployment-revision-id")?);
            }
            "--plan-hash" => {
                plan_hash = Some(next_arg(&mut args, "--plan-hash")?);
            }
            "--worker-id" => {
                worker_id = Some(next_arg(&mut args, "--worker-id")?);
            }
            "--lease-id" => {
                lease_id = Some(next_arg(&mut args, "--lease-id")?);
            }
            "--now-unix-ms" => {
                let value = next_arg(&mut args, "--now-unix-ms")?;
                now_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--now-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            "--expires-at-unix-ms" => {
                let value = next_arg(&mut args, "--expires-at-unix-ms")?;
                expires_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--expires-at-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            _ => return Err(CliError::Usage(format!("unsupported argument: {arg}"))),
        }
    }

    let checkpoint_store = checkpoint_store
        .ok_or_else(|| CliError::Usage("--checkpoint-store is required".to_owned()))?;
    let run_id = run_id.ok_or_else(|| CliError::Usage("--run-id is required".to_owned()))?;
    let release_id =
        release_id.ok_or_else(|| CliError::Usage("--release-id is required".to_owned()))?;
    let deployment_revision_id = deployment_revision_id
        .ok_or_else(|| CliError::Usage("--deployment-revision-id is required".to_owned()))?;
    let plan_hash =
        plan_hash.ok_or_else(|| CliError::Usage("--plan-hash is required".to_owned()))?;
    let worker_id =
        worker_id.ok_or_else(|| CliError::Usage("--worker-id is required".to_owned()))?;
    let lease_id = lease_id.ok_or_else(|| CliError::Usage("--lease-id is required".to_owned()))?;
    let now_unix_ms =
        now_unix_ms.ok_or_else(|| CliError::Usage("--now-unix-ms is required".to_owned()))?;
    let expires_at_unix_ms = expires_at_unix_ms
        .ok_or_else(|| CliError::Usage("--expires-at-unix-ms is required".to_owned()))?;

    let mut store = SqliteCheckpointStore::open(checkpoint_store)
        .map_err(|error| CliError::CheckpointStore(format!("{error:?}")))?;
    let recovery = store
        .claim_latest_compatible(
            &run_id,
            &release_id,
            &deployment_revision_id,
            &plan_hash,
            &worker_id,
            &lease_id,
            now_unix_ms,
            expires_at_unix_ms,
        )
        .map_err(|error| CliError::CheckpointStore(format!("{error:?}")))?;

    Ok(json!({
        "ok": true,
        "checkpoint": {
            "checkpointId": recovery.checkpoint.checkpoint_id,
            "runId": recovery.checkpoint.run_id,
            "releaseId": recovery.checkpoint.release_id,
            "deploymentRevisionId": recovery.checkpoint.deployment_revision_id,
            "planHash": recovery.checkpoint.plan_hash,
            "stateRevision": recovery.checkpoint.state_revision,
        },
        "claim": {
            "runId": recovery.claim.run_id,
            "checkpointId": recovery.claim.checkpoint_id,
            "workerId": recovery.claim.worker_id,
            "leaseId": recovery.claim.lease_id,
            "fencingEpoch": recovery.claim.fencing_epoch,
            "claimedAtUnixMs": recovery.claim.claimed_at_unix_ms,
            "expiresAtUnixMs": recovery.claim.expires_at_unix_ms,
        },
    }))
}

fn run_complete_checkpoint_claim(args: Vec<String>) -> Result<Value, CliError> {
    let mut checkpoint_store = None;
    let mut run_id = None;
    let mut checkpoint_id = None;
    let mut worker_id = None;
    let mut lease_id = None;
    let mut fencing_epoch = None;
    let mut claimed_at_unix_ms = None;
    let mut expires_at_unix_ms = None;
    let mut now_unix_ms = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--checkpoint-store" => {
                checkpoint_store = Some(next_arg(&mut args, "--checkpoint-store")?);
            }
            "--run-id" => {
                run_id = Some(next_arg(&mut args, "--run-id")?);
            }
            "--checkpoint-id" => {
                checkpoint_id = Some(next_arg(&mut args, "--checkpoint-id")?);
            }
            "--worker-id" => {
                worker_id = Some(next_arg(&mut args, "--worker-id")?);
            }
            "--lease-id" => {
                lease_id = Some(next_arg(&mut args, "--lease-id")?);
            }
            "--fencing-epoch" => {
                let value = next_arg(&mut args, "--fencing-epoch")?;
                fencing_epoch = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--fencing-epoch requires an unsigned integer: {error}"
                    ))
                })?);
            }
            "--claimed-at-unix-ms" => {
                let value = next_arg(&mut args, "--claimed-at-unix-ms")?;
                claimed_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--claimed-at-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            "--expires-at-unix-ms" => {
                let value = next_arg(&mut args, "--expires-at-unix-ms")?;
                expires_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--expires-at-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            "--now-unix-ms" => {
                let value = next_arg(&mut args, "--now-unix-ms")?;
                now_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--now-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            _ => return Err(CliError::Usage(format!("unsupported argument: {arg}"))),
        }
    }

    let checkpoint_store = checkpoint_store
        .ok_or_else(|| CliError::Usage("--checkpoint-store is required".to_owned()))?;
    let claim = CheckpointRecoveryClaim {
        run_id: run_id.ok_or_else(|| CliError::Usage("--run-id is required".to_owned()))?,
        checkpoint_id: checkpoint_id
            .ok_or_else(|| CliError::Usage("--checkpoint-id is required".to_owned()))?,
        worker_id: worker_id
            .ok_or_else(|| CliError::Usage("--worker-id is required".to_owned()))?,
        lease_id: lease_id.ok_or_else(|| CliError::Usage("--lease-id is required".to_owned()))?,
        fencing_epoch: fencing_epoch
            .ok_or_else(|| CliError::Usage("--fencing-epoch is required".to_owned()))?,
        claimed_at_unix_ms: claimed_at_unix_ms
            .ok_or_else(|| CliError::Usage("--claimed-at-unix-ms is required".to_owned()))?,
        expires_at_unix_ms: expires_at_unix_ms
            .ok_or_else(|| CliError::Usage("--expires-at-unix-ms is required".to_owned()))?,
    };
    let now_unix_ms =
        now_unix_ms.ok_or_else(|| CliError::Usage("--now-unix-ms is required".to_owned()))?;
    let mut store = SqliteCheckpointStore::open(checkpoint_store)
        .map_err(|error| CliError::CheckpointStore(format!("{error:?}")))?;
    store
        .complete_claim(&claim, now_unix_ms)
        .map_err(|error| CliError::CheckpointStore(format!("{error:?}")))?;

    Ok(json!({
        "ok": true,
        "claim": {
            "runId": claim.run_id,
            "checkpointId": claim.checkpoint_id,
            "workerId": claim.worker_id,
            "leaseId": claim.lease_id,
            "fencingEpoch": claim.fencing_epoch,
            "claimedAtUnixMs": claim.claimed_at_unix_ms,
            "expiresAtUnixMs": claim.expires_at_unix_ms,
            "completedAtUnixMs": now_unix_ms,
        },
    }))
}

fn parse_admit_worker_message_options(
    args: Vec<String>,
) -> Result<AdmitWorkerMessageOptions, CliError> {
    let mut options = AdmitWorkerMessageOptions::default();
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--daemon-id" => {
                options.daemon_id = next_arg(&mut args, "--daemon-id")?;
            }
            "--bind-address" => {
                options.bind_address = next_arg(&mut args, "--bind-address")?;
            }
            "--package-lock-hash" => {
                options.package_lock_hash = Some(next_arg(&mut args, "--package-lock-hash")?);
            }
            "--max-workers" => {
                let value = next_arg(&mut args, "--max-workers")?;
                options.max_workers = value.parse::<usize>().map_err(|error| {
                    CliError::Usage(format!(
                        "--max-workers requires a positive integer: {error}"
                    ))
                })?;
            }
            "--response-message-id" => {
                options.response_message_id = next_arg(&mut args, "--response-message-id")?;
            }
            "--response-sequence" => {
                let value = next_arg(&mut args, "--response-sequence")?;
                options.response_sequence = value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--response-sequence requires an unsigned integer: {error}"
                    ))
                })?;
            }
            _ => return Err(CliError::Usage(format!("unsupported argument: {arg}"))),
        }
    }
    Ok(options)
}

fn next_arg(
    args: &mut impl Iterator<Item = String>,
    flag: &'static str,
) -> Result<String, CliError> {
    args.next()
        .ok_or_else(|| CliError::Usage(format!("{flag} requires an argument")))
}

fn daemon_status_json(status: &DaemonStatus) -> Value {
    json!({
        "daemonId": status.daemon_id,
        "bindAddress": status.bind_address,
        "protocolVersion": status.protocol_version,
        "readyWorkers": status.ready_workers,
        "saturatedWorkers": status.saturated_workers,
        "drainingWorkers": status.draining_workers,
        "admittedWorkers": status.admitted_workers,
        "rejectedWorkers": status.rejected_workers,
    })
}

fn print_json(value: &Value, stderr: bool) -> Result<(), CliError> {
    let rendered =
        serde_json::to_string_pretty(value).map_err(|error| CliError::Render(error.to_string()))?;
    if stderr {
        eprintln!("{rendered}");
    } else {
        println!("{rendered}");
    }
    Ok(())
}

impl CliError {
    fn exit_code(&self) -> i32 {
        match self {
            Self::Usage(_) | Self::ReadStdin(_) | Self::ParseJson(_) | Self::Config(_) => 2,
            Self::Registry(_) | Self::CheckpointStore(_) | Self::Render(_) => 1,
        }
    }

    fn to_json(&self) -> Value {
        match self {
            Self::Usage(message) => {
                json!({"ok": false, "error": {"code": "usage", "message": message}})
            }
            Self::ReadStdin(message) => {
                json!({"ok": false, "error": {"code": "stdin.read_failed", "message": message}})
            }
            Self::ParseJson(message) => {
                json!({"ok": false, "error": {"code": "json.parse_failed", "message": message}})
            }
            Self::Config(message) => {
                json!({"ok": false, "error": {"code": "daemon.invalid_config", "message": message}})
            }
            Self::Registry(error) => {
                json!({"ok": false, "error": worker_registry_error_json(error)})
            }
            Self::CheckpointStore(message) => {
                json!({"ok": false, "error": {"code": "daemon.checkpoint_store", "message": message}})
            }
            Self::Render(message) => {
                json!({"ok": false, "error": {"code": "json.render_failed", "message": message}})
            }
        }
    }
}

fn worker_registry_error_json(error: &WorkerRegistryError) -> Value {
    match error {
        WorkerRegistryError::UnknownWorker { worker_id } => {
            json!({"code": "daemon.unknown_worker", "workerId": worker_id})
        }
        WorkerRegistryError::DrainPlan { source } => {
            json!({"code": "daemon.invalid_drain_plan", "message": format!("{source:?}")})
        }
        WorkerRegistryError::IncompatibleMessageProtocolVersion { expected, actual } => json!({
            "code": "daemon.incompatible_message_protocol_version",
            "expected": expected,
            "actual": actual,
        }),
        WorkerRegistryError::EmptyMessageId => json!({"code": "daemon.empty_message_id"}),
        WorkerRegistryError::EmptyCorrelationId => json!({"code": "daemon.empty_correlation_id"}),
        WorkerRegistryError::EmptyCausationId => json!({"code": "daemon.empty_causation_id"}),
        WorkerRegistryError::KindPayloadMismatch { kind, payload_kind } => json!({
            "code": "daemon.kind_payload_mismatch",
            "kind": worker_message_kind_name(*kind),
            "payloadKind": worker_message_kind_name(*payload_kind),
        }),
        WorkerRegistryError::UnexpectedWorkerMessageKind { kind } => json!({
            "code": "daemon.unexpected_worker_message_kind",
            "kind": worker_message_kind_name(*kind),
        }),
        WorkerRegistryError::InvalidWireMessage { field, expected } => json!({
            "code": "daemon.invalid_wire_message",
            "field": field,
            "expected": expected,
        }),
        WorkerRegistryError::WirePayloadDecode { kind, source } => json!({
            "code": "daemon.wire_payload_decode_failed",
            "kind": worker_message_kind_name(*kind),
            "message": source,
        }),
    }
}

fn worker_message_kind_name(kind: WorkerProtocolMessageKind) -> &'static str {
    match kind {
        WorkerProtocolMessageKind::Advertisement => "advertisement",
        WorkerProtocolMessageKind::AdmissionDecision => "admission_decision",
        WorkerProtocolMessageKind::InvokeRequest => "invoke_request",
        WorkerProtocolMessageKind::InvokeResult => "invoke_result",
        WorkerProtocolMessageKind::DrainPlan => "drain_plan",
        WorkerProtocolMessageKind::Error => "error",
    }
}
