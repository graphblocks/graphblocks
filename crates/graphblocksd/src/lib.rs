use std::collections::BTreeMap;

use graphblocks_protocol::{
    WORKER_PROTOCOL_VERSION, WorkerAdmissionDecision, WorkerAdmissionPolicy, WorkerAdvertisement,
    WorkerDrainError, WorkerDrainPlan, WorkerDrainPolicy, WorkerDrainTask, WorkerProtocolMessage,
    WorkerProtocolMessageKind, WorkerProtocolMessagePayload, WorkerState,
    evaluate_worker_admission,
};
use serde_json::Value;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DaemonConfig {
    pub daemon_id: String,
    pub bind_address: String,
    pub protocol_version: u16,
    pub package_lock_hash: Option<String>,
    pub max_workers: usize,
}

impl DaemonConfig {
    pub fn new(daemon_id: impl Into<String>, bind_address: impl Into<String>) -> Self {
        Self {
            daemon_id: daemon_id.into(),
            bind_address: bind_address.into(),
            protocol_version: WORKER_PROTOCOL_VERSION,
            package_lock_hash: None,
            max_workers: 1024,
        }
    }

    pub fn with_protocol_version(mut self, protocol_version: u16) -> Self {
        self.protocol_version = protocol_version;
        self
    }

    pub fn require_package_lock_hash(mut self, package_lock_hash: impl Into<String>) -> Self {
        self.package_lock_hash = Some(package_lock_hash.into());
        self
    }

    pub fn with_max_workers(mut self, max_workers: usize) -> Self {
        self.max_workers = max_workers;
        self
    }

