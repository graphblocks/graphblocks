use std::collections::BTreeMap;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde::ser::SerializeStruct;
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
    EmptyBlockCapability,
    MissingRequiredBlock { required_block: String },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkerProtocolMessageKind {
    Advertisement,
    AdmissionDecision,
    InvokeRequest,
    InvokeResult,
    DrainPlan,
    Error,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerProtocolErrorPayload {
    pub code: String,
    pub message: String,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub details: Option<BTreeMap<String, Value>>,
}

impl WorkerProtocolErrorPayload {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            retryable: false,
            details: None,
        }
    }

    pub fn retryable(mut self, retryable: bool) -> Self {
        self.retryable = retryable;
        self
    }

    pub fn with_detail(mut self, key: impl Into<String>, value: Value) -> Self {
        self.details
            .get_or_insert_with(BTreeMap::new)
            .insert(key.into(), value);
        self
    }

    pub fn validate(&self) -> Result<(), WorkerProtocolMessageError> {
        if self.code.trim().is_empty() {
            return Err(WorkerProtocolMessageError::InvalidErrorPayload { field: "code" });
        }
        if self.message.trim().is_empty() {
            return Err(WorkerProtocolMessageError::InvalidErrorPayload { field: "message" });
        }
        if let Some(details) = &self.details
            && details.keys().any(|key| key.trim().is_empty())
        {
            return Err(WorkerProtocolMessageError::InvalidErrorPayload { field: "details" });
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum WorkerProtocolMessagePayload {
    Advertisement(WorkerAdvertisement),
    AdmissionDecision(WorkerAdmissionDecision),
    InvokeRequest(Box<WorkerInvokeRequest>),
    InvokeResult(WorkerInvokeResult),
    DrainPlan(WorkerDrainPlan),
    Error(WorkerProtocolErrorPayload),
}

impl WorkerProtocolMessagePayload {
    pub fn kind(&self) -> WorkerProtocolMessageKind {
        match self {
            Self::Advertisement(_) => WorkerProtocolMessageKind::Advertisement,
            Self::AdmissionDecision(_) => WorkerProtocolMessageKind::AdmissionDecision,
            Self::InvokeRequest(_) => WorkerProtocolMessageKind::InvokeRequest,
            Self::InvokeResult(_) => WorkerProtocolMessageKind::InvokeResult,
            Self::DrainPlan(_) => WorkerProtocolMessageKind::DrainPlan,
            Self::Error(_) => WorkerProtocolMessageKind::Error,
        }
    }

    pub fn validate(&self) -> Result<(), WorkerProtocolMessageError> {
        match self {
            Self::Advertisement(advertisement) => admit_worker(advertisement)
                .map_err(|source| WorkerProtocolMessageError::InvalidAdvertisement { source }),
            Self::AdmissionDecision(decision) => validate_worker_admission_decision(decision),
            Self::InvokeRequest(request) => request
                .validate()
                .map_err(|source| WorkerProtocolMessageError::InvalidInvokeRequest { source }),
            Self::InvokeResult(result) => result
                .validate()
                .map_err(|source| WorkerProtocolMessageError::InvalidInvokeResult { source }),
            Self::DrainPlan(plan) => plan
                .validate()
                .map_err(|source| WorkerProtocolMessageError::InvalidDrainPlan { source }),
            Self::Error(payload) => payload.validate(),
        }
    }

    fn to_value(&self) -> Result<Value, WorkerProtocolMessageError> {
        match self {
            Self::Advertisement(payload) => serde_json::to_value(payload),
            Self::AdmissionDecision(payload) => serde_json::to_value(payload),
            Self::InvokeRequest(payload) => serde_json::to_value(payload),
            Self::InvokeResult(payload) => serde_json::to_value(payload),
            Self::DrainPlan(payload) => serde_json::to_value(payload),
            Self::Error(payload) => serde_json::to_value(payload),
        }
        .map_err(|source| WorkerProtocolMessageError::PayloadEncoding {
            kind: self.kind(),
            source: source.to_string(),
        })
    }

    fn from_kind_and_value(
        kind: WorkerProtocolMessageKind,
        payload: Value,
    ) -> Result<Self, WorkerProtocolMessageError> {
        let payload = match kind {
            WorkerProtocolMessageKind::Advertisement => {
                Self::Advertisement(serde_json::from_value(payload).map_err(|source| {
                    WorkerProtocolMessageError::PayloadDecoding {
                        kind,
                        source: source.to_string(),
                    }
                })?)
            }
            WorkerProtocolMessageKind::AdmissionDecision => {
                Self::AdmissionDecision(serde_json::from_value(payload).map_err(|source| {
                    WorkerProtocolMessageError::PayloadDecoding {
                        kind,
                        source: source.to_string(),
                    }
                })?)
            }
            WorkerProtocolMessageKind::InvokeRequest => {
                Self::InvokeRequest(Box::new(serde_json::from_value(payload).map_err(
                    |source| WorkerProtocolMessageError::PayloadDecoding {
                        kind,
                        source: source.to_string(),
                    },
                )?))
            }
            WorkerProtocolMessageKind::InvokeResult => {
                Self::InvokeResult(serde_json::from_value(payload).map_err(|source| {
                    WorkerProtocolMessageError::PayloadDecoding {
                        kind,
                        source: source.to_string(),
                    }
                })?)
            }
            WorkerProtocolMessageKind::DrainPlan => {
                Self::DrainPlan(serde_json::from_value(payload).map_err(|source| {
                    WorkerProtocolMessageError::PayloadDecoding {
                        kind,
                        source: source.to_string(),
                    }
                })?)
            }
            WorkerProtocolMessageKind::Error => {
                Self::Error(serde_json::from_value(payload).map_err(|source| {
                    WorkerProtocolMessageError::PayloadDecoding {
                        kind,
                        source: source.to_string(),
                    }
                })?)
            }
        };
        payload.validate()?;
        Ok(payload)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WorkerProtocolMessage {
    pub protocol_version: u16,
    pub message_id: String,
    pub kind: WorkerProtocolMessageKind,
    pub sequence: u64,
    pub correlation_id: Option<String>,
    pub causation_id: Option<String>,
    pub payload: WorkerProtocolMessagePayload,
}

impl WorkerProtocolMessage {
    pub fn new(
        message_id: impl Into<String>,
        sequence: u64,
        payload: WorkerProtocolMessagePayload,
    ) -> Self {
        let kind = payload.kind();
        Self {
            protocol_version: WORKER_PROTOCOL_VERSION,
            message_id: message_id.into(),
            kind,
            sequence,
            correlation_id: None,
            causation_id: None,
            payload,
        }
    }

    pub fn advertisement(
        message_id: impl Into<String>,
        sequence: u64,
        advertisement: WorkerAdvertisement,
    ) -> Self {
        Self::new(
            message_id,
            sequence,
            WorkerProtocolMessagePayload::Advertisement(advertisement),
        )
    }

    pub fn admission_decision(
        message_id: impl Into<String>,
        sequence: u64,
        decision: WorkerAdmissionDecision,
    ) -> Self {
        Self::new(
            message_id,
            sequence,
            WorkerProtocolMessagePayload::AdmissionDecision(decision),
        )
    }

    pub fn invoke_request(
        message_id: impl Into<String>,
        sequence: u64,
        request: WorkerInvokeRequest,
    ) -> Self {
        let correlation_id = request.invocation_id.clone();
        Self::new(
            message_id,
            sequence,
            WorkerProtocolMessagePayload::InvokeRequest(Box::new(request)),
        )
        .with_correlation_id(correlation_id)
    }

    pub fn invoke_result(
        message_id: impl Into<String>,
        sequence: u64,
        result: WorkerInvokeResult,
    ) -> Self {
        let correlation_id = result.invocation_id.clone();
        Self::new(
            message_id,
            sequence,
            WorkerProtocolMessagePayload::InvokeResult(result),
        )
        .with_correlation_id(correlation_id)
    }

    pub fn drain_plan(message_id: impl Into<String>, sequence: u64, plan: WorkerDrainPlan) -> Self {
        Self::new(
            message_id,
            sequence,
            WorkerProtocolMessagePayload::DrainPlan(plan),
        )
    }

    pub fn error(
        message_id: impl Into<String>,
        sequence: u64,
        code: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self::new(
            message_id,
            sequence,
            WorkerProtocolMessagePayload::Error(WorkerProtocolErrorPayload::new(code, message)),
        )
    }

    pub fn with_correlation_id(mut self, correlation_id: impl Into<String>) -> Self {
        self.correlation_id = Some(correlation_id.into());
        self
    }

    pub fn with_causation_id(mut self, causation_id: impl Into<String>) -> Self {
        self.causation_id = Some(causation_id.into());
        self
    }

    pub fn validate(&self) -> Result<(), WorkerProtocolMessageError> {
        if self.protocol_version != WORKER_PROTOCOL_VERSION {
            return Err(WorkerProtocolMessageError::IncompatibleVersion {
                expected: WORKER_PROTOCOL_VERSION,
                actual: self.protocol_version,
            });
        }
        if self.message_id.trim().is_empty() {
            return Err(WorkerProtocolMessageError::EmptyMessageId);
        }
        if let Some(correlation_id) = &self.correlation_id
            && correlation_id.trim().is_empty()
        {
            return Err(WorkerProtocolMessageError::EmptyCorrelationId);
        }
        if let Some(causation_id) = &self.causation_id
            && causation_id.trim().is_empty()
        {
            return Err(WorkerProtocolMessageError::EmptyCausationId);
        }
        let payload_kind = self.payload.kind();
        if self.kind != payload_kind {
            return Err(WorkerProtocolMessageError::KindPayloadMismatch {
                kind: self.kind,
                payload_kind,
            });
        }
        self.payload.validate()
    }

    pub fn to_wire_value(&self) -> Result<Value, WorkerProtocolMessageError> {
        self.validate()?;
        serde_json::to_value(self).map_err(|source| WorkerProtocolMessageError::MessageEncoding {
            source: source.to_string(),
        })
    }

    pub fn content_digest(&self) -> Result<String, WorkerProtocolMessageError> {
        Ok(canonical_hash(&self.to_wire_value()?))
    }
}

impl Serialize for WorkerProtocolMessage {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        let mut state = serializer.serialize_struct("WorkerProtocolMessage", 7)?;
        state.serialize_field("protocolVersion", &self.protocol_version)?;
        state.serialize_field("messageId", &self.message_id)?;
        state.serialize_field("kind", &self.kind)?;
        state.serialize_field("sequence", &self.sequence)?;
        state.serialize_field("correlationId", &self.correlation_id)?;
        state.serialize_field("causationId", &self.causation_id)?;
        state.serialize_field(
            "payload",
            &self.payload.to_value().map_err(serde::ser::Error::custom)?,
        )?;
        state.end()
    }
}

impl<'de> Deserialize<'de> for WorkerProtocolMessage {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct WireWorkerProtocolMessage {
            #[serde(default = "default_worker_protocol_version")]
            protocol_version: u16,
            message_id: String,
            kind: WorkerProtocolMessageKind,
            sequence: u64,
            #[serde(default)]
            correlation_id: Option<String>,
            #[serde(default)]
            causation_id: Option<String>,
            payload: Value,
        }

        let wire = WireWorkerProtocolMessage::deserialize(deserializer)?;
        let payload = WorkerProtocolMessagePayload::from_kind_and_value(wire.kind, wire.payload)
            .map_err(serde::de::Error::custom)?;
        let message = Self {
            protocol_version: wire.protocol_version,
            message_id: wire.message_id,
            kind: wire.kind,
            sequence: wire.sequence,
            correlation_id: wire.correlation_id,
            causation_id: wire.causation_id,
            payload,
        };
        message.validate().map_err(serde::de::Error::custom)?;
        Ok(message)
    }
}

fn default_worker_protocol_version() -> u16 {
    WORKER_PROTOCOL_VERSION
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerProtocolMessageError {
    IncompatibleVersion {
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
    PayloadEncoding {
        kind: WorkerProtocolMessageKind,
        source: String,
    },
    PayloadDecoding {
        kind: WorkerProtocolMessageKind,
        source: String,
    },
    MessageEncoding {
        source: String,
    },
    InvalidAdvertisement {
        source: WorkerProtocolError,
    },
    InvalidAdmissionDecision {
        field: &'static str,
    },
    InvalidInvokeRequest {
        source: WorkerInvokeRequestError,
    },
    InvalidInvokeResult {
        source: WorkerInvokeResultError,
    },
    InvalidDrainPlan {
        source: WorkerDrainError,
    },
    InvalidErrorPayload {
        field: &'static str,
    },
}

impl fmt::Display for WorkerProtocolMessageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{self:?}")
    }
}

impl std::error::Error for WorkerProtocolMessageError {}

fn validate_worker_admission_decision(
    decision: &WorkerAdmissionDecision,
) -> Result<(), WorkerProtocolMessageError> {
    if decision.worker_id.trim().is_empty() {
        return Err(WorkerProtocolMessageError::InvalidAdmissionDecision { field: "worker_id" });
    }
    if decision.target_id.trim().is_empty() {
        return Err(WorkerProtocolMessageError::InvalidAdmissionDecision { field: "target_id" });
    }
    if decision.package_lock_hash.trim().is_empty() {
        return Err(WorkerProtocolMessageError::InvalidAdmissionDecision {
            field: "package_lock_hash",
        });
    }
    if decision
        .reason_codes
        .iter()
        .any(|reason_code| reason_code.trim().is_empty())
    {
        return Err(WorkerProtocolMessageError::InvalidAdmissionDecision {
            field: "reason_codes",
        });
    }
    if let Some(required_block) = &decision.required_block
        && required_block.trim().is_empty()
    {
        return Err(WorkerProtocolMessageError::InvalidAdmissionDecision {
            field: "required_block",
        });
    }
    Ok(())
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
    if advertisement.worker_id.trim().is_empty() {
        return Err(WorkerProtocolError::EmptyWorkerId);
    }
    if advertisement.target_id.trim().is_empty() {
        return Err(WorkerProtocolError::EmptyTargetId);
    }
    if advertisement.package_lock_hash.trim().is_empty() {
        return Err(WorkerProtocolError::EmptyPackageLockHash);
    }
    if advertisement.image_digest.trim().is_empty() {
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
    if advertisement
        .supported_blocks
        .iter()
        .any(|capability| capability.block.trim().is_empty())
    {
        return Err(WorkerProtocolError::EmptyBlockCapability);
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
    if advertisement.worker_id.trim().is_empty() {
        reason_codes.push("worker.empty_worker_id".to_owned());
    }
    if advertisement.target_id.trim().is_empty() {
        reason_codes.push("worker.empty_target_id".to_owned());
    }
    if advertisement.package_lock_hash.trim().is_empty() {
        reason_codes.push("worker.empty_package_lock_hash".to_owned());
    }
    if advertisement.image_digest.trim().is_empty() {
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
    if advertisement
        .supported_blocks
        .iter()
        .any(|capability| capability.block.trim().is_empty())
    {
        reason_codes.push("worker.empty_block_capability".to_owned());
    }
    if !matches!(
        advertisement.state,
        WorkerState::Ready | WorkerState::Saturated
    ) {
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
        if admit_worker(worker).is_err() {
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RunOwnershipLeaseError {
    EmptyRunId,
    EmptyOwnerInstanceId,
    EmptyLastCheckpoint,
}

impl RunOwnershipLease {
    pub fn validate(&self) -> Result<(), RunOwnershipLeaseError> {
        if self.run_id.trim().is_empty() {
            return Err(RunOwnershipLeaseError::EmptyRunId);
        }
        if self.owner_instance_id.trim().is_empty() {
            return Err(RunOwnershipLeaseError::EmptyOwnerInstanceId);
        }
        if let Some(last_checkpoint) = &self.last_checkpoint
            && last_checkpoint.trim().is_empty()
        {
            return Err(RunOwnershipLeaseError::EmptyLastCheckpoint);
        }
        Ok(())
    }
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
    InvalidSchema,
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
        RemotePayload::Inline { schema, value } => {
            if schema.trim().is_empty() {
                return Err(RemotePayloadError::InvalidSchema);
            }
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
        RemotePayload::ArtifactRef { schema, artifact } => {
            if schema.trim().is_empty() {
                return Err(RemotePayloadError::InvalidSchema);
            }
            if artifact.artifact_id.trim().is_empty() {
                return Err(RemotePayloadError::InvalidArtifactRef {
                    field: "artifact_id".to_owned(),
                });
            }
            if artifact.uri.trim().is_empty() {
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
        if self.release_id.trim().is_empty() {
            return Err(WorkerInvocationContextError::EmptyRequiredField {
                field: "release_id".to_owned(),
            });
        }
        if self.deployment_revision_id.trim().is_empty() {
            return Err(WorkerInvocationContextError::EmptyRequiredField {
                field: "deployment_revision_id".to_owned(),
            });
        }
        if let Some(trace_id) = &self.trace_id
            && trace_id.trim().is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "trace_id".to_owned(),
            });
        }
        if let Some(parent_span_id) = &self.parent_span_id
            && parent_span_id.trim().is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "parent_span_id".to_owned(),
            });
        }
        if let Some(policy_snapshot_id) = &self.policy_snapshot_id
            && policy_snapshot_id.trim().is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "policy_snapshot_id".to_owned(),
            });
        }
        if let Some(policy_snapshot_digest) = &self.policy_snapshot_digest
            && policy_snapshot_digest.trim().is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "policy_snapshot_digest".to_owned(),
            });
        }
        if let Some(budget_permit_id) = &self.budget_permit_id
            && budget_permit_id.trim().is_empty()
        {
            return Err(WorkerInvocationContextError::EmptyOptionalField {
                field: "budget_permit_id".to_owned(),
            });
        }
        if let Some(budget_permit_digest) = &self.budget_permit_digest
            && budget_permit_digest.trim().is_empty()
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
        if self.attributes.keys().any(|key| key.trim().is_empty()) {
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerInvokeRequestError {
    EmptyField {
        field: String,
    },
    InvalidContext {
        source: WorkerInvocationContextError,
    },
}

impl WorkerInvokeRequest {
    pub fn validate(&self) -> Result<(), WorkerInvokeRequestError> {
        if self.invocation_id.trim().is_empty() {
            return Err(WorkerInvokeRequestError::EmptyField {
                field: "invocation_id".to_owned(),
            });
        }
        if self.run_id.trim().is_empty() {
            return Err(WorkerInvokeRequestError::EmptyField {
                field: "run_id".to_owned(),
            });
        }
        if self.node_id.trim().is_empty() {
            return Err(WorkerInvokeRequestError::EmptyField {
                field: "node_id".to_owned(),
            });
        }
        if self.node_attempt_id.trim().is_empty() {
            return Err(WorkerInvokeRequestError::EmptyField {
                field: "node_attempt_id".to_owned(),
            });
        }
        if self.block.trim().is_empty() {
            return Err(WorkerInvokeRequestError::EmptyField {
                field: "block".to_owned(),
            });
        }
        self.context
            .validate()
            .map_err(|source| WorkerInvokeRequestError::InvalidContext { source })?;
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkerDrainWorkloadKind {
    OnlineRequest,
    DurableTask,
    RealtimeSession,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkerDrainDisposition {
    FinishInPlace,
    Cancel,
    Checkpoint,
    DisconnectWithResumeToken,
}

const DEFAULT_WORKER_DRAIN_ONLINE_REQUEST_TIMEOUT_MS: u64 = 30_000;
const DEFAULT_WORKER_DRAIN_DURABLE_TASK_TIMEOUT_MS: u64 = 300_000;
const DEFAULT_WORKER_DRAIN_REALTIME_SESSION_TIMEOUT_MS: u64 = 600_000;

fn default_worker_drain_online_request_timeout_ms() -> u64 {
    DEFAULT_WORKER_DRAIN_ONLINE_REQUEST_TIMEOUT_MS
}

fn default_worker_drain_durable_task_timeout_ms() -> u64 {
    DEFAULT_WORKER_DRAIN_DURABLE_TASK_TIMEOUT_MS
}

fn default_worker_drain_realtime_session_timeout_ms() -> u64 {
    DEFAULT_WORKER_DRAIN_REALTIME_SESSION_TIMEOUT_MS
}

fn default_worker_drain_online_request_disposition() -> WorkerDrainDisposition {
    WorkerDrainDisposition::Cancel
}

fn default_worker_drain_durable_task_disposition() -> WorkerDrainDisposition {
    WorkerDrainDisposition::Checkpoint
}

fn default_worker_drain_realtime_session_disposition() -> WorkerDrainDisposition {
    WorkerDrainDisposition::DisconnectWithResumeToken
}

fn default_worker_drain_plan_state() -> WorkerState {
    WorkerState::Draining
}

fn default_worker_drain_admission_closed() -> bool {
    true
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerDrainDeadlinePolicy {
    #[serde(default = "default_worker_drain_online_request_disposition")]
    pub online_request: WorkerDrainDisposition,
    #[serde(default = "default_worker_drain_durable_task_disposition")]
    pub durable_task: WorkerDrainDisposition,
    #[serde(default = "default_worker_drain_realtime_session_disposition")]
    pub realtime_session: WorkerDrainDisposition,
}

impl Default for WorkerDrainDeadlinePolicy {
    fn default() -> Self {
        Self {
            online_request: default_worker_drain_online_request_disposition(),
            durable_task: default_worker_drain_durable_task_disposition(),
            realtime_session: default_worker_drain_realtime_session_disposition(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerDrainPolicy {
    #[serde(default = "default_worker_drain_online_request_timeout_ms")]
    pub online_request_timeout_ms: u64,
    #[serde(default = "default_worker_drain_durable_task_timeout_ms")]
    pub durable_task_timeout_ms: u64,
    #[serde(default = "default_worker_drain_realtime_session_timeout_ms")]
    pub realtime_session_timeout_ms: u64,
    #[serde(default)]
    pub on_deadline: WorkerDrainDeadlinePolicy,
}

impl Default for WorkerDrainPolicy {
    fn default() -> Self {
        Self {
            online_request_timeout_ms: default_worker_drain_online_request_timeout_ms(),
            durable_task_timeout_ms: default_worker_drain_durable_task_timeout_ms(),
            realtime_session_timeout_ms: default_worker_drain_realtime_session_timeout_ms(),
            on_deadline: WorkerDrainDeadlinePolicy::default(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerDrainError {
    NonPositiveTimeout {
        field: &'static str,
    },
    InvalidTaskRequest {
        source: WorkerInvokeRequestError,
    },
    EmptyDecisionField {
        field: &'static str,
    },
    EmptyPlanField {
        field: &'static str,
    },
    WorkerStateNotDraining {
        state: WorkerState,
    },
    InvalidPolicy {
        source: Box<WorkerDrainError>,
    },
    InvalidTask {
        source: Box<WorkerDrainError>,
    },
    InvalidDecision {
        index: usize,
        source: Box<WorkerDrainError>,
    },
}

impl WorkerDrainPolicy {
    pub fn validate(&self) -> Result<(), WorkerDrainError> {
        if self.online_request_timeout_ms == 0 {
            return Err(WorkerDrainError::NonPositiveTimeout {
                field: "online_request_timeout_ms",
            });
        }
        if self.durable_task_timeout_ms == 0 {
            return Err(WorkerDrainError::NonPositiveTimeout {
                field: "durable_task_timeout_ms",
            });
        }
        if self.realtime_session_timeout_ms == 0 {
            return Err(WorkerDrainError::NonPositiveTimeout {
                field: "realtime_session_timeout_ms",
            });
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerDrainTask {
    pub workload: WorkerDrainWorkloadKind,
    pub request: WorkerInvokeRequest,
    pub started_at_unix_ms: u64,
    #[serde(default)]
    pub checkpointable: bool,
}

impl WorkerDrainTask {
    pub fn validate(&self) -> Result<(), WorkerDrainError> {
        self.request
            .validate()
            .map_err(|source| WorkerDrainError::InvalidTaskRequest { source })?;
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerDrainDecision {
    pub workload: WorkerDrainWorkloadKind,
    pub run_id: String,
    pub invocation_id: String,
    pub node_attempt_id: String,
    pub lease_epoch: u64,
    pub release_id: String,
    pub deployment_revision_id: String,
    pub disposition: WorkerDrainDisposition,
    pub deadline_unix_ms: u64,
    pub reason: String,
}

impl WorkerDrainDecision {
    pub fn validate(&self) -> Result<(), WorkerDrainError> {
        for (field, value) in [
            ("run_id", self.run_id.as_str()),
            ("invocation_id", self.invocation_id.as_str()),
            ("node_attempt_id", self.node_attempt_id.as_str()),
            ("release_id", self.release_id.as_str()),
            (
                "deployment_revision_id",
                self.deployment_revision_id.as_str(),
            ),
            ("reason", self.reason.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(WorkerDrainError::EmptyDecisionField { field });
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerDrainPlan {
    pub worker_id: String,
    pub target_id: String,
    #[serde(default = "default_worker_drain_plan_state")]
    pub worker_state: WorkerState,
    #[serde(default = "default_worker_drain_admission_closed")]
    pub admission_closed: bool,
    pub drain_started_at_unix_ms: u64,
    #[serde(default)]
    pub decisions: Vec<WorkerDrainDecision>,
}

impl WorkerDrainPlan {
    pub fn validate(&self) -> Result<(), WorkerDrainError> {
        if self.worker_id.trim().is_empty() {
            return Err(WorkerDrainError::EmptyPlanField { field: "worker_id" });
        }
        if self.target_id.trim().is_empty() {
            return Err(WorkerDrainError::EmptyPlanField { field: "target_id" });
        }
        if self.worker_state != WorkerState::Draining {
            return Err(WorkerDrainError::WorkerStateNotDraining {
                state: self.worker_state,
            });
        }
        for (index, decision) in self.decisions.iter().enumerate() {
            decision
                .validate()
                .map_err(|source| WorkerDrainError::InvalidDecision {
                    index,
                    source: Box::new(source),
                })?;
        }
        Ok(())
    }

    pub fn for_worker<I>(
        worker: &WorkerAdvertisement,
        policy: &WorkerDrainPolicy,
        tasks: I,
        drain_started_at_unix_ms: u64,
        now_unix_ms: u64,
    ) -> Result<Self, WorkerDrainError>
    where
        I: IntoIterator<Item = WorkerDrainTask>,
    {
        policy
            .validate()
            .map_err(|source| WorkerDrainError::InvalidPolicy {
                source: Box::new(source),
            })?;

        let mut decisions = Vec::new();
        for task in tasks {
            task.validate()
                .map_err(|source| WorkerDrainError::InvalidTask {
                    source: Box::new(source),
                })?;
            let (timeout_ms, deadline_disposition) = match task.workload {
                WorkerDrainWorkloadKind::OnlineRequest => (
                    policy.online_request_timeout_ms,
                    policy.on_deadline.online_request,
                ),
                WorkerDrainWorkloadKind::DurableTask => (
                    policy.durable_task_timeout_ms,
                    policy.on_deadline.durable_task,
                ),
                WorkerDrainWorkloadKind::RealtimeSession => (
                    policy.realtime_session_timeout_ms,
                    policy.on_deadline.realtime_session,
                ),
            };
            let deadline_unix_ms = drain_started_at_unix_ms.saturating_add(timeout_ms);
            let (disposition, reason) = if now_unix_ms >= deadline_unix_ms {
                if deadline_disposition == WorkerDrainDisposition::Checkpoint
                    && !task.checkpointable
                {
                    (
                        WorkerDrainDisposition::Cancel,
                        "checkpoint_unavailable".to_owned(),
                    )
                } else {
                    (deadline_disposition, "deadline_reached".to_owned())
                }
            } else {
                (
                    WorkerDrainDisposition::FinishInPlace,
                    "within_drain_deadline".to_owned(),
                )
            };
            decisions.push(WorkerDrainDecision {
                workload: task.workload,
                run_id: task.request.run_id,
                invocation_id: task.request.invocation_id,
                node_attempt_id: task.request.node_attempt_id,
                lease_epoch: task.request.lease_epoch,
                release_id: task.request.context.release_id,
                deployment_revision_id: task.request.context.deployment_revision_id,
                disposition,
                deadline_unix_ms,
                reason,
            });
        }

        let plan = Self {
            worker_id: worker.worker_id.clone(),
            target_id: worker.target_id.clone(),
            worker_state: WorkerState::Draining,
            admission_closed: true,
            drain_started_at_unix_ms,
            decisions,
        };
        plan.validate()?;
        Ok(plan)
    }
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
pub enum WorkerInvokeResultError {
    EmptyField { field: String },
    EmptyOutputKey,
}

impl WorkerInvokeResult {
    pub fn validate(&self) -> Result<(), WorkerInvokeResultError> {
        if self.invocation_id.trim().is_empty() {
            return Err(WorkerInvokeResultError::EmptyField {
                field: "invocation_id".to_owned(),
            });
        }
        if self.node_attempt_id.trim().is_empty() {
            return Err(WorkerInvokeResultError::EmptyField {
                field: "node_attempt_id".to_owned(),
            });
        }
        if self.outputs.keys().any(|key| key.trim().is_empty()) {
            return Err(WorkerInvokeResultError::EmptyOutputKey);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerResultError {
    InvalidRequest { source: WorkerInvokeRequestError },
    InvalidResult { source: WorkerInvokeResultError },
    MismatchedInvocationId { expected: String, actual: String },
    MismatchedNodeAttempt { expected: String, actual: String },
    StaleLeaseEpoch { expected: u64, actual: u64 },
}

pub fn validate_worker_result(
    request: &WorkerInvokeRequest,
    result: &WorkerInvokeResult,
) -> Result<(), WorkerResultError> {
    request
        .validate()
        .map_err(|source| WorkerResultError::InvalidRequest { source })?;
    result
        .validate()
        .map_err(|source| WorkerResultError::InvalidResult { source })?;
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
