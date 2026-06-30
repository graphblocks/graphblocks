use std::io::{self, Read};

use graphblocks_protocol::WorkerProtocolMessageKind;
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
    Render(String),
}

fn main() {
    let mut args = std::env::args().skip(1);
    let command = args.next();
    let result = match command.as_deref() {
        Some("admit-worker-message") => run_admit_worker_message(args.collect()),
        _ => Err(CliError::Usage(
            "usage: graphblocksd admit-worker-message [--daemon-id ID] [--bind-address ADDR] [--package-lock-hash HASH] [--max-workers N] [--response-message-id ID] [--response-sequence N] < worker-message.json".to_owned(),
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
            Self::Registry(_) | Self::Render(_) => 1,
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