    pub fn validate(&self) -> Result<(), DaemonConfigError> {
        if self.daemon_id.trim().is_empty() {
            return Err(DaemonConfigError::EmptyDaemonId);
        }
        if self.bind_address.trim().is_empty() {
            return Err(DaemonConfigError::EmptyBindAddress);
        }
        if self.protocol_version != WORKER_PROTOCOL_VERSION {
            return Err(DaemonConfigError::UnsupportedProtocolVersion {
                expected: WORKER_PROTOCOL_VERSION,
                actual: self.protocol_version,
            });
        }
        if self.max_workers == 0 {
            return Err(DaemonConfigError::ZeroMaxWorkers);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DaemonConfigError {
    EmptyDaemonId,
    EmptyBindAddress,
    ZeroMaxWorkers,
    UnsupportedProtocolVersion { expected: u16, actual: u16 },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DaemonStatus {
    pub daemon_id: String,
    pub bind_address: String,
    pub protocol_version: u16,
    pub ready_workers: usize,
    pub saturated_workers: usize,
    pub draining_workers: usize,
    pub admitted_workers: usize,
    pub rejected_workers: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerRegistry {
    config: DaemonConfig,
    admitted_workers: BTreeMap<String, WorkerAdmissionDecision>,
    admitted_advertisements: BTreeMap<String, WorkerAdvertisement>,
    rejected_workers: usize,
}

impl WorkerRegistry {
    pub fn new(config: DaemonConfig) -> Result<Self, DaemonConfigError> {
        config.validate()?;
        Ok(Self {
            config,
            admitted_workers: BTreeMap::new(),
            admitted_advertisements: BTreeMap::new(),
            rejected_workers: 0,
        })
    }

    pub fn admit_worker(&mut self, advertisement: WorkerAdvertisement) -> WorkerAdmissionDecision {
        let policy = WorkerAdmissionPolicy {
            protocol_version: self.config.protocol_version,
            package_lock_hash: self.config.package_lock_hash.clone(),
            required_block: None,
        };
        let mut decision = evaluate_worker_admission(&policy, &advertisement);
        let is_known_worker = self.admitted_workers.contains_key(&decision.worker_id);
        if decision.admitted
            && !is_known_worker
            && self.admitted_workers.len() >= self.config.max_workers
        {
            decision.admitted = false;
            decision
                .reason_codes
                .push("daemon.max_workers_exceeded".to_owned());
        }

        if decision.admitted {
            self.admitted_workers
                .insert(decision.worker_id.clone(), decision.clone());
            self.admitted_advertisements
                .insert(decision.worker_id.clone(), advertisement);
        } else {
            self.rejected_workers += 1;
        }
        decision
    }

    pub fn admit_worker_message(
        &mut self,
        message: WorkerProtocolMessage,
        response_message_id: impl Into<String>,
        response_sequence: u64,
    ) -> Result<WorkerProtocolMessage, WorkerRegistryError> {
        if message.protocol_version != WORKER_PROTOCOL_VERSION {
            return Err(WorkerRegistryError::IncompatibleMessageProtocolVersion {
                expected: WORKER_PROTOCOL_VERSION,
                actual: message.protocol_version,
            });
        }
        if message.message_id.trim().is_empty() {
            return Err(WorkerRegistryError::EmptyMessageId);
        }
        if message
            .correlation_id
            .as_ref()
            .is_some_and(|correlation_id| correlation_id.trim().is_empty())
        {
            return Err(WorkerRegistryError::EmptyCorrelationId);
        }
        if message
            .causation_id
            .as_ref()
            .is_some_and(|causation_id| causation_id.trim().is_empty())
        {
            return Err(WorkerRegistryError::EmptyCausationId);
        }
        let payload_kind = message.payload.kind();
        if message.kind != payload_kind {
            return Err(WorkerRegistryError::KindPayloadMismatch {
                kind: message.kind,
                payload_kind,
            });
        }
        let correlation_id = message.correlation_id.clone();
        let causation_id = message.message_id.clone();
        let WorkerProtocolMessagePayload::Advertisement(advertisement) = message.payload else {
            return Err(WorkerRegistryError::UnexpectedWorkerMessageKind { kind: message.kind });
        };
        let decision = self.admit_worker(advertisement);
        let mut response = WorkerProtocolMessage::admission_decision(
            response_message_id,
            response_sequence,
            decision,
        )
        .with_causation_id(causation_id);
        if let Some(correlation_id) = correlation_id {
            response = response.with_correlation_id(correlation_id);
        }
        Ok(response)
    }

    pub fn admit_worker_message_wire_value(
        &mut self,
        message: &Value,
        response_message_id: impl Into<String>,
        response_sequence: u64,
    ) -> Result<WorkerProtocolMessage, WorkerRegistryError> {
        let message = parse_worker_advertisement_message_wire_value(message)?;
        self.admit_worker_message(message, response_message_id, response_sequence)
    }

    pub fn ready_worker_ids(&self) -> Vec<String> {
        self.worker_ids_by_state(WorkerState::Ready)
    }

    pub fn worker_ids_by_state(&self, state: WorkerState) -> Vec<String> {
        self.admitted_workers
            .values()
            .filter(|decision| decision.state == state)
            .map(|decision| decision.worker_id.clone())
            .collect()
    }

    pub fn status(&self) -> DaemonStatus {
        let ready_workers = self.worker_ids_by_state(WorkerState::Ready).len();
        let saturated_workers = self.worker_ids_by_state(WorkerState::Saturated).len();
        let draining_workers = self.worker_ids_by_state(WorkerState::Draining).len();
        DaemonStatus {
            daemon_id: self.config.daemon_id.clone(),
            bind_address: self.config.bind_address.clone(),
            protocol_version: self.config.protocol_version,
            ready_workers,
            saturated_workers,
            draining_workers,
            admitted_workers: self.admitted_workers.len(),
            rejected_workers: self.rejected_workers,
        }
    }

    pub fn drain_worker<I>(
        &mut self,
        worker_id: impl AsRef<str>,
        policy: &WorkerDrainPolicy,
        tasks: I,
        drain_started_at_unix_ms: u64,
        now_unix_ms: u64,
    ) -> Result<WorkerDrainPlan, WorkerRegistryError>
    where
        I: IntoIterator<Item = WorkerDrainTask>,
    {
        let worker_id = worker_id.as_ref();
        let Some(worker) = self.admitted_advertisements.get(worker_id).cloned() else {
            return Err(WorkerRegistryError::UnknownWorker {
                worker_id: worker_id.to_owned(),
            });
        };
        let plan = WorkerDrainPlan::for_worker(
            &worker,
            policy,
            tasks,
            drain_started_at_unix_ms,
            now_unix_ms,
        )
        .map_err(|source| WorkerRegistryError::DrainPlan { source })?;
        if let Some(decision) = self.admitted_workers.get_mut(worker_id) {
            decision.state = WorkerState::Draining;
        }
        if let Some(advertisement) = self.admitted_advertisements.get_mut(worker_id) {
            advertisement.state = WorkerState::Draining;
        }
        Ok(plan)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerRegistryError {
    UnknownWorker {
        worker_id: String,
    },
    DrainPlan {
        source: WorkerDrainError,
    },
    IncompatibleMessageProtocolVersion {
        expected: u16,
        actual: u16,
    },
    EmptyMessageId,
    EmptyCorrelationId,
    EmptyCausationId,
    KindPayloadMismatch {
        kind: WorkerProtocolMessageKind,
        payload_kind: WorkerProtocolMessageKind,
    },
    UnexpectedWorkerMessageKind {
        kind: WorkerProtocolMessageKind,
    },
    InvalidWireMessage {
        field: &'static str,
        expected: &'static str,
    },
    WirePayloadDecode {
        kind: WorkerProtocolMessageKind,
        source: String,
    },
}

fn parse_worker_advertisement_message_wire_value(
    value: &Value,
) -> Result<WorkerProtocolMessage, WorkerRegistryError> {
    let Some(object) = value.as_object() else {
        return Err(WorkerRegistryError::InvalidWireMessage {
            field: "$",
            expected: "object",
        });
    };
    let protocol_version = optional_wire_u16(
        object,
        "protocolVersion",
        "protocol_version",
        WORKER_PROTOCOL_VERSION,
        "$.protocolVersion",
    )?;
    let message_id = required_wire_string(object, "messageId", "message_id", "$.messageId")?;
    let kind_value =
        wire_alias(object, "kind", "kind").ok_or(WorkerRegistryError::InvalidWireMessage {
            field: "$.kind",
            expected: "worker protocol message kind",
        })?;
    let kind =
        serde_json::from_value::<WorkerProtocolMessageKind>(kind_value.clone()).map_err(|_| {
            WorkerRegistryError::InvalidWireMessage {
                field: "$.kind",
                expected: "worker protocol message kind",
            }
        })?;
    if kind != WorkerProtocolMessageKind::Advertisement {
        return Err(WorkerRegistryError::UnexpectedWorkerMessageKind { kind });
    }
    let sequence = required_wire_u64(object, "sequence", "sequence", "$.sequence")?;
    let correlation_id =
        optional_wire_string(object, "correlationId", "correlation_id", "$.correlationId")?;
    let causation_id =
        optional_wire_string(object, "causationId", "causation_id", "$.causationId")?;
    let payload = wire_alias(object, "payload", "payload").ok_or(
        WorkerRegistryError::InvalidWireMessage {
            field: "$.payload",
            expected: "advertisement payload",
        },
    )?;
    let advertisement =
        serde_json::from_value::<WorkerAdvertisement>(payload.clone()).map_err(|source| {
            WorkerRegistryError::WirePayloadDecode {
                kind,
                source: source.to_string(),
            }
        })?;

    Ok(WorkerProtocolMessage {
        protocol_version,
        message_id: message_id.to_owned(),
        kind,
        sequence,
        correlation_id: correlation_id.map(str::to_owned),
        causation_id: causation_id.map(str::to_owned),
        payload: WorkerProtocolMessagePayload::Advertisement(advertisement),
    })
}

fn wire_alias<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
) -> Option<&'a Value> {
    object.get(primary).or_else(|| object.get(alternate))
}

fn required_wire_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &'static str,
    alternate: &'static str,
    field: &'static str,
) -> Result<&'a str, WorkerRegistryError> {
    wire_alias(object, primary, alternate)
        .and_then(Value::as_str)
        .ok_or(WorkerRegistryError::InvalidWireMessage {
            field,
            expected: "string",
        })
}

fn optional_wire_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &'static str,
    alternate: &'static str,
    field: &'static str,
) -> Result<Option<&'a str>, WorkerRegistryError> {
    let Some(value) = wire_alias(object, primary, alternate) else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_str()
        .map(Some)
        .ok_or(WorkerRegistryError::InvalidWireMessage {
            field,
            expected: "string or null",
        })
}

fn required_wire_u64(
    object: &serde_json::Map<String, Value>,
    primary: &'static str,
    alternate: &'static str,
    field: &'static str,
) -> Result<u64, WorkerRegistryError> {
    wire_alias(object, primary, alternate)
        .and_then(Value::as_u64)
        .ok_or(WorkerRegistryError::InvalidWireMessage {
            field,
            expected: "unsigned integer",
        })
}

fn optional_wire_u16(
    object: &serde_json::Map<String, Value>,
    primary: &'static str,
    alternate: &'static str,
    default_value: u16,
    field: &'static str,
) -> Result<u16, WorkerRegistryError> {
    let Some(value) = wire_alias(object, primary, alternate) else {
        return Ok(default_value);
    };
    let Some(value) = value.as_u64() else {
        return Err(WorkerRegistryError::InvalidWireMessage {
            field,
            expected: "unsigned integer",
        });
    };
    u16::try_from(value).map_err(|_| WorkerRegistryError::InvalidWireMessage {
        field,
        expected: "u16",
    })
}
