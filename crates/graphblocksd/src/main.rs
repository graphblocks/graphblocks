use std::io::{self, Read};

use graphblocks_protocol::WorkerProtocolMessageKind;
use graphblocks_runtime_core::async_operation::{
    AsyncCallbackSubmission, AsyncOperation, AsyncOperationError, AsyncOperationKind,
    AsyncOperationState, SqliteAsyncOperationStore,
};
use graphblocks_runtime_core::run_store::{
    RunOwnershipLease, RunStatus, RunStoreError, SqliteRunStore,
};
use graphblocks_runtime_core::tool_schema::{JsonSchema, ToolSchemaRegistry};
use graphblocks_runtime_durable::{
    CheckpointRecoveryClaim, CheckpointStoreError, SqliteCheckpointStore,
};
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

#[derive(Clone, Debug, PartialEq)]
enum CliError {
    Usage(String),
    ReadStdin(String),
    ParseJson(String),
    Config(String),
    Registry(WorkerRegistryError),
    RunStore(RunStoreError),
    AsyncOperation(AsyncOperationError),
    CheckpointStore(CheckpointStoreError),
    Render(String),
}

fn main() {
    let mut args = std::env::args().skip(1);
    let command = args.next();
    let result = match command.as_deref() {
        Some("admit-worker-message") => run_admit_worker_message(args.collect()),
        Some("acquire-run-lease") => run_acquire_run_lease(args.collect()),
        Some("renew-run-lease") => run_renew_run_lease(args.collect()),
        Some("set-run-status-with-lease") => run_set_run_status_with_lease(args.collect()),
        Some("register-async-operation") => run_register_async_operation(args.collect()),
        Some("submit-async-callback") => run_submit_async_callback(args.collect()),
        Some("claim-checkpoint") => run_claim_checkpoint(args.collect()),
        Some("renew-checkpoint-claim") => run_renew_checkpoint_claim(args.collect()),
        Some("complete-checkpoint-claim") => run_complete_checkpoint_claim(args.collect()),
        _ => Err(CliError::Usage(
            "usage: graphblocksd <admit-worker-message|acquire-run-lease|renew-run-lease|set-run-status-with-lease|register-async-operation|submit-async-callback|claim-checkpoint|renew-checkpoint-claim|complete-checkpoint-claim> [options]".to_owned(),
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

fn run_acquire_run_lease(args: Vec<String>) -> Result<Value, CliError> {
    let mut run_store = None;
    let mut run_id = None;
    let mut owner = None;
    let mut acquired_at_unix_ms = None;
    let mut expires_at_unix_ms = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--run-store" => {
                run_store = Some(next_arg(&mut args, "--run-store")?);
            }
            "--run-id" => {
                run_id = Some(next_arg(&mut args, "--run-id")?);
            }
            "--owner" => {
                owner = Some(next_arg(&mut args, "--owner")?);
            }
            "--acquired-at-unix-ms" => {
                let value = next_arg(&mut args, "--acquired-at-unix-ms")?;
                acquired_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--acquired-at-unix-ms requires an unsigned integer: {error}"
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

    let run_store =
        run_store.ok_or_else(|| CliError::Usage("--run-store is required".to_owned()))?;
    let run_id = run_id.ok_or_else(|| CliError::Usage("--run-id is required".to_owned()))?;
    let owner = owner.ok_or_else(|| CliError::Usage("--owner is required".to_owned()))?;
    let acquired_at_unix_ms = acquired_at_unix_ms
        .ok_or_else(|| CliError::Usage("--acquired-at-unix-ms is required".to_owned()))?;
    let expires_at_unix_ms = expires_at_unix_ms
        .ok_or_else(|| CliError::Usage("--expires-at-unix-ms is required".to_owned()))?;

    let mut store = SqliteRunStore::open(run_store).map_err(CliError::RunStore)?;
    let lease = store
        .acquire_ownership_lease(&run_id, &owner, acquired_at_unix_ms, expires_at_unix_ms)
        .map_err(CliError::RunStore)?;

    Ok(json!({
        "ok": true,
        "lease": run_ownership_lease_json(&lease),
    }))
}

fn run_renew_run_lease(args: Vec<String>) -> Result<Value, CliError> {
    let mut run_store = None;
    let mut run_id = None;
    let mut owner = None;
    let mut lease_id = None;
    let mut fencing_epoch = None;
    let mut now_unix_ms = None;
    let mut new_expires_at_unix_ms = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--run-store" => {
                run_store = Some(next_arg(&mut args, "--run-store")?);
            }
            "--run-id" => {
                run_id = Some(next_arg(&mut args, "--run-id")?);
            }
            "--owner" => {
                owner = Some(next_arg(&mut args, "--owner")?);
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
            "--now-unix-ms" => {
                let value = next_arg(&mut args, "--now-unix-ms")?;
                now_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--now-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            "--new-expires-at-unix-ms" => {
                let value = next_arg(&mut args, "--new-expires-at-unix-ms")?;
                new_expires_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--new-expires-at-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            _ => return Err(CliError::Usage(format!("unsupported argument: {arg}"))),
        }
    }

    let run_store =
        run_store.ok_or_else(|| CliError::Usage("--run-store is required".to_owned()))?;
    let run_id = run_id.ok_or_else(|| CliError::Usage("--run-id is required".to_owned()))?;
    let owner = owner.ok_or_else(|| CliError::Usage("--owner is required".to_owned()))?;
    let lease_id = lease_id.ok_or_else(|| CliError::Usage("--lease-id is required".to_owned()))?;
    let fencing_epoch =
        fencing_epoch.ok_or_else(|| CliError::Usage("--fencing-epoch is required".to_owned()))?;
    let now_unix_ms =
        now_unix_ms.ok_or_else(|| CliError::Usage("--now-unix-ms is required".to_owned()))?;
    let new_expires_at_unix_ms = new_expires_at_unix_ms
        .ok_or_else(|| CliError::Usage("--new-expires-at-unix-ms is required".to_owned()))?;

    let mut store = SqliteRunStore::open(run_store).map_err(CliError::RunStore)?;
    let lease = store
        .renew_ownership_lease(
            &run_id,
            &owner,
            &lease_id,
            fencing_epoch,
            now_unix_ms,
            new_expires_at_unix_ms,
        )
        .map_err(CliError::RunStore)?;
    let mut lease_json = run_ownership_lease_json(&lease);
    if let Some(object) = lease_json.as_object_mut() {
        object.insert("renewedAtUnixMs".to_owned(), json!(now_unix_ms));
    }

    Ok(json!({
        "ok": true,
        "lease": lease_json,
    }))
}

fn run_set_run_status_with_lease(args: Vec<String>) -> Result<Value, CliError> {
    let mut run_store = None;
    let mut run_id = None;
    let mut status = None;
    let mut owner = None;
    let mut lease_id = None;
    let mut fencing_epoch = None;
    let mut now_unix_ms = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--run-store" => {
                run_store = Some(next_arg(&mut args, "--run-store")?);
            }
            "--run-id" => {
                run_id = Some(next_arg(&mut args, "--run-id")?);
            }
            "--status" => {
                let value = next_arg(&mut args, "--status")?;
                status = Some(value.parse::<RunStatus>().map_err(|_| {
                    CliError::Usage(format!("--status uses an unsupported run status: {value}"))
                })?);
            }
            "--owner" => {
                owner = Some(next_arg(&mut args, "--owner")?);
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

    let run_store =
        run_store.ok_or_else(|| CliError::Usage("--run-store is required".to_owned()))?;
    let run_id = run_id.ok_or_else(|| CliError::Usage("--run-id is required".to_owned()))?;
    let status = status.ok_or_else(|| CliError::Usage("--status is required".to_owned()))?;
    let owner = owner.ok_or_else(|| CliError::Usage("--owner is required".to_owned()))?;
    let lease_id = lease_id.ok_or_else(|| CliError::Usage("--lease-id is required".to_owned()))?;
    let fencing_epoch =
        fencing_epoch.ok_or_else(|| CliError::Usage("--fencing-epoch is required".to_owned()))?;
    let now_unix_ms =
        now_unix_ms.ok_or_else(|| CliError::Usage("--now-unix-ms is required".to_owned()))?;

    let mut store = SqliteRunStore::open(run_store).map_err(CliError::RunStore)?;
    let run = store
        .set_status_with_ownership_lease(
            &run_id,
            status,
            &owner,
            &lease_id,
            fencing_epoch,
            now_unix_ms,
        )
        .map_err(CliError::RunStore)?;

    Ok(json!({
        "ok": true,
        "run": {
            "runId": run.run_id,
            "sequence": run.sequence,
            "invocationMode": run.invocation_mode.as_str(),
            "status": run.status.as_str(),
            "stateRevision": run.state_revision,
        },
        "lease": {
            "runId": run_id,
            "owner": owner,
            "leaseId": lease_id,
            "fencingEpoch": fencing_epoch,
            "validatedAtUnixMs": now_unix_ms,
        },
    }))
}

fn run_register_async_operation(args: Vec<String>) -> Result<Value, CliError> {
    let mut async_operation_store = None;
    let mut operation_id = None;
    let mut run_id = None;
    let mut node_id = None;
    let mut attempt_id = None;
    let mut kind = None;
    let mut resume_token_hash = None;
    let mut idempotency_key = None;
    let mut expected_schema = None;
    let mut created_at_unix_ms = None;
    let mut provider_operation_id = None;
    let mut submitted_at_unix_ms = None;
    let mut waiting_callback = false;
    let mut waiting_callback_expires_at_unix_ms = None;
    let mut infinite_wait_policy = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--async-operation-store" => {
                async_operation_store = Some(next_arg(&mut args, "--async-operation-store")?);
            }
            "--operation-id" => {
                operation_id = Some(next_arg(&mut args, "--operation-id")?);
            }
            "--run-id" => {
                run_id = Some(next_arg(&mut args, "--run-id")?);
            }
            "--node-id" => {
                node_id = Some(next_arg(&mut args, "--node-id")?);
            }
            "--attempt-id" => {
                attempt_id = Some(next_arg(&mut args, "--attempt-id")?);
            }
            "--kind" => {
                let value = next_arg(&mut args, "--kind")?;
                kind = Some(match value.as_str() {
                    "tool" => AsyncOperationKind::Tool,
                    "sandbox_task" => AsyncOperationKind::SandboxTask,
                    "ci_job" => AsyncOperationKind::CiJob,
                    "browser_task" => AsyncOperationKind::BrowserTask,
                    "workspace_trial" => AsyncOperationKind::WorkspaceTrial,
                    "external_provider_job" => AsyncOperationKind::ExternalProviderJob,
                    "document_job" => AsyncOperationKind::DocumentJob,
                    "research_task" => AsyncOperationKind::ResearchTask,
                    "custom" => AsyncOperationKind::Custom,
                    _ => {
                        return Err(CliError::Usage(format!(
                            "--kind uses an unsupported async operation kind: {value}"
                        )));
                    }
                });
            }
            "--resume-token-hash" => {
                resume_token_hash = Some(next_arg(&mut args, "--resume-token-hash")?);
            }
            "--idempotency-key" => {
                idempotency_key = Some(next_arg(&mut args, "--idempotency-key")?);
            }
            "--expected-schema" => {
                expected_schema = Some(next_arg(&mut args, "--expected-schema")?);
            }
            "--created-at-unix-ms" => {
                let value = next_arg(&mut args, "--created-at-unix-ms")?;
                created_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--created-at-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            "--provider-operation-id" => {
                provider_operation_id = Some(next_arg(&mut args, "--provider-operation-id")?);
            }
            "--submitted-at-unix-ms" => {
                let value = next_arg(&mut args, "--submitted-at-unix-ms")?;
                submitted_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--submitted-at-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            "--waiting-callback" => {
                waiting_callback = true;
            }
            "--waiting-callback-expires-at-unix-ms" => {
                let value = next_arg(&mut args, "--waiting-callback-expires-at-unix-ms")?;
                waiting_callback = true;
                waiting_callback_expires_at_unix_ms =
                    Some(value.parse::<u64>().map_err(|error| {
                        CliError::Usage(format!(
                            "--waiting-callback-expires-at-unix-ms requires an unsigned integer: {error}"
                        ))
                    })?);
            }
            "--infinite-wait-policy" => {
                waiting_callback = true;
                infinite_wait_policy = Some(next_arg(&mut args, "--infinite-wait-policy")?);
            }
            _ => return Err(CliError::Usage(format!("unsupported argument: {arg}"))),
        }
    }

    let async_operation_store = async_operation_store
        .ok_or_else(|| CliError::Usage("--async-operation-store is required".to_owned()))?;
    let operation_id =
        operation_id.ok_or_else(|| CliError::Usage("--operation-id is required".to_owned()))?;
    let run_id = run_id.ok_or_else(|| CliError::Usage("--run-id is required".to_owned()))?;
    let node_id = node_id.ok_or_else(|| CliError::Usage("--node-id is required".to_owned()))?;
    let attempt_id =
        attempt_id.ok_or_else(|| CliError::Usage("--attempt-id is required".to_owned()))?;
    let kind = kind.ok_or_else(|| CliError::Usage("--kind is required".to_owned()))?;
    let resume_token_hash = resume_token_hash
        .ok_or_else(|| CliError::Usage("--resume-token-hash is required".to_owned()))?;
    let idempotency_key = idempotency_key
        .ok_or_else(|| CliError::Usage("--idempotency-key is required".to_owned()))?;
    let expected_schema = expected_schema
        .ok_or_else(|| CliError::Usage("--expected-schema is required".to_owned()))?;
    let created_at_unix_ms = created_at_unix_ms
        .ok_or_else(|| CliError::Usage("--created-at-unix-ms is required".to_owned()))?;

    let mut operation = AsyncOperation::new(
        operation_id,
        run_id,
        node_id,
        attempt_id,
        kind,
        resume_token_hash,
        idempotency_key,
        expected_schema,
        created_at_unix_ms,
    );
    operation.provider_operation_id = provider_operation_id;
    if let Some(submitted_at_unix_ms) = submitted_at_unix_ms {
        operation.submitted_at_unix_ms = Some(submitted_at_unix_ms);
        operation.state = AsyncOperationState::Submitted;
    }
    if waiting_callback {
        operation.state = AsyncOperationState::WaitingCallback;
        operation.expires_at_unix_ms = waiting_callback_expires_at_unix_ms;
        operation.infinite_wait_policy = infinite_wait_policy;
    }

    let store =
        SqliteAsyncOperationStore::open(async_operation_store).map_err(CliError::AsyncOperation)?;
    store
        .register(operation.clone())
        .map_err(CliError::AsyncOperation)?;

    let kind = match operation.kind {
        AsyncOperationKind::Tool => "tool",
        AsyncOperationKind::SandboxTask => "sandbox_task",
        AsyncOperationKind::CiJob => "ci_job",
        AsyncOperationKind::BrowserTask => "browser_task",
        AsyncOperationKind::WorkspaceTrial => "workspace_trial",
        AsyncOperationKind::ExternalProviderJob => "external_provider_job",
        AsyncOperationKind::DocumentJob => "document_job",
        AsyncOperationKind::ResearchTask => "research_task",
        AsyncOperationKind::Custom => "custom",
    };

    Ok(json!({
        "ok": true,
        "operation": {
            "operationId": operation.operation_id,
            "runId": operation.run_id,
            "nodeId": operation.node_id,
            "attemptId": operation.attempt_id,
            "kind": kind,
            "providerOperationId": operation.provider_operation_id,
            "state": async_operation_state_name(operation.state),
            "resumeTokenHash": operation.resume_token_hash,
            "idempotencyKey": operation.idempotency_key,
            "expectedSchema": operation.expected_schema,
            "createdAtUnixMs": operation.created_at_unix_ms,
            "submittedAtUnixMs": operation.submitted_at_unix_ms,
            "expiresAtUnixMs": operation.expires_at_unix_ms,
            "infiniteWaitPolicy": operation.infinite_wait_policy,
        },
    }))
}

fn run_submit_async_callback(args: Vec<String>) -> Result<Value, CliError> {
    let mut async_operation_store = None;
    let mut callback_id = None;
    let mut operation_id = None;
    let mut run_id = None;
    let mut node_id = None;
    let mut attempt_id = None;
    let mut provider_operation_id = None;
    let mut idempotency_key = None;
    let mut received_at_unix_ms = None;
    let mut verified_by = None;
    let mut policy_snapshot_id = None;
    let mut schema_id = None;
    let mut schema_json = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--async-operation-store" => {
                async_operation_store = Some(next_arg(&mut args, "--async-operation-store")?);
            }
            "--callback-id" => {
                callback_id = Some(next_arg(&mut args, "--callback-id")?);
            }
            "--operation-id" => {
                operation_id = Some(next_arg(&mut args, "--operation-id")?);
            }
            "--run-id" => {
                run_id = Some(next_arg(&mut args, "--run-id")?);
            }
            "--node-id" => {
                node_id = Some(next_arg(&mut args, "--node-id")?);
            }
            "--attempt-id" => {
                attempt_id = Some(next_arg(&mut args, "--attempt-id")?);
            }
            "--provider-operation-id" => {
                provider_operation_id = Some(next_arg(&mut args, "--provider-operation-id")?);
            }
            "--idempotency-key" => {
                idempotency_key = Some(next_arg(&mut args, "--idempotency-key")?);
            }
            "--received-at-unix-ms" => {
                let value = next_arg(&mut args, "--received-at-unix-ms")?;
                received_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--received-at-unix-ms requires an unsigned integer: {error}"
                    ))
                })?);
            }
            "--verified-by" => {
                verified_by = Some(next_arg(&mut args, "--verified-by")?);
            }
            "--policy-snapshot-id" => {
                policy_snapshot_id = Some(next_arg(&mut args, "--policy-snapshot-id")?);
            }
            "--schema-id" => {
                schema_id = Some(next_arg(&mut args, "--schema-id")?);
            }
            "--schema-json" => {
                schema_json = Some(next_arg(&mut args, "--schema-json")?);
            }
            _ => return Err(CliError::Usage(format!("unsupported argument: {arg}"))),
        }
    }

    let async_operation_store = async_operation_store
        .ok_or_else(|| CliError::Usage("--async-operation-store is required".to_owned()))?;
    let callback_id =
        callback_id.ok_or_else(|| CliError::Usage("--callback-id is required".to_owned()))?;
    let operation_id =
        operation_id.ok_or_else(|| CliError::Usage("--operation-id is required".to_owned()))?;
    let run_id = run_id.ok_or_else(|| CliError::Usage("--run-id is required".to_owned()))?;
    let node_id = node_id.ok_or_else(|| CliError::Usage("--node-id is required".to_owned()))?;
    let attempt_id =
        attempt_id.ok_or_else(|| CliError::Usage("--attempt-id is required".to_owned()))?;
    let idempotency_key = idempotency_key
        .ok_or_else(|| CliError::Usage("--idempotency-key is required".to_owned()))?;
    let received_at_unix_ms = received_at_unix_ms
        .ok_or_else(|| CliError::Usage("--received-at-unix-ms is required".to_owned()))?;
    let verified_by =
        verified_by.ok_or_else(|| CliError::Usage("--verified-by is required".to_owned()))?;
    let policy_snapshot_id = policy_snapshot_id
        .ok_or_else(|| CliError::Usage("--policy-snapshot-id is required".to_owned()))?;
    let schema_id =
        schema_id.ok_or_else(|| CliError::Usage("--schema-id is required".to_owned()))?;
    let schema_json =
        schema_json.ok_or_else(|| CliError::Usage("--schema-json is required".to_owned()))?;

    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .map_err(|error| CliError::ReadStdin(error.to_string()))?;
    let payload = serde_json::from_str::<Value>(&input)
        .map_err(|error| CliError::ParseJson(error.to_string()))?;
    let schema_value = serde_json::from_str::<Value>(&schema_json).map_err(|error| {
        CliError::Usage(format!(
            "--schema-json must be a JSON schema object: {error}"
        ))
    })?;
    let schema = JsonSchema::from_json_schema_value(schema_id, &schema_value).map_err(|error| {
        CliError::Usage(format!(
            "--schema-json is not supported by graphblocksd: {error:?}"
        ))
    })?;
    let registry = ToolSchemaRegistry::new([schema])
        .map_err(|error| CliError::Usage(format!("invalid callback schema registry: {error:?}")))?;

    let mut submission = AsyncCallbackSubmission::new(
        callback_id,
        operation_id,
        run_id,
        node_id,
        attempt_id,
        idempotency_key,
        payload,
        received_at_unix_ms,
        verified_by,
        policy_snapshot_id,
    );
    if let Some(provider_operation_id) = provider_operation_id {
        submission = submission.with_provider_operation_id(provider_operation_id);
    }

    let store =
        SqliteAsyncOperationStore::open(async_operation_store).map_err(CliError::AsyncOperation)?;
    let accepted = store
        .accept_callback(submission, &registry)
        .map_err(CliError::AsyncOperation)?;

    Ok(json!({
        "ok": true,
        "accepted": {
            "duplicate": accepted.duplicate,
            "shouldResume": accepted.should_resume,
        },
        "receipt": {
            "callbackId": accepted.receipt.callback_id,
            "operationId": accepted.receipt.operation_id,
            "runId": accepted.receipt.run_id,
            "nodeId": accepted.receipt.node_id,
            "attemptId": accepted.receipt.attempt_id,
            "providerOperationId": accepted.receipt.provider_operation_id,
            "idempotencyKey": accepted.receipt.idempotency_key,
            "payloadDigest": accepted.receipt.payload_digest,
            "payload": accepted.receipt.payload,
            "receivedAtUnixMs": accepted.receipt.received_at_unix_ms,
            "verifiedBy": accepted.receipt.verified_by,
            "policySnapshotId": accepted.receipt.policy_snapshot_id,
            "artifacts": accepted.receipt.artifacts.into_iter().map(|artifact| {
                json!({
                    "artifactId": artifact.artifact_id,
                    "uri": artifact.uri,
                    "mediaType": artifact.media_type,
                    "checksum": artifact.checksum,
                })
            }).collect::<Vec<_>>(),
        },
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

    let mut store =
        SqliteCheckpointStore::open(checkpoint_store).map_err(CliError::CheckpointStore)?;
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
        .map_err(CliError::CheckpointStore)?;

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
    let mut store =
        SqliteCheckpointStore::open(checkpoint_store).map_err(CliError::CheckpointStore)?;
    store
        .complete_claim(&claim, now_unix_ms)
        .map_err(CliError::CheckpointStore)?;

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

fn run_renew_checkpoint_claim(args: Vec<String>) -> Result<Value, CliError> {
    let mut checkpoint_store = None;
    let mut run_id = None;
    let mut checkpoint_id = None;
    let mut worker_id = None;
    let mut lease_id = None;
    let mut fencing_epoch = None;
    let mut claimed_at_unix_ms = None;
    let mut expires_at_unix_ms = None;
    let mut now_unix_ms = None;
    let mut new_expires_at_unix_ms = None;
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
            "--new-expires-at-unix-ms" => {
                let value = next_arg(&mut args, "--new-expires-at-unix-ms")?;
                new_expires_at_unix_ms = Some(value.parse::<u64>().map_err(|error| {
                    CliError::Usage(format!(
                        "--new-expires-at-unix-ms requires an unsigned integer: {error}"
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
    let new_expires_at_unix_ms = new_expires_at_unix_ms
        .ok_or_else(|| CliError::Usage("--new-expires-at-unix-ms is required".to_owned()))?;
    let mut store =
        SqliteCheckpointStore::open(checkpoint_store).map_err(CliError::CheckpointStore)?;
    let renewed = store
        .renew_claim(&claim, now_unix_ms, new_expires_at_unix_ms)
        .map_err(CliError::CheckpointStore)?;

    Ok(json!({
        "ok": true,
        "claim": {
            "runId": renewed.run_id,
            "checkpointId": renewed.checkpoint_id,
            "workerId": renewed.worker_id,
            "leaseId": renewed.lease_id,
            "fencingEpoch": renewed.fencing_epoch,
            "claimedAtUnixMs": renewed.claimed_at_unix_ms,
            "expiresAtUnixMs": renewed.expires_at_unix_ms,
            "renewedAtUnixMs": now_unix_ms,
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

fn run_ownership_lease_json(lease: &RunOwnershipLease) -> Value {
    json!({
        "runId": lease.run_id,
        "leaseId": lease.lease_id,
        "owner": lease.owner,
        "fencingEpoch": lease.fencing_epoch,
        "acquiredAtUnixMs": lease.acquired_at_unix_ms,
        "expiresAtUnixMs": lease.expires_at_unix_ms,
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
            Self::Registry(_)
            | Self::RunStore(_)
            | Self::AsyncOperation(_)
            | Self::CheckpointStore(_)
            | Self::Render(_) => 1,
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
            Self::RunStore(error) => {
                json!({"ok": false, "error": run_store_error_json(error)})
            }
            Self::AsyncOperation(error) => {
                json!({"ok": false, "error": async_operation_error_json(error)})
            }
            Self::CheckpointStore(error) => {
                let error = match error {
                    CheckpointStoreError::InvalidBarrier(source) => json!({
                        "code": "daemon.checkpoint.invalid_barrier",
                        "message": format!("{source:?}"),
                    }),
                    CheckpointStoreError::StaleStateRevision {
                        run_id,
                        current,
                        attempted,
                    } => json!({
                        "code": "daemon.checkpoint.stale_state_revision",
                        "runId": run_id,
                        "current": current,
                        "attempted": attempted,
                    }),
                    CheckpointStoreError::CompatibleCheckpointNotFound {
                        run_id,
                        release_id,
                        deployment_revision_id,
                        plan_hash,
                    } => json!({
                        "code": "daemon.checkpoint.not_found",
                        "runId": run_id,
                        "releaseId": release_id,
                        "deploymentRevisionId": deployment_revision_id,
                        "planHash": plan_hash,
                    }),
                    CheckpointStoreError::InvalidRecoveryClaim { field } => json!({
                        "code": "daemon.checkpoint.invalid_recovery_claim",
                        "field": field,
                    }),
                    CheckpointStoreError::ActiveRecoveryClaim {
                        run_id,
                        worker_id,
                        lease_id,
                        expires_at_unix_ms,
                    } => json!({
                        "code": "daemon.checkpoint.active_recovery_claim",
                        "runId": run_id,
                        "workerId": worker_id,
                        "leaseId": lease_id,
                        "expiresAtUnixMs": expires_at_unix_ms,
                    }),
                    CheckpointStoreError::RecoveryClaimNotFound { run_id } => json!({
                        "code": "daemon.checkpoint.recovery_claim_not_found",
                        "runId": run_id,
                    }),
                    CheckpointStoreError::RecoveryClaimMismatch {
                        run_id,
                        expected,
                        actual,
                    } => json!({
                        "code": "daemon.checkpoint.recovery_claim_mismatch",
                        "runId": run_id,
                        "expectedCheckpointId": expected.checkpoint_id,
                        "expectedWorkerId": expected.worker_id,
                        "expectedLeaseId": expected.lease_id,
                        "expectedFencingEpoch": expected.fencing_epoch,
                        "actualCheckpointId": actual.checkpoint_id,
                        "actualWorkerId": actual.worker_id,
                        "actualLeaseId": actual.lease_id,
                        "actualFencingEpoch": actual.fencing_epoch,
                    }),
                    CheckpointStoreError::RecoveryClaimExpired {
                        run_id,
                        lease_id,
                        expires_at_unix_ms,
                        now_unix_ms,
                    } => json!({
                        "code": "daemon.checkpoint.recovery_claim_expired",
                        "runId": run_id,
                        "leaseId": lease_id,
                        "expiresAtUnixMs": expires_at_unix_ms,
                        "nowUnixMs": now_unix_ms,
                    }),
                    CheckpointStoreError::Storage { message } => json!({
                        "code": "daemon.checkpoint.storage",
                        "message": message,
                    }),
                };
                json!({"ok": false, "error": error})
            }
            Self::Render(message) => {
                json!({"ok": false, "error": {"code": "json.render_failed", "message": message}})
            }
        }
    }
}

fn run_store_error_json(error: &RunStoreError) -> Value {
    match error {
        RunStoreError::EmptyField { field } => json!({
            "code": "daemon.run_lease.empty_field",
            "field": field,
        }),
        RunStoreError::NotFound { run_id } => json!({
            "code": "daemon.run.not_found",
            "runId": run_id,
        }),
        RunStoreError::InvalidRunOwnershipLease { run_id, reason } => json!({
            "code": "daemon.run_lease.invalid",
            "runId": run_id,
            "reason": reason,
        }),
        RunStoreError::RunOwnershipLeaseActive {
            run_id,
            owner,
            expires_at_unix_ms,
        } => json!({
            "code": "daemon.run_lease.active",
            "runId": run_id,
            "owner": owner,
            "expiresAtUnixMs": expires_at_unix_ms,
        }),
        RunStoreError::RunOwnershipLeaseMismatch {
            run_id,
            expected,
            actual,
        } => json!({
            "code": "daemon.run_lease.mismatch",
            "runId": run_id,
            "expectedOwner": expected.owner,
            "expectedLeaseId": expected.lease_id,
            "expectedFencingEpoch": expected.fencing_epoch,
            "actualOwner": actual.owner,
            "actualLeaseId": actual.lease_id,
            "actualFencingEpoch": actual.fencing_epoch,
        }),
        RunStoreError::RunOwnershipLeaseExpired {
            run_id,
            lease_id,
            expires_at_unix_ms,
            now_unix_ms,
        } => json!({
            "code": "daemon.run_lease.expired",
            "runId": run_id,
            "leaseId": lease_id,
            "expiresAtUnixMs": expires_at_unix_ms,
            "nowUnixMs": now_unix_ms,
        }),
        RunStoreError::Storage { message } => json!({
            "code": "daemon.run_store.storage",
            "message": message,
        }),
        other => json!({
            "code": "daemon.run_store.error",
            "message": format!("{other:?}"),
        }),
    }
}

fn async_operation_error_json(error: &AsyncOperationError) -> Value {
    match error {
        AsyncOperationError::EmptyField { field } => json!({
            "code": "daemon.async_operation.empty_field",
            "field": field,
        }),
        AsyncOperationError::InvalidOperation {
            operation_id,
            reason,
        } => json!({
            "code": "daemon.async_operation.invalid_operation",
            "operationId": operation_id,
            "reason": reason,
        }),
        AsyncOperationError::InvalidExpiration {
            operation_id,
            created_at_unix_ms,
            expires_at_unix_ms,
        } => json!({
            "code": "daemon.async_operation.invalid_expiration",
            "operationId": operation_id,
            "createdAtUnixMs": created_at_unix_ms,
            "expiresAtUnixMs": expires_at_unix_ms,
        }),
        AsyncOperationError::DuplicateOperation { operation_id } => json!({
            "code": "daemon.async_operation.duplicate_operation",
            "operationId": operation_id,
        }),
        AsyncOperationError::OperationNotFound { operation_id } => json!({
            "code": "daemon.async_operation.not_found",
            "operationId": operation_id,
        }),
        AsyncOperationError::OperationIdentityMismatch {
            operation_id,
            field,
            expected,
            actual,
        } => json!({
            "code": "daemon.async_operation.identity_mismatch",
            "operationId": operation_id,
            "field": field,
            "expected": expected,
            "actual": actual,
        }),
        AsyncOperationError::OperationNotWaitingCallback {
            operation_id,
            state,
        } => json!({
            "code": "daemon.async_operation.not_waiting_callback",
            "operationId": operation_id,
            "state": async_operation_state_name(*state),
        }),
        AsyncOperationError::OperationTerminal {
            operation_id,
            state,
        } => json!({
            "code": "daemon.async_operation.terminal",
            "operationId": operation_id,
            "state": async_operation_state_name(*state),
        }),
        AsyncOperationError::StaleAttempt {
            operation_id,
            expected_attempt_id,
            actual_attempt_id,
        } => json!({
            "code": "daemon.async_operation.stale_attempt",
            "operationId": operation_id,
            "expectedAttemptId": expected_attempt_id,
            "actualAttemptId": actual_attempt_id,
        }),
        AsyncOperationError::CallbackSchemaMissing { schema_id } => json!({
            "code": "daemon.async_operation.callback_schema_missing",
            "schemaId": schema_id,
        }),
        AsyncOperationError::CallbackSchemaInvalid {
            operation_id,
            schema_id,
            path,
            expected,
        } => json!({
            "code": "daemon.async_operation.callback_schema_invalid",
            "operationId": operation_id,
            "schemaId": schema_id,
            "path": path,
            "expected": expected,
        }),
        AsyncOperationError::RequiredCallbackPropertyMissing {
            operation_id,
            schema_id,
            path,
            property,
        } => json!({
            "code": "daemon.async_operation.callback_required_property_missing",
            "operationId": operation_id,
            "schemaId": schema_id,
            "path": path,
            "property": property,
        }),
        AsyncOperationError::CallbackAuthenticationFailed {
            endpoint_id,
            reason,
        } => json!({
            "code": "daemon.async_operation.callback_authentication_failed",
            "endpointId": endpoint_id,
            "reason": reason,
        }),
        AsyncOperationError::CallbackPayloadTooLarge {
            operation_id,
            max_payload_bytes,
            actual_payload_bytes,
        } => json!({
            "code": "daemon.async_operation.callback_payload_too_large",
            "operationId": operation_id,
            "maxPayloadBytes": max_payload_bytes,
            "actualPayloadBytes": actual_payload_bytes,
        }),
        AsyncOperationError::CallbackIdempotencyConflict {
            operation_id,
            idempotency_key,
            field,
        } => json!({
            "code": "daemon.async_operation.callback_idempotency_conflict",
            "operationId": operation_id,
            "idempotencyKey": idempotency_key,
            "field": field,
        }),
        AsyncOperationError::Storage { message } => json!({
            "code": "daemon.async_operation.storage",
            "message": message,
        }),
    }
}

fn async_operation_state_name(state: AsyncOperationState) -> &'static str {
    match state {
        AsyncOperationState::Created => "created",
        AsyncOperationState::Submitted => "submitted",
        AsyncOperationState::WaitingCallback => "waiting_callback",
        AsyncOperationState::CallbackReceived => "callback_received",
        AsyncOperationState::Polling => "polling",
        AsyncOperationState::Resuming => "resuming",
        AsyncOperationState::Completed => "completed",
        AsyncOperationState::Failed => "failed",
        AsyncOperationState::Cancelled => "cancelled",
        AsyncOperationState::Expired => "expired",
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
