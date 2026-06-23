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
    pub supported_blocks: Vec<BlockCapability>,
    pub draining: bool,
}

impl WorkerAdvertisement {
    pub fn new<I>(worker_id: impl Into<String>, supported_blocks: I) -> Self
    where
        I: IntoIterator<Item = BlockCapability>,
    {
        Self {
            protocol_version: WORKER_PROTOCOL_VERSION,
            worker_id: worker_id.into(),
            supported_blocks: supported_blocks.into_iter().collect(),
            draining: false,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkerProtocolError {
    IncompatibleVersion { expected: u16, actual: u16 },
    EmptyWorkerId,
    EmptySupportedBlocks,
}

pub fn admit_worker(advertisement: &WorkerAdvertisement) -> Result<(), WorkerProtocolError> {
    if advertisement.protocol_version != WORKER_PROTOCOL_VERSION {
        return Err(WorkerProtocolError::IncompatibleVersion {
            expected: WORKER_PROTOCOL_VERSION,
            actual: advertisement.protocol_version,
        });
    }
    if advertisement.worker_id.is_empty() {
        return Err(WorkerProtocolError::EmptyWorkerId);
    }
    if advertisement.supported_blocks.is_empty() {
        return Err(WorkerProtocolError::EmptySupportedBlocks);
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerInvokeRequest {
    pub invocation_id: String,
    pub run_id: String,
    pub node_id: String,
    pub block: String,
    pub inputs: Value,
    pub config: Value,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerInvokeResult {
    pub invocation_id: String,
    pub outputs: BTreeMap<String, Value>,
}
