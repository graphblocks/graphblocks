use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const WORKER_PROTOCOL_VERSION: u16 = 1;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BlockCapability {
    pub block: String,
}

impl BlockCapability {
    pub fn new(block: impl Into<String>) -> Self {
        Self {
            block: block.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerAdvertisement {
    pub protocol_version: u16,
    pub worker_id: String,
    pub target_id: String,
    pub package_lock_hash: String,
    pub image_digest: String,
    pub supported_blocks: Vec<BlockCapability>,
    pub state: WorkerState,
}

impl WorkerAdvertisement {
    pub fn new<I>(
        worker_id: impl Into<String>,
        target_id: impl Into<String>,
        package_lock_hash: impl Into<String>,
        image_digest: impl Into<String>,
        supported_blocks: I,
    ) -> Self
    where
        I: IntoIterator<Item = BlockCapability>,
    {
        Self {
            protocol_version: WORKER_PROTOCOL_VERSION,
            worker_id: worker_id.into(),
            target_id: target_id.into(),
            package_lock_hash: package_lock_hash.into(),
            image_digest: image_digest.into(),
            supported_blocks: supported_blocks.into_iter().collect(),
            state: WorkerState::Ready,
        }
    }

    pub fn with_state(mut self, state: WorkerState) -> Self {
        self.state = state;
        self
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkerState {
    Starting,
    Warming,
    Ready,
    Saturated,
    Draining,
    Degraded,
    Unhealthy,
    Terminated,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerAdmissionPolicy {
    pub protocol_version: u16,
    pub package_lock_hash: Option<String>,
}

impl WorkerAdmissionPolicy {
    pub fn current() -> Self {
        Self {
            protocol_version: WORKER_PROTOCOL_VERSION,
            package_lock_hash: None,
        }
    }

    pub fn require_package_lock_hash(mut self, package_lock_hash: impl Into<String>) -> Self {
        self.package_lock_hash = Some(package_lock_hash.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerProtocolError {
    IncompatibleVersion { expected: u16, actual: u16 },
    IncompatiblePackageLock { expected: String, actual: String },
    EmptyWorkerId,
    EmptyTargetId,
    EmptyPackageLockHash,
    EmptyImageDigest,
    EmptySupportedBlocks,
}

pub fn admit_worker(advertisement: &WorkerAdvertisement) -> Result<(), WorkerProtocolError> {
    admit_worker_with_policy(&WorkerAdmissionPolicy::current(), advertisement)
}

pub fn admit_worker_with_policy(
    policy: &WorkerAdmissionPolicy,
    advertisement: &WorkerAdvertisement,
) -> Result<(), WorkerProtocolError> {
    if advertisement.protocol_version != policy.protocol_version {
        return Err(WorkerProtocolError::IncompatibleVersion {
            expected: policy.protocol_version,
            actual: advertisement.protocol_version,
        });
    }
    if advertisement.worker_id.is_empty() {
        return Err(WorkerProtocolError::EmptyWorkerId);
    }
    if advertisement.target_id.is_empty() {
        return Err(WorkerProtocolError::EmptyTargetId);
    }
    if advertisement.package_lock_hash.is_empty() {
        return Err(WorkerProtocolError::EmptyPackageLockHash);
    }
    if advertisement.image_digest.is_empty() {
        return Err(WorkerProtocolError::EmptyImageDigest);
    }
    if let Some(expected_package_lock_hash) = &policy.package_lock_hash
        && &advertisement.package_lock_hash != expected_package_lock_hash
    {
        return Err(WorkerProtocolError::IncompatiblePackageLock {
            expected: expected_package_lock_hash.clone(),
            actual: advertisement.package_lock_hash.clone(),
        });
    }
    if advertisement.supported_blocks.is_empty() {
        return Err(WorkerProtocolError::EmptySupportedBlocks);
    }
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerSelectionError {
    NoEligibleWorker { block: String },
}

pub fn select_worker_for_block<'a, I>(
    workers: I,
    block: &str,
) -> Result<&'a WorkerAdvertisement, WorkerSelectionError>
where
    I: IntoIterator<Item = &'a WorkerAdvertisement>,
{
    let mut selected: Option<&WorkerAdvertisement> = None;
    for worker in workers {
        if worker.state != WorkerState::Ready {
            continue;
        }
        if !worker
            .supported_blocks
            .iter()
            .any(|capability| capability.block == block)
        {
            continue;
        }
        match selected {
            Some(current) if current.worker_id <= worker.worker_id => {}
            _ => selected = Some(worker),
        }
    }
    selected.ok_or_else(|| WorkerSelectionError::NoEligibleWorker {
        block: block.to_owned(),
    })
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RunOwnershipLease {
    pub run_id: String,
    pub owner_instance_id: String,
    pub lease_epoch: u64,
    pub expires_at_unix_ms: u64,
    pub last_checkpoint: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerInvokeRequest {
    pub invocation_id: String,
    pub run_id: String,
    pub node_id: String,
    pub node_attempt_id: String,
    pub lease_epoch: u64,
    pub block: String,
    pub inputs: Value,
    pub config: Value,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerInvokeResult {
    pub invocation_id: String,
    pub node_attempt_id: String,
    pub lease_epoch: u64,
    pub outputs: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerResultError {
    MismatchedInvocationId { expected: String, actual: String },
    MismatchedNodeAttempt { expected: String, actual: String },
    StaleLeaseEpoch { expected: u64, actual: u64 },
}

pub fn validate_worker_result(
    request: &WorkerInvokeRequest,
    result: &WorkerInvokeResult,
) -> Result<(), WorkerResultError> {
    if request.invocation_id != result.invocation_id {
        return Err(WorkerResultError::MismatchedInvocationId {
            expected: request.invocation_id.clone(),
            actual: result.invocation_id.clone(),
        });
    }
    if request.node_attempt_id != result.node_attempt_id {
        return Err(WorkerResultError::MismatchedNodeAttempt {
            expected: request.node_attempt_id.clone(),
            actual: result.node_attempt_id.clone(),
        });
    }
    if request.lease_epoch != result.lease_epoch {
        return Err(WorkerResultError::StaleLeaseEpoch {
            expected: request.lease_epoch,
            actual: result.lease_epoch,
        });
    }
    Ok(())
}
