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
pub struct ArtifactRef {
    pub artifact_id: String,
    pub uri: String,
    pub media_type: Option<String>,
    pub size_bytes: Option<u64>,
    pub checksum: Option<String>,
    pub etag: Option<String>,
    pub version: Option<String>,
    pub filename: Option<String>,
    pub metadata: BTreeMap<String, String>,
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
    pub required_block: Option<String>,
}

impl WorkerAdmissionPolicy {
    pub fn current() -> Self {
        Self {
            protocol_version: WORKER_PROTOCOL_VERSION,
            package_lock_hash: None,
            required_block: None,
        }
    }

    pub fn require_package_lock_hash(mut self, package_lock_hash: impl Into<String>) -> Self {
        self.package_lock_hash = Some(package_lock_hash.into());
        self
    }

    pub fn require_block(mut self, block: impl Into<String>) -> Self {
        self.required_block = Some(block.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerAdmissionDecision {
    pub admitted: bool,
    pub worker_id: String,
    pub target_id: String,
    pub protocol_version: u16,
    pub package_lock_hash: String,
    pub state: WorkerState,
    pub reason_codes: Vec<String>,
    pub required_block: Option<String>,
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
    MissingRequiredBlock { required_block: String },
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
    if let Some(required_block) = &policy.required_block
        && !advertisement
            .supported_blocks
            .iter()
            .any(|capability| &capability.block == required_block)
    {
        return Err(WorkerProtocolError::MissingRequiredBlock {
            required_block: required_block.clone(),
        });
    }
    Ok(())
}

pub fn evaluate_worker_admission(
    policy: &WorkerAdmissionPolicy,
    advertisement: &WorkerAdvertisement,
) -> WorkerAdmissionDecision {
    let mut reason_codes = Vec::new();
    if advertisement.protocol_version != policy.protocol_version {
        reason_codes.push("worker.incompatible_protocol_version".to_owned());
    }
    if advertisement.worker_id.is_empty() {
        reason_codes.push("worker.empty_worker_id".to_owned());
    }
    if advertisement.target_id.is_empty() {
        reason_codes.push("worker.empty_target_id".to_owned());
    }
    if advertisement.package_lock_hash.is_empty() {
        reason_codes.push("worker.empty_package_lock_hash".to_owned());
    }
    if advertisement.image_digest.is_empty() {
        reason_codes.push("worker.empty_image_digest".to_owned());
    }
    if let Some(expected_package_lock_hash) = &policy.package_lock_hash
        && &advertisement.package_lock_hash != expected_package_lock_hash
    {
        reason_codes.push("worker.incompatible_package_lock".to_owned());
    }
    if advertisement.supported_blocks.is_empty() {
        reason_codes.push("worker.empty_supported_blocks".to_owned());
    }
    if advertisement.state != WorkerState::Ready {
        reason_codes.push("worker.not_ready".to_owned());
    }
    if let Some(required_block) = &policy.required_block
        && !advertisement
            .supported_blocks
            .iter()
            .any(|capability| &capability.block == required_block)
    {
        reason_codes.push("worker.missing_required_block".to_owned());
    }
    WorkerAdmissionDecision {
        admitted: reason_codes.is_empty(),
        worker_id: advertisement.worker_id.clone(),
        target_id: advertisement.target_id.clone(),
        protocol_version: advertisement.protocol_version,
        package_lock_hash: advertisement.package_lock_hash.clone(),
        state: advertisement.state,
        reason_codes,
        required_block: policy.required_block.clone(),
    }
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
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum RemotePayload {
    Inline {
        schema: String,
        value: Value,
    },
    ArtifactRef {
        schema: String,
        artifact: ArtifactRef,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RemotePayloadLimits {
    pub max_inline_bytes: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RemotePayloadError {
    OversizedInlinePayload {
        max_inline_bytes: usize,
        actual_inline_bytes: usize,
    },
    InvalidArtifactRef {
        field: String,
    },
    InlineJsonEncoding,
}

pub fn validate_remote_payload(
    payload: &RemotePayload,
    limits: &RemotePayloadLimits,
) -> Result<(), RemotePayloadError> {
    match payload {
        RemotePayload::Inline { value, .. } => {
            let actual_inline_bytes = serde_json::to_vec(value)
                .map_err(|_| RemotePayloadError::InlineJsonEncoding)?
                .len();
            if actual_inline_bytes > limits.max_inline_bytes {
                return Err(RemotePayloadError::OversizedInlinePayload {
                    max_inline_bytes: limits.max_inline_bytes,
                    actual_inline_bytes,
                });
            }
            Ok(())
        }
        RemotePayload::ArtifactRef { artifact, .. } => {
            if artifact.artifact_id.is_empty() {
                return Err(RemotePayloadError::InvalidArtifactRef {
                    field: "artifact_id".to_owned(),
                });
            }
            if artifact.uri.is_empty() {
                return Err(RemotePayloadError::InvalidArtifactRef {
                    field: "uri".to_owned(),
                });
            }
            Ok(())
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerInvocationContext {
    pub release_id: String,
    pub deployment_revision_id: String,
    pub trace_id: Option<String>,
    pub parent_span_id: Option<String>,
    pub policy_snapshot_id: Option<String>,
    pub policy_snapshot_digest: Option<String>,
    pub budget_permit_id: Option<String>,
    pub budget_permit_digest: Option<String>,
    pub attributes: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerInvocationContextError {
    EmptyRequiredField { field: String },
    EmptyOptionalField { field: String },
    MissingPolicySnapshotDigest,
    MissingPolicySnapshotId,
    MissingBudgetPermitDigest,
    MissingBudgetPermitId,
    EmptyAttributeKey,
}

impl WorkerInvocationContext {
    pub fn new(release_id: impl Into<String>, deployment_revision_id: impl Into<String>) -> Self {
        Self {
            release_id: release_id.into(),
            deployment_revision_id: deployment_revision_id.into(),
            trace_id: None,
            parent_span_id: None,
            policy_snapshot_id: None,
            policy_snapshot_digest: None,
            budget_permit_id: None,
            budget_permit_digest: None,
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_trace(
        mut self,
        trace_id: impl Into<String>,
        parent_span_id: impl Into<String>,
    ) -> Self {
        self.trace_id = Some(trace_id.into());
        self.parent_span_id = Some(parent_span_id.into());
        self
    }

    pub fn with_policy_snapshot(
        mut self,
        policy_snapshot_id: impl Into<String>,
        policy_snapshot_digest: impl Into<String>,
    ) -> Self {
        self.policy_snapshot_id = Some(policy_snapshot_id.into());
        self.policy_snapshot_digest = Some(policy_snapshot_digest.into());
        self
    }

    pub fn with_budget_permit(
        mut self,
        budget_permit_id: impl Into<String>,
        budget_permit_digest: impl Into<String>,
    ) -> Self {
        self.budget_permit_id = Some(budget_permit_id.into());
        self.budget_permit_digest = Some(budget_permit_digest.into());
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.attributes.insert(key.into(), value.into());
        self
    }

    pub fn validate(&self) -> Result<(), WorkerInvocationContextError> {
        if self.release_id.is_empty() {
            return Err(WorkerInvocationContextError::EmptyRequiredField {
                field: "release_id".to_owned(),
            });
        }
        if self.deployment_revision_id.is_empty() {
            return Err(WorkerInvocationContextError::EmptyRequiredField {
                field: "deployment_revision_id".to_owned(),
            });
        }
        if let Some(trace_id) = &self.trace_id
            && trace_id.is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "trace_id".to_owned(),
            });
        }
        if let Some(parent_span_id) = &self.parent_span_id
            && parent_span_id.is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "parent_span_id".to_owned(),
            });
        }
        if let Some(policy_snapshot_id) = &self.policy_snapshot_id
            && policy_snapshot_id.is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "policy_snapshot_id".to_owned(),
            });
        }
        if let Some(policy_snapshot_digest) = &self.policy_snapshot_digest
            && policy_snapshot_digest.is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "policy_snapshot_digest".to_owned(),
            });
        }
        if let Some(budget_permit_id) = &self.budget_permit_id
            && budget_permit_id.is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "budget_permit_id".to_owned(),
            });
        }
        if let Some(budget_permit_digest) = &self.budget_permit_digest
            && budget_permit_digest.is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "budget_permit_digest".to_owned(),
            });
        }
        match (&self.policy_snapshot_id, &self.policy_snapshot_digest) {
            (Some(_), Some(_)) | (None, None) => {}
            (Some(_), None) => {
                return Err(WorkerInvocationContextError::MissingPolicySnapshotDigest);
            }
            (None, Some(_)) => return Err(WorkerInvocationContextError::MissingPolicySnapshotId),
        }
        match (&self.budget_permit_id, &self.budget_permit_digest) {
            (Some(_), Some(_)) | (None, None) => {}
            (Some(_), None) => return Err(WorkerInvocationContextError::MissingBudgetPermitDigest),
            (None, Some(_)) => return Err(WorkerInvocationContextError::MissingBudgetPermitId),
        }
        if self.attributes.keys().any(|key| key.is_empty()) {
            return Err(WorkerInvocationContextError::EmptyAttributeKey);
        }
        Ok(())
    }
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
    pub context: WorkerInvocationContext,
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
