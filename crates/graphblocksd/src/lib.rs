use std::collections::BTreeMap;

use graphblocks_protocol::{
    WORKER_PROTOCOL_VERSION, WorkerAdmissionDecision, WorkerAdmissionPolicy, WorkerAdvertisement,
    WorkerDrainError, WorkerDrainPlan, WorkerDrainPolicy, WorkerDrainTask, WorkerState,
    evaluate_worker_admission,
};

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
        if decision.admitted && self.admitted_workers.len() >= self.config.max_workers {
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

    pub fn ready_worker_ids(&self) -> Vec<String> {
        self.admitted_workers
            .values()
            .filter(|decision| decision.state == WorkerState::Ready)
            .map(|decision| decision.worker_id.clone())
            .collect()
    }

    pub fn status(&self) -> DaemonStatus {
        DaemonStatus {
            daemon_id: self.config.daemon_id.clone(),
            bind_address: self.config.bind_address.clone(),
            protocol_version: self.config.protocol_version,
            ready_workers: self.ready_worker_ids().len(),
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
    UnknownWorker { worker_id: String },
    DrainPlan { source: WorkerDrainError },
}
