use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::fs::{File, OpenOptions};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_compiler::compiler::{
    BlockCatalog, BlockDescriptor, ExecutionPhase, compile_graph_with_catalog,
};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_schema::{parse_canonical_json, parse_duration_milliseconds};
use hmac::{Hmac, Mac};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use sha2::Sha256;

use crate::application_event::{
    ApplicationProtocolEvent, ApplicationProtocolEventKind, ApplicationProtocolEventMetadata,
    SqliteApplicationProtocolLog,
};
use crate::async_operation::{
    AsyncCallbackResumeDecision, AsyncCallbackSubmission, AsyncOperation, AsyncOperationEvent,
    AsyncOperationKind, AsyncOperationResult, AsyncOperationResultStatus, AsyncOperationState,
    CallbackArtifactRef, ExternalEffectRecord, SqliteAsyncOperationStore,
};
use crate::journal::{JournalMetadata, JournalRecord, SqliteExecutionJournal};
use crate::outcome::{BlockError, ErrorCategory, Outcome, SkipReason};
use crate::readiness::{InputDependency, PortRef, ResolvedInput};
use crate::run_store::{RunDeploymentProvenance, RunStatus, RunStoreError, SqliteRunStore};
use crate::scheduler::{ScheduledCondition, ScheduledNode, StartedNode};
use crate::stdlib_blocks::stdlib_block_catalog;
use crate::test_runtime::{InProcessTestRuntime, NodeExecutor, TestRunResult, TestRunStatus};
use crate::tool::{
    BlockToolImplementation, GraphToolImplementation, McpToolImplementation,
    OpenApiToolImplementation, RemoteToolImplementation, ResolvedTool, ToolApproval, ToolBinding,
    ToolCancellation, ToolCatalog, ToolDefinition, ToolEffect, ToolIdempotency, ToolImplementation,
    ToolResolutionScope, ToolResultMode,
};
use crate::tool_result::ToolEffectOutcome;
use crate::tool_schema::{JsonSchema, JsonSchemaNode, ToolSchemaRegistry};
use crate::typed_graph::GraphDocument;

type HmacSha256 = Hmac<Sha256>;
const MIN_CALLBACK_ADMISSION_HMAC_KEY_BYTES: usize = 32;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StdlibRuntimeError {
    InvalidInput { message: String },
    Runtime { message: String },
    Serialization { message: String },
}

impl StdlibRuntimeError {
    fn invalid(message: impl Into<String>) -> Self {
        Self::InvalidInput {
            message: message.into(),
        }
    }

    fn runtime(message: impl Into<String>) -> Self {
        Self::Runtime {
            message: message.into(),
        }
    }

    fn serialization(error: serde_json::Error) -> Self {
        Self::Serialization {
            message: error.to_string(),
        }
    }
}

impl fmt::Display for StdlibRuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidInput { message } => formatter.write_str(message),
            Self::Runtime { message } => formatter.write_str(message),
            Self::Serialization { message } => formatter.write_str(message),
        }
    }
}

impl Error for StdlibRuntimeError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StdlibRunStatus {
    Succeeded,
    Failed,
    Cancelled,
    WaitingCallback,
}

impl StdlibRunStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::WaitingCallback => "waiting_callback",
        }
    }
}

#[derive(Clone, Default, PartialEq)]
pub struct StdlibRunOptions {
    pub run_id: Option<String>,
    pub run_store_path: Option<String>,
    pub journal_store_path: Option<String>,
    pub application_event_store_path: Option<String>,
    pub checkpoint_store_path: Option<String>,
    pub async_operation_store_path: Option<String>,
    pub callback_receipt: Option<Value>,
    pub callback_admission_hmac_key: Option<String>,
    pub deployment_provenance: Option<RunDeploymentProvenance>,
}

impl fmt::Debug for StdlibRunOptions {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("StdlibRunOptions")
            .field("run_id", &self.run_id)
            .field("run_store_path", &self.run_store_path)
            .field("journal_store_path", &self.journal_store_path)
            .field(
                "application_event_store_path",
                &self.application_event_store_path,
            )
            .field("checkpoint_store_path", &self.checkpoint_store_path)
            .field(
                "async_operation_store_path",
                &self.async_operation_store_path,
            )
            .field(
                "callback_receipt",
                &self.callback_receipt.as_ref().map(|_| "<redacted>"),
            )
            .field(
                "callback_admission_hmac_key",
                &self
                    .callback_admission_hmac_key
                    .as_ref()
                    .map(|_| "<redacted>"),
            )
            .field("deployment_provenance", &self.deployment_provenance)
            .finish()
    }
}

impl StdlibRunOptions {
    pub fn with_run_id(mut self, run_id: impl Into<String>) -> Self {
        self.run_id = Some(run_id.into());
        self
    }

    pub fn with_run_store_path(mut self, path: impl Into<String>) -> Self {
        self.run_store_path = Some(path.into());
        self
    }

    pub fn with_journal_store_path(mut self, path: impl Into<String>) -> Self {
        self.journal_store_path = Some(path.into());
        self
    }

    pub fn with_application_event_store_path(mut self, path: impl Into<String>) -> Self {
        self.application_event_store_path = Some(path.into());
        self
    }

    pub fn with_checkpoint_store_path(mut self, path: impl Into<String>) -> Self {
        self.checkpoint_store_path = Some(path.into());
        self
    }

    pub fn with_async_operation_store_path(mut self, path: impl Into<String>) -> Self {
        self.async_operation_store_path = Some(path.into());
        self
    }

    pub fn with_callback_receipt(mut self, receipt: Value) -> Self {
        self.callback_receipt = Some(receipt);
        self
    }

    /// Supplies host-trusted key material used to verify callback resume admissions.
    ///
    /// This key is runtime configuration, not callback payload data, and must be
    /// injected by the trusted ingress host after authenticating the callback.
    pub fn with_callback_admission_hmac_key(mut self, key: impl Into<String>) -> Self {
        self.callback_admission_hmac_key = Some(key.into());
        self
    }

    pub fn with_deployment_provenance(mut self, provenance: RunDeploymentProvenance) -> Self {
        self.deployment_provenance = Some(provenance);
        self
    }

    fn canonical_value(&self) -> serde_json::Map<String, Value> {
        let mut value = serde_json::Map::new();
        if let Some(run_id) = &self.run_id {
            value.insert("runId".to_owned(), Value::String(run_id.clone()));
        }
        if let Some(path) = &self.run_store_path {
            value.insert("runStorePath".to_owned(), Value::String(path.clone()));
        }
        if let Some(path) = &self.journal_store_path {
            value.insert("journalStorePath".to_owned(), Value::String(path.clone()));
        }
        if let Some(path) = &self.application_event_store_path {
            value.insert(
                "applicationEventStorePath".to_owned(),
                Value::String(path.clone()),
            );
        }
        if let Some(path) = &self.checkpoint_store_path {
            value.insert(
                "checkpointStorePath".to_owned(),
                Value::String(path.clone()),
            );
        }
        if let Some(path) = &self.async_operation_store_path {
            value.insert(
                "asyncOperationStorePath".to_owned(),
                Value::String(path.clone()),
            );
        }
        if let Some(receipt) = &self.callback_receipt {
            value.insert("callbackReceipt".to_owned(), receipt.clone());
        }
        if let Some(key) = &self.callback_admission_hmac_key {
            value.insert(
                "callbackAdmissionHmacKey".to_owned(),
                Value::String(key.clone()),
            );
        }
        if let Some(provenance) = &self.deployment_provenance {
            value.insert(
                "deploymentProvenance".to_owned(),
                provenance.canonical_value(),
            );
        }
        value
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct StdlibRunResult {
    pub run_id: String,
    pub graph_hash: String,
    pub status: StdlibRunStatus,
    pub outputs: Value,
    pub journal: Vec<Value>,
    pub checkpoint: Option<Value>,
    pub deployment_provenance: Option<RunDeploymentProvenance>,
}

impl StdlibRunResult {
    pub fn canonical_value(&self) -> Value {
        let mut payload = json!({
            "runId": self.run_id,
            "graphHash": self.graph_hash,
            "status": self.status.as_str(),
            "outputs": self.outputs,
            "journal": self.journal,
            "checkpoint": self.checkpoint,
        });
        if let (Some(provenance), Some(payload)) =
            (&self.deployment_provenance, payload.as_object_mut())
        {
            payload.insert(
                "deploymentProvenance".to_owned(),
                provenance.canonical_value(),
            );
        }
        payload
    }
}

struct RuntimeBridgePlan {
    graph_hash: String,
    nodes: BTreeMap<String, Value>,
    descriptors_by_node: BTreeMap<String, BlockDescriptor>,
    scheduled_nodes: Vec<ScheduledNode>,
    input_output_projections: Vec<OutputProjection>,
    output_projections_by_node: BTreeMap<String, Vec<OutputProjection>>,
    output_ports_by_node: BTreeMap<String, Vec<String>>,
}

const INTERNAL_WHEN_INPUT: &str = "\0graphblocks.when";
const INTERNAL_SKIPPED_OUTPUT: &str = "\0graphblocks.skipped";

#[derive(Clone, Debug, Eq, PartialEq)]
struct OutputProjection {
    source: String,
    source_path: String,
    target: String,
    target_path: String,
}

struct StdlibExecutor {
    nodes: BTreeMap<String, Value>,
    descriptors_by_node: BTreeMap<String, BlockDescriptor>,
    output_projections_by_node: BTreeMap<String, Vec<OutputProjection>>,
    output_ports_by_node: BTreeMap<String, Vec<String>>,
    output_values: Value,
    replay_node_outputs: BTreeMap<String, Value>,
    resume_wait: Option<NativeResumeWait>,
    executed_node_outputs: BTreeMap<String, Value>,
    suspension: Option<NativeCallbackSuspension>,
}

#[derive(Clone, Debug)]
struct NativeResumeWait {
    node_id: String,
    operation: Value,
    callback: Value,
}

#[derive(Clone, Debug)]
struct NativeCallbackSuspension {
    wait_node: String,
    operation: Value,
}

struct NativeCheckpointBuildRequest<'a> {
    run_id: &'a str,
    graph_hash: &'a str,
    inputs: &'a Value,
    node_names: &'a [String],
    suspension: &'a NativeCallbackSuspension,
    node_outputs: &'a BTreeMap<String, Value>,
    output_values: &'a Value,
    journal_prefix: &'a [Value],
    deployment_provenance: Option<&'a RunDeploymentProvenance>,
}

struct RuntimeEvidencePersistence<'a> {
    result: &'a TestRunResult,
    graph_hash: &'a str,
    inputs: &'a Value,
    run_store_path: Option<&'a str>,
    journal_store_path: Option<&'a str>,
    application_event_store_path: Option<&'a str>,
    output_values: &'a Value,
    deployment_provenance: Option<&'a RunDeploymentProvenance>,
}

impl NodeExecutor for StdlibExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        let mut resolved_inputs = node.inputs;
        let Some(node_spec) = self.nodes.get(&node.node_id).and_then(Value::as_object) else {
            return Err(BlockError::new(
                format!("{}.missing_node", node.node_id),
                ErrorCategory::Configuration,
                "node spec must be an object",
                false,
            ));
        };
        if self.suspension.is_some() {
            let reason = SkipReason::new("run_waiting_callback");
            return Ok(self
                .output_ports_by_node
                .get(&node.node_id)
                .into_iter()
                .flatten()
                .map(|port| {
                    (
                        PortRef::new(node.node_id.clone(), port),
                        Outcome::Skipped(reason.clone()),
                    )
                })
                .collect());
        }
        if let Some(when) = node_spec.get("when") {
            let when = when
                .as_str()
                .expect("runtime bridge validates when references");
            let mut guard = match resolved_inputs.remove(INTERNAL_WHEN_INPUT) {
                Some(ResolvedInput::Value(value)) => value,
                _ => {
                    return Err(BlockError::new(
                        format!("{}.invalid_when", node.node_id),
                        ErrorCategory::Configuration,
                        "node.when guard did not resolve to a value",
                        false,
                    ));
                }
            };
            let guard_path = when
                .split_once('.')
                .expect("runtime bridge validates when references")
                .1;
            for part in guard_path.split('.').skip(1) {
                let Some(value) = guard.get(part).cloned() else {
                    return Err(BlockError::new(
                        format!("{}.invalid_when", node.node_id),
                        ErrorCategory::Configuration,
                        format!("node.when guard {when:?} is missing path segment {part:?}"),
                        false,
                    ));
                };
                guard = value;
            }
            match guard {
                Value::Bool(true) => {}
                Value::Bool(false) => {
                    let reason = SkipReason::new("condition_false");
                    let mut skipped_outputs = self
                        .output_ports_by_node
                        .get(&node.node_id)
                        .into_iter()
                        .flatten()
                        .map(|port| {
                            (
                                PortRef::new(node.node_id.clone(), port),
                                Outcome::Skipped(reason.clone()),
                            )
                        })
                        .collect::<Vec<_>>();
                    skipped_outputs.push((
                        PortRef::new(node.node_id, INTERNAL_SKIPPED_OUTPUT),
                        Outcome::Skipped(reason),
                    ));
                    return Ok(skipped_outputs);
                }
                _ => {
                    return Err(BlockError::new(
                        format!("{}.invalid_when", node.node_id),
                        ErrorCategory::Configuration,
                        format!("node.when guard {when:?} must resolve to a boolean"),
                        false,
                    ));
                }
            }
        }
        let inputs = resolved_inputs_to_json(&resolved_inputs)?;
        let Some(block_id) = node_spec.get("block").and_then(Value::as_str) else {
            return Err(BlockError::new(
                format!("{}.missing_block", node.node_id),
                ErrorCategory::Configuration,
                "node.block must be a string",
                false,
            ));
        };
        let config = node_spec
            .get("config")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let (outputs, phase) = if let Some(outputs) = self.replay_node_outputs.get(&node.node_id) {
            (outputs.clone(), ExecutionPhase::Initial)
        } else if self
            .resume_wait
            .as_ref()
            .is_some_and(|resume| resume.node_id == node.node_id)
        {
            let resume = self.resume_wait.as_ref().expect("resume wait exists");
            let mut operation = resume.operation.clone();
            operation["state"] = json!("resuming");
            (
                json!({
                    "wait": {
                        "state": "resumed",
                        "operation": operation,
                        "checkpoint": false,
                    },
                    "callback": resume.callback,
                    "operation": operation,
                }),
                ExecutionPhase::Resumed,
            )
        } else {
            (
                execute_stdlib_block(block_id, &inputs, &config)?,
                ExecutionPhase::Initial,
            )
        };
        let Some(outputs_object) = outputs.as_object() else {
            return Err(BlockError::new(
                format!("{block_id}.invalid_outputs"),
                ErrorCategory::Internal,
                "stdlib block returned non-object outputs",
                false,
            ));
        };
        let descriptor = self.descriptors_by_node.get(&node.node_id).ok_or_else(|| {
            BlockError::new(
                format!("{block_id}.missing_descriptor"),
                ErrorCategory::Internal,
                format!("stdlib node {:?} has no block descriptor", node.node_id),
                false,
            )
        })?;
        validate_stdlib_output_contract(block_id, descriptor, outputs_object, &config, phase)?;
        if block_id == "async.await_callback@1"
            && self.resume_wait.is_none()
            && outputs
                .pointer("/wait/checkpoint")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        {
            let operation = outputs.pointer("/wait/operation").cloned().ok_or_else(|| {
                BlockError::new(
                    "async.await_callback.invalid_checkpoint",
                    ErrorCategory::Internal,
                    "async callback checkpoint is missing operation state",
                    false,
                )
            })?;
            self.suspension = Some(NativeCallbackSuspension {
                wait_node: node.node_id.clone(),
                operation,
            });
        } else if self.resume_wait.is_none() {
            self.executed_node_outputs
                .insert(node.node_id.clone(), outputs.clone());
        }
        let projections = self
            .output_projections_by_node
            .get(&node.node_id)
            .cloned()
            .unwrap_or_default();
        for projection in &projections {
            project_output_value(&mut self.output_values, &outputs, projection).map_err(
                |message| {
                    BlockError::new(
                        format!("{}.output_projection", node.node_id),
                        ErrorCategory::Internal,
                        message,
                        false,
                    )
                },
            )?;
        }
        let port_outputs = outputs_object
            .iter()
            .map(|(port, value)| {
                (
                    PortRef::new(node.node_id.clone(), port.clone()),
                    Outcome::Value(value.clone()),
                )
            })
            .collect();
        Ok(port_outputs)
    }
}

fn validate_stdlib_output_contract(
    block_id: &str,
    descriptor: &BlockDescriptor,
    outputs: &serde_json::Map<String, Value>,
    config: &Value,
    phase: ExecutionPhase,
) -> Result<(), BlockError> {
    let declared_outputs = descriptor
        .outputs
        .iter()
        .map(|port| port.name.as_str())
        .collect::<BTreeSet<_>>();
    let unexpected = outputs
        .keys()
        .filter(|name| !declared_outputs.contains(name.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    if !unexpected.is_empty() {
        return Err(BlockError::new(
            format!("{block_id}.invalid_outputs"),
            ErrorCategory::Internal,
            format!(
                "{block_id} returned undeclared output(s): {}",
                unexpected.join(", ")
            ),
            false,
        ));
    }
    let missing = descriptor
        .outputs
        .iter()
        .filter(|port| port.required_for(config, phase) && !outputs.contains_key(&port.name))
        .map(|port| port.name.clone())
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return Err(BlockError::new(
            format!("{block_id}.invalid_outputs"),
            ErrorCategory::Internal,
            format!(
                "{block_id} omitted required output(s) in {} phase: {}",
                match phase {
                    ExecutionPhase::Initial => "initial",
                    ExecutionPhase::Resumed => "resumed",
                },
                missing.join(", ")
            ),
            false,
        ));
    }
    Ok(())
}

fn project_output_value(
    output_values: &mut Value,
    source_value: &Value,
    projection: &OutputProjection,
) -> Result<(), String> {
    let mut value = source_value.clone();
    if !projection.source_path.is_empty() {
        for part in projection.source_path.split('.') {
            value = value.get(part).cloned().ok_or_else(|| {
                format!(
                    "output edge source {:?} is missing path segment {:?}",
                    projection.source, part
                )
            })?;
        }
    }
    let target_parts = projection.target_path.split('.').collect::<Vec<_>>();
    if target_parts.is_empty() || target_parts.iter().any(|part| part.is_empty()) {
        return Err(format!(
            "output edge target {:?} must include an output path",
            projection.target
        ));
    }
    let mut current = output_values;
    for part in &target_parts[..target_parts.len() - 1] {
        let Some(current_object) = current.as_object_mut() else {
            return Err(format!("output path conflict at {:?}", projection.target));
        };
        current = current_object
            .entry((*part).to_owned())
            .or_insert_with(|| json!({}));
    }
    let Some(current_object) = current.as_object_mut() else {
        return Err(format!("output path conflict at {:?}", projection.target));
    };
    if let Some(target_name) = target_parts.last() {
        current_object.insert((*target_name).to_owned(), value);
    }
    Ok(())
}

pub fn run_stdlib_graph_json(
    graph_json: &str,
    inputs_json: &str,
) -> Result<String, StdlibRuntimeError> {
    run_stdlib_graph_with_options_json(graph_json, inputs_json, "{}")
}

pub fn run_stdlib_graph_with_options_json(
    graph_json: &str,
    inputs_json: &str,
    options_json: &str,
) -> Result<String, StdlibRuntimeError> {
    let graph = parse_json_argument(graph_json, "graph document")?;
    let inputs = parse_json_argument(inputs_json, "runtime inputs")?;
    let options = parse_json_argument(options_json, "runtime options")?;
    let Some(options) = options.as_object() else {
        return Err(StdlibRuntimeError::invalid(
            "runtime options JSON must be an object",
        ));
    };
    let block_catalog = stdlib_block_catalog().map_err(|error| {
        StdlibRuntimeError::invalid(format!("invalid stdlib block catalog: {error}"))
    })?;
    let result = run_stdlib_graph_values(&graph, &inputs, options, &block_catalog)?;
    serde_json::to_string(&result.canonical_value()).map_err(StdlibRuntimeError::serialization)
}

pub fn run_stdlib_graph(
    graph: &GraphDocument,
    inputs: &Value,
) -> Result<StdlibRunResult, StdlibRuntimeError> {
    run_stdlib_graph_with_options(graph, inputs, &StdlibRunOptions::default())
}

pub fn run_stdlib_graph_with_options(
    graph: &GraphDocument,
    inputs: &Value,
    options: &StdlibRunOptions,
) -> Result<StdlibRunResult, StdlibRuntimeError> {
    run_stdlib_graph_values(
        graph.as_value(),
        inputs,
        &options.canonical_value(),
        graph.block_catalog(),
    )
}

fn run_stdlib_graph_values(
    graph: &Value,
    inputs: &Value,
    options: &serde_json::Map<String, Value>,
    block_catalog: &BlockCatalog,
) -> Result<StdlibRunResult, StdlibRuntimeError> {
    let run_id = options
        .get("runId")
        .or_else(|| options.get("run_id"))
        .filter(|value| !value.is_null())
        .map(|value| {
            let Some(run_id) = value.as_str() else {
                return Err(StdlibRuntimeError::invalid(
                    "runtime options field runId must be a string",
                ));
            };
            if run_id.trim().is_empty() {
                return Err(StdlibRuntimeError::invalid(
                    "runtime options field runId must not be empty",
                ));
            }
            Ok(run_id)
        })
        .transpose()?
        .unwrap_or("run-000001");
    let run_store_path = optional_options_string(options, "runStorePath", "run_store_path")?;
    let journal_store_path =
        optional_options_string(options, "journalStorePath", "journal_store_path")?;
    let application_event_store_path = optional_options_string(
        options,
        "applicationEventStorePath",
        "application_event_store_path",
    )?;
    let checkpoint_store_path =
        optional_options_string(options, "checkpointStorePath", "checkpoint_store_path")?;
    let async_operation_store_path = optional_options_string(
        options,
        "asyncOperationStorePath",
        "async_operation_store_path",
    )?;
    let callback_receipt = options
        .get("callbackReceipt")
        .or_else(|| options.get("callback_receipt"))
        .filter(|value| !value.is_null());
    let callback_admission_hmac_key = optional_options_string(
        options,
        "callbackAdmissionHmacKey",
        "callback_admission_hmac_key",
    )?;
    let deployment_provenance = options
        .get("deploymentProvenance")
        .or_else(|| options.get("deployment_provenance"))
        .filter(|value| !value.is_null())
        .map(RunDeploymentProvenance::from_production_value)
        .transpose()
        .map_err(|message| StdlibRuntimeError::invalid(format!("runtime options {message}")))?;
    let bridge_plan = build_runtime_bridge_plan(graph, block_catalog)?;
    let mut initial_output_values = json!({});
    for projection in &bridge_plan.input_output_projections {
        project_output_value(&mut initial_output_values, inputs, projection)
            .map_err(StdlibRuntimeError::invalid)?;
    }
    if let Some(checkpoint_store_path) = checkpoint_store_path {
        return run_native_callback_graph_values(NativeCallbackRunRequest {
            bridge_plan,
            inputs,
            run_id,
            checkpoint_store_path,
            async_operation_store_path: async_operation_store_path.unwrap_or(checkpoint_store_path),
            run_store_path,
            journal_store_path,
            callback_receipt,
            callback_admission_hmac_key,
            initial_output_values,
            deployment_provenance: deployment_provenance.as_ref(),
        });
    }
    if callback_receipt.is_some() {
        return Err(StdlibRuntimeError::invalid(
            "runtime callbackReceipt requires checkpointStorePath",
        ));
    }
    let mut runtime = runtime_with_inputs(bridge_plan.scheduled_nodes, inputs, run_id)?;
    let mut executor = StdlibExecutor {
        nodes: bridge_plan.nodes,
        descriptors_by_node: bridge_plan.descriptors_by_node,
        output_projections_by_node: bridge_plan.output_projections_by_node,
        output_ports_by_node: bridge_plan.output_ports_by_node,
        output_values: initial_output_values,
        replay_node_outputs: BTreeMap::new(),
        resume_wait: None,
        executed_node_outputs: BTreeMap::new(),
        suspension: None,
    };
    let result = runtime.run(&mut executor).map_err(|error| {
        StdlibRuntimeError::runtime(format!("stdlib runtime execution failed: {error:?}"))
    })?;
    if executor.suspension.is_some() {
        return Err(StdlibRuntimeError::invalid(
            "native callback suspension requires checkpointStorePath",
        ));
    }
    let output_values = if result.status == TestRunStatus::Succeeded {
        executor.output_values
    } else {
        json!({})
    };
    persist_runtime_evidence(RuntimeEvidencePersistence {
        result: &result,
        graph_hash: &bridge_plan.graph_hash,
        inputs,
        run_store_path,
        journal_store_path,
        application_event_store_path,
        output_values: &output_values,
        deployment_provenance: deployment_provenance.as_ref(),
    })?;
    Ok(build_runtime_result(
        result,
        bridge_plan.graph_hash,
        output_values,
        deployment_provenance.as_ref(),
    ))
}

struct NativeCallbackRunRequest<'a> {
    bridge_plan: RuntimeBridgePlan,
    inputs: &'a Value,
    run_id: &'a str,
    checkpoint_store_path: &'a str,
    async_operation_store_path: &'a str,
    run_store_path: Option<&'a str>,
    journal_store_path: Option<&'a str>,
    callback_receipt: Option<&'a Value>,
    callback_admission_hmac_key: Option<&'a str>,
    initial_output_values: Value,
    deployment_provenance: Option<&'a RunDeploymentProvenance>,
}

#[derive(Clone, Copy)]
struct NativeCallbackEvidenceContext<'a> {
    async_operation_store_path: &'a str,
    run_store_path: Option<&'a str>,
    journal_store_path: Option<&'a str>,
    callback_admission_hmac_key: Option<&'a str>,
    inputs: &'a Value,
    deployment_provenance: Option<&'a RunDeploymentProvenance>,
}

impl<'a> From<&NativeCallbackRunRequest<'a>> for NativeCallbackEvidenceContext<'a> {
    fn from(request: &NativeCallbackRunRequest<'a>) -> Self {
        Self {
            async_operation_store_path: request.async_operation_store_path,
            run_store_path: request.run_store_path,
            journal_store_path: request.journal_store_path,
            callback_admission_hmac_key: request.callback_admission_hmac_key,
            inputs: request.inputs,
            deployment_provenance: request.deployment_provenance,
        }
    }
}

struct StoredNativeCallbackRun {
    checkpoint: Value,
    result: StdlibRunResult,
    phase: NativeCallbackCoordinatorPhase,
    callback_idempotency_key: Option<String>,
    callback_payload_digest: Option<String>,
    callback_receipt: Option<Value>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum NativeCallbackCoordinatorPhase {
    WaitingEvidencePending,
    WaitingCallback,
    CallbackAccepted,
    TerminalEvidencePending,
    Terminal,
}

impl NativeCallbackCoordinatorPhase {
    fn as_str(self) -> &'static str {
        match self {
            Self::WaitingEvidencePending => "waiting_evidence_pending",
            Self::WaitingCallback => "waiting_callback",
            Self::CallbackAccepted => "callback_accepted",
            Self::TerminalEvidencePending => "terminal_evidence_pending",
            Self::Terminal => "terminal",
        }
    }

    fn parse(value: &str) -> Option<Self> {
        match value {
            "waiting_evidence_pending" => Some(Self::WaitingEvidencePending),
            "waiting_callback" => Some(Self::WaitingCallback),
            "callback_accepted" => Some(Self::CallbackAccepted),
            "terminal_evidence_pending" => Some(Self::TerminalEvidencePending),
            "terminal" => Some(Self::Terminal),
            _ => None,
        }
    }

    fn is_pre_acceptance(self) -> bool {
        matches!(self, Self::WaitingEvidencePending | Self::WaitingCallback)
    }

    fn has_waiting_result(self) -> bool {
        self.is_pre_acceptance() || self == Self::CallbackAccepted
    }

    fn is_terminal(self) -> bool {
        matches!(self, Self::TerminalEvidencePending | Self::Terminal)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct TrustedNativeCallbackResumeAdmission {
    authorized: bool,
    authentication_decision_id: String,
    policy_decision_id: String,
    budget_reservation_id: String,
    compatible_release_digest: String,
    run_id: String,
    operation_id: String,
    node_id: String,
    attempt_id: String,
    checkpoint_id: String,
    checkpoint_state_digest: String,
    owner_id: String,
    lease_id: String,
    fencing_epoch: u64,
    fence_token: String,
    schema_verification_id: String,
    schema_id: String,
    payload_digest: String,
    schema_verified_by: String,
}

fn run_native_callback_graph_values(
    request: NativeCallbackRunRequest<'_>,
) -> Result<StdlibRunResult, StdlibRuntimeError> {
    let admission = request
        .callback_receipt
        .map(|receipt| {
            validate_native_callback_receipt_shape(receipt, request.callback_admission_hmac_key)
        })
        .transpose()?;
    if admission
        .as_ref()
        .is_some_and(|admission| !admission.authorized)
    {
        return Err(trusted_native_callback_admission_rejected());
    }
    let stored = load_native_callback_run(request.checkpoint_store_path, request.run_id)?;
    match (stored, request.callback_receipt) {
        (Some(_), Some(receipt)) => resume_native_callback_run(
            request,
            receipt,
            admission
                .as_ref()
                .expect("callback receipt admission was parsed"),
        ),
        (Some(stored), None) => {
            validate_native_checkpoint_identity(
                &stored.checkpoint,
                request.run_id,
                &request.bridge_plan.graph_hash,
                request.inputs,
                request.deployment_provenance,
            )?;
            reconcile_native_callback_evidence(
                NativeCallbackEvidenceContext::from(&request),
                &stored,
            )?;
            let reconciled_phase = match stored.phase {
                NativeCallbackCoordinatorPhase::WaitingEvidencePending => {
                    NativeCallbackCoordinatorPhase::WaitingCallback
                }
                NativeCallbackCoordinatorPhase::WaitingCallback
                | NativeCallbackCoordinatorPhase::CallbackAccepted => stored.phase,
                NativeCallbackCoordinatorPhase::TerminalEvidencePending
                | NativeCallbackCoordinatorPhase::Terminal => {
                    NativeCallbackCoordinatorPhase::Terminal
                }
            };
            mark_native_callback_phase(
                request.checkpoint_store_path,
                request.run_id,
                stored.phase,
                reconciled_phase,
            )?;
            Ok(stored.result)
        }
        (None, Some(_)) => Err(StdlibRuntimeError::invalid(
            "native async callback rejected",
        )),
        (None, None) => start_native_callback_run(request),
    }
}

fn start_native_callback_run(
    request: NativeCallbackRunRequest<'_>,
) -> Result<StdlibRunResult, StdlibRuntimeError> {
    let evidence_context = NativeCallbackEvidenceContext::from(&request);
    let RuntimeBridgePlan {
        graph_hash,
        nodes,
        descriptors_by_node,
        scheduled_nodes,
        input_output_projections: _,
        output_projections_by_node,
        output_ports_by_node,
    } = request.bridge_plan;
    let node_names = nodes.keys().cloned().collect::<Vec<_>>();
    let mut runtime = runtime_with_inputs(scheduled_nodes, request.inputs, request.run_id)?;
    let mut executor = StdlibExecutor {
        nodes,
        descriptors_by_node,
        output_projections_by_node,
        output_ports_by_node,
        output_values: request.initial_output_values,
        replay_node_outputs: BTreeMap::new(),
        resume_wait: None,
        executed_node_outputs: BTreeMap::new(),
        suspension: None,
    };
    let result = runtime.run(&mut executor).map_err(|error| {
        StdlibRuntimeError::runtime(format!("stdlib runtime execution failed: {error:?}"))
    })?;
    let Some(suspension) = executor.suspension else {
        let output_values = if result.status == TestRunStatus::Succeeded {
            executor.output_values
        } else {
            json!({})
        };
        persist_runtime_evidence(RuntimeEvidencePersistence {
            result: &result,
            graph_hash: &graph_hash,
            inputs: request.inputs,
            run_store_path: request.run_store_path,
            journal_store_path: request.journal_store_path,
            application_event_store_path: None,
            output_values: &output_values,
            deployment_provenance: request.deployment_provenance,
        })?;
        return Ok(build_runtime_result(
            result,
            graph_hash,
            output_values,
            request.deployment_provenance,
        ));
    };

    let journal_prefix = native_waiting_journal_prefix(&result, &suspension);
    let checkpoint = build_native_callback_checkpoint(NativeCheckpointBuildRequest {
        run_id: request.run_id,
        graph_hash: &graph_hash,
        inputs: request.inputs,
        node_names: &node_names,
        suspension: &suspension,
        node_outputs: &executor.executed_node_outputs,
        output_values: &executor.output_values,
        journal_prefix: &journal_prefix,
        deployment_provenance: request.deployment_provenance,
    });
    validate_native_checkpoint_identity(
        &checkpoint,
        request.run_id,
        &graph_hash,
        request.inputs,
        request.deployment_provenance,
    )?;
    let journal = waiting_native_journal(journal_prefix, &suspension, &checkpoint);
    let waiting = StdlibRunResult {
        run_id: request.run_id.to_owned(),
        graph_hash: graph_hash.clone(),
        status: StdlibRunStatus::WaitingCallback,
        outputs: executor.output_values,
        journal,
        checkpoint: Some(checkpoint.clone()),
        deployment_provenance: request.deployment_provenance.cloned(),
    };
    persist_native_callback_run(
        request.checkpoint_store_path,
        &checkpoint,
        &waiting,
        NativeCallbackPersistence {
            expected_phase: None,
            phase: NativeCallbackCoordinatorPhase::WaitingEvidencePending,
            callback_idempotency_key: None,
            callback_payload_digest: None,
            callback_receipt: None,
        },
    )?;
    let stored = StoredNativeCallbackRun {
        checkpoint,
        result: waiting.clone(),
        phase: NativeCallbackCoordinatorPhase::WaitingEvidencePending,
        callback_idempotency_key: None,
        callback_payload_digest: None,
        callback_receipt: None,
    };
    reconcile_native_callback_evidence(evidence_context, &stored)?;
    mark_native_callback_phase(
        request.checkpoint_store_path,
        request.run_id,
        NativeCallbackCoordinatorPhase::WaitingEvidencePending,
        NativeCallbackCoordinatorPhase::WaitingCallback,
    )?;
    Ok(waiting)
}

fn resume_native_callback_run(
    request: NativeCallbackRunRequest<'_>,
    receipt: &Value,
    admission: &TrustedNativeCallbackResumeAdmission,
) -> Result<StdlibRunResult, StdlibRuntimeError> {
    // Serialize reload, callback acceptance, replay, and terminal persistence for
    // this run across threads and worker processes. The persistent lock inode is
    // intentional: unlinking it could let a waiter lock a different inode while
    // the current owner is still in the critical section.
    let _resume_lock =
        acquire_native_callback_resume_lock(request.checkpoint_store_path, request.run_id)?;
    let mut stored = load_native_callback_run(request.checkpoint_store_path, request.run_id)?
        .ok_or_else(native_callback_rejected)?;
    let evidence_context = NativeCallbackEvidenceContext::from(&request);
    validate_native_checkpoint_identity(
        &stored.checkpoint,
        request.run_id,
        &request.bridge_plan.graph_hash,
        request.inputs,
        request.deployment_provenance,
    )?;
    let receipt_object = receipt
        .as_object()
        .expect("callback receipt shape validation requires an object");
    let callback_idempotency_key = receipt_object["callback_idempotency_key"]
        .as_str()
        .expect("callback idempotency validation requires a string");
    let callback_payload_digest = receipt_object["payload_digest"]
        .as_str()
        .expect("callback payload digest validation requires a string");
    validate_native_callback_against_checkpoint(receipt, &stored.checkpoint)?;
    validate_trusted_native_callback_admission(
        admission,
        receipt,
        &stored.checkpoint,
        request.deployment_provenance,
        &request.bridge_plan.graph_hash,
    )?;
    if stored.phase.is_terminal() {
        if stored.callback_idempotency_key.as_deref() == Some(callback_idempotency_key)
            && stored.callback_payload_digest.as_deref() == Some(callback_payload_digest)
        {
            reconcile_native_callback_evidence(evidence_context, &stored)?;
            mark_native_callback_phase(
                request.checkpoint_store_path,
                request.run_id,
                stored.phase,
                NativeCallbackCoordinatorPhase::Terminal,
            )?;
            return Ok(stored.result);
        }
        return Err(StdlibRuntimeError::invalid(
            "native async callback rejected",
        ));
    }
    if stored.phase == NativeCallbackCoordinatorPhase::CallbackAccepted {
        if stored.callback_receipt.as_ref() != Some(receipt)
            || stored.callback_idempotency_key.as_deref() != Some(callback_idempotency_key)
            || stored.callback_payload_digest.as_deref() != Some(callback_payload_digest)
        {
            return Err(native_callback_rejected());
        }
    } else {
        let operation_id = stored
            .checkpoint
            .pointer("/operation/operation_id")
            .and_then(Value::as_str)
            .expect("validated callback checkpoint has an operation id");
        let operation_store = SqliteAsyncOperationStore::open(request.async_operation_store_path)
            .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to open native async operation store: {error:?}"
            ))
        })?;
        let operation_state =
            operation_store
                .try_operation_state(operation_id)
                .map_err(|error| {
                    StdlibRuntimeError::runtime(format!(
                        "failed to load native callback operation: {error:?}"
                    ))
                })?;
        match operation_state {
            Some(AsyncOperationState::CallbackReceived | AsyncOperationState::Resuming) => {
                validate_persisted_native_operation_identity(
                    request.async_operation_store_path,
                    &stored.checkpoint["operation"],
                )?;
                validate_persisted_native_callback_acceptance(
                    &operation_store,
                    receipt,
                    admission,
                )?;
            }
            Some(AsyncOperationState::WaitingCallback) | None => {
                reconcile_native_callback_evidence(evidence_context, &stored)?;
                mark_native_callback_phase(
                    request.checkpoint_store_path,
                    request.run_id,
                    stored.phase,
                    NativeCallbackCoordinatorPhase::WaitingCallback,
                )?;
                stored.phase = NativeCallbackCoordinatorPhase::WaitingCallback;
                accept_native_callback(request.async_operation_store_path, receipt, admission)?;
            }
            _ => return Err(native_callback_rejected()),
        }
        persist_native_callback_run(
            request.checkpoint_store_path,
            &stored.checkpoint,
            &stored.result,
            NativeCallbackPersistence {
                expected_phase: Some(stored.phase),
                phase: NativeCallbackCoordinatorPhase::CallbackAccepted,
                callback_idempotency_key: Some(callback_idempotency_key),
                callback_payload_digest: Some(callback_payload_digest),
                callback_receipt: Some(receipt),
            },
        )?;
        stored.phase = NativeCallbackCoordinatorPhase::CallbackAccepted;
        stored.callback_idempotency_key = Some(callback_idempotency_key.to_owned());
        stored.callback_payload_digest = Some(callback_payload_digest.to_owned());
        stored.callback_receipt = Some(receipt.clone());
    }
    reconcile_native_callback_evidence(evidence_context, &stored)?;
    let operation = stored
        .checkpoint
        .get("operation")
        .cloned()
        .expect("validated checkpoint has operation");
    let replay_node_outputs = value_object_to_btree(
        stored
            .checkpoint
            .get("node_outputs")
            .expect("validated checkpoint has node_outputs"),
        "native callback checkpoint node_outputs",
    )?;
    let wait_node = stored.checkpoint["wait_node"]
        .as_str()
        .expect("validated checkpoint wait_node is a string")
        .to_owned();
    let callback = receipt_object["payload"].clone();
    let RuntimeBridgePlan {
        graph_hash,
        nodes,
        descriptors_by_node,
        scheduled_nodes,
        input_output_projections: _,
        output_projections_by_node,
        output_ports_by_node,
    } = request.bridge_plan;
    let mut runtime = runtime_with_inputs(scheduled_nodes, request.inputs, request.run_id)?;
    let mut executor = StdlibExecutor {
        nodes,
        descriptors_by_node,
        output_projections_by_node,
        output_ports_by_node,
        output_values: stored.checkpoint["output_values"].clone(),
        replay_node_outputs: replay_node_outputs.clone(),
        resume_wait: Some(NativeResumeWait {
            node_id: wait_node,
            operation,
            callback,
        }),
        executed_node_outputs: BTreeMap::new(),
        suspension: None,
    };
    let resumed = match runtime.run(&mut executor) {
        Ok(result) => {
            let output_values = if result.status == TestRunStatus::Succeeded {
                executor.output_values
            } else {
                json!({})
            };
            let status = match result.status {
                TestRunStatus::Succeeded => StdlibRunStatus::Succeeded,
                TestRunStatus::Failed => StdlibRunStatus::Failed,
                TestRunStatus::Cancelled => StdlibRunStatus::Cancelled,
            };
            let journal = resumed_native_journal(
                &stored.result.journal,
                &result,
                &replay_node_outputs,
                receipt,
            );
            StdlibRunResult {
                run_id: request.run_id.to_owned(),
                graph_hash,
                status,
                outputs: output_values,
                journal,
                checkpoint: None,
                deployment_provenance: request.deployment_provenance.cloned(),
            }
        }
        Err(error) => failed_native_callback_resume_result(
            &stored.result,
            request.run_id,
            graph_hash,
            receipt,
            format!("stdlib callback resume failed: {error:?}"),
            request.deployment_provenance,
        ),
    };
    persist_native_callback_run(
        request.checkpoint_store_path,
        &stored.checkpoint,
        &resumed,
        NativeCallbackPersistence {
            expected_phase: Some(NativeCallbackCoordinatorPhase::CallbackAccepted),
            phase: NativeCallbackCoordinatorPhase::TerminalEvidencePending,
            callback_idempotency_key: Some(callback_idempotency_key),
            callback_payload_digest: Some(callback_payload_digest),
            callback_receipt: Some(receipt),
        },
    )?;
    let terminal = StoredNativeCallbackRun {
        checkpoint: stored.checkpoint,
        result: resumed.clone(),
        phase: NativeCallbackCoordinatorPhase::TerminalEvidencePending,
        callback_idempotency_key: Some(callback_idempotency_key.to_owned()),
        callback_payload_digest: Some(callback_payload_digest.to_owned()),
        callback_receipt: Some(receipt.clone()),
    };
    reconcile_native_callback_evidence(evidence_context, &terminal)?;
    mark_native_callback_phase(
        request.checkpoint_store_path,
        request.run_id,
        NativeCallbackCoordinatorPhase::TerminalEvidencePending,
        NativeCallbackCoordinatorPhase::Terminal,
    )?;
    Ok(resumed)
}

struct NativeCallbackResumeLock {
    _file: File,
}

fn acquire_native_callback_resume_lock(
    checkpoint_store_path: &str,
    run_id: &str,
) -> Result<NativeCallbackResumeLock, StdlibRuntimeError> {
    let mut lock_path = PathBuf::from(checkpoint_store_path);
    let file_name = lock_path
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| {
            StdlibRuntimeError::invalid("checkpointStorePath cannot identify a callback lock")
        })?;
    let run_digest = canonical_hash(&Value::String(run_id.to_owned()));
    lock_path.set_file_name(format!(
        "{file_name}.{}.callback-resume.lock",
        run_digest.trim_start_matches("sha256:")
    ));
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to open native callback resume lock: {error}"
            ))
        })?;
    file.lock().map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to acquire native callback resume lock: {error}"
        ))
    })?;
    Ok(NativeCallbackResumeLock { _file: file })
}

fn native_callback_connection(path: &str) -> Result<Connection, StdlibRuntimeError> {
    let connection = Connection::open(path).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to open native callback checkpoint store: {error}"
        ))
    })?;
    connection
        .execute_batch(
            "
            PRAGMA busy_timeout = 5000;
            CREATE TABLE IF NOT EXISTS native_callback_checkpoints (
                run_id TEXT PRIMARY KEY NOT NULL,
                checkpoint_json TEXT NOT NULL,
                state_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL,
                callback_idempotency_key TEXT,
                callback_payload_digest TEXT,
                callback_receipt_json TEXT,
                terminal_journal_position INTEGER,
                terminal_journal_digest TEXT,
                rejected_idempotency_key TEXT,
                rejected_payload_digest TEXT
            );
            ",
        )
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to initialize native callback checkpoint store: {error}"
            ))
        })?;
    let columns = connection
        .prepare("PRAGMA table_info(native_callback_checkpoints)")
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to inspect native callback checkpoint store: {error}"
            ))
        })?
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to inspect native callback checkpoint store: {error}"
            ))
        })?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to inspect native callback checkpoint store: {error}"
            ))
        })?;
    for (name, data_type) in [
        ("callback_receipt_json", "TEXT"),
        ("terminal_journal_position", "INTEGER"),
        ("terminal_journal_digest", "TEXT"),
    ] {
        if !columns.iter().any(|column| column == name) {
            connection
                .execute(
                    &format!(
                        "ALTER TABLE native_callback_checkpoints ADD COLUMN {name} {data_type}"
                    ),
                    [],
                )
                .map_err(|error| {
                    StdlibRuntimeError::runtime(format!(
                        "failed to migrate native callback checkpoint store: {error}"
                    ))
                })?;
        }
    }
    Ok(connection)
}

fn load_native_callback_run(
    path: &str,
    run_id: &str,
) -> Result<Option<StoredNativeCallbackRun>, StdlibRuntimeError> {
    let connection = native_callback_connection(path)?;
    let row = connection
        .query_row(
            "
            SELECT checkpoint_json,
                   state_digest,
                   status,
                   result_json,
                   callback_idempotency_key,
                   callback_payload_digest,
                   callback_receipt_json,
                   terminal_journal_position,
                   terminal_journal_digest
              FROM native_callback_checkpoints
             WHERE run_id = ?1
            ",
            [run_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, Option<String>>(5)?,
                    row.get::<_, Option<String>>(6)?,
                    row.get::<_, Option<i64>>(7)?,
                    row.get::<_, Option<String>>(8)?,
                ))
            },
        )
        .optional()
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to load native callback checkpoint: {error}"
            ))
        })?;
    row.map(
        |(
            checkpoint_json,
            stored_state_digest,
            status,
            result_json,
            callback_idempotency_key,
            callback_payload_digest,
            callback_receipt_json,
            terminal_journal_position,
            terminal_journal_digest,
        )| {
            let phase = NativeCallbackCoordinatorPhase::parse(&status).ok_or_else(|| {
                StdlibRuntimeError::runtime("stored native callback coordinator phase is invalid")
            })?;
            let checkpoint = parse_json_argument(&checkpoint_json, "stored native checkpoint")?;
            if checkpoint.get("state_digest").and_then(Value::as_str)
                != Some(stored_state_digest.as_str())
                || native_checkpoint_state_digest(&checkpoint)? != stored_state_digest
            {
                return Err(StdlibRuntimeError::runtime(
                    "stored native callback checkpoint digest mismatch",
                ));
            }
            let result_value = parse_json_argument(&result_json, "stored native callback result")?;
            let result = stdlib_run_result_from_value(&result_value)?;
            let callback_receipt = callback_receipt_json
                .as_deref()
                .map(|value| parse_json_argument(value, "stored native callback receipt"))
                .transpose()?;
            let terminal_journal_position = terminal_journal_position
                .map(|position| {
                    usize::try_from(position).map_err(|_| {
                        StdlibRuntimeError::runtime(
                            "stored native callback terminal journal position is invalid",
                        )
                    })
                })
                .transpose()?;
            let valid_state = if phase.has_waiting_result() {
                let callback_state_valid =
                    if phase == NativeCallbackCoordinatorPhase::CallbackAccepted {
                        callback_idempotency_key.is_some()
                            && callback_payload_digest.is_some()
                            && callback_receipt.is_some()
                    } else {
                        callback_idempotency_key.is_none()
                            && callback_payload_digest.is_none()
                            && callback_receipt.is_none()
                    };
                result.status == StdlibRunStatus::WaitingCallback
                    && result.checkpoint.as_ref() == Some(&checkpoint)
                    && callback_state_valid
                    && terminal_journal_position.is_none()
                    && terminal_journal_digest.is_none()
            } else {
                result.status != StdlibRunStatus::WaitingCallback
                    && result.checkpoint.is_none()
                    && callback_idempotency_key.is_some()
                    && callback_payload_digest.is_some()
                    && callback_receipt.is_some()
                    && terminal_journal_position == Some(result.journal.len())
                    && terminal_journal_digest.as_deref()
                        == Some(canonical_hash(&Value::Array(result.journal.clone())).as_str())
            };
            if !valid_state
                || result.run_id != run_id
                || checkpoint.get("graph_hash").and_then(Value::as_str)
                    != Some(result.graph_hash.as_str())
            {
                return Err(StdlibRuntimeError::runtime(
                    "stored native callback checkpoint and result state disagree",
                ));
            }
            validate_native_checkpoint_journal_binding(&checkpoint, &result.journal)?;
            Ok(StoredNativeCallbackRun {
                checkpoint,
                result,
                phase,
                callback_idempotency_key,
                callback_payload_digest,
                callback_receipt,
            })
        },
    )
    .transpose()
}

struct NativeCallbackPersistence<'a> {
    expected_phase: Option<NativeCallbackCoordinatorPhase>,
    phase: NativeCallbackCoordinatorPhase,
    callback_idempotency_key: Option<&'a str>,
    callback_payload_digest: Option<&'a str>,
    callback_receipt: Option<&'a Value>,
}

fn persist_native_callback_run(
    path: &str,
    checkpoint: &Value,
    result: &StdlibRunResult,
    persistence: NativeCallbackPersistence<'_>,
) -> Result<(), StdlibRuntimeError> {
    let NativeCallbackPersistence {
        expected_phase,
        phase,
        callback_idempotency_key,
        callback_payload_digest,
        callback_receipt,
    } = persistence;
    let connection = native_callback_connection(path)?;
    let checkpoint_json =
        serde_json::to_string(checkpoint).map_err(StdlibRuntimeError::serialization)?;
    let result_json = serde_json::to_string(&result.canonical_value())
        .map_err(StdlibRuntimeError::serialization)?;
    let state_digest = checkpoint
        .get("state_digest")
        .and_then(Value::as_str)
        .ok_or_else(|| StdlibRuntimeError::runtime("native checkpoint state digest is missing"))?;
    let callback_receipt_json = callback_receipt
        .map(serde_json::to_string)
        .transpose()
        .map_err(StdlibRuntimeError::serialization)?;
    let (terminal_journal_position, terminal_journal_digest) = if phase.is_terminal() {
        (
            Some(i64::try_from(result.journal.len()).map_err(|_| {
                StdlibRuntimeError::runtime("native callback journal position exceeds i64")
            })?),
            Some(canonical_hash(&Value::Array(result.journal.clone()))),
        )
    } else {
        (None, None)
    };
    let changed = if let Some(expected_phase) = expected_phase {
        connection.execute(
            "
            UPDATE native_callback_checkpoints
               SET checkpoint_json = ?2,
                   state_digest = ?3,
                   status = ?4,
                   result_json = ?5,
                   callback_idempotency_key = ?6,
                   callback_payload_digest = ?7,
                   callback_receipt_json = ?8,
                   terminal_journal_position = ?9,
                   terminal_journal_digest = ?10
             WHERE run_id = ?1 AND status = ?11
            ",
            params![
                result.run_id,
                checkpoint_json,
                state_digest,
                phase.as_str(),
                result_json,
                callback_idempotency_key,
                callback_payload_digest,
                callback_receipt_json,
                terminal_journal_position,
                terminal_journal_digest,
                expected_phase.as_str(),
            ],
        )
    } else {
        connection.execute(
            "
            INSERT INTO native_callback_checkpoints (
                run_id,
                checkpoint_json,
                state_digest,
                status,
                result_json,
                callback_idempotency_key,
                callback_payload_digest,
                callback_receipt_json,
                terminal_journal_position,
                terminal_journal_digest
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
            ",
            params![
                result.run_id,
                checkpoint_json,
                state_digest,
                phase.as_str(),
                result_json,
                callback_idempotency_key,
                callback_payload_digest,
                callback_receipt_json,
                terminal_journal_position,
                terminal_journal_digest,
            ],
        )
    }
    .map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to persist native callback checkpoint: {error}"
        ))
    })?;
    if changed != 1 {
        return Err(StdlibRuntimeError::runtime(
            "native callback coordinator phase changed concurrently",
        ));
    }
    Ok(())
}

fn mark_native_callback_phase(
    path: &str,
    run_id: &str,
    expected_phase: NativeCallbackCoordinatorPhase,
    phase: NativeCallbackCoordinatorPhase,
) -> Result<(), StdlibRuntimeError> {
    let connection = native_callback_connection(path)?;
    let changed = connection
        .execute(
            "UPDATE native_callback_checkpoints
                SET status = ?2
              WHERE run_id = ?1 AND status = ?3",
            params![run_id, phase.as_str(), expected_phase.as_str()],
        )
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to advance native callback coordinator phase: {error}"
            ))
        })?;
    if changed != 1 {
        return Err(StdlibRuntimeError::runtime(
            "native callback coordinator phase changed concurrently",
        ));
    }
    Ok(())
}

fn reconcile_native_callback_evidence(
    context: NativeCallbackEvidenceContext<'_>,
    stored: &StoredNativeCallbackRun,
) -> Result<(), StdlibRuntimeError> {
    reconcile_native_callback_operation(
        context.async_operation_store_path,
        context.callback_admission_hmac_key,
        stored,
    )?;
    reconcile_native_callback_run(context, stored)?;
    if let Some(path) = context.journal_store_path {
        reconcile_native_callback_journal(path, &stored.result)?;
    }
    Ok(())
}

fn reconcile_native_callback_operation(
    path: &str,
    callback_admission_hmac_key: Option<&str>,
    stored: &StoredNativeCallbackRun,
) -> Result<(), StdlibRuntimeError> {
    let operation_id = stored
        .checkpoint
        .pointer("/operation/operation_id")
        .and_then(Value::as_str)
        .expect("validated callback checkpoint has an operation id");
    let store = SqliteAsyncOperationStore::open(path).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to open native async operation store: {error:?}"
        ))
    })?;
    let mut state = store.try_operation_state(operation_id).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to load native callback operation: {error:?}"
        ))
    })?;
    if state.is_none() {
        register_native_waiting_operation(path, &stored.checkpoint["operation"])?;
        state = Some(AsyncOperationState::WaitingCallback);
    }
    validate_persisted_native_operation_identity(path, &stored.checkpoint["operation"])?;
    if stored.phase.is_pre_acceptance() {
        if state != Some(AsyncOperationState::WaitingCallback) {
            return Err(StdlibRuntimeError::runtime(
                "stored native callback operation and coordinator phase disagree",
            ));
        }
        return Ok(());
    }

    if stored.phase == NativeCallbackCoordinatorPhase::CallbackAccepted {
        if !matches!(
            state,
            Some(AsyncOperationState::CallbackReceived | AsyncOperationState::Resuming)
        ) {
            return Err(StdlibRuntimeError::runtime(
                "accepted native callback operation and coordinator phase disagree",
            ));
        }
        let receipt = stored.callback_receipt.as_ref().ok_or_else(|| {
            StdlibRuntimeError::runtime(
                "accepted native callback coordinator is missing its receipt",
            )
        })?;
        let admission =
            validate_native_callback_receipt_shape(receipt, callback_admission_hmac_key)?;
        validate_persisted_native_callback_acceptance(&store, receipt, &admission)?;
        return Ok(());
    }

    match state {
        Some(AsyncOperationState::WaitingCallback) => {
            let receipt = stored.callback_receipt.as_ref().ok_or_else(|| {
                StdlibRuntimeError::runtime(
                    "terminal native callback coordinator is missing its receipt",
                )
            })?;
            let admission =
                validate_native_callback_receipt_shape(receipt, callback_admission_hmac_key)?;
            if !admission.authorized {
                return Err(StdlibRuntimeError::runtime(
                    "terminal native callback coordinator has a denied receipt",
                ));
            }
            accept_native_callback(path, receipt, &admission)?;
        }
        Some(AsyncOperationState::CallbackReceived | AsyncOperationState::Resuming) => {
            let receipt = stored.callback_receipt.as_ref().ok_or_else(|| {
                StdlibRuntimeError::runtime(
                    "terminal native callback coordinator is missing its receipt",
                )
            })?;
            let admission =
                validate_native_callback_receipt_shape(receipt, callback_admission_hmac_key)?;
            if !admission.authorized {
                return Err(StdlibRuntimeError::runtime(
                    "terminal native callback coordinator has a denied receipt",
                ));
            }
            validate_persisted_native_callback_acceptance(&store, receipt, &admission)?;
        }
        _ => {
            return Err(StdlibRuntimeError::runtime(
                "stored native callback operation and coordinator phase disagree",
            ));
        }
    }
    Ok(())
}

fn validate_persisted_native_operation_identity(
    path: &str,
    expected: &Value,
) -> Result<(), StdlibRuntimeError> {
    let operation_id = expected
        .get("operation_id")
        .and_then(Value::as_str)
        .expect("validated callback operation has an id");
    let connection = Connection::open(path).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to open native async operation store for identity validation: {error}"
        ))
    })?;
    let operation_json = connection
        .query_row(
            "SELECT operation_json FROM async_operations WHERE operation_id = ?1",
            [operation_id],
            |row| row.get::<_, String>(0),
        )
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to load native callback operation identity: {error}"
            ))
        })?;
    let persisted = parse_json_argument(&operation_json, "stored native callback operation")?;
    for field in [
        "operation_id",
        "run_id",
        "node_id",
        "attempt_id",
        "kind",
        "provider_operation_id",
        "resume_token_hash",
        "idempotency_key",
        "expected_schema",
        "created_at_unix_ms",
        "submitted_at_unix_ms",
        "expires_at_unix_ms",
        "infinite_wait_policy",
        "completed_at_unix_ms",
    ] {
        if persisted.get(field) != expected.get(field) {
            return Err(StdlibRuntimeError::runtime(
                "stored native callback operation identity does not match checkpoint",
            ));
        }
    }
    Ok(())
}

fn reconcile_native_callback_run(
    context: NativeCallbackEvidenceContext<'_>,
    stored: &StoredNativeCallbackRun,
) -> Result<(), StdlibRuntimeError> {
    let Some(path) = context.run_store_path else {
        return Ok(());
    };
    let mut store = SqliteRunStore::open(path).map_err(|error| {
        StdlibRuntimeError::runtime(format!("failed to open SQLite run store: {error:?}"))
    })?;
    let expected_provenance = context.deployment_provenance.cloned().unwrap_or_default();
    let run = match store.get_run(&stored.result.run_id) {
        Ok(run) => run,
        Err(RunStoreError::NotFound { .. }) => store
            .create_run_with_run_id_and_provenance(
                &stored.result.run_id,
                &stored.result.graph_hash,
                context.inputs.clone(),
                expected_provenance.clone(),
            )
            .map_err(|error| {
                StdlibRuntimeError::runtime(format!(
                    "failed to reconstruct native callback run: {error:?}"
                ))
            })?,
        Err(error) => {
            return Err(StdlibRuntimeError::runtime(format!(
                "failed to load native callback run: {error:?}"
            )));
        }
    };
    if run.graph_hash != stored.result.graph_hash
        || run.inputs != *context.inputs
        || run.deployment_provenance != expected_provenance
    {
        return Err(StdlibRuntimeError::runtime(
            "stored native callback run identity does not match coordinator",
        ));
    }

    let target = if stored.phase.has_waiting_result() {
        RunStatus::WaitingCallback
    } else {
        match stored.result.status {
            StdlibRunStatus::Succeeded => RunStatus::Completed,
            StdlibRunStatus::Failed => RunStatus::Failed,
            StdlibRunStatus::Cancelled => RunStatus::Cancelled,
            StdlibRunStatus::WaitingCallback => {
                return Err(StdlibRuntimeError::runtime(
                    "terminal native callback coordinator has a waiting result",
                ));
            }
        }
    };
    advance_native_callback_run_status(&mut store, &run.run_id, run.status, target)
}

fn advance_native_callback_run_status(
    store: &mut SqliteRunStore,
    run_id: &str,
    current: RunStatus,
    target: RunStatus,
) -> Result<(), StdlibRuntimeError> {
    if current == target {
        return Ok(());
    }
    if current.is_terminal() {
        return Err(StdlibRuntimeError::runtime(
            "stored native callback run terminal status disagrees with coordinator",
        ));
    }
    let statuses: &[RunStatus] = if target == RunStatus::WaitingCallback {
        match current {
            RunStatus::Created => &[RunStatus::Running, RunStatus::WaitingCallback],
            RunStatus::Running => &[RunStatus::WaitingCallback],
            _ => {
                return Err(StdlibRuntimeError::runtime(
                    "stored native callback run status disagrees with waiting coordinator",
                ));
            }
        }
    } else {
        match current {
            RunStatus::Created => &[
                RunStatus::Running,
                RunStatus::WaitingCallback,
                RunStatus::Resuming,
                target,
            ],
            RunStatus::Running => &[RunStatus::WaitingCallback, RunStatus::Resuming, target],
            RunStatus::WaitingCallback => &[RunStatus::Resuming, target],
            RunStatus::Resuming => &[target],
            _ => {
                return Err(StdlibRuntimeError::runtime(
                    "stored native callback run status disagrees with terminal coordinator",
                ));
            }
        }
    };
    for status in statuses {
        store.set_status(run_id, *status).map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to reconcile native callback run status: {error:?}"
            ))
        })?;
    }
    Ok(())
}

fn reconcile_native_callback_journal(
    path: &str,
    result: &StdlibRunResult,
) -> Result<(), StdlibRuntimeError> {
    let journal = SqliteExecutionJournal::open(path, &result.run_id).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to open SQLite execution journal: {error:?}"
        ))
    })?;
    let persisted = journal.records().map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to load native callback journal prefix: {error:?}"
        ))
    })?;
    if persisted.len() > result.journal.len()
        || persisted
            .iter()
            .zip(&result.journal)
            .any(|(actual, expected)| native_journal_record_value(actual) != *expected)
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback journal prefix does not match coordinator",
        ));
    }
    append_native_journal_records(path, &result.run_id, &result.journal[persisted.len()..])
}

fn native_journal_record_value(record: &JournalRecord) -> Value {
    json!({
        "recordId": record.record_id,
        "runId": record.run_id,
        "runSequence": record.run_sequence,
        "kind": record.kind,
        "causationId": record.causation_id,
        "nodeId": record.node_id,
        "attemptId": record.attempt_id,
        "leaseEpoch": record.lease_epoch,
        "payload": record.payload,
        "terminal": record.terminal,
    })
}

fn build_native_callback_checkpoint(request: NativeCheckpointBuildRequest<'_>) -> Value {
    let completed_nodes = request
        .node_outputs
        .keys()
        .cloned()
        .collect::<BTreeSet<_>>();
    let remaining_nodes = request
        .node_names
        .iter()
        .filter(|node| !completed_nodes.contains(*node))
        .cloned()
        .collect::<Vec<_>>();
    let journal_binding = json!({
        "prefix_position": request.journal_prefix.len(),
        "prefix_digest": canonical_hash(&Value::Array(request.journal_prefix.to_vec())),
        "waiting_position": request.journal_prefix.len() + 1,
        "terminal_position": Value::Null,
    });
    let checkpoint_seed = json!({
        "run_id": request.run_id,
        "graph_hash": request.graph_hash,
        "wait_node": request.suspension.wait_node,
        "remaining_nodes": remaining_nodes,
        "inputs": request.inputs,
        "node_outputs": request.node_outputs,
        "output_values": request.output_values,
        "operation": request.suspension.operation,
        "journal_binding": journal_binding,
        "deployment_provenance": request.deployment_provenance.map(RunDeploymentProvenance::canonical_value),
    });
    let checkpoint_id = format!("checkpoint-{}", canonical_hash(&checkpoint_seed));
    let mut checkpoint = json!({
        "checkpoint_id": checkpoint_id,
        "run_id": request.run_id,
        "graph_hash": request.graph_hash,
        "wait_node": request.suspension.wait_node,
        "remaining_nodes": remaining_nodes,
        "inputs": request.inputs,
        "node_outputs": request.node_outputs,
        "output_values": request.output_values,
        "operation": request.suspension.operation,
        "journal_binding": journal_binding,
        "deployment_provenance": request.deployment_provenance.map(RunDeploymentProvenance::canonical_value),
    });
    let state_digest = canonical_hash(&checkpoint);
    checkpoint["state_digest"] = json!(state_digest);
    checkpoint
}

fn native_checkpoint_state_digest(checkpoint: &Value) -> Result<String, StdlibRuntimeError> {
    let Some(object) = checkpoint.as_object() else {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint must be an object",
        ));
    };
    let mut content = object.clone();
    content.remove("state_digest");
    Ok(canonical_hash(&Value::Object(content)))
}

fn native_checkpoint_id(checkpoint: &Value) -> Result<String, StdlibRuntimeError> {
    let Some(object) = checkpoint.as_object() else {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint must be an object",
        ));
    };
    let mut content = object.clone();
    content.remove("checkpoint_id");
    content.remove("state_digest");
    Ok(format!(
        "checkpoint-{}",
        canonical_hash(&Value::Object(content))
    ))
}

fn validate_native_checkpoint_identity(
    checkpoint: &Value,
    run_id: &str,
    graph_hash: &str,
    inputs: &Value,
    deployment_provenance: Option<&RunDeploymentProvenance>,
) -> Result<(), StdlibRuntimeError> {
    let expected_provenance = deployment_provenance
        .map(RunDeploymentProvenance::canonical_value)
        .unwrap_or(Value::Null);
    if checkpoint.get("run_id").and_then(Value::as_str) != Some(run_id)
        || checkpoint.get("graph_hash").and_then(Value::as_str) != Some(graph_hash)
        || checkpoint.get("inputs") != Some(inputs)
        || checkpoint.get("deployment_provenance") != Some(&expected_provenance)
        || checkpoint.get("state_digest").and_then(Value::as_str)
            != Some(native_checkpoint_state_digest(checkpoint)?.as_str())
    {
        return Err(StdlibRuntimeError::invalid(
            "native callback checkpoint does not match graph and inputs",
        ));
    }
    for field in ["checkpoint_id", "wait_node"] {
        if !checkpoint
            .get(field)
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty() && value == value.trim())
        {
            return Err(StdlibRuntimeError::runtime(format!(
                "native callback checkpoint field {field} is invalid"
            )));
        }
    }
    if checkpoint.get("checkpoint_id").and_then(Value::as_str)
        != Some(native_checkpoint_id(checkpoint)?.as_str())
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint id does not match its canonical state",
        ));
    }
    if !checkpoint.get("node_outputs").is_some_and(Value::is_object)
        || !checkpoint
            .get("output_values")
            .is_some_and(Value::is_object)
        || !checkpoint.get("operation").is_some_and(Value::is_object)
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint state is invalid",
        ));
    }
    let binding = checkpoint
        .get("journal_binding")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            StdlibRuntimeError::runtime("native callback checkpoint journal binding is invalid")
        })?;
    let prefix_position = binding
        .get("prefix_position")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| {
            StdlibRuntimeError::runtime("native callback checkpoint journal prefix is invalid")
        })?;
    let waiting_position = binding
        .get("waiting_position")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| {
            StdlibRuntimeError::runtime("native callback checkpoint journal position is invalid")
        })?;
    if waiting_position != prefix_position + 1
        || binding
            .get("prefix_digest")
            .and_then(Value::as_str)
            .is_none_or(|digest| digest.trim().is_empty())
        || binding.get("terminal_position") != Some(&Value::Null)
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint journal binding is invalid",
        ));
    }
    let wait_node = checkpoint["wait_node"]
        .as_str()
        .expect("checkpoint wait_node was validated");
    let operation = checkpoint["operation"]
        .as_object()
        .expect("checkpoint operation was validated");
    if operation.get("run_id").and_then(Value::as_str) != Some(run_id)
        || operation.get("node_id").and_then(Value::as_str) != Some(wait_node)
        || operation.get("state").and_then(Value::as_str) != Some("waiting_callback")
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint operation identity is invalid",
        ));
    }
    let Some(remaining_nodes) = checkpoint.get("remaining_nodes").and_then(Value::as_array) else {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint remaining_nodes is invalid",
        ));
    };
    let mut unique_remaining_nodes = BTreeSet::new();
    if remaining_nodes.iter().any(|node| {
        node.as_str()
            .filter(|node| !node.is_empty() && *node == node.trim())
            .is_none_or(|node| !unique_remaining_nodes.insert(node))
    }) || !unique_remaining_nodes.contains(wait_node)
        || checkpoint["node_outputs"].get(wait_node).is_some()
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint node partition is invalid",
        ));
    }
    Ok(())
}

fn validate_native_checkpoint_journal_binding(
    checkpoint: &Value,
    journal: &[Value],
) -> Result<(), StdlibRuntimeError> {
    let binding = checkpoint
        .get("journal_binding")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            StdlibRuntimeError::runtime("native callback checkpoint journal binding is invalid")
        })?;
    let prefix_position = binding
        .get("prefix_position")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| {
            StdlibRuntimeError::runtime("native callback checkpoint journal prefix is invalid")
        })?;
    let waiting_position = binding
        .get("waiting_position")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| {
            StdlibRuntimeError::runtime("native callback checkpoint journal position is invalid")
        })?;
    if waiting_position != prefix_position + 1 || journal.len() < waiting_position {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint journal prefix position mismatch",
        ));
    }
    let expected_prefix_digest = canonical_hash(&Value::Array(journal[..prefix_position].to_vec()));
    if binding.get("prefix_digest").and_then(Value::as_str) != Some(expected_prefix_digest.as_str())
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint journal prefix digest mismatch",
        ));
    }
    let waiting_record = &journal[waiting_position - 1];
    if waiting_record.get("kind").and_then(Value::as_str) != Some("run_waiting_callback")
        || waiting_record.get("terminal") != Some(&Value::Bool(false))
        || waiting_record.pointer("/payload/checkpointId") != checkpoint.get("checkpoint_id")
        || waiting_record.pointer("/payload/stateDigest") != checkpoint.get("state_digest")
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint journal wait boundary mismatch",
        ));
    }
    if journal[..waiting_position]
        .iter()
        .any(|record| record.get("terminal") == Some(&Value::Bool(true)))
    {
        return Err(StdlibRuntimeError::runtime(
            "native callback checkpoint journal prefix is terminal",
        ));
    }
    Ok(())
}

fn stdlib_run_result_from_value(value: &Value) -> Result<StdlibRunResult, StdlibRuntimeError> {
    let status = match value.get("status").and_then(Value::as_str) {
        Some("succeeded") => StdlibRunStatus::Succeeded,
        Some("failed") => StdlibRunStatus::Failed,
        Some("cancelled") => StdlibRunStatus::Cancelled,
        Some("waiting_callback") => StdlibRunStatus::WaitingCallback,
        _ => {
            return Err(StdlibRuntimeError::runtime(
                "stored native result status is invalid",
            ));
        }
    };
    let required_string = |field: &'static str| {
        value
            .get(field)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .map(str::to_owned)
            .ok_or_else(|| {
                StdlibRuntimeError::runtime(format!(
                    "stored native result field {field} is invalid"
                ))
            })
    };
    let journal = value
        .get("journal")
        .and_then(Value::as_array)
        .cloned()
        .ok_or_else(|| StdlibRuntimeError::runtime("stored native result journal is invalid"))?;
    let deployment_provenance = value
        .get("deploymentProvenance")
        .filter(|provenance| !provenance.is_null())
        .map(RunDeploymentProvenance::from_production_value)
        .transpose()
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "stored native result deployment provenance is invalid: {error}"
            ))
        })?;
    Ok(StdlibRunResult {
        run_id: required_string("runId")?,
        graph_hash: required_string("graphHash")?,
        status,
        outputs: value.get("outputs").cloned().unwrap_or_else(|| json!({})),
        journal,
        checkpoint: value
            .get("checkpoint")
            .filter(|item| !item.is_null())
            .cloned(),
        deployment_provenance,
    })
}

fn value_object_to_btree(
    value: &Value,
    owner: &str,
) -> Result<BTreeMap<String, Value>, StdlibRuntimeError> {
    value
        .as_object()
        .map(|object| {
            object
                .iter()
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect()
        })
        .ok_or_else(|| StdlibRuntimeError::runtime(format!("{owner} must be an object")))
}

fn validate_native_callback_receipt_shape(
    receipt: &Value,
    callback_admission_hmac_key: Option<&str>,
) -> Result<TrustedNativeCallbackResumeAdmission, StdlibRuntimeError> {
    let Some(receipt) = receipt.as_object() else {
        return Err(native_callback_rejected());
    };
    for field in [
        "operation_id",
        "run_id",
        "node_id",
        "attempt_id",
        "provider_operation_id",
        "operation_idempotency_key",
        "callback_idempotency_key",
        "resume_token_hash",
        "schema_id",
        "payload_digest",
        "verified_by",
    ] {
        if !receipt
            .get(field)
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty() && value == value.trim())
        {
            return Err(StdlibRuntimeError::invalid(
                "native async callback rejected",
            ));
        }
    }
    if receipt.get("verified_by").and_then(Value::as_str) == Some("unauthenticated") {
        return Err(native_callback_rejected());
    }
    if receipt.get("schema_validated") != Some(&Value::Bool(true)) {
        return Err(native_callback_rejected());
    }
    let payload = receipt
        .get("payload")
        .filter(|value| value.is_object())
        .ok_or_else(|| StdlibRuntimeError::invalid("native async callback rejected"))?;
    if receipt.get("payload_digest").and_then(Value::as_str)
        != Some(canonical_hash(payload).as_str())
    {
        return Err(StdlibRuntimeError::invalid(
            "native async callback rejected",
        ));
    }
    if receipt
        .get("received_at_unix_ms")
        .and_then(Value::as_u64)
        .is_none_or(|value| value == 0)
    {
        return Err(StdlibRuntimeError::invalid(
            "native async callback rejected",
        ));
    }
    let admission = receipt
        .get("resume_admission")
        .and_then(Value::as_object)
        .ok_or_else(trusted_native_callback_admission_rejected)?;
    if admission.get("contract").and_then(Value::as_str)
        != Some("graphblocks.trusted-callback-resume-admission.v1")
    {
        return Err(trusted_native_callback_admission_rejected());
    }
    verify_trusted_native_callback_admission(admission, callback_admission_hmac_key)?;
    let outcome = trusted_admission_string(admission, "outcome")?;
    let authorized = match outcome.as_str() {
        "authorized" => true,
        "denied" => false,
        _ => return Err(trusted_native_callback_admission_rejected()),
    };
    let ownership = admission
        .get("ownership")
        .and_then(Value::as_object)
        .ok_or_else(trusted_native_callback_admission_rejected)?;
    let schema_verification = admission
        .get("schema_verification")
        .and_then(Value::as_object)
        .ok_or_else(trusted_native_callback_admission_rejected)?;
    let fencing_epoch = ownership
        .get("fencing_epoch")
        .and_then(Value::as_u64)
        .filter(|value| *value > 0)
        .ok_or_else(trusted_native_callback_admission_rejected)?;
    Ok(TrustedNativeCallbackResumeAdmission {
        authorized,
        authentication_decision_id: trusted_admission_string(
            admission,
            "authentication_decision_id",
        )?,
        policy_decision_id: trusted_admission_string(admission, "policy_decision_id")?,
        budget_reservation_id: trusted_admission_string(admission, "budget_reservation_id")?,
        compatible_release_digest: trusted_admission_string(
            admission,
            "compatible_release_digest",
        )?,
        run_id: trusted_admission_string(admission, "run_id")?,
        operation_id: trusted_admission_string(admission, "operation_id")?,
        node_id: trusted_admission_string(admission, "node_id")?,
        attempt_id: trusted_admission_string(admission, "attempt_id")?,
        checkpoint_id: trusted_admission_string(admission, "checkpoint_id")?,
        checkpoint_state_digest: trusted_admission_string(admission, "checkpoint_state_digest")?,
        owner_id: trusted_admission_string(ownership, "owner_id")?,
        lease_id: trusted_admission_string(ownership, "lease_id")?,
        fencing_epoch,
        fence_token: trusted_admission_string(ownership, "fence_token")?,
        schema_verification_id: trusted_admission_string(schema_verification, "verification_id")?,
        schema_id: trusted_admission_string(schema_verification, "schema_id")?,
        payload_digest: trusted_admission_string(schema_verification, "payload_digest")?,
        schema_verified_by: trusted_admission_string(schema_verification, "verified_by")?,
    })
}

fn verify_trusted_native_callback_admission(
    admission: &serde_json::Map<String, Value>,
    callback_admission_hmac_key: Option<&str>,
) -> Result<(), StdlibRuntimeError> {
    let key = callback_admission_hmac_key
        .filter(|key| key.len() >= MIN_CALLBACK_ADMISSION_HMAC_KEY_BYTES)
        .ok_or_else(trusted_native_callback_admission_rejected)?;
    let signature = admission
        .get("signature")
        .and_then(Value::as_str)
        .and_then(|signature| signature.strip_prefix("hmac-sha256:"))
        .filter(|signature| signature.len() == 64 && signature.is_ascii())
        .ok_or_else(trusted_native_callback_admission_rejected)?;
    let mut signature_bytes = [0_u8; 32];
    for (index, output) in signature_bytes.iter_mut().enumerate() {
        let start = index * 2;
        *output = u8::from_str_radix(&signature[start..start + 2], 16)
            .map_err(|_| trusted_native_callback_admission_rejected())?;
    }

    let mut unsigned_admission = admission.clone();
    unsigned_admission.remove("signature");
    let admission_digest = canonical_hash(&Value::Object(unsigned_admission));
    let message = format!("graphblocks.trusted-callback-resume-admission.v1\n{admission_digest}");
    let mut mac = HmacSha256::new_from_slice(key.as_bytes())
        .map_err(|_| trusted_native_callback_admission_rejected())?;
    mac.update(message.as_bytes());
    mac.verify_slice(&signature_bytes)
        .map_err(|_| trusted_native_callback_admission_rejected())
}

fn trusted_admission_string(
    value: &serde_json::Map<String, Value>,
    field: &str,
) -> Result<String, StdlibRuntimeError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty() && *text == text.trim())
        .map(str::to_owned)
        .ok_or_else(trusted_native_callback_admission_rejected)
}

fn trusted_native_callback_admission_rejected() -> StdlibRuntimeError {
    native_callback_rejected()
}

fn native_callback_rejected() -> StdlibRuntimeError {
    StdlibRuntimeError::invalid("native async callback rejected")
}

fn validate_trusted_native_callback_admission(
    admission: &TrustedNativeCallbackResumeAdmission,
    receipt: &Value,
    checkpoint: &Value,
    deployment_provenance: Option<&RunDeploymentProvenance>,
    graph_hash: &str,
) -> Result<(), StdlibRuntimeError> {
    let receipt = receipt
        .as_object()
        .expect("native callback receipt was validated");
    let expected_release_digest = deployment_provenance
        .and_then(|provenance| provenance.release_digest.as_deref())
        .unwrap_or(graph_hash);
    if !admission.authorized
        || admission.run_id != checkpoint["run_id"]
        || admission.operation_id != receipt["operation_id"]
        || admission.node_id != receipt["node_id"]
        || admission.attempt_id != receipt["attempt_id"]
        || admission.checkpoint_id != checkpoint["checkpoint_id"]
        || admission.checkpoint_state_digest != checkpoint["state_digest"]
        || admission.compatible_release_digest != expected_release_digest
        || admission.schema_id != receipt["schema_id"]
        || admission.payload_digest != receipt["payload_digest"]
        || admission.schema_verified_by != receipt["verified_by"]
    {
        return Err(trusted_native_callback_admission_rejected());
    }
    Ok(())
}

fn native_callback_ownership_fence_token(
    admission: &TrustedNativeCallbackResumeAdmission,
) -> String {
    canonical_hash(&json!({
        "authenticationDecisionId": admission.authentication_decision_id,
        "ownerId": admission.owner_id,
        "leaseId": admission.lease_id,
        "fencingEpoch": admission.fencing_epoch,
        "fenceToken": admission.fence_token,
        "schemaVerificationId": admission.schema_verification_id,
    }))
}

fn validate_persisted_native_callback_acceptance(
    store: &SqliteAsyncOperationStore,
    receipt: &Value,
    admission: &TrustedNativeCallbackResumeAdmission,
) -> Result<(), StdlibRuntimeError> {
    let receipt = receipt
        .as_object()
        .expect("native callback receipt was validated");
    let operation_id = receipt["operation_id"]
        .as_str()
        .expect("native callback operation id was validated");
    let events = store
        .try_events_for_operation(operation_id)
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to load persisted native callback acceptance: {error:?}"
            ))
        })?;
    let expected_provider_operation_id = receipt["provider_operation_id"]
        .as_str()
        .expect("native callback provider operation id was validated");
    let expected_ownership_fence_token = native_callback_ownership_fence_token(admission);
    let mut callback_position = None;
    let mut authorization_position = None;
    for (position, event) in events.iter().enumerate() {
        match event {
            AsyncOperationEvent::ExternalCallbackReceived { receipt: persisted }
                if persisted.idempotency_key == receipt["callback_idempotency_key"] =>
            {
                if callback_position.is_some()
                    || persisted.callback_id != receipt["callback_idempotency_key"]
                    || persisted.operation_id != receipt["operation_id"]
                    || persisted.run_id != receipt["run_id"]
                    || persisted.node_id != receipt["node_id"]
                    || persisted.attempt_id != receipt["attempt_id"]
                    || persisted.provider_operation_id.as_deref()
                        != Some(expected_provider_operation_id)
                    || persisted.payload != receipt["payload"]
                    || persisted.payload_digest != receipt["payload_digest"]
                    || persisted.received_at_unix_ms != receipt["received_at_unix_ms"]
                    || persisted.verified_by != receipt["verified_by"]
                    || persisted.policy_snapshot_id != "runtime-callback-resume"
                    || !persisted.artifacts.is_empty()
                {
                    return Err(native_callback_rejected());
                }
                callback_position = Some(position);
            }
            AsyncOperationEvent::CallbackResumeAuthorized {
                operation_id: persisted_operation_id,
                policy_decision_id,
                budget_reservation_id,
                compatible_release_id,
                ownership_fence_token,
                ..
            } if persisted_operation_id == operation_id => {
                if authorization_position.is_some()
                    || policy_decision_id != &admission.policy_decision_id
                    || budget_reservation_id != &admission.budget_reservation_id
                    || compatible_release_id != &admission.compatible_release_digest
                    || ownership_fence_token != &expected_ownership_fence_token
                {
                    return Err(native_callback_rejected());
                }
                authorization_position = Some(position);
            }
            _ => {}
        }
    }
    if !matches!(
        (callback_position, authorization_position),
        (Some(callback), Some(authorization)) if callback < authorization
    ) {
        return Err(native_callback_rejected());
    }
    Ok(())
}

fn validate_native_callback_against_checkpoint(
    receipt: &Value,
    checkpoint: &Value,
) -> Result<(), StdlibRuntimeError> {
    let receipt = receipt
        .as_object()
        .expect("callback receipt shape was validated");
    let operation = checkpoint["operation"]
        .as_object()
        .expect("checkpoint operation was validated");
    for (receipt_field, operation_field) in [
        ("operation_id", "operation_id"),
        ("run_id", "run_id"),
        ("node_id", "node_id"),
        ("attempt_id", "attempt_id"),
        ("provider_operation_id", "provider_operation_id"),
        ("operation_idempotency_key", "idempotency_key"),
        ("resume_token_hash", "resume_token_hash"),
        ("schema_id", "expected_schema"),
    ] {
        if receipt.get(receipt_field) != operation.get(operation_field) {
            return Err(StdlibRuntimeError::invalid(
                "native async callback rejected",
            ));
        }
    }
    let received_at = receipt["received_at_unix_ms"]
        .as_u64()
        .expect("callback timestamp was validated");
    if operation
        .get("submitted_at_unix_ms")
        .and_then(Value::as_u64)
        .is_some_and(|submitted_at| received_at < submitted_at)
        || operation
            .get("expires_at_unix_ms")
            .and_then(Value::as_u64)
            .is_some_and(|expires_at| received_at >= expires_at)
    {
        return Err(StdlibRuntimeError::invalid(
            "native async callback rejected",
        ));
    }
    Ok(())
}

fn register_native_waiting_operation(path: &str, value: &Value) -> Result<(), StdlibRuntimeError> {
    let operation = native_async_operation_from_value(value)?;
    let store = SqliteAsyncOperationStore::open(path).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to open native async operation store: {error:?}"
        ))
    })?;
    store.register(operation).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to persist native waiting operation: {error:?}"
        ))
    })
}

fn native_async_operation_from_value(value: &Value) -> Result<AsyncOperation, StdlibRuntimeError> {
    let Some(value) = value.as_object() else {
        return Err(StdlibRuntimeError::runtime(
            "native callback operation must be an object",
        ));
    };
    let string = |field: &'static str| {
        value
            .get(field)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty() && *text == text.trim())
            .map(str::to_owned)
            .ok_or_else(|| {
                StdlibRuntimeError::runtime(format!(
                    "native callback operation field {field} is invalid"
                ))
            })
    };
    let kind = match string("kind")?.as_str() {
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
            return Err(StdlibRuntimeError::runtime(
                "native callback operation kind is invalid",
            ));
        }
    };
    let created_at = value
        .get("created_at_unix_ms")
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            StdlibRuntimeError::runtime("native callback operation created_at is invalid")
        })?;
    let submitted_at = value
        .get("submitted_at_unix_ms")
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            StdlibRuntimeError::runtime("native callback operation submitted_at is invalid")
        })?;
    let provider_operation_id = string("provider_operation_id")?;
    let mut operation = AsyncOperation::new(
        string("operation_id")?,
        string("run_id")?,
        string("node_id")?,
        string("attempt_id")?,
        kind,
        string("resume_token_hash")?,
        string("idempotency_key")?,
        string("expected_schema")?,
        created_at,
    )
    .submitted(provider_operation_id, submitted_at);
    if let Some(expires_at) = value.get("expires_at_unix_ms").and_then(Value::as_u64) {
        operation = operation.waiting_callback(expires_at);
    } else if let Some(infinite_wait_policy) =
        value.get("infinite_wait_policy").and_then(Value::as_str)
    {
        operation = operation.with_infinite_wait_policy(infinite_wait_policy);
        operation.state = AsyncOperationState::WaitingCallback;
    } else {
        return Err(StdlibRuntimeError::runtime(
            "native callback operation has no wait bound",
        ));
    }
    operation.validate().map_err(|error| {
        StdlibRuntimeError::runtime(format!("native callback operation is invalid: {error:?}"))
    })?;
    Ok(operation)
}

fn accept_native_callback(
    path: &str,
    receipt: &Value,
    admission: &TrustedNativeCallbackResumeAdmission,
) -> Result<(), StdlibRuntimeError> {
    let receipt = receipt
        .as_object()
        .expect("native callback receipt was validated");
    let schema_id = receipt["schema_id"]
        .as_str()
        .expect("native callback schema was validated");
    // The trusted admission signature binds the external schema verifier,
    // verification id, schema id, and payload digest. The local registry still
    // enforces the callback envelope's object contract instead of silently
    // accepting arbitrary JSON under an `Any` schema.
    let registry = ToolSchemaRegistry::new([JsonSchema::new(schema_id, JsonSchemaNode::object())])
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to build callback schema registry: {error:?}"
            ))
        })?;
    let mut submission = AsyncCallbackSubmission::new(
        receipt["callback_idempotency_key"]
            .as_str()
            .expect("callback idempotency key was validated"),
        receipt["operation_id"]
            .as_str()
            .expect("operation id was validated"),
        receipt["run_id"].as_str().expect("run id was validated"),
        receipt["node_id"].as_str().expect("node id was validated"),
        receipt["attempt_id"]
            .as_str()
            .expect("attempt id was validated"),
        receipt["callback_idempotency_key"]
            .as_str()
            .expect("callback idempotency key was validated"),
        receipt["payload"].clone(),
        receipt["received_at_unix_ms"]
            .as_u64()
            .expect("callback timestamp was validated"),
        receipt["verified_by"]
            .as_str()
            .expect("callback verifier was validated"),
        "runtime-callback-resume",
    );
    submission = submission.with_provider_operation_id(
        receipt["provider_operation_id"]
            .as_str()
            .expect("provider operation id was validated"),
    );
    let store = SqliteAsyncOperationStore::open(path).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to open native async operation store: {error:?}"
        ))
    })?;
    let accepted = store
        .accept_callback_with_resume_decision(
            submission,
            &registry,
            AsyncCallbackResumeDecision::ResumeAuthorized {
                authentication_verified: admission.authorized,
                policy_decision_id: admission.policy_decision_id.clone(),
                budget_reservation_id: admission.budget_reservation_id.clone(),
                compatible_release_id: admission.compatible_release_digest.clone(),
                ownership_fence_token: native_callback_ownership_fence_token(admission),
            },
        )
        .map_err(|_| StdlibRuntimeError::invalid("native async callback rejected"))?;
    if !accepted.should_resume && !accepted.duplicate {
        return Err(StdlibRuntimeError::invalid(
            "native async callback rejected",
        ));
    }
    Ok(())
}

fn native_waiting_journal_prefix(
    result: &TestRunResult,
    suspension: &NativeCallbackSuspension,
) -> Vec<Value> {
    let mut records = Vec::new();
    for record in test_journal_values(result) {
        if record.get("nodeId").and_then(Value::as_str) == Some(&suspension.wait_node)
            && record.get("kind").and_then(Value::as_str) == Some("node_completed")
        {
            break;
        }
        if record.get("terminal") == Some(&Value::Bool(true)) {
            break;
        }
        records.push(record);
    }
    records
}

fn waiting_native_journal(
    mut records: Vec<Value>,
    suspension: &NativeCallbackSuspension,
    checkpoint: &Value,
) -> Vec<Value> {
    push_native_journal_record(
        &mut records,
        checkpoint["run_id"]
            .as_str()
            .expect("validated callback checkpoint run_id is a string"),
        "run_waiting_callback",
        None,
        Some(json!({
            "checkpointId": checkpoint["checkpoint_id"],
            "operationId": suspension.operation["operation_id"],
            "waitNode": suspension.wait_node,
            "stateDigest": checkpoint["state_digest"],
        })),
        false,
    );
    records
}

fn resumed_native_journal(
    waiting_records: &[Value],
    result: &TestRunResult,
    replay_node_outputs: &BTreeMap<String, Value>,
    receipt: &Value,
) -> Vec<Value> {
    let mut records = waiting_records.to_vec();
    append_native_resume_journal_boundary(&mut records, &result.run_id, receipt);
    for record in test_journal_values(result) {
        let kind = record.get("kind").and_then(Value::as_str);
        let node_id = record.get("nodeId").and_then(Value::as_str);
        if kind == Some("run_started")
            || node_id.is_some_and(|node_id| replay_node_outputs.contains_key(node_id))
            || (kind == Some("node_started")
                && node_id
                    == waiting_records
                        .iter()
                        .rev()
                        .find(|record| {
                            record.get("kind").and_then(Value::as_str)
                                == Some("run_waiting_callback")
                        })
                        .and_then(|record| record.pointer("/payload/waitNode"))
                        .and_then(Value::as_str))
        {
            continue;
        }
        let metadata = record.get("nodeId").and_then(Value::as_str);
        push_native_journal_record(
            &mut records,
            &result.run_id,
            kind.unwrap_or("runtime_record"),
            metadata,
            record
                .get("payload")
                .filter(|value| !value.is_null())
                .cloned(),
            record.get("terminal") == Some(&Value::Bool(true)),
        );
    }
    records
}

fn append_native_resume_journal_boundary(records: &mut Vec<Value>, run_id: &str, receipt: &Value) {
    let receipt = receipt
        .as_object()
        .expect("native callback receipt was validated");
    push_native_journal_record(
        records,
        run_id,
        "external_callback_received",
        None,
        Some(json!({
            "operationId": receipt["operation_id"],
            "callbackIdempotencyKey": receipt["callback_idempotency_key"],
            "payloadDigest": receipt["payload_digest"],
            "receivedAtUnixMs": receipt["received_at_unix_ms"],
            "verifiedBy": receipt["verified_by"],
        })),
        false,
    );
    push_native_journal_record(
        records,
        run_id,
        "run_resuming",
        None,
        Some(json!({
            "operationId": receipt["operation_id"],
            "reevaluated": ["policy", "budget", "release", "ownership_lease"],
        })),
        false,
    );
}

fn failed_native_callback_resume_result(
    waiting: &StdlibRunResult,
    run_id: &str,
    graph_hash: String,
    receipt: &Value,
    message: String,
    deployment_provenance: Option<&RunDeploymentProvenance>,
) -> StdlibRunResult {
    let mut journal = waiting.journal.clone();
    append_native_resume_journal_boundary(&mut journal, run_id, receipt);
    push_native_journal_record(
        &mut journal,
        run_id,
        "run_failed",
        None,
        Some(json!({"error": message})),
        true,
    );
    StdlibRunResult {
        run_id: run_id.to_owned(),
        graph_hash,
        status: StdlibRunStatus::Failed,
        outputs: json!({}),
        journal,
        checkpoint: None,
        deployment_provenance: deployment_provenance.cloned(),
    }
}

fn push_native_journal_record(
    records: &mut Vec<Value>,
    run_id: &str,
    kind: &str,
    node_id: Option<&str>,
    payload: Option<Value>,
    terminal: bool,
) {
    let sequence = records.len() as u64 + 1;
    records.push(json!({
        "recordId": format!("{run_id}:{sequence}"),
        "runId": run_id,
        "runSequence": sequence,
        "kind": kind,
        "causationId": Value::Null,
        "nodeId": node_id,
        "attemptId": Value::Null,
        "leaseEpoch": Value::Null,
        "payload": payload,
        "terminal": terminal,
    }));
}

fn append_native_journal_records(
    path: &str,
    run_id: &str,
    records: &[Value],
) -> Result<(), StdlibRuntimeError> {
    let mut journal = SqliteExecutionJournal::open(path, run_id).map_err(|error| {
        StdlibRuntimeError::runtime(format!(
            "failed to open SQLite execution journal: {error:?}"
        ))
    })?;
    for record in records {
        let kind = record
            .get("kind")
            .and_then(Value::as_str)
            .ok_or_else(|| StdlibRuntimeError::runtime("native journal kind is invalid"))?;
        let metadata = JournalMetadata {
            causation_id: record
                .get("causationId")
                .and_then(Value::as_str)
                .map(str::to_owned),
            node_id: record
                .get("nodeId")
                .and_then(Value::as_str)
                .map(str::to_owned),
            attempt_id: record
                .get("attemptId")
                .and_then(Value::as_str)
                .map(str::to_owned),
            lease_epoch: record.get("leaseEpoch").and_then(Value::as_u64),
        };
        let payload = record
            .get("payload")
            .filter(|value| !value.is_null())
            .cloned();
        let result = if record.get("terminal") == Some(&Value::Bool(true)) {
            journal.append_terminal_with_metadata(kind, metadata, payload)
        } else {
            journal.append_with_metadata(kind, metadata, payload)
        };
        result.map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to persist native callback journal: {error:?}"
            ))
        })?;
    }
    Ok(())
}

fn parse_json_argument(text: &str, label: &str) -> Result<Value, StdlibRuntimeError> {
    parse_canonical_json(text)
        .map_err(|error| StdlibRuntimeError::invalid(format!("invalid {label} JSON: {error}")))
}

fn build_runtime_bridge_plan(
    graph: &Value,
    block_catalog: &BlockCatalog,
) -> Result<RuntimeBridgePlan, StdlibRuntimeError> {
    let runtime_catalog = stdlib_block_catalog().map_err(|error| {
        StdlibRuntimeError::invalid(format!("invalid stdlib block catalog: {error}"))
    })?;
    if let Some(nodes) = graph
        .get("spec")
        .and_then(|spec| spec.get("nodes"))
        .and_then(Value::as_object)
    {
        for (node_id, node) in nodes {
            let Some(block_id) = node.get("block").and_then(Value::as_str) else {
                continue;
            };
            if runtime_catalog.get(block_id).is_none() {
                return Err(StdlibRuntimeError::invalid(format!(
                    "stdlib runtime node {node_id:?} uses unregistered block {block_id:?}"
                )));
            }
        }
    }
    let plan = compile_graph_with_catalog(graph, block_catalog);
    if !plan.ok() {
        let error_codes = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        return Err(StdlibRuntimeError::invalid(format!(
            "graph did not compile: {error_codes}"
        )));
    }

    let spec = plan
        .normalized
        .get("spec")
        .and_then(Value::as_object)
        .ok_or_else(|| StdlibRuntimeError::invalid("normalized graph spec must be an object"))?;
    let nodes = spec
        .get("nodes")
        .and_then(Value::as_object)
        .ok_or_else(|| StdlibRuntimeError::invalid("normalized graph nodes must be an object"))?;
    let node_specs = nodes
        .iter()
        .map(|(node_id, node)| (node_id.clone(), node.clone()))
        .collect::<BTreeMap<_, _>>();
    let descriptors_by_node = nodes
        .iter()
        .map(|(node_id, node)| {
            let block_id = node
                .get("block")
                .and_then(Value::as_str)
                .expect("compiled stdlib node has a block id");
            let descriptor = runtime_catalog
                .get(block_id)
                .expect("stdlib registration was validated before compilation")
                .clone();
            (node_id.clone(), descriptor)
        })
        .collect::<BTreeMap<_, _>>();
    let edges = spec
        .get("edges")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut dependencies_by_node = nodes
        .keys()
        .map(|node_id| (node_id.clone(), Vec::new()))
        .collect::<BTreeMap<_, _>>();
    let mut conditions_by_node = BTreeMap::<String, ScheduledCondition>::new();
    let mut input_output_projections = Vec::new();
    let mut output_projections_by_node = BTreeMap::<String, Vec<OutputProjection>>::new();
    let mut output_ports_by_node = nodes
        .keys()
        .map(|node_id| (node_id.clone(), BTreeSet::<String>::new()))
        .collect::<BTreeMap<_, _>>();

    for (node_id, node) in nodes {
        let Some(when) = node.get("when") else {
            continue;
        };
        let Some(when) = when.as_str() else {
            return Err(StdlibRuntimeError::invalid(format!(
                "node {node_id:?} when reference must be a string"
            )));
        };
        let Some((source_owner, source_path)) = when.split_once('.') else {
            return Err(StdlibRuntimeError::invalid(format!(
                "node {node_id:?} when reference {when:?} must include a port path"
            )));
        };
        let path_parts = source_path.split('.').collect::<Vec<_>>();
        let source_port = path_parts.first().copied().unwrap_or_default();
        if source_owner.is_empty()
            || source_port.is_empty()
            || path_parts.iter().any(|part| part.is_empty())
        {
            return Err(StdlibRuntimeError::invalid(format!(
                "node {node_id:?} when reference {when:?} must include a non-empty owner and port path"
            )));
        }
        if matches!(source_owner, "$context" | "$state" | "$execution") {
            return Err(StdlibRuntimeError::invalid(format!(
                "native stdlib runtime does not support {source_owner} references"
            )));
        }
        conditions_by_node.insert(
            node_id.clone(),
            ScheduledCondition::new(
                INTERNAL_WHEN_INPUT,
                PortRef::new(source_owner, source_port),
                path_parts.iter().skip(1).copied(),
            ),
        );
    }

    for edge in &edges {
        let Some(edge) = edge.as_object() else {
            continue;
        };
        let (Some(source), Some(target)) = (
            edge.get("from").and_then(Value::as_str),
            edge.get("to").and_then(Value::as_str),
        ) else {
            continue;
        };
        let (source_owner, source_path) = source.split_once('.').unwrap_or((source, ""));
        let (target_owner, target_path) = target.split_once('.').unwrap_or((target, ""));
        if matches!(source_owner, "$context" | "$state" | "$execution")
            || matches!(target_owner, "$context" | "$state" | "$execution")
        {
            return Err(StdlibRuntimeError::invalid(format!(
                "native stdlib runtime does not support pseudo-node edge {source:?} -> {target:?}"
            )));
        }
        if source_owner.starts_with('$') && source_owner != "$input" {
            continue;
        }
        let Some(source_port) = source_path
            .split('.')
            .next()
            .filter(|port| !port.is_empty())
        else {
            return Err(StdlibRuntimeError::invalid(format!(
                "edge source {source:?} must include a port"
            )));
        };
        if let Some(output_ports) = output_ports_by_node.get_mut(source_owner) {
            output_ports.insert(source_port.to_owned());
        }
        if target_owner == "$output" {
            if target_path.is_empty() || target_path.split('.').any(str::is_empty) {
                return Err(StdlibRuntimeError::invalid(format!(
                    "output edge target {target:?} must include an output path"
                )));
            }
            let projection = OutputProjection {
                source: source.to_owned(),
                source_path: source_path.to_owned(),
                target: target.to_owned(),
                target_path: target_path.to_owned(),
            };
            if source_owner == "$input" {
                input_output_projections.push(projection);
            } else {
                output_projections_by_node
                    .entry(source_owner.to_owned())
                    .or_default()
                    .push(projection);
            }
            continue;
        }
        if target_owner.starts_with('$') {
            continue;
        }
        let Some(_target_input) = target_path
            .split('.')
            .next()
            .filter(|port| !port.is_empty())
        else {
            return Err(StdlibRuntimeError::invalid(format!(
                "edge target {target:?} must include an input"
            )));
        };
        let Some(dependencies) = dependencies_by_node.get_mut(target_owner) else {
            return Err(StdlibRuntimeError::invalid(format!(
                "edge target references unknown node {target_owner:?}"
            )));
        };
        dependencies.push(
            InputDependency::value(target_path, PortRef::new(source_owner, source_port))
                .with_source_path(source_path.split('.').skip(1)),
        );
    }

    let mut scheduled_nodes = Vec::with_capacity(dependencies_by_node.len());
    for (node_id, dependencies) in dependencies_by_node {
        let mut scheduled_node = ScheduledNode::new(node_id.clone(), dependencies);
        if let Some(condition) = conditions_by_node.remove(&node_id) {
            scheduled_node = scheduled_node.with_condition(condition);
        }
        scheduled_nodes.push(scheduled_node);
    }
    let output_ports_by_node = output_ports_by_node
        .into_iter()
        .map(|(node_id, ports)| (node_id, ports.into_iter().collect::<Vec<_>>()))
        .collect();

    Ok(RuntimeBridgePlan {
        graph_hash: plan.graph_hash,
        nodes: node_specs,
        descriptors_by_node,
        scheduled_nodes,
        input_output_projections,
        output_projections_by_node,
        output_ports_by_node,
    })
}

fn runtime_with_inputs(
    scheduled_nodes: Vec<ScheduledNode>,
    inputs: &Value,
    run_id: &str,
) -> Result<InProcessTestRuntime, StdlibRuntimeError> {
    let mut runtime = InProcessTestRuntime::new(run_id, scheduled_nodes).map_err(|error| {
        StdlibRuntimeError::invalid(format!("failed to create test runtime: {error:?}"))
    })?;
    if let Some(input_object) = inputs.as_object() {
        for (input_name, value) in input_object {
            runtime = runtime.with_initial_value(PortRef::new("$input", input_name), value.clone());
        }
    }
    Ok(runtime)
}

fn optional_options_string<'a>(
    options: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
) -> Result<Option<&'a str>, StdlibRuntimeError> {
    options
        .get(primary)
        .or_else(|| options.get(alternate))
        .filter(|value| !value.is_null())
        .map(|value| {
            let Some(text) = value.as_str() else {
                return Err(StdlibRuntimeError::invalid(format!(
                    "runtime options field {primary} must be a string"
                )));
            };
            if text.trim().is_empty() {
                return Err(StdlibRuntimeError::invalid(format!(
                    "runtime options field {primary} must not be empty"
                )));
            }
            Ok(text)
        })
        .transpose()
}

fn persist_runtime_evidence(
    evidence: RuntimeEvidencePersistence<'_>,
) -> Result<(), StdlibRuntimeError> {
    if let Some(run_store_path) = evidence.run_store_path {
        let mut store = SqliteRunStore::open(run_store_path).map_err(|error| {
            StdlibRuntimeError::runtime(format!("failed to open SQLite run store: {error:?}"))
        })?;
        store
            .create_run_with_run_id_and_provenance(
                &evidence.result.run_id,
                evidence.graph_hash,
                evidence.inputs.clone(),
                evidence.deployment_provenance.cloned().unwrap_or_default(),
            )
            .map_err(|error| {
                StdlibRuntimeError::runtime(format!(
                    "failed to persist native runtime run record: {error:?}"
                ))
            })?;
        store
            .set_status(&evidence.result.run_id, RunStatus::Running)
            .map_err(|error| {
                StdlibRuntimeError::runtime(format!(
                    "failed to persist native runtime run status: {error:?}"
                ))
            })?;
        let status = match evidence.result.status {
            TestRunStatus::Succeeded => RunStatus::Completed,
            TestRunStatus::Failed => RunStatus::Failed,
            TestRunStatus::Cancelled => RunStatus::Cancelled,
        };
        store
            .set_status(&evidence.result.run_id, status)
            .map_err(|error| {
                StdlibRuntimeError::runtime(format!(
                    "failed to persist native runtime terminal status: {error:?}"
                ))
            })?;
    }

    if let Some(journal_store_path) = evidence.journal_store_path {
        let mut journal = SqliteExecutionJournal::open(journal_store_path, &evidence.result.run_id)
            .map_err(|error| {
                StdlibRuntimeError::runtime(format!(
                    "failed to open SQLite execution journal: {error:?}"
                ))
            })?;
        for record in evidence.result.journal.records() {
            let metadata = JournalMetadata {
                causation_id: record.causation_id.clone(),
                node_id: record.node_id.clone(),
                attempt_id: record.attempt_id.clone(),
                lease_epoch: record.lease_epoch,
            };
            let append_result = if record.terminal {
                journal.append_terminal_with_metadata(
                    record.kind.clone(),
                    metadata,
                    record.payload.clone(),
                )
            } else {
                journal.append_with_metadata(record.kind.clone(), metadata, record.payload.clone())
            };
            append_result.map_err(|error| {
                StdlibRuntimeError::runtime(format!(
                    "failed to persist native runtime journal record: {error:?}"
                ))
            })?;
        }
    }

    if let Some(application_event_store_path) = evidence.application_event_store_path {
        let log =
            SqliteApplicationProtocolLog::open(application_event_store_path).map_err(|error| {
                StdlibRuntimeError::runtime(format!(
                    "failed to open SQLite application event stream: {error:?}"
                ))
            })?;
        let duration = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| StdlibRuntimeError::runtime(format!("system clock error: {error}")))?;
        let occurred_at_unix_ms = u64::try_from(duration.as_millis()).map_err(|_| {
            StdlibRuntimeError::runtime("current timestamp exceeds u64 millisecond range")
        })?;
        let started_cursor = "evt-000001";
        let run_started = ApplicationProtocolEvent::new(
            ApplicationProtocolEventKind::RunStarted,
            ApplicationProtocolEventMetadata {
                event_id: format!("{}:{started_cursor}", evidence.result.run_id),
                protocol_version: "graphblocks.app.v1".to_owned(),
                run_id: evidence.result.run_id.clone(),
                release_id: evidence.graph_hash.to_owned(),
                turn_id: None,
                operation_id: None,
                sequence: 1,
                cursor: Some(started_cursor.to_owned()),
                occurred_at_unix_ms,
            },
            json!({
                "status": "running",
                "graph_hash": evidence.graph_hash,
            }),
        )
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to build RunStarted application event: {error:?}"
            ))
        })?;
        log.append(run_started).map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to persist RunStarted application event: {error:?}"
            ))
        })?;

        let (terminal_kind, status) = match evidence.result.status {
            TestRunStatus::Succeeded => (ApplicationProtocolEventKind::RunCompleted, "succeeded"),
            TestRunStatus::Failed => (ApplicationProtocolEventKind::RunFailed, "failed"),
            TestRunStatus::Cancelled => (ApplicationProtocolEventKind::RunCancelled, "cancelled"),
        };
        let terminal_cursor = "evt-000002";
        let terminal_event = ApplicationProtocolEvent::new(
            terminal_kind,
            ApplicationProtocolEventMetadata {
                event_id: format!("{}:{terminal_cursor}", evidence.result.run_id),
                protocol_version: "graphblocks.app.v1".to_owned(),
                run_id: evidence.result.run_id.clone(),
                release_id: evidence.graph_hash.to_owned(),
                turn_id: None,
                operation_id: None,
                sequence: 2,
                cursor: Some(terminal_cursor.to_owned()),
                occurred_at_unix_ms: occurred_at_unix_ms.saturating_add(1),
            },
            json!({
                "status": status,
                "graph_hash": evidence.graph_hash,
                "outputs": evidence.output_values,
            }),
        )
        .map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to build terminal application event: {error:?}"
            ))
        })?;
        log.append(terminal_event).map_err(|error| {
            StdlibRuntimeError::runtime(format!(
                "failed to persist terminal application event: {error:?}"
            ))
        })?;
    }

    Ok(())
}

fn build_runtime_result(
    result: TestRunResult,
    graph_hash: String,
    output_values: Value,
    deployment_provenance: Option<&RunDeploymentProvenance>,
) -> StdlibRunResult {
    let status = match result.status {
        TestRunStatus::Succeeded => StdlibRunStatus::Succeeded,
        TestRunStatus::Failed => StdlibRunStatus::Failed,
        TestRunStatus::Cancelled => StdlibRunStatus::Cancelled,
    };
    let journal = test_journal_values(&result);
    StdlibRunResult {
        run_id: result.run_id,
        graph_hash,
        status,
        outputs: output_values,
        journal,
        checkpoint: None,
        deployment_provenance: deployment_provenance.cloned(),
    }
}

fn test_journal_values(result: &TestRunResult) -> Vec<Value> {
    result
        .journal
        .records()
        .iter()
        .map(|record| {
            json!({
                "recordId": record.record_id.as_str(),
                "runId": record.run_id.as_str(),
                "runSequence": record.run_sequence,
                "kind": record.kind.as_str(),
                "causationId": record.causation_id.as_deref(),
                "nodeId": record.node_id.as_deref(),
                "attemptId": record.attempt_id.as_deref(),
                "leaseEpoch": record.lease_epoch,
                "payload": record.payload.as_ref(),
                "terminal": record.terminal,
            })
        })
        .collect()
}

fn resolved_inputs_to_json(inputs: &BTreeMap<String, ResolvedInput>) -> Result<Value, BlockError> {
    let mut object = serde_json::Map::new();
    for (name, input) in inputs {
        match input {
            ResolvedInput::Value(value) => {
                insert_resolved_input_path(&mut object, name, value.clone())?;
            }
            ResolvedInput::Outcome(_) => {
                return Err(BlockError::new(
                    "stdlib.outcome_input",
                    ErrorCategory::Configuration,
                    "stdlib executor does not accept outcome-mode inputs",
                    false,
                ));
            }
        }
    }
    Ok(Value::Object(object))
}

fn insert_resolved_input_path(
    object: &mut serde_json::Map<String, Value>,
    path: &str,
    value: Value,
) -> Result<(), BlockError> {
    let parts = path.split('.').collect::<Vec<_>>();
    if parts.is_empty() || parts.iter().any(|part| part.is_empty()) {
        return Err(BlockError::new(
            "stdlib.invalid_input_path",
            ErrorCategory::Configuration,
            format!("runtime input path {path:?} must contain non-empty segments"),
            false,
        ));
    }
    let mut current = object;
    for part in &parts[..parts.len() - 1] {
        let nested = current
            .entry((*part).to_owned())
            .or_insert_with(|| json!({}));
        let Some(nested) = nested.as_object_mut() else {
            return Err(BlockError::new(
                "stdlib.conflicting_input_path",
                ErrorCategory::Configuration,
                format!("runtime input path {path:?} conflicts at segment {part:?}"),
                false,
            ));
        };
        current = nested;
    }
    let leaf = parts
        .last()
        .expect("validated path has at least one segment");
    if current.insert((*leaf).to_owned(), value).is_some() {
        return Err(BlockError::new(
            "stdlib.duplicate_input_path",
            ErrorCategory::Configuration,
            format!("runtime input path {path:?} was resolved more than once"),
            false,
        ));
    }
    Ok(())
}

fn value_at_path<'a>(value: &'a Value, path: &str) -> Option<&'a Value> {
    let mut current = value;
    for part in path.split('.') {
        current = current.get(part)?;
    }
    Some(current)
}

fn json_display(value: &Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string())
}

fn execute_stdlib_block(
    block_id: &str,
    inputs: &Value,
    config: &Value,
) -> Result<Value, BlockError> {
    match block_id {
        "conversation.begin_turn@1" => execute_begin_turn(inputs, config),
        "prompt.render@1" => execute_prompt_render(inputs, config),
        "model.generate@1" => execute_scripted_generate(inputs, config),
        "model.structured_generate@1" => execute_structured_generate(inputs, config),
        "retrieve.execute_plan@1" => execute_retrieval_plan(inputs, config),
        "retrieve.fuse@1" => execute_retrieval_fusion(inputs, config),
        "rank.documents@1" => execute_document_ranking(inputs, config),
        "context.build@1" => execute_context_build(inputs, config),
        "answer.validate_grounding@1" => execute_grounding_validation(inputs, config),
        "check.run_suite@1" => execute_check_suite(inputs, config),
        "gate.evaluate@1" => execute_gate_evaluation(inputs, config),
        "review.request@1" => execute_review_request(inputs, config),
        "result.bundle@1" => execute_result_bundle(inputs, config),
        "tools.resolve@1" => execute_resolve_tools(inputs, config),
        "agent.run@1" => execute_scripted_agent_run(inputs, config),
        "async.start_operation@1" => execute_async_start_operation(inputs, config),
        "async.await_callback@1" => execute_async_await_callback(inputs, config),
        "async.poll_operation@1" => execute_async_poll_operation(inputs, config),
        "async.complete_operation@1" => execute_async_complete_operation(inputs, config),
        "async.cancel_operation@1" => execute_async_cancel_operation(inputs, config),
        "async.expire_operation@1" => execute_async_expire_operation(inputs, config),
        "conversation.commit_turn@1" => execute_commit_turn(inputs),
        "conversation.policy_stop_turn@1" => execute_policy_stop_turn(inputs, config),
        "control.map@2" => execute_control_map(inputs, config),
        "control.select@1" => execute_control_select(inputs, config),
        _ => Err(BlockError::new(
            format!("{block_id}.unsupported"),
            ErrorCategory::Configuration,
            "unsupported stdlib block",
            false,
        )),
    }
}

fn execute_begin_turn(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let conversation_id = inputs
        .get("conversationId")
        .and_then(Value::as_str)
        .or_else(|| {
            inputs
                .get("conversation")
                .and_then(|conversation| {
                    conversation
                        .get("conversationId")
                        .or_else(|| conversation.get("conversation_id"))
                })
                .and_then(Value::as_str)
        })
        .or_else(|| config.get("conversationId").and_then(Value::as_str))
        .unwrap_or("conversation-default");

    Ok(json!({
        "transaction": {
            "conversationId": conversation_id,
            "turnId": "turn-000001",
        }
    }))
}

fn execute_prompt_render(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let template = config
        .get("template")
        .and_then(Value::as_str)
        .unwrap_or("{message.text}");
    let mut rendered = String::new();
    let mut cursor = 0;
    while let Some(start_offset) = template[cursor..].find('{') {
        let start = cursor + start_offset;
        rendered.push_str(&template[cursor..start]);
        let Some(end_offset) = template[start + 1..].find('}') else {
            return Err(BlockError::new(
                "prompt.render.unclosed_placeholder",
                ErrorCategory::Configuration,
                "prompt template has an unclosed placeholder",
                false,
            ));
        };
        let end = start + 1 + end_offset;
        let path = &template[start + 1..end];
        let Some(value) = value_at_path(inputs, path) else {
            return Err(BlockError::new(
                format!("prompt.render.missing.{path}"),
                ErrorCategory::Configuration,
                "prompt input path is missing",
                false,
            ));
        };
        rendered.push_str(&json_display(value));
        cursor = end + 1;
    }
    rendered.push_str(&template[cursor..]);
    Ok(json!({ "prompt": rendered }))
}

fn execute_scripted_generate(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let prompt = inputs
        .get("prompt")
        .or_else(|| inputs.get("context"))
        .map(json_display)
        .unwrap_or_default();
    let response = config
        .get("script")
        .and_then(Value::as_object)
        .and_then(|script| script.get(&prompt))
        .or_else(|| config.get("response"))
        .map(json_display)
        .unwrap_or(prompt);

    Ok(json!({ "response": response }))
}

fn execute_structured_generate(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let output_schema = config
        .get("outputSchema")
        .or_else(|| config.get("output_schema"))
        .and_then(Value::as_str)
        .filter(|schema| !schema.trim().is_empty())
        .ok_or_else(|| {
            BlockError::new(
                "model.structured_generate.missing_output_schema",
                ErrorCategory::Configuration,
                "model.structured_generate@1 requires config.outputSchema",
                false,
            )
        })?;
    let response = config
        .get("response")
        .or_else(|| inputs.get("response"))
        .or_else(|| inputs.get("diagnosis"))
        .cloned()
        .unwrap_or_else(|| inputs.clone());
    if !response.is_object() && !response.is_array() {
        return Err(BlockError::new(
            "model.structured_generate.invalid_response",
            ErrorCategory::Validation,
            "model.structured_generate@1 response must be an object or array",
            false,
        ));
    }
    let items = response
        .get("items")
        .cloned()
        .or_else(|| response.as_array().map(|items| Value::Array(items.clone())))
        .unwrap_or_else(|| json!([response.clone()]));
    let content_digest = canonical_hash(&response);
    Ok(json!({
        "value": response.clone(),
        "response": response,
        "items": items,
        "schemaId": output_schema,
        "schemaRef": output_schema,
        "contentDigest": content_digest,
    }))
}

fn execute_retrieval_plan(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let query = inputs
        .get("query")
        .or_else(|| inputs.get("request"))
        .cloned()
        .ok_or_else(|| {
            BlockError::new(
                "retrieve.execute_plan.missing_query",
                ErrorCategory::Configuration,
                "retrieve.execute_plan@1 requires query or request input",
                false,
            )
        })?;
    let raw_sources = inputs
        .get("sources")
        .or_else(|| config.get("sources"))
        .cloned()
        .unwrap_or_else(|| json!([]));
    let sources = normalize_named_values(&raw_sources, "sourceId").map_err(|message| {
        BlockError::new(
            "retrieve.execute_plan.invalid_sources",
            ErrorCategory::Configuration,
            message,
            false,
        )
    })?;
    let minimum_successful = config
        .get("minimumSuccessfulSources")
        .or_else(|| config.get("minimum_successful_sources"))
        .and_then(Value::as_u64)
        .unwrap_or(1) as usize;
    let mut normalized = Vec::new();
    let mut successful = Vec::new();
    let mut failed = Vec::new();
    for (index, source) in sources.into_iter().enumerate() {
        let source_id = source
            .get("sourceId")
            .or_else(|| source.get("source_id"))
            .or_else(|| source.get("id"))
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| format!("source-{}", index + 1));
        let error = source
            .get("error")
            .filter(|error| !error.is_null())
            .cloned();
        let hits = source
            .get("hits")
            .or_else(|| source.pointer("/result/hits"))
            .cloned()
            .unwrap_or_else(|| json!([]));
        let weight = source.get("weight").cloned().unwrap_or_else(|| json!(1.0));
        if !hits.is_array() {
            return Err(BlockError::new(
                "retrieve.execute_plan.invalid_source_hits",
                ErrorCategory::Validation,
                format!("retrieve source {source_id:?} hits must be an array"),
                false,
            ));
        }
        if let Some(error) = error {
            failed.push(json!({"sourceId": source_id, "error": error}));
            normalized.push(json!({
                "sourceId": source_id,
                "status": "failed",
                "hits": hits,
                "error": error,
                "weight": weight,
            }));
        } else {
            successful.push(Value::String(source_id.clone()));
            normalized.push(json!({
                "sourceId": source_id,
                "status": "succeeded",
                "hits": hits,
                "weight": weight,
            }));
        }
    }
    if successful.len() < minimum_successful {
        return Err(BlockError::new(
            "retrieve.execute_plan.insufficient_sources",
            ErrorCategory::Provider,
            format!(
                "retrieve.execute_plan@1 required {minimum_successful} successful source(s), got {}",
                successful.len()
            ),
            true,
        ));
    }
    let result = json!({
        "retrievalId": format!("retrieval-{}", canonical_hash(&json!({"query": query, "sources": normalized}))),
        "query": query,
        "sources": normalized,
        "successfulSources": successful,
        "failedSources": failed,
    });
    Ok(json!({"result": result, "sources": result["sources"]}))
}

fn execute_retrieval_fusion(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    #[derive(Clone)]
    struct FusionCandidate {
        dedupe_key: String,
        hit_id: String,
        rank: u64,
        normalized_score: f64,
        hit: Value,
    }

    struct FusionGroup {
        representative: Value,
        minimum_rank: u64,
        score: f64,
    }

    let sources = inputs.get("sources").ok_or_else(|| {
        BlockError::new(
            "retrieve.fuse.missing_sources",
            ErrorCategory::Configuration,
            "retrieve.fuse@1 requires sources input",
            false,
        )
    })?;
    let source_values = normalize_named_values(sources, "sourceId").map_err(|message| {
        BlockError::new(
            "retrieve.fuse.invalid_sources",
            ErrorCategory::Validation,
            message,
            false,
        )
    })?;
    let source_count = source_values.len();
    let algorithm = match config.get("algorithm") {
        None => "reciprocal_rank_fusion",
        Some(value) => value.as_str().ok_or_else(|| {
            BlockError::new(
                "retrieve.fuse.invalid_algorithm",
                ErrorCategory::Configuration,
                "retrieve.fuse@1 config.algorithm must be a string",
                false,
            )
        })?,
    };
    if !matches!(
        algorithm,
        "concatenate"
            | "reciprocal_rank_fusion"
            | "weighted_rank"
            | "normalized_score"
            | "interleave"
    ) {
        return Err(BlockError::new(
            "retrieve.fuse.invalid_algorithm",
            ErrorCategory::Configuration,
            "retrieve.fuse@1 config.algorithm must be concatenate, reciprocal_rank_fusion, weighted_rank, normalized_score, or interleave",
            false,
        ));
    }
    let k = match config.get("k") {
        None => 60,
        Some(value) => value.as_u64().ok_or_else(|| {
            BlockError::new(
                "retrieve.fuse.invalid_k",
                ErrorCategory::Configuration,
                "retrieve.fuse@1 config.k must be a positive integer",
                false,
            )
        })?,
    };
    if k == 0 {
        return Err(BlockError::new(
            "retrieve.fuse.invalid_k",
            ErrorCategory::Configuration,
            "retrieve.fuse@1 config.k must be positive",
            false,
        ));
    }
    let mut hit_sets = Vec::new();
    let mut weights = Vec::new();
    for source in source_values {
        let weight = match source.get("weight") {
            None => 1.0,
            Some(value) => value.as_f64().ok_or_else(|| {
                BlockError::new(
                    "retrieve.fuse.invalid_source_weight",
                    ErrorCategory::Validation,
                    "retrieve.fuse@1 source weight must be a positive finite number",
                    false,
                )
            })?,
        };
        if !weight.is_finite() || weight <= 0.0 {
            return Err(BlockError::new(
                "retrieve.fuse.invalid_source_weight",
                ErrorCategory::Validation,
                "retrieve.fuse@1 source weight must be a positive finite number",
                false,
            ));
        }
        let hits = source
            .get("hits")
            .or_else(|| source.pointer("/result/hits"))
            .and_then(Value::as_array)
            .ok_or_else(|| {
                BlockError::new(
                    "retrieve.fuse.invalid_source_hits",
                    ErrorCategory::Validation,
                    "retrieve.fuse@1 each source must contain a hits array",
                    false,
                )
            })?;
        let mut candidates = Vec::new();
        for (index, hit) in hits.iter().enumerate() {
            let rank = hit
                .get("rank")
                .and_then(Value::as_u64)
                .unwrap_or((index + 1) as u64);
            let dedupe_key = hit
                .get("canonicalSource")
                .or_else(|| hit.get("canonical_source"))
                .or_else(|| hit.pointer("/item/source/sourceId"))
                .or_else(|| hit.pointer("/item/source/source_id"))
                .or_else(|| hit.pointer("/item/itemId"))
                .or_else(|| hit.pointer("/item/item_id"))
                .or_else(|| hit.get("hitId"))
                .or_else(|| hit.get("hit_id"))
                .and_then(Value::as_str)
                .map(str::to_owned)
                .unwrap_or_else(|| canonical_hash(hit));
            let normalized_score = hit
                .get("normalizedScore")
                .or_else(|| hit.get("normalized_score"))
                .filter(|value| !value.is_null())
                .map(|value| {
                    value.as_f64().ok_or_else(|| {
                        BlockError::new(
                            "retrieve.fuse.invalid_normalized_score",
                            ErrorCategory::Validation,
                            "retrieve.fuse@1 hit normalizedScore must be a finite number",
                            false,
                        )
                    })
                })
                .transpose()?
                .unwrap_or(0.0);
            if !normalized_score.is_finite() || !(0.0..=1.0).contains(&normalized_score) {
                return Err(BlockError::new(
                    "retrieve.fuse.invalid_normalized_score",
                    ErrorCategory::Validation,
                    "retrieve.fuse@1 hit normalizedScore must be between 0 and 1",
                    false,
                ));
            }
            let hit_id = hit
                .get("hitId")
                .or_else(|| hit.get("hit_id"))
                .and_then(Value::as_str)
                .map(str::to_owned)
                .unwrap_or_else(|| canonical_hash(hit));
            candidates.push(FusionCandidate {
                dedupe_key,
                hit_id,
                rank,
                normalized_score,
                hit: hit.clone(),
            });
        }
        hit_sets.push(candidates);
        weights.push(weight);
    }

    let mut groups: BTreeMap<String, FusionGroup> = BTreeMap::new();
    let mut first_seen = Vec::new();
    for (source_index, hit_set) in hit_sets.iter().enumerate() {
        let weight = weights[source_index];
        for candidate in hit_set {
            if !groups.contains_key(&candidate.dedupe_key) {
                first_seen.push(candidate.dedupe_key.clone());
            }
            let score = match algorithm {
                "reciprocal_rank_fusion" => {
                    weight / (k.saturating_add(candidate.rank.max(1))) as f64
                }
                "weighted_rank" => weight / candidate.rank.max(1) as f64,
                "normalized_score" => weight * candidate.normalized_score,
                "concatenate" | "interleave" => 0.0,
                _ => unreachable!(),
            };
            groups
                .entry(candidate.dedupe_key.clone())
                .and_modify(|group| {
                    group.minimum_rank = group.minimum_rank.min(candidate.rank);
                    group.score += score;
                })
                .or_insert_with(|| FusionGroup {
                    representative: candidate.hit.clone(),
                    minimum_rank: candidate.rank,
                    score,
                });
        }
    }

    let mut ordered_keys = if algorithm == "concatenate" {
        first_seen
    } else if algorithm == "interleave" {
        let mut sorted_hit_sets = hit_sets.clone();
        for hit_set in &mut sorted_hit_sets {
            hit_set.sort_by(|left, right| {
                left.rank
                    .cmp(&right.rank)
                    .then_with(|| left.hit_id.cmp(&right.hit_id))
            });
        }
        let max_len = sorted_hit_sets.iter().map(Vec::len).max().unwrap_or(0);
        let mut keys = Vec::new();
        let mut seen = BTreeSet::new();
        for index in 0..max_len {
            for hit_set in &sorted_hit_sets {
                if let Some(candidate) = hit_set.get(index)
                    && seen.insert(candidate.dedupe_key.clone())
                {
                    keys.push(candidate.dedupe_key.clone());
                }
            }
        }
        keys
    } else {
        let mut keys = groups.keys().cloned().collect::<Vec<_>>();
        keys.sort_by(|left, right| {
            let left_group = &groups[left];
            let right_group = &groups[right];
            right_group
                .score
                .partial_cmp(&left_group.score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| left_group.minimum_rank.cmp(&right_group.minimum_rank))
                .then_with(|| left.cmp(right))
        });
        keys
    };
    if let Some(top_k) = config.get("topK").or_else(|| config.get("top_k")) {
        let top_k = top_k.as_u64().ok_or_else(|| {
            BlockError::new(
                "retrieve.fuse.invalid_top_k",
                ErrorCategory::Configuration,
                "retrieve.fuse@1 config.topK must be a positive integer",
                false,
            )
        })?;
        if top_k == 0 {
            return Err(BlockError::new(
                "retrieve.fuse.invalid_top_k",
                ErrorCategory::Configuration,
                "retrieve.fuse@1 config.topK must be positive",
                false,
            ));
        }
        ordered_keys.truncate(top_k as usize);
    }
    let max_score = ordered_keys
        .first()
        .and_then(|key| groups.get(key))
        .map(|group| group.score)
        .filter(|score| *score > 0.0);
    let hits = ordered_keys
        .into_iter()
        .enumerate()
        .map(|(index, key)| {
            let group = &groups[&key];
            let mut hit = group.representative.clone();
            if !hit.is_object() {
                hit = json!({"value": hit});
            }
            hit["rank"] = json!(index + 1);
            hit["fusionStrategy"] = json!(algorithm);
            if matches!(
                algorithm,
                "reciprocal_rank_fusion" | "weighted_rank" | "normalized_score"
            ) {
                hit["fusionScore"] = json!(group.score);
                hit["normalizedScore"] = max_score
                    .map(|maximum| json!(group.score / maximum))
                    .unwrap_or(Value::Null);
            } else {
                hit["fusionScore"] = Value::Null;
            }
            hit
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "hits": hits,
        "metadata": {
            "algorithm": algorithm,
            "sourceCount": source_count,
        },
    }))
}

fn execute_document_ranking(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let configured_terms = config
        .get("queryTerms")
        .or_else(|| config.get("query_terms"));
    let query = inputs.get("query");
    let raw_terms = if let Some(terms) = configured_terms.or_else(|| {
        query
            .and_then(Value::as_object)
            .and_then(|query| query.get("queryTerms").or_else(|| query.get("query_terms")))
    }) {
        let Some(terms) = terms.as_array() else {
            return Err(BlockError::new(
                "rank.documents.invalid_query_terms",
                ErrorCategory::Configuration,
                "rank.documents@1 queryTerms must be an array of strings",
                false,
            ));
        };
        terms
            .iter()
            .map(|term| {
                term.as_str().map(str::to_owned).ok_or_else(|| {
                    BlockError::new(
                        "rank.documents.invalid_query_terms",
                        ErrorCategory::Configuration,
                        "rank.documents@1 queryTerms must be an array of strings",
                        false,
                    )
                })
            })
            .collect::<Result<Vec<_>, _>>()?
    } else {
        let query_text = query
            .and_then(Value::as_object)
            .and_then(|query| {
                query
                    .get("queryText")
                    .or_else(|| query.get("query_text"))
                    .or_else(|| query.get("original"))
                    .or_else(|| query.get("text"))
            })
            .or_else(|| query.filter(|value| value.is_string()))
            .map(json_display)
            .unwrap_or_default();
        vec![query_text]
    };
    let terms = raw_terms
        .iter()
        .flat_map(|term| {
            term.split(|character: char| !character.is_alphanumeric() && character != '_')
                .filter(|term| !term.is_empty())
                .map(str::to_ascii_lowercase)
        })
        .collect::<Vec<_>>();
    let hits = inputs
        .get("hits")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            BlockError::new(
                "rank.documents.invalid_hits",
                ErrorCategory::Configuration,
                "rank.documents@1 requires a hits array",
                false,
            )
        })?;
    let input_limit = config
        .get("inputLimit")
        .or_else(|| config.get("input_limit"))
        .and_then(Value::as_u64)
        .map(|limit| limit as usize)
        .unwrap_or(hits.len());
    if input_limit == 0 {
        return Err(BlockError::new(
            "rank.documents.invalid_input_limit",
            ErrorCategory::Configuration,
            "rank.documents@1 inputLimit must be positive",
            false,
        ));
    }
    let reranker = match config
        .get("rerankerId")
        .or_else(|| config.get("reranker_id"))
        .or_else(|| config.get("reranker"))
    {
        None => "deterministic-term-reranker",
        Some(value) => value
            .as_str()
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                BlockError::new(
                    "rank.documents.invalid_reranker_id",
                    ErrorCategory::Configuration,
                    "rank.documents@1 config.rerankerId must be a non-empty string",
                    false,
                )
            })?,
    };
    let mut ranked = hits
        .iter()
        .take(input_limit)
        .enumerate()
        .map(|(index, hit)| {
            let text = hit
                .pointer("/item/preview")
                .or_else(|| hit.pointer("/item/text"))
                .or_else(|| hit.get("text"))
                .map(json_display)
                .unwrap_or_default()
                .to_ascii_lowercase();
            let score = terms
                .iter()
                .map(|term| text.matches(term).count())
                .sum::<usize>();
            (score, index, hit.clone())
        })
        .collect::<Vec<_>>();
    ranked.sort_by(|left, right| right.0.cmp(&left.0).then_with(|| left.1.cmp(&right.1)));
    let hits = ranked
        .into_iter()
        .enumerate()
        .map(|(index, (score, _original_index, mut hit))| {
            if !hit.is_object() {
                hit = json!({"value": hit});
            }
            hit["rank"] = json!(index + 1);
            hit["rerankScore"] = json!(score);
            hit["reranker"] = json!(reranker);
            hit
        })
        .collect::<Vec<_>>();
    let result = json!({
        "hits": hits,
        "reranker": reranker,
        "inputCount": inputs["hits"].as_array().map_or(0, Vec::len),
        "evaluatedCount": hits.len(),
    });
    Ok(json!({"hits": result["hits"], "result": result}))
}

fn execute_context_build(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let evidence = inputs
        .get("evidence")
        .or_else(|| inputs.get("hits"))
        .and_then(Value::as_array)
        .ok_or_else(|| {
            BlockError::new(
                "context.build.invalid_evidence",
                ErrorCategory::Configuration,
                "context.build@1 requires an evidence or hits array",
                false,
            )
        })?;
    let max_tokens = config
        .get("maxTokens")
        .or_else(|| config.get("max_tokens"))
        .and_then(Value::as_u64)
        .unwrap_or(8_192) as usize;
    let reserve_tokens = config
        .get("reserveOutputTokens")
        .or_else(|| config.get("reserve_output_tokens"))
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    if reserve_tokens > max_tokens {
        return Err(BlockError::new(
            "context.build.invalid_budget",
            ErrorCategory::Configuration,
            "context.build@1 reserveOutputTokens must not exceed maxTokens",
            false,
        ));
    }
    let context_id = match config.get("contextId").or_else(|| config.get("context_id")) {
        None => None,
        Some(value) => {
            let context_id = value
                .as_str()
                .filter(|value| !value.trim().is_empty())
                .ok_or_else(|| {
                    BlockError::new(
                        "context.build.invalid_context_id",
                        ErrorCategory::Configuration,
                        "context.build@1 config.contextId must be a non-empty string",
                        false,
                    )
                })?;
            Some(context_id)
        }
    };
    let available = max_tokens - reserve_tokens;
    let mut selected = Vec::new();
    let mut token_count = 0usize;
    for hit in evidence {
        let tokens = json_display(hit).split_whitespace().count().max(1);
        if token_count.saturating_add(tokens) <= available {
            selected.push(hit.clone());
            token_count += tokens;
        }
    }
    let pack = json!({
        "contextId": context_id.map(str::to_owned).unwrap_or_else(|| format!("context-{}", canonical_hash(&json!({"evidence": selected, "history": inputs.get("history"), "currentMessage": inputs.get("currentMessage")})))),
        "hits": selected,
        "history": inputs.get("history").cloned().unwrap_or_else(|| json!([])),
        "currentMessage": inputs.get("currentMessage").cloned().unwrap_or(Value::Null),
        "tokenBudget": available,
        "tokenCount": token_count,
        "droppedCount": evidence.len().saturating_sub(selected.len()),
    });
    Ok(json!({"pack": pack}))
}

fn execute_grounding_validation(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let response = inputs
        .get("response")
        .or_else(|| inputs.get("answer"))
        .cloned()
        .ok_or_else(|| {
            BlockError::new(
                "answer.validate_grounding.missing_response",
                ErrorCategory::Configuration,
                "answer.validate_grounding@1 requires response or answer input",
                false,
            )
        })?;
    let context = inputs.get("context").ok_or_else(|| {
        BlockError::new(
            "answer.validate_grounding.missing_context",
            ErrorCategory::Configuration,
            "answer.validate_grounding@1 requires context input",
            false,
        )
    })?;
    let require_citation = config
        .get("requireCitation")
        .or_else(|| config.get("require_citation"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let policy = match config
        .get("onInsufficientEvidence")
        .or_else(|| config.get("on_insufficient_evidence"))
    {
        None => "fail",
        Some(value) => value.as_str().ok_or_else(|| {
            BlockError::new(
                "answer.validate_grounding.invalid_failure_policy",
                ErrorCategory::Configuration,
                "answer.validate_grounding@1 onInsufficientEvidence must be a string",
                false,
            )
        })?,
    };
    if !matches!(
        policy,
        "warn" | "fail" | "abstain" | "repair" | "remove_invalid"
    ) {
        return Err(BlockError::new(
            "answer.validate_grounding.invalid_failure_policy",
            ErrorCategory::Configuration,
            "answer.validate_grounding@1 onInsufficientEvidence must be warn, fail, abstain, repair, or remove_invalid",
            false,
        ));
    }
    let context_hits = context
        .get("hits")
        .or_else(|| context.get("evidence"))
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    let citation_count = response
        .get("citations")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    let mut issues = Vec::new();
    if context_hits == 0 && !json_display(&response).trim().is_empty() {
        issues.push("grounding.insufficient_context");
    }
    if require_citation && citation_count == 0 {
        issues.push("grounding.citation_required");
    }
    let issues_detected = !issues.is_empty();
    let ok = !issues_detected || policy == "warn";
    let abstained = issues_detected && policy == "abstain";
    let repair_attempted = issues_detected && matches!(policy, "repair" | "remove_invalid");
    // The lightweight stdlib validator can currently detect missing context and
    // missing citations, but neither issue can be repaired without inventing
    // evidence. Keep the original candidate and report the unrepaired issues.
    let repaired = false;
    let unrepaired_issues = if repair_attempted {
        issues.clone()
    } else {
        Vec::new()
    };
    let validated_response = if abstained {
        json!({
            "text": "I do not have enough validated source support to answer.",
            "abstention": {"reason": "insufficient_evidence", "issueCodes": issues},
        })
    } else {
        response.clone()
    };
    let result = json!({
        "ok": ok,
        "issues": issues,
        "abstained": abstained,
        "policy": policy,
        "repaired": repaired,
        "repairAttempted": repair_attempted,
        "unrepairedIssues": unrepaired_issues,
        "response": validated_response,
    });
    Ok(json!({
        "candidate": result["response"].clone(),
        "response": result["response"].clone(),
        "result": result.clone(),
        "validation": result,
    }))
}

fn execute_check_suite(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let checks = config
        .get("checks")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            BlockError::new(
                "check.run_suite.invalid_checks",
                ErrorCategory::Configuration,
                "check.run_suite@1 requires config.checks array",
                false,
            )
        })?;
    let supplied = inputs
        .get("checkResults")
        .or_else(|| inputs.get("check_results"))
        .or_else(|| config.get("results"));
    let mut results = Vec::new();
    for check in checks {
        let check_id = check.as_str().ok_or_else(|| {
            BlockError::new(
                "check.run_suite.invalid_check",
                ErrorCategory::Configuration,
                "check.run_suite@1 check names must be strings",
                false,
            )
        })?;
        let supplied_result = supplied.and_then(|results| {
            results.get(check_id).or_else(|| {
                results.as_array().and_then(|results| {
                    results.iter().find(|result| {
                        result
                            .get("checkId")
                            .or_else(|| result.get("check_id"))
                            .and_then(Value::as_str)
                            == Some(check_id)
                    })
                })
            })
        });
        let subject_result = inputs
            .get("subject")
            .and_then(|subject| subject.get("checks").or(Some(subject)))
            .and_then(|subject| subject.get(check_id));
        let value = supplied_result.or(subject_result);
        let status = match value {
            Some(Value::Bool(true)) => "passed",
            Some(Value::Bool(false)) => "failed",
            Some(Value::String(status)) if matches!(status.as_str(), "pass" | "passed") => "passed",
            Some(Value::String(status)) if matches!(status.as_str(), "fail" | "failed") => "failed",
            Some(Value::Object(result)) => result
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("inconclusive"),
            _ => "inconclusive",
        };
        results.push(json!({
            "checkId": check_id,
            "status": status,
            "diagnostics": value.and_then(|value| value.get("diagnostics")).cloned().unwrap_or_else(|| json!([])),
        }));
        if config
            .get("stopOnFailure")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            && status == "failed"
        {
            break;
        }
    }
    let passed = results
        .iter()
        .all(|result| result.get("status") == Some(&json!("passed")));
    let diagnostics = results
        .iter()
        .flat_map(|result| {
            result
                .get("diagnostics")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .cloned()
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "results": results.clone(),
        "checks": results,
        "passed": passed,
        "hardGatePassed": passed,
        "diagnostics": diagnostics,
    }))
}

fn execute_gate_evaluation(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let checks = inputs
        .get("checks")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            BlockError::new(
                "gate.evaluate.invalid_checks",
                ErrorCategory::Configuration,
                "gate.evaluate@1 requires a checks array",
                false,
            )
        })?;
    let flattened = checks
        .iter()
        .flat_map(|value| {
            value
                .as_array()
                .map_or_else(|| vec![value.clone()], Clone::clone)
        })
        .collect::<Vec<_>>();
    let hard_constraints = config
        .get("hardConstraints")
        .or_else(|| config.get("hard_constraints"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_else(|| {
            flattened
                .iter()
                .filter_map(|check| {
                    check
                        .get("checkId")
                        .or_else(|| check.get("check_id"))
                        .cloned()
                })
                .collect()
        });
    let mut violated = Vec::new();
    for required in &hard_constraints {
        let Some(required) = required.as_str() else {
            return Err(BlockError::new(
                "gate.evaluate.invalid_constraint",
                ErrorCategory::Configuration,
                "gate.evaluate@1 hardConstraints entries must be strings",
                false,
            ));
        };
        let status = flattened.iter().find_map(|check| {
            let id = check
                .get("checkId")
                .or_else(|| check.get("check_id"))
                .and_then(Value::as_str);
            (id == Some(required))
                .then(|| check.get("status").and_then(Value::as_str))
                .flatten()
        });
        if !matches!(status, Some("pass" | "passed")) {
            violated.push(format!("check:{required}"));
        }
    }
    let has_inconclusive = flattened.iter().any(|check| {
        matches!(
            check.get("status").and_then(Value::as_str),
            Some("inconclusive" | "error" | "timeout")
        )
    });
    let metrics = match inputs.get("metrics") {
        None => Vec::new(),
        Some(metrics) => metrics.as_array().cloned().ok_or_else(|| {
            BlockError::new(
                "gate.evaluate.invalid_metrics",
                ErrorCategory::Configuration,
                "gate.evaluate@1 metrics must be an array",
                false,
            )
        })?,
    };
    let constraints = match config.get("constraints") {
        None => Vec::new(),
        Some(constraints) => constraints.as_array().cloned().ok_or_else(|| {
            BlockError::new(
                "gate.evaluate.invalid_constraints",
                ErrorCategory::Configuration,
                "gate.evaluate@1 constraints must be an array",
                false,
            )
        })?,
    };
    for constraint in constraints {
        let Some(constraint) = constraint.as_object() else {
            return Err(BlockError::new(
                "gate.evaluate.invalid_constraint",
                ErrorCategory::Configuration,
                "gate.evaluate@1 constraint must be an object",
                false,
            ));
        };
        let metric_name = constraint
            .get("metric")
            .or_else(|| constraint.get("metricName"))
            .and_then(Value::as_str)
            .filter(|name| !name.trim().is_empty())
            .ok_or_else(|| {
                BlockError::new(
                    "gate.evaluate.invalid_constraint",
                    ErrorCategory::Configuration,
                    "gate.evaluate@1 constraint metric must be a non-empty string",
                    false,
                )
            })?;
        let operator = constraint
            .get("operator")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                BlockError::new(
                    "gate.evaluate.invalid_constraint",
                    ErrorCategory::Configuration,
                    "gate.evaluate@1 constraint operator must be a string",
                    false,
                )
            })?;
        if !matches!(operator, "equals" | "at_least" | "at_most") {
            return Err(BlockError::new(
                "gate.evaluate.invalid_constraint",
                ErrorCategory::Configuration,
                "gate.evaluate@1 constraint operator must be equals, at_least, or at_most",
                false,
            ));
        }
        let threshold = constraint.get("threshold").ok_or_else(|| {
            BlockError::new(
                "gate.evaluate.invalid_constraint",
                ErrorCategory::Configuration,
                "gate.evaluate@1 constraint requires threshold",
                false,
            )
        })?;
        let metric_value = metrics.iter().find_map(|metric| {
            (metric.get("name").and_then(Value::as_str) == Some(metric_name))
                .then(|| metric.get("value"))
                .flatten()
        });
        let satisfied = match (operator, metric_value) {
            ("equals", Some(value)) => value == threshold,
            ("at_least", Some(value)) => {
                value
                    .as_f64()
                    .zip(threshold.as_f64())
                    .is_some_and(|(value, threshold)| {
                        value.is_finite() && threshold.is_finite() && value >= threshold
                    })
            }
            ("at_most", Some(value)) => {
                value
                    .as_f64()
                    .zip(threshold.as_f64())
                    .is_some_and(|(value, threshold)| {
                        value.is_finite() && threshold.is_finite() && value <= threshold
                    })
            }
            _ => false,
        };
        if !satisfied {
            violated.push(format!("metric:{metric_name}"));
        }
    }
    let decision = if !violated.is_empty() {
        "fail"
    } else if has_inconclusive {
        "inconclusive"
    } else {
        "pass"
    };
    let subject = inputs
        .get("subject")
        .cloned()
        .or_else(|| {
            flattened
                .iter()
                .find_map(|check| check.get("subject").cloned())
        })
        .unwrap_or_else(|| {
            json!({
                "resourceId": "gate-subject",
                "digest": canonical_hash(&Value::Array(flattened.clone())),
            })
        });
    let gate_id = config
        .get("gateId")
        .or_else(|| config.get("gate_id"))
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| {
            format!(
                "gate-{}",
                canonical_hash(&json!({"checks": flattened, "constraints": hard_constraints}))
            )
        });
    let result = json!({
        "gateId": gate_id,
        "subject": subject,
        "decision": decision,
        "checkIds": hard_constraints,
        "violatedConstraints": violated.clone(),
        "metrics": metrics,
        "policyRef": config.get("policyRef").or_else(|| config.get("policy_ref")).cloned(),
    });
    Ok(json!({
        "result": result,
        "decision": decision,
        "passed": decision == "pass",
        "violations": violated,
    }))
}

fn execute_review_request(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let subject = inputs.get("subject").cloned().ok_or_else(|| {
        BlockError::new(
            "review.request.missing_subject",
            ErrorCategory::Configuration,
            "review.request@1 requires subject input",
            false,
        )
    })?;
    let subject_digest = canonical_hash(&subject);
    let supplied_review = inputs.get("review");
    if let Some(expected) = supplied_review
        .and_then(|review| {
            review
                .get("subjectDigest")
                .or_else(|| review.get("subject_digest"))
        })
        .or_else(|| inputs.get("subjectDigest"))
        .or_else(|| inputs.get("subject_digest"))
        .and_then(Value::as_str)
        && expected != subject_digest
    {
        return Err(BlockError::new(
            "review.request.subject_digest_mismatch",
            ErrorCategory::Policy,
            "review.request@1 subject digest does not match the supplied subject",
            false,
        ));
    }
    let decision = supplied_review
        .and_then(|review| review.get("decision"))
        .or_else(|| inputs.get("decision"))
        .or_else(|| config.get("decision"))
        .and_then(Value::as_str)
        .unwrap_or("pending");
    if !matches!(
        decision,
        "pending" | "accept" | "accept_with_conditions" | "revise" | "reject"
    ) {
        return Err(BlockError::new(
            "review.request.invalid_decision",
            ErrorCategory::Configuration,
            "review.request@1 decision is not recognized",
            false,
        ));
    }
    let required_credential = config
        .get("requiredCredential")
        .or_else(|| config.get("required_credential"))
        .and_then(Value::as_str);
    let credential_refs = supplied_review
        .and_then(|review| {
            review
                .get("credentialRefs")
                .or_else(|| review.get("credential_refs"))
        })
        .or_else(|| inputs.get("credentialRefs"))
        .or_else(|| inputs.get("credential_refs"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if decision != "pending"
        && required_credential.is_some_and(|required| {
            !credential_refs
                .iter()
                .any(|credential| credential.as_str() == Some(required))
        })
    {
        return Err(BlockError::new(
            "review.request.missing_credential",
            ErrorCategory::Policy,
            "review.request@1 decision is missing the required reviewer credential",
            false,
        ));
    }
    let requested_by = inputs
        .get("requestedBy")
        .or_else(|| inputs.get("requested_by"))
        .or_else(|| config.get("requestedBy"))
        .or_else(|| config.get("requested_by"))
        .cloned()
        .unwrap_or_else(|| json!({"principalId": "graphblocks-runtime"}));
    let mut record = json!({
        "reviewId": supplied_review
            .and_then(|review| review.get("reviewId").or_else(|| review.get("review_id")))
            .cloned()
            .unwrap_or_else(|| json!(format!("review-{}", canonical_hash(&json!({"subjectDigest": subject_digest, "scope": config.get("scope")}))))),
        "subject": subject,
        "subjectDigest": subject_digest,
        "scope": config.get("scope").cloned().unwrap_or_else(|| json!("general")),
        "decision": decision,
        "requestedBy": requested_by,
        "reviewer": supplied_review
            .and_then(|review| review.get("reviewer"))
            .or_else(|| inputs.get("reviewer"))
            .cloned()
            .unwrap_or(Value::Null),
        "credentialRefs": credential_refs,
        "invalidateOnSubjectChange": config.get("invalidateOnSubjectChange").and_then(Value::as_bool).unwrap_or(true),
    });
    if let Some(gate) = inputs.get("gate") {
        record["gate"] = gate.clone();
    }
    let request_digest = canonical_hash(&record);
    Ok(json!({
        "request": record.clone(),
        "record": record,
        "requestDigest": request_digest,
        "pending": decision == "pending",
        "approved": matches!(decision, "accept" | "accept_with_conditions"),
        "accepted": matches!(decision, "accept" | "accept_with_conditions"),
        "status": decision,
        "waitMode": if decision == "pending" { json!("application_event") } else { Value::Null },
    }))
}

fn execute_result_bundle(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let outputs = inputs.get("outputs").cloned().unwrap_or_else(|| json!([]));
    if !outputs.is_array() && !outputs.is_object() {
        return Err(BlockError::new(
            "result.bundle.invalid_outputs",
            ErrorCategory::Configuration,
            "result.bundle@1 outputs must be an array or object",
            false,
        ));
    }
    let mut content = serde_json::Map::new();
    content.insert("outputs".to_owned(), outputs);
    for name in [
        "inputs",
        "evidence",
        "checks",
        "gate",
        "reviews",
        "metrics",
        "artifacts",
        "diagnostics",
        "usage",
        "usageRecords",
        "policyDecisionRefs",
    ] {
        content.insert(
            name.to_owned(),
            inputs.get(name).cloned().unwrap_or_else(|| json!([])),
        );
    }
    let content = Value::Object(content);
    let digest = canonical_hash(&content);
    let result = json!({
        "bundleId": config.get("bundleId").or_else(|| config.get("bundle_id")).cloned().unwrap_or_else(|| json!(format!("bundle-{digest}"))),
        "runId": config.get("runId").or_else(|| config.get("run_id")).cloned().unwrap_or_else(|| json!("run-unknown")),
        "releaseId": config.get("releaseId").or_else(|| config.get("release_id")).cloned().unwrap_or_else(|| json!("release-unknown")),
        "contentDigest": digest.clone(),
        "content": content,
    });
    Ok(json!({
        "result": result.clone(),
        "bundle": result,
        "contentDigest": digest,
    }))
}

fn normalize_named_values(value: &Value, identity_key: &str) -> Result<Vec<Value>, String> {
    if let Some(values) = value.as_array() {
        return Ok(values.clone());
    }
    if let Some(values) = value.as_object() {
        return Ok(values
            .iter()
            .map(|(name, value)| {
                let mut value = value.clone();
                if !value.is_object() {
                    value = json!({"value": value});
                }
                if value.get(identity_key).is_none() {
                    value[identity_key] = json!(name);
                }
                value
            })
            .collect());
    }
    Err("value must be an array or object".to_owned())
}

fn execute_async_start_operation(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let Some(config) = config.as_object() else {
        return Err(BlockError::new(
            "async.start_operation.invalid_config",
            ErrorCategory::Configuration,
            "async.start_operation@1 config must be an object",
            false,
        ));
    };
    let operation_id = required_alias_object_str(
        config,
        "operationId",
        "operation_id",
        "async.start_operation.invalid_config",
    )?;
    let run_id = required_alias_object_str(
        config,
        "runId",
        "run_id",
        "async.start_operation.invalid_config",
    )?;
    let node_id = required_alias_object_str(
        config,
        "nodeId",
        "node_id",
        "async.start_operation.invalid_config",
    )?;
    let attempt_id = required_alias_object_str(
        config,
        "attemptId",
        "attempt_id",
        "async.start_operation.invalid_config",
    )?;
    let kind = parse_async_operation_kind(required_object_str(
        config,
        "kind",
        "async.start_operation.invalid_config",
    )?)?;
    let resume_token_hash = required_alias_object_str(
        config,
        "resumeTokenHash",
        "resume_token_hash",
        "async.start_operation.invalid_config",
    )?;
    let idempotency_key = required_alias_object_str(
        config,
        "idempotencyKey",
        "idempotency_key",
        "async.start_operation.invalid_config",
    )?;
    let expected_schema = required_alias_object_str(
        config,
        "expectedSchema",
        "expected_schema",
        "async.start_operation.invalid_config",
    )?;
    let created_at_unix_ms = required_alias_object_u64(
        config,
        "createdAtUnixMs",
        "created_at_unix_ms",
        "async.start_operation.invalid_config",
    )?;
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
    if let Some(provider_operation_id) =
        optional_alias_string(config, "providerOperationId", "provider_operation_id")?
    {
        let submitted_at_unix_ms = required_alias_object_u64(
            config,
            "submittedAtUnixMs",
            "submitted_at_unix_ms",
            "async.start_operation.invalid_config",
        )?;
        operation = operation.submitted(provider_operation_id, submitted_at_unix_ms);
    }
    let explicit_expires_at_unix_ms =
        optional_alias_u64(config, "expiresAtUnixMs", "expires_at_unix_ms")?;
    let timeout_ms = optional_alias_duration_ms(
        config,
        &["timeoutMs", "timeout_ms", "timeout"],
        "async.start_operation.invalid_config",
        "async.start_operation@1 timeout must be a positive duration",
    )?;
    if explicit_expires_at_unix_ms.is_some() && timeout_ms.is_some() {
        return Err(BlockError::new(
            "async.start_operation.invalid_config",
            ErrorCategory::Configuration,
            "async.start_operation@1 must not define both expiresAtUnixMs and timeout",
            false,
        ));
    }
    let expires_at_unix_ms = if explicit_expires_at_unix_ms.is_some() {
        explicit_expires_at_unix_ms
    } else {
        timeout_ms
            .map(|timeout_ms| {
                created_at_unix_ms.checked_add(timeout_ms).ok_or_else(|| {
                    BlockError::new(
                        "async.start_operation.invalid_config",
                        ErrorCategory::Configuration,
                        "async.start_operation@1 timeout exceeds timestamp range",
                        false,
                    )
                })
            })
            .transpose()?
    };
    let infinite_wait_policy = optional_infinite_wait_policy(
        config,
        "async.start_operation.invalid_config",
        "async.start_operation@1",
    )?;
    if expires_at_unix_ms.is_some() && infinite_wait_policy.is_some() {
        return Err(BlockError::new(
            "async.start_operation.invalid_config",
            ErrorCategory::Configuration,
            "async.start_operation@1 must not define both timeout and infiniteWaitPolicy",
            false,
        ));
    }
    if let Some(infinite_wait_policy) = infinite_wait_policy {
        operation = operation.with_infinite_wait_policy(infinite_wait_policy);
    }
    if let Some(expires_at_unix_ms) = expires_at_unix_ms {
        operation = operation.waiting_callback(expires_at_unix_ms);
    } else if operation.infinite_wait_policy.is_some() {
        operation.state = AsyncOperationState::WaitingCallback;
    }
    operation.validate().map_err(|error| {
        BlockError::new(
            "async.start_operation.invalid_operation",
            ErrorCategory::Configuration,
            format!("async.start_operation@1 invalid operation: {error:?}"),
            false,
        )
    })?;

    Ok(json!({
        "operation": async_operation_json(
            &operation,
            inputs
                .get("subject")
                .or_else(|| inputs.get("changeset"))
                .cloned(),
        ),
    }))
}

fn execute_async_await_callback(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let operation = required_async_operation_input(inputs, "async.await_callback@1")?;
    let operation_object = operation
        .as_object()
        .expect("required_async_operation_input returns an object");
    let state = operation_object
        .get("state")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            BlockError::new(
                "async.await_callback.invalid_operation",
                ErrorCategory::Configuration,
                "async.await_callback@1 input operation.state must be a string",
                false,
            )
        })?;
    if state != "waiting_callback" {
        return Err(BlockError::new(
            "async.await_callback.not_waiting",
            ErrorCategory::Configuration,
            format!("async.await_callback@1 operation must be waiting_callback, got {state:?}"),
            false,
        ));
    }
    let checkpoint = match config.get("checkpoint") {
        Some(value) => value.as_bool().ok_or_else(|| {
            BlockError::new(
                "async.await_callback.invalid_config",
                ErrorCategory::Configuration,
                "async.await_callback@1 checkpoint must be a boolean",
                false,
            )
        })?,
        None => true,
    };
    let on_timeout = config
        .get("onTimeout")
        .or_else(|| config.get("on_timeout"))
        .and_then(Value::as_str)
        .unwrap_or("fail");
    if !matches!(on_timeout, "fail" | "cancel" | "expire") {
        return Err(BlockError::new(
            "async.await_callback.invalid_config",
            ErrorCategory::Configuration,
            "async.await_callback@1 onTimeout must be one of fail, cancel, or expire",
            false,
        ));
    }
    let timeout_ms = config
        .as_object()
        .map(|config| {
            optional_alias_duration_ms(
                config,
                &["timeoutMs", "timeout_ms", "timeout"],
                "async.await_callback.invalid_config",
                "async.await_callback@1 timeout must be a positive duration",
            )
        })
        .transpose()?
        .flatten();
    let infinite_wait_policy = config
        .as_object()
        .map(|config| {
            optional_infinite_wait_policy(
                config,
                "async.await_callback.invalid_config",
                "async.await_callback@1",
            )
        })
        .transpose()?
        .flatten();
    if timeout_ms.is_some() && infinite_wait_policy.is_some() {
        return Err(BlockError::new(
            "async.await_callback.invalid_config",
            ErrorCategory::Configuration,
            "async.await_callback@1 must not define both timeout and infiniteWaitPolicy",
            false,
        ));
    }

    let mut wait = json!({
        "state": "waiting_callback",
        "operation": operation,
        "checkpoint": checkpoint,
        "onTimeout": on_timeout,
    });
    if let Some(timeout_ms) = timeout_ms {
        wait["timeoutMs"] = json!(timeout_ms);
    }
    if let Some(infinite_wait_policy) = infinite_wait_policy {
        wait["infiniteWaitPolicy"] = json!(infinite_wait_policy);
    }

    Ok(json!({
        "wait": wait,
    }))
}

fn execute_async_poll_operation(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let operation = required_async_operation_input(inputs, "async.poll_operation@1")?;
    let Some(config) = config.as_object() else {
        return Err(BlockError::new(
            "async.poll_operation.invalid_config",
            ErrorCategory::Configuration,
            "async.poll_operation@1 config must be an object",
            false,
        ));
    };
    let interval_ms = optional_alias_duration_ms(
        config,
        &["intervalMs", "interval_ms", "interval"],
        "async.poll_operation.invalid_config",
        "async.poll_operation@1 interval must be a positive duration",
    )?
    .unwrap_or(30_000);
    let max_interval_ms = optional_alias_duration_ms(
        config,
        &[
            "maxIntervalMs",
            "max_interval_ms",
            "maxInterval",
            "max_interval",
        ],
        "async.poll_operation.invalid_config",
        "async.poll_operation@1 maxInterval must be a positive duration",
    )?
    .unwrap_or(interval_ms);
    if max_interval_ms < interval_ms {
        return Err(BlockError::new(
            "async.poll_operation.invalid_config",
            ErrorCategory::Configuration,
            "async.poll_operation@1 maxInterval must not be less than interval",
            false,
        ));
    }
    let timeout_ms = optional_alias_duration_ms(
        config,
        &["timeoutMs", "timeout_ms", "timeout"],
        "async.poll_operation.missing_timeout",
        "async.poll_operation@1 timeoutMs must be a positive duration",
    )?;
    let infinite_wait_policy = optional_infinite_wait_policy(
        config,
        "async.poll_operation.invalid_config",
        "async.poll_operation@1",
    )?;
    if timeout_ms.is_none() && infinite_wait_policy.is_none() {
        return Err(BlockError::new(
            "async.poll_operation.missing_timeout",
            ErrorCategory::Configuration,
            "async.poll_operation@1 requires timeoutMs",
            false,
        ));
    }
    if timeout_ms.is_some() && infinite_wait_policy.is_some() {
        return Err(BlockError::new(
            "async.poll_operation.invalid_config",
            ErrorCategory::Configuration,
            "async.poll_operation@1 must not define both timeout and infiniteWaitPolicy",
            false,
        ));
    }
    let mut polling_operation = operation.clone();
    polling_operation["state"] = json!("polling");

    let mut poll = json!({
        "state": "polling",
        "operation": polling_operation,
        "intervalMs": interval_ms,
        "maxIntervalMs": max_interval_ms,
    });
    if let Some(timeout_ms) = timeout_ms {
        poll["timeoutMs"] = json!(timeout_ms);
    }
    if let Some(infinite_wait_policy) = infinite_wait_policy {
        poll["infiniteWaitPolicy"] = json!(infinite_wait_policy);
    }

    Ok(json!({
        "poll": poll
    }))
}

fn execute_async_complete_operation(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let config = required_async_terminal_config(config, "async.complete_operation@1")?;
    let operation = required_async_operation_input(inputs, "async.complete_operation@1")?;
    let operation_id = required_async_operation_id(operation, "async.complete_operation@1")?;
    let output = inputs.get("output").cloned().unwrap_or(Value::Null);
    let completed_at_unix_ms = optional_async_terminal_u64(
        config,
        "completedAtUnixMs",
        "completed_at_unix_ms",
        "async.complete_operation.invalid_config",
    )?;
    validate_async_terminal_timestamp(
        operation,
        completed_at_unix_ms,
        "async.complete_operation@1",
        "async.complete_operation.invalid_config",
    )?;
    let external_effects = parse_async_external_effects(config, "async.complete_operation@1")?;
    let mut result = AsyncOperationResult::completed(operation_id)
        .with_output(output)
        .with_external_effects(external_effects);
    apply_async_result_projections(config, "async.complete_operation@1", &mut result)?;

    Ok(json!({
        "result": async_operation_result_json(result, completed_at_unix_ms)?,
    }))
}

fn execute_async_cancel_operation(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let config = required_async_terminal_config(config, "async.cancel_operation@1")?;
    let operation = required_async_operation_input(inputs, "async.cancel_operation@1")?;
    let operation_id = required_async_operation_id(operation, "async.cancel_operation@1")?;
    let completed_at_unix_ms = optional_async_terminal_u64(
        config,
        "cancelledAtUnixMs",
        "cancelled_at_unix_ms",
        "async.cancel_operation.invalid_config",
    )?;
    validate_async_terminal_timestamp(
        operation,
        completed_at_unix_ms,
        "async.cancel_operation@1",
        "async.cancel_operation.invalid_config",
    )?;
    let external_effects = parse_async_external_effects(config, "async.cancel_operation@1")?;
    let mut result =
        AsyncOperationResult::cancelled(operation_id).with_external_effects(external_effects);
    apply_async_result_projections(config, "async.cancel_operation@1", &mut result)?;

    Ok(json!({
        "result": async_operation_result_json(result, completed_at_unix_ms)?,
    }))
}

fn execute_async_expire_operation(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let config = required_async_terminal_config(config, "async.expire_operation@1")?;
    let operation = required_async_operation_input(inputs, "async.expire_operation@1")?;
    let operation_id = required_async_operation_id(operation, "async.expire_operation@1")?;
    let completed_at_unix_ms = optional_async_terminal_u64(
        config,
        "expiredAtUnixMs",
        "expired_at_unix_ms",
        "async.expire_operation.invalid_config",
    )?;
    validate_async_terminal_timestamp(
        operation,
        completed_at_unix_ms,
        "async.expire_operation@1",
        "async.expire_operation.invalid_config",
    )?;
    let external_effects = parse_async_external_effects(config, "async.expire_operation@1")?;
    let mut result =
        AsyncOperationResult::expired(operation_id).with_external_effects(external_effects);
    apply_async_result_projections(config, "async.expire_operation@1", &mut result)?;

    Ok(json!({
        "result": async_operation_result_json(result, completed_at_unix_ms)?,
    }))
}

fn execute_resolve_tools(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let Some(config) = config.as_object() else {
        return Err(BlockError::new(
            "tools.resolve.invalid_config",
            ErrorCategory::Configuration,
            "tools.resolve@1 config must be an object",
            false,
        ));
    };

    let mut definitions = Vec::new();
    if let Some(raw_definitions) = config.get("definitions") {
        let Some(raw_definitions) = raw_definitions.as_array() else {
            return Err(BlockError::new(
                "tools.resolve.invalid_definitions",
                ErrorCategory::Configuration,
                "tools.resolve@1 config.definitions must be an array",
                false,
            ));
        };
        for (index, definition) in raw_definitions.iter().enumerate() {
            let Some(definition) = definition.as_object() else {
                return Err(BlockError::new(
                    "tools.resolve.invalid_definition",
                    ErrorCategory::Configuration,
                    format!("tools.resolve@1 config.definitions[{index}] must be an object"),
                    false,
                ));
            };
            let name = required_object_str(definition, "name", "tools.resolve.invalid_definition")?;
            let description = definition
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or("");
            let input_schema = definition
                .get("inputSchema")
                .or_else(|| definition.get("input_schema"))
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    BlockError::new(
                        "tools.resolve.invalid_definition",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.definitions[{index}].inputSchema must be a string"
                        ),
                        false,
                    )
                })?;
            let mut parsed = ToolDefinition::new(name, description, input_schema);
            if let Some(output_schema) = definition
                .get("outputSchema")
                .or_else(|| definition.get("output_schema"))
                .filter(|value| !value.is_null())
            {
                let Some(output_schema) = output_schema.as_str() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_definition",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.definitions[{index}].outputSchema must be a string"
                        ),
                        false,
                    ));
                };
                parsed = parsed.with_output_schema(output_schema);
            }
            if let Some(tags) = definition.get("tags") {
                let Some(tags) = tags.as_array() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_definition",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.definitions[{index}].tags must be an array"
                        ),
                        false,
                    ));
                };
                let mut parsed_tags = Vec::new();
                for (tag_index, tag) in tags.iter().enumerate() {
                    let Some(tag) = tag.as_str() else {
                        return Err(BlockError::new(
                            "tools.resolve.invalid_definition",
                            ErrorCategory::Configuration,
                            format!(
                                "tools.resolve@1 config.definitions[{index}].tags[{tag_index}] must be a string"
                            ),
                            false,
                        ));
                    };
                    if tag.trim().is_empty() {
                        return Err(BlockError::new(
                            "tools.resolve.invalid_definition",
                            ErrorCategory::Configuration,
                            format!(
                                "tools.resolve@1 config.definitions[{index}].tags[{tag_index}] must not be empty"
                            ),
                            false,
                        ));
                    }
                    parsed_tags.push(tag.to_owned());
                }
                parsed = parsed.with_tags(parsed_tags);
            }
            if let Some(version) = definition.get("version").filter(|value| !value.is_null()) {
                let Some(version) = version.as_str() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_definition",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.definitions[{index}].version must be a string"
                        ),
                        false,
                    ));
                };
                parsed = parsed.with_version(version);
            }
            definitions.push(parsed);
        }
    }

    let mut bindings = Vec::new();
    if let Some(raw_bindings) = config.get("bindings") {
        let Some(raw_bindings) = raw_bindings.as_array() else {
            return Err(BlockError::new(
                "tools.resolve.invalid_bindings",
                ErrorCategory::Configuration,
                "tools.resolve@1 config.bindings must be an array",
                false,
            ));
        };
        for (index, binding) in raw_bindings.iter().enumerate() {
            let Some(binding) = binding.as_object() else {
                return Err(BlockError::new(
                    "tools.resolve.invalid_binding",
                    ErrorCategory::Configuration,
                    format!("tools.resolve@1 config.bindings[{index}] must be an object"),
                    false,
                ));
            };
            let binding_id = required_alias_object_str(
                binding,
                "bindingId",
                "binding_id",
                "tools.resolve.invalid_binding",
            )?;
            let tool_name = required_alias_object_str(
                binding,
                "toolName",
                "tool_name",
                "tools.resolve.invalid_binding",
            )?;
            let implementation = binding
                .get("implementation")
                .and_then(Value::as_object)
                .ok_or_else(|| {
                    BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].implementation must be an object"
                        ),
                        false,
                    )
                })?;
            let kind = implementation
                .get("kind")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].implementation.kind must be a string"
                        ),
                        false,
                    )
                })?;
            let implementation = match kind {
                "block" => {
                    let mut block = BlockToolImplementation::new(required_object_str(
                        implementation,
                        "block",
                        "tools.resolve.invalid_binding",
                    )?);
                    block.input_mapping = parse_string_map(
                        implementation
                            .get("inputMapping")
                            .or_else(|| implementation.get("input_mapping")),
                        "implementation.inputMapping",
                    )?;
                    block.output_mapping = parse_string_map(
                        implementation
                            .get("outputMapping")
                            .or_else(|| implementation.get("output_mapping")),
                        "implementation.outputMapping",
                    )?;
                    ToolImplementation::Block(block)
                }
                "graph" => {
                    let mut graph = GraphToolImplementation::new(required_object_str(
                        implementation,
                        "graph",
                        "tools.resolve.invalid_binding",
                    )?);
                    graph.input_mapping = parse_string_map(
                        implementation
                            .get("inputMapping")
                            .or_else(|| implementation.get("input_mapping")),
                        "implementation.inputMapping",
                    )?;
                    graph.output_mapping = parse_string_map(
                        implementation
                            .get("outputMapping")
                            .or_else(|| implementation.get("output_mapping")),
                        "implementation.outputMapping",
                    )?;
                    ToolImplementation::Graph(graph)
                }
                "remote" => ToolImplementation::Remote(RemoteToolImplementation::new(
                    required_object_str(
                        implementation,
                        "connection",
                        "tools.resolve.invalid_binding",
                    )?,
                    required_object_str(
                        implementation,
                        "operation",
                        "tools.resolve.invalid_binding",
                    )?,
                )),
                "mcp" => ToolImplementation::Mcp(McpToolImplementation::new(
                    required_object_str(implementation, "server", "tools.resolve.invalid_binding")?,
                    required_alias_object_str(
                        implementation,
                        "remoteName",
                        "remote_name",
                        "tools.resolve.invalid_binding",
                    )?,
                )),
                "openapi" => ToolImplementation::OpenApi(OpenApiToolImplementation::new(
                    required_object_str(
                        implementation,
                        "connection",
                        "tools.resolve.invalid_binding",
                    )?,
                    required_alias_object_str(
                        implementation,
                        "operationId",
                        "operation_id",
                        "tools.resolve.invalid_binding",
                    )?,
                )),
                _ => {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!("tools.resolve@1 unsupported implementation kind {kind:?}"),
                        false,
                    ));
                }
            };
            let mut parsed = ToolBinding::new(binding_id, tool_name, implementation);
            if let Some(effects) = binding.get("effects") {
                parsed = parsed.with_effects(parse_tool_effects(effects)?);
            }
            if let Some(approval) = optional_string(binding, "approval")? {
                parsed = parsed.with_approval(match approval {
                    "never" => ToolApproval::Never,
                    "policy" => ToolApproval::Policy,
                    "always" => ToolApproval::Always,
                    _ => {
                        return Err(BlockError::new(
                            "tools.resolve.invalid_binding",
                            ErrorCategory::Configuration,
                            format!("tools.resolve@1 invalid tool approval {approval:?}"),
                            false,
                        ));
                    }
                });
            }
            if let Some(idempotency) = optional_string(binding, "idempotency")? {
                parsed = parsed.with_idempotency(match idempotency {
                    "not_applicable" => ToolIdempotency::NotApplicable,
                    "optional" => ToolIdempotency::Optional,
                    "required" => ToolIdempotency::Required,
                    _ => {
                        return Err(BlockError::new(
                            "tools.resolve.invalid_binding",
                            ErrorCategory::Configuration,
                            format!("tools.resolve@1 invalid tool idempotency {idempotency:?}"),
                            false,
                        ));
                    }
                });
            }
            if let Some(cancellation) = optional_string(binding, "cancellation")? {
                parsed = parsed.with_cancellation(match cancellation {
                    "unsupported" => ToolCancellation::Unsupported,
                    "cooperative" => ToolCancellation::Cooperative,
                    "force_terminable" => ToolCancellation::ForceTerminable,
                    _ => {
                        return Err(BlockError::new(
                            "tools.resolve.invalid_binding",
                            ErrorCategory::Configuration,
                            format!("tools.resolve@1 invalid tool cancellation {cancellation:?}"),
                            false,
                        ));
                    }
                });
            }
            if let Some(result_mode) = optional_alias_string(binding, "resultMode", "result_mode")?
            {
                parsed = parsed.with_result_mode(match result_mode {
                    "value" => ToolResultMode::Value,
                    "incremental" => ToolResultMode::Incremental,
                    "bounded_sequence" => ToolResultMode::BoundedSequence,
                    "artifact_reference" => ToolResultMode::ArtifactReference,
                    _ => {
                        return Err(BlockError::new(
                            "tools.resolve.invalid_binding",
                            ErrorCategory::Configuration,
                            format!("tools.resolve@1 invalid tool result mode {result_mode:?}"),
                            false,
                        ));
                    }
                });
            }
            if let Some(timeout_ms) = binding
                .get("timeoutMs")
                .or_else(|| binding.get("timeout_ms"))
                .filter(|value| !value.is_null())
            {
                let Some(timeout_ms) = timeout_ms.as_u64() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].timeoutMs must be an unsigned integer"
                        ),
                        false,
                    ));
                };
                parsed = parsed.with_timeout_ms(timeout_ms);
            }
            if let Some(retry_policy_ref) =
                optional_alias_string(binding, "retryPolicyRef", "retry_policy_ref")?
            {
                parsed.retry_policy_ref = Some(retry_policy_ref.to_owned());
            }
            if let Some(policy_profile_ref) =
                optional_alias_string(binding, "policyProfileRef", "policy_profile_ref")?
            {
                parsed.policy_profile_ref = Some(policy_profile_ref.to_owned());
            }
            if let Some(execution_class) =
                optional_alias_string(binding, "executionClass", "execution_class")?
            {
                parsed.execution_class = Some(execution_class.to_owned());
            }
            bindings.push(parsed);
        }
    }

    let mut scope = ToolResolutionScope::new();
    if let Some(raw_scope) = config.get("scope") {
        let Some(raw_scope) = raw_scope.as_object() else {
            return Err(BlockError::new(
                "tools.resolve.invalid_scope",
                ErrorCategory::Configuration,
                "tools.resolve@1 config.scope must be an object",
                false,
            ));
        };
        if let Some(tools) =
            parse_tool_name_list(raw_scope, "applicationTools", "application_tools")?
        {
            scope = scope.with_application_tools(tools);
        }
        if let Some(tools) = parse_tool_name_list(raw_scope, "graphTools", "graph_tools")? {
            scope = scope.with_graph_tools(tools);
        }
        if let Some(tools) = parse_tool_name_list(raw_scope, "principalTools", "principal_tools")? {
            scope = scope.with_principal_tools(tools);
        }
        if let Some(tools) =
            parse_tool_name_list(raw_scope, "tenantPolicyTools", "tenant_policy_tools")?
        {
            scope = scope.with_tenant_policy_tools(tools);
        }
        if let Some(tools) = parse_tool_name_list(
            raw_scope,
            "conversationPolicyTools",
            "conversation_policy_tools",
        )? {
            scope = scope.with_conversation_policy_tools(tools);
        }
        if let Some(tools) = parse_tool_name_list(
            raw_scope,
            "dataClassificationTools",
            "data_classification_tools",
        )? {
            scope = scope.with_data_classification_tools(tools);
        }
        if let Some(tools) = parse_tool_name_list(raw_scope, "deploymentTools", "deployment_tools")?
        {
            scope = scope.with_deployment_tools(tools);
        }
        if let Some(tools) = parse_tool_name_list(raw_scope, "budgetTools", "budget_tools")? {
            scope = scope.with_budget_tools(tools);
        }
    }

    let mut effective_policy_snapshot_id = config
        .get("effectivePolicySnapshotId")
        .or_else(|| config.get("effective_policy_snapshot_id"))
        .and_then(Value::as_str)
        .unwrap_or("policy-snapshot-local")
        .to_owned();
    if let Some(policy_snapshot) = inputs.get("policySnapshot").and_then(Value::as_object)
        && let Some(snapshot_id) = policy_snapshot
            .get("snapshotId")
            .or_else(|| policy_snapshot.get("snapshot_id"))
            .and_then(Value::as_str)
    {
        effective_policy_snapshot_id = snapshot_id.to_owned();
    }

    let catalog = ToolCatalog::new(definitions, bindings).map_err(|error| {
        BlockError::new(
            "tools.resolve.catalog_error",
            ErrorCategory::Configuration,
            format!("tools.resolve@1 catalog error: {error:?}"),
            false,
        )
    })?;
    let resolved = catalog
        .resolve(scope, effective_policy_snapshot_id)
        .map_err(|error| {
            BlockError::new(
                "tools.resolve.resolution_error",
                ErrorCategory::Policy,
                format!("tools.resolve@1 resolution error: {error:?}"),
                false,
            )
        })?;
    let tools = resolved.iter().map(resolved_tool_json).collect::<Vec<_>>();
    Ok(json!({ "tools": tools }))
}

fn execute_scripted_agent_run(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let tools = match inputs.get("tools") {
        Some(Value::Array(tools)) => tools.as_slice(),
        Some(_) => {
            return Err(BlockError::new(
                "agent.run.invalid_tools",
                ErrorCategory::Configuration,
                "agent.run@1 input 'tools' must be a list",
                false,
            ));
        }
        None => &[],
    };
    let mut model_visible_tools = Vec::new();
    for (index, tool) in tools.iter().enumerate() {
        let Some(tool) = tool.as_object() else {
            return Err(BlockError::new(
                "agent.run.invalid_tools",
                ErrorCategory::Configuration,
                format!("agent.run@1 input 'tools[{index}]' must be an object"),
                false,
            ));
        };
        let Some(definition) = tool.get("definition").and_then(Value::as_object) else {
            return Err(BlockError::new(
                "agent.run.invalid_tool",
                ErrorCategory::Configuration,
                format!("agent.run@1 input 'tools[{index}].definition' must be an object"),
                false,
            ));
        };
        let tool_name = required_object_str(definition, "name", "agent.run.invalid_tool")
            .and_then(|value| {
                if value.trim().is_empty() {
                    Err(BlockError::new(
                        "agent.run.invalid_tool",
                        ErrorCategory::Configuration,
                        format!(
                            "agent.run@1 input 'tools[{index}].definition.name' must not be empty"
                        ),
                        false,
                    ))
                } else {
                    Ok(value)
                }
            })?;
        let resolved_tool_id = required_alias_object_str(
            tool,
            "resolved_tool_id",
            "resolvedToolId",
            "agent.run.invalid_tool",
        )
        .and_then(|value| {
            if value.trim().is_empty() {
                Err(BlockError::new(
                    "agent.run.invalid_tool",
                    ErrorCategory::Configuration,
                    format!(
                        "agent.run@1 input 'tools[{index}].resolved_tool_id' must not be empty"
                    ),
                    false,
                ))
            } else {
                Ok(value)
            }
        })?;
        let definition_digest = required_alias_object_str(
            tool,
            "definition_digest",
            "definitionDigest",
            "agent.run.invalid_tool",
        )
        .and_then(|value| {
            if value.trim().is_empty() {
                Err(BlockError::new(
                    "agent.run.invalid_tool",
                    ErrorCategory::Configuration,
                    format!(
                        "agent.run@1 input 'tools[{index}].definition_digest' must not be empty"
                    ),
                    false,
                ))
            } else {
                Ok(value)
            }
        })?;
        let binding_digest = required_alias_object_str(
            tool,
            "binding_digest",
            "bindingDigest",
            "agent.run.invalid_tool",
        )
        .and_then(|value| {
            if value.trim().is_empty() {
                Err(BlockError::new(
                    "agent.run.invalid_tool",
                    ErrorCategory::Configuration,
                    format!("agent.run@1 input 'tools[{index}].binding_digest' must not be empty"),
                    false,
                ))
            } else {
                Ok(value)
            }
        })?;
        let effective_policy_snapshot_id = required_alias_object_str(
            tool,
            "effective_policy_snapshot_id",
            "effectivePolicySnapshotId",
            "agent.run.invalid_tool",
        )
        .and_then(|value| {
            if value.trim().is_empty() {
                Err(BlockError::new(
                    "agent.run.invalid_tool",
                    ErrorCategory::Configuration,
                    format!(
                        "agent.run@1 input 'tools[{index}].effective_policy_snapshot_id' must not be empty"
                    ),
                    false,
                ))
            } else {
                Ok(value)
            }
        })?;
        let allowed_for_principal = tool
            .get("allowed_for_principal")
            .or_else(|| tool.get("allowedForPrincipal"))
            .and_then(Value::as_bool)
            .ok_or_else(|| {
                BlockError::new(
                    "agent.run.invalid_tool",
                    ErrorCategory::Configuration,
                    format!(
                        "agent.run@1 input 'tools[{index}].allowed_for_principal' must be a boolean"
                    ),
                    false,
                )
            })?;
        if !allowed_for_principal {
            return Err(BlockError::new(
                "agent.run.tool_not_allowed",
                ErrorCategory::Policy,
                format!("agent.run@1 input 'tools[{index}]' is not allowed for principal"),
                false,
            ));
        }
        model_visible_tools.push(json!({
            "toolName": tool_name,
            "resolvedToolId": resolved_tool_id,
            "definitionDigest": definition_digest,
            "bindingDigest": binding_digest,
            "effectivePolicySnapshotId": effective_policy_snapshot_id,
            "allowedForPrincipal": allowed_for_principal,
            "validUntil": tool
                .get("valid_until")
                .or_else(|| tool.get("validUntil"))
                .cloned()
                .unwrap_or(Value::Null),
        }));
    }
    model_visible_tools.sort_by(|left, right| {
        let left_key = (
            left.get("toolName").and_then(Value::as_str).unwrap_or(""),
            left.get("resolvedToolId")
                .and_then(Value::as_str)
                .unwrap_or(""),
        );
        let right_key = (
            right.get("toolName").and_then(Value::as_str).unwrap_or(""),
            right
                .get("resolvedToolId")
                .and_then(Value::as_str)
                .unwrap_or(""),
        );
        left_key.cmp(&right_key)
    });
    let messages = match inputs.get("messages") {
        Some(Value::Array(messages)) => messages.as_slice(),
        Some(_) => {
            return Err(BlockError::new(
                "agent.run.invalid_messages",
                ErrorCategory::Configuration,
                "agent.run@1 input 'messages' must be a list",
                false,
            ));
        }
        None => &[],
    };

    let (text, finish_reason) = if let Some(response) = config.get("response") {
        (json_display(response), "scripted")
    } else if let Some(message) = messages.last() {
        let text = message
            .as_object()
            .and_then(|message| message.get("content").or_else(|| message.get("text")))
            .map(json_display)
            .unwrap_or_else(|| json_display(message));
        (text, "echo")
    } else {
        (String::new(), "empty")
    };
    let output_policy_profile_ref = config
        .get("outputPolicy")
        .or_else(|| config.get("output_policy"))
        .and_then(Value::as_object)
        .and_then(|output_policy| {
            output_policy
                .get("profileRef")
                .or_else(|| output_policy.get("profile_ref"))
        })
        .and_then(Value::as_str)
        .filter(|profile_ref| !profile_ref.trim().is_empty());

    let mut candidate = json!({
        "text": text,
        "finishReason": finish_reason,
        "toolCount": tools.len(),
        "modelVisibleTools": model_visible_tools,
    });
    if let Some(output_policy_profile_ref) = output_policy_profile_ref
        && let Some(candidate) = candidate.as_object_mut()
    {
        candidate.insert(
            "outputPolicyProfileRef".to_owned(),
            json!(output_policy_profile_ref),
        );
    }

    Ok(json!({ "candidate": candidate }))
}

fn execute_control_map(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let Some(items) = inputs.get("items").and_then(Value::as_array) else {
        return Err(BlockError::new(
            "control.map.invalid_items",
            ErrorCategory::Configuration,
            "control.map@2 input 'items' must be a list",
            false,
        ));
    };
    let Some(block_id) = config.get("block").and_then(Value::as_str) else {
        return Err(BlockError::new(
            "control.map.missing_block",
            ErrorCategory::Configuration,
            "control.map@2 config.block must be a string",
            false,
        ));
    };
    let input_name = config
        .get("inputName")
        .map(json_display)
        .unwrap_or_else(|| "item".to_owned());
    let output_name = config.get("outputName").map(json_display);
    let block_config = config.get("config").cloned().unwrap_or_else(|| json!({}));
    if !block_config.is_object() {
        return Err(BlockError::new(
            "control.map.invalid_config",
            ErrorCategory::Configuration,
            "control.map@2 config.config must be a mapping",
            false,
        ));
    }
    let collect_errors = config.get("onError").and_then(Value::as_str) == Some("collect");
    let mut values = Vec::new();
    let mut outcomes = Vec::new();

    for (index, item) in items.iter().enumerate() {
        let item_result = (|| {
            let mut mapped_inputs = serde_json::Map::new();
            mapped_inputs.insert(input_name.clone(), item.clone());
            let result =
                execute_stdlib_block(block_id, &Value::Object(mapped_inputs), &block_config)?;
            let Some(result_object) = result.as_object() else {
                return Err(BlockError::new(
                    "control.map.invalid_mapped_outputs",
                    ErrorCategory::Internal,
                    "mapped block returned non-mapping output",
                    false,
                ));
            };
            let value = if let Some(output_name) = &output_name {
                result_object.get(output_name).cloned().ok_or_else(|| {
                    BlockError::new(
                        format!("control.map.missing_output.{output_name}"),
                        ErrorCategory::Configuration,
                        "mapped block output is missing",
                        false,
                    )
                })?
            } else {
                result
            };
            Ok(value)
        })();

        match item_result {
            Ok(value) => {
                values.push(value.clone());
                outcomes.push(json!({"status": "succeeded", "value": value}));
            }
            Err(error) => {
                if !collect_errors {
                    return Err(error);
                }
                outcomes.push(json!({
                    "status": "failed",
                    "error": format!("map item {index} failed: {error:?}"),
                }));
            }
        }
    }

    if collect_errors {
        Ok(json!({"outcomes": outcomes, "values": values}))
    } else {
        Ok(json!({"values": values}))
    }
}

fn execute_control_select(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let Some(cases) = inputs.get("cases").and_then(Value::as_object) else {
        return Err(BlockError::new(
            "control.select.invalid_cases",
            ErrorCategory::Configuration,
            "control.select@1 input 'cases' must be a mapping",
            false,
        ));
    };
    let order = if let Some(order) = config.get("order") {
        let Some(order) = order.as_array() else {
            return Err(BlockError::new(
                "control.select.invalid_order",
                ErrorCategory::Configuration,
                "control.select@1 config.order must be a list",
                false,
            ));
        };
        order.iter().map(json_display).collect::<Vec<_>>()
    } else {
        cases.keys().cloned().collect::<Vec<_>>()
    };

    for key in order {
        if let Some(value) = cases.get(&key) {
            return Ok(json!({"value": value, "selected": key}));
        }
    }
    if let Some(default) = config.get("default") {
        return Ok(json!({"value": default, "selected": "default"}));
    }

    Err(BlockError::new(
        "control.select.missing_case",
        ErrorCategory::Configuration,
        "control.select@1 found no present case",
        false,
    ))
}

fn execute_commit_turn(inputs: &Value) -> Result<Value, BlockError> {
    let transaction = inputs
        .get("transaction")
        .or_else(|| inputs.get("turn"))
        .and_then(Value::as_object);
    if transaction
        .and_then(|transaction| transaction.get("status"))
        .and_then(Value::as_str)
        == Some("policy_stopped")
    {
        return Err(BlockError::new(
            "conversation.commit_turn.policy_stopped",
            ErrorCategory::Policy,
            "conversation.commit_turn@1 cannot commit policy-stopped turn",
            false,
        ));
    }
    let candidate = inputs.get("candidate").or_else(|| inputs.get("response"));
    let result = candidate.cloned().unwrap_or(Value::Null);
    let text = candidate
        .unwrap_or(&Value::Null)
        .get("text")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| candidate.map_or_else(String::new, json_display));

    let answer = json!({
        "conversationId": transaction
            .and_then(|transaction| transaction
            .get("conversationId")
            .or_else(|| transaction.get("conversation_id")))
            .and_then(Value::as_str)
            .unwrap_or("conversation-default"),
        "text": text,
        "turnId": transaction
            .and_then(|transaction| transaction
            .get("turnId")
            .or_else(|| transaction.get("turn_id")))
            .and_then(Value::as_str)
            .unwrap_or("turn-000001"),
    });
    Ok(json!({ "answer": answer, "result": result }))
}

fn execute_policy_stop_turn(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let Some(transaction) = inputs.get("transaction").and_then(Value::as_object) else {
        return Err(BlockError::new(
            "conversation.policy_stop_turn.missing_transaction",
            ErrorCategory::Configuration,
            "conversation.policy_stop_turn@1 requires transaction input",
            false,
        ));
    };
    let conversation_id = transaction
        .get("conversationId")
        .and_then(Value::as_str)
        .unwrap_or("conversation-default");
    let turn_id = transaction
        .get("turnId")
        .and_then(Value::as_str)
        .unwrap_or("turn-000001");
    let draft_disposition = config
        .get("draftDisposition")
        .and_then(Value::as_str)
        .unwrap_or("retract");
    let stopped = json!({
        "conversationId": conversation_id,
        "turnId": turn_id,
        "status": "policy_stopped",
        "draftDisposition": draft_disposition,
        "committedMessageIds": [],
    });

    Ok(json!({
        "transaction": stopped,
        "turn": stopped,
    }))
}

fn required_object_str<'a>(
    object: &'a serde_json::Map<String, Value>,
    field: &str,
    code: impl Into<String>,
) -> Result<&'a str, BlockError> {
    object.get(field).and_then(Value::as_str).ok_or_else(|| {
        BlockError::new(
            code.into(),
            ErrorCategory::Configuration,
            format!("field {field} must be a string"),
            false,
        )
    })
}

fn required_alias_object_str<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    code: impl Into<String>,
) -> Result<&'a str, BlockError> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .and_then(Value::as_str)
        .ok_or_else(|| {
            BlockError::new(
                code.into(),
                ErrorCategory::Configuration,
                format!("field {primary} must be a string"),
                false,
            )
        })
}

fn required_alias_object_u64(
    object: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    code: impl Into<String>,
) -> Result<u64, BlockError> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            BlockError::new(
                code.into(),
                ErrorCategory::Configuration,
                format!("field {primary} must be an unsigned integer"),
                false,
            )
        })
}

fn optional_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    field: &str,
) -> Result<Option<&'a str>, BlockError> {
    object
        .get(field)
        .filter(|value| !value.is_null())
        .map(|value| {
            value.as_str().ok_or_else(|| {
                BlockError::new(
                    "tools.resolve.invalid_binding",
                    ErrorCategory::Configuration,
                    format!("field {field} must be a string"),
                    false,
                )
            })
        })
        .transpose()
}

fn optional_alias_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
) -> Result<Option<&'a str>, BlockError> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .filter(|value| !value.is_null())
        .map(|value| {
            value.as_str().ok_or_else(|| {
                BlockError::new(
                    "tools.resolve.invalid_binding",
                    ErrorCategory::Configuration,
                    format!("field {primary} must be a string"),
                    false,
                )
            })
        })
        .transpose()
}

fn optional_alias_u64(
    object: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
) -> Result<Option<u64>, BlockError> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .filter(|value| !value.is_null())
        .map(|value| {
            value.as_u64().ok_or_else(|| {
                BlockError::new(
                    "async.start_operation.invalid_config",
                    ErrorCategory::Configuration,
                    format!("field {primary} must be an unsigned integer"),
                    false,
                )
            })
        })
        .transpose()
}

fn optional_alias_duration_ms(
    object: &serde_json::Map<String, Value>,
    fields: &[&str],
    code: impl Into<String>,
    message: &'static str,
) -> Result<Option<u64>, BlockError> {
    let code = code.into();
    let Some(value) = fields
        .iter()
        .find_map(|field| object.get(*field).filter(|value| !value.is_null()))
    else {
        return Ok(None);
    };
    parse_duration_milliseconds(value)
        .map(Some)
        .ok_or_else(|| BlockError::new(code, ErrorCategory::Configuration, message, false))
}

fn optional_infinite_wait_policy<'a>(
    object: &'a serde_json::Map<String, Value>,
    code: &'static str,
    block_label: &'static str,
) -> Result<Option<&'a str>, BlockError> {
    object
        .get("infiniteWaitPolicy")
        .or_else(|| object.get("infinite_wait_policy"))
        .filter(|value| !value.is_null())
        .map(|value| {
            value
                .as_str()
                .filter(|text| !text.trim().is_empty())
                .ok_or_else(|| {
                    BlockError::new(
                        code,
                        ErrorCategory::Configuration,
                        format!("{block_label} infiniteWaitPolicy must be a non-empty string"),
                        false,
                    )
                })
        })
        .transpose()
}

fn parse_string_map(
    value: Option<&Value>,
    label: &str,
) -> Result<BTreeMap<String, String>, BlockError> {
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return Ok(BTreeMap::new());
    };
    let Some(value) = value.as_object() else {
        return Err(BlockError::new(
            "tools.resolve.invalid_binding",
            ErrorCategory::Configuration,
            format!("tools.resolve@1 {label} must be an object"),
            false,
        ));
    };
    let mut parsed = BTreeMap::new();
    for (entry_key, entry_value) in value {
        if entry_key.trim().is_empty() {
            return Err(BlockError::new(
                "tools.resolve.invalid_binding",
                ErrorCategory::Configuration,
                format!("tools.resolve@1 {label} keys must not be empty"),
                false,
            ));
        }
        let Some(entry_value) = entry_value.as_str() else {
            return Err(BlockError::new(
                "tools.resolve.invalid_binding",
                ErrorCategory::Configuration,
                format!("tools.resolve@1 {label}.{entry_key} must be a string"),
                false,
            ));
        };
        if entry_value.trim().is_empty() {
            return Err(BlockError::new(
                "tools.resolve.invalid_binding",
                ErrorCategory::Configuration,
                format!("tools.resolve@1 {label}.{entry_key} must not be empty"),
                false,
            ));
        }
        parsed.insert(entry_key.clone(), entry_value.to_owned());
    }
    Ok(parsed)
}

fn parse_tool_effects(value: &Value) -> Result<BTreeSet<ToolEffect>, BlockError> {
    let Some(effects) = value.as_array() else {
        return Err(BlockError::new(
            "tools.resolve.invalid_binding",
            ErrorCategory::Configuration,
            "tools.resolve@1 effects must be an array",
            false,
        ));
    };
    let mut parsed_effects = BTreeSet::new();
    for effect in effects {
        let Some(effect) = effect.as_str() else {
            return Err(BlockError::new(
                "tools.resolve.invalid_binding",
                ErrorCategory::Configuration,
                "tools.resolve@1 effect entries must be strings",
                false,
            ));
        };
        parsed_effects.insert(match effect {
            "none" => ToolEffect::None,
            "external_read" => ToolEffect::ExternalRead,
            "external_write" => ToolEffect::ExternalWrite,
            "filesystem_read" => ToolEffect::FilesystemRead,
            "filesystem_write" => ToolEffect::FilesystemWrite,
            "process" => ToolEffect::Process,
            "network" => ToolEffect::Network,
            "destructive" => ToolEffect::Destructive,
            _ => {
                return Err(BlockError::new(
                    "tools.resolve.invalid_binding",
                    ErrorCategory::Configuration,
                    format!("tools.resolve@1 invalid tool effect {effect:?}"),
                    false,
                ));
            }
        });
    }
    Ok(parsed_effects)
}

fn parse_tool_name_list(
    raw_scope: &serde_json::Map<String, Value>,
    camel_key: &str,
    snake_key: &str,
) -> Result<Option<Vec<String>>, BlockError> {
    let Some(value) = raw_scope
        .get(camel_key)
        .or_else(|| raw_scope.get(snake_key))
    else {
        return Ok(None);
    };
    let Some(value) = value.as_array() else {
        return Err(BlockError::new(
            "tools.resolve.invalid_scope",
            ErrorCategory::Configuration,
            format!("tools.resolve@1 config.scope.{camel_key} must be an array"),
            false,
        ));
    };
    let mut names = Vec::new();
    for (index, item) in value.iter().enumerate() {
        let Some(item) = item.as_str() else {
            return Err(BlockError::new(
                "tools.resolve.invalid_scope",
                ErrorCategory::Configuration,
                format!("tools.resolve@1 config.scope.{camel_key}[{index}] must be a string"),
                false,
            ));
        };
        if item.trim().is_empty() {
            return Err(BlockError::new(
                "tools.resolve.invalid_scope",
                ErrorCategory::Configuration,
                format!("tools.resolve@1 config.scope.{camel_key}[{index}] must not be empty"),
                false,
            ));
        }
        names.push(item.to_owned());
    }
    Ok(Some(names))
}

fn resolved_tool_json(tool: &ResolvedTool) -> Value {
    let implementation = match &tool.binding.implementation {
        ToolImplementation::Block(implementation) => json!({
            "kind": "block",
            "block": implementation.block,
            "input_mapping": implementation.input_mapping,
            "output_mapping": implementation.output_mapping,
        }),
        ToolImplementation::Graph(implementation) => json!({
            "kind": "graph",
            "graph": implementation.graph,
            "input_mapping": implementation.input_mapping,
            "output_mapping": implementation.output_mapping,
        }),
        ToolImplementation::Remote(implementation) => json!({
            "kind": "remote",
            "connection": implementation.connection,
            "operation": implementation.operation,
        }),
        ToolImplementation::Mcp(implementation) => json!({
            "kind": "mcp",
            "server": implementation.server,
            "remote_name": implementation.remote_name,
        }),
        ToolImplementation::OpenApi(implementation) => json!({
            "kind": "openapi",
            "connection": implementation.connection,
            "operation_id": implementation.operation_id,
        }),
    };
    let approval = match tool.binding.approval {
        ToolApproval::Never => "never",
        ToolApproval::Policy => "policy",
        ToolApproval::Always => "always",
    };
    let idempotency = match tool.binding.idempotency {
        ToolIdempotency::NotApplicable => "not_applicable",
        ToolIdempotency::Optional => "optional",
        ToolIdempotency::Required => "required",
    };
    let cancellation = match tool.binding.cancellation {
        ToolCancellation::Unsupported => "unsupported",
        ToolCancellation::Cooperative => "cooperative",
        ToolCancellation::ForceTerminable => "force_terminable",
    };
    let result_mode = match tool.binding.result_mode {
        ToolResultMode::Value => "value",
        ToolResultMode::Incremental => "incremental",
        ToolResultMode::BoundedSequence => "bounded_sequence",
        ToolResultMode::ArtifactReference => "artifact_reference",
    };

    json!({
        "resolved_tool_id": tool.resolved_tool_id,
        "definition": {
            "name": tool.definition.name,
            "description": tool.definition.description,
            "input_schema": tool.definition.input_schema,
            "output_schema": tool.definition.output_schema,
            "tags": tool.definition.tags.iter().collect::<Vec<_>>(),
            "version": tool.definition.version,
        },
        "binding": {
            "binding_id": tool.binding.binding_id,
            "tool_name": tool.binding.tool_name,
            "implementation": implementation,
            "effects": tool.binding.effects.iter().map(|effect| effect.as_str()).collect::<Vec<_>>(),
            "approval": approval,
            "idempotency": idempotency,
            "cancellation": cancellation,
            "result_mode": result_mode,
            "timeout_ms": tool.binding.timeout_ms,
            "retry_policy_ref": tool.binding.retry_policy_ref,
            "policy_profile_ref": tool.binding.policy_profile_ref,
            "execution_class": tool.binding.execution_class,
        },
        "definition_digest": tool.definition_digest,
        "binding_digest": tool.binding_digest,
        "effective_policy_snapshot_id": tool.effective_policy_snapshot_id,
        "allowed_for_principal": tool.allowed_for_principal,
        "valid_until": tool.valid_until_unix_ms,
    })
}

fn parse_async_operation_kind(kind: &str) -> Result<AsyncOperationKind, BlockError> {
    match kind {
        "tool" => Ok(AsyncOperationKind::Tool),
        "sandbox_task" => Ok(AsyncOperationKind::SandboxTask),
        "ci_job" => Ok(AsyncOperationKind::CiJob),
        "browser_task" => Ok(AsyncOperationKind::BrowserTask),
        "workspace_trial" => Ok(AsyncOperationKind::WorkspaceTrial),
        "external_provider_job" => Ok(AsyncOperationKind::ExternalProviderJob),
        "document_job" => Ok(AsyncOperationKind::DocumentJob),
        "research_task" => Ok(AsyncOperationKind::ResearchTask),
        "custom" => Ok(AsyncOperationKind::Custom),
        _ => Err(BlockError::new(
            "async.start_operation.invalid_config",
            ErrorCategory::Configuration,
            format!("async.start_operation@1 unsupported operation kind {kind:?}"),
            false,
        )),
    }
}

fn async_operation_kind_as_str(kind: &AsyncOperationKind) -> &'static str {
    match kind {
        AsyncOperationKind::Tool => "tool",
        AsyncOperationKind::SandboxTask => "sandbox_task",
        AsyncOperationKind::CiJob => "ci_job",
        AsyncOperationKind::BrowserTask => "browser_task",
        AsyncOperationKind::WorkspaceTrial => "workspace_trial",
        AsyncOperationKind::ExternalProviderJob => "external_provider_job",
        AsyncOperationKind::DocumentJob => "document_job",
        AsyncOperationKind::ResearchTask => "research_task",
        AsyncOperationKind::Custom => "custom",
    }
}

fn async_operation_json(operation: &AsyncOperation, subject: Option<Value>) -> Value {
    let state = match operation.state {
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
    };
    json!({
        "operation_id": operation.operation_id,
        "run_id": operation.run_id,
        "node_id": operation.node_id,
        "attempt_id": operation.attempt_id,
        "kind": async_operation_kind_as_str(&operation.kind),
        "provider_operation_id": operation.provider_operation_id,
        "state": state,
        "resume_token_hash": operation.resume_token_hash,
        "idempotency_key": operation.idempotency_key,
        "expected_schema": operation.expected_schema,
        "created_at_unix_ms": operation.created_at_unix_ms,
        "submitted_at_unix_ms": operation.submitted_at_unix_ms,
        "expires_at_unix_ms": operation.expires_at_unix_ms,
        "infinite_wait_policy": operation.infinite_wait_policy,
        "completed_at_unix_ms": operation.completed_at_unix_ms,
        "subject": subject,
    })
}

fn required_async_operation_input<'a>(
    inputs: &'a Value,
    block_label: &str,
) -> Result<&'a Value, BlockError> {
    let operation = inputs.get("operation").ok_or_else(|| {
        BlockError::new(
            format!("{block_label}.missing_operation"),
            ErrorCategory::Configuration,
            format!("{block_label} requires operation input"),
            false,
        )
    })?;
    if !operation.is_object() {
        return Err(BlockError::new(
            format!("{block_label}.invalid_operation"),
            ErrorCategory::Configuration,
            format!("{block_label} input operation must be an object"),
            false,
        ));
    }
    let operation_object = operation
        .as_object()
        .expect("operation object was checked above");
    for (primary, alternate, label) in [
        ("operation_id", "operationId", "operation_id"),
        ("run_id", "runId", "run_id"),
        ("node_id", "nodeId", "node_id"),
        ("attempt_id", "attemptId", "attempt_id"),
        ("kind", "kind", "kind"),
        ("state", "state", "state"),
        ("resume_token_hash", "resumeTokenHash", "resume_token_hash"),
        ("idempotency_key", "idempotencyKey", "idempotency_key"),
        ("expected_schema", "expectedSchema", "expected_schema"),
    ] {
        if operation_object
            .get(primary)
            .or_else(|| operation_object.get(alternate))
            .and_then(Value::as_str)
            .is_none_or(|value| value.trim().is_empty())
        {
            return Err(BlockError::new(
                format!("{block_label}.invalid_operation"),
                ErrorCategory::Configuration,
                format!("{block_label} input operation.{label} must be a non-empty string"),
                false,
            ));
        }
    }
    Ok(operation)
}

fn required_async_operation_id<'a>(
    operation: &'a Value,
    block_label: &str,
) -> Result<&'a str, BlockError> {
    operation
        .get("operation_id")
        .or_else(|| operation.get("operationId"))
        .and_then(Value::as_str)
        .filter(|operation_id| !operation_id.trim().is_empty())
        .ok_or_else(|| {
            BlockError::new(
                format!("{block_label}.invalid_operation"),
                ErrorCategory::Configuration,
                format!("{block_label} input operation.operation_id must be a non-empty string"),
                false,
            )
        })
}

fn parse_async_external_effects(
    config: &serde_json::Map<String, Value>,
    block_label: &str,
) -> Result<Vec<ExternalEffectRecord>, BlockError> {
    let Some(raw_effects) = config
        .get("externalEffects")
        .or_else(|| config.get("external_effects"))
    else {
        return Ok(Vec::new());
    };
    let Some(raw_effects) = raw_effects.as_array() else {
        return Err(BlockError::new(
            format!("{block_label}.invalid_config"),
            ErrorCategory::Configuration,
            format!("{block_label} config.externalEffects must be an array"),
            false,
        ));
    };
    let mut effects = Vec::new();
    for (index, effect) in raw_effects.iter().enumerate() {
        let Some(effect) = effect.as_object() else {
            return Err(BlockError::new(
                format!("{block_label}.invalid_config"),
                ErrorCategory::Configuration,
                format!("{block_label} config.externalEffects[{index}] must be an object"),
                false,
            ));
        };
        let mut parsed = ExternalEffectRecord::new(
            required_alias_object_str(
                effect,
                "effectId",
                "effect_id",
                format!("{block_label}.invalid_config"),
            )?,
            required_object_str(effect, "target", format!("{block_label}.invalid_config"))?,
            required_object_str(effect, "operation", format!("{block_label}.invalid_config"))?,
            parse_tool_effect_outcome(required_object_str(
                effect,
                "outcome",
                format!("{block_label}.invalid_config"),
            )?)
            .map_err(|outcome| {
                BlockError::new(
                    format!("{block_label}.invalid_config"),
                    ErrorCategory::Configuration,
                    format!(
                        "{block_label} config.externalEffects[{index}].outcome unsupported value {outcome:?}"
                    ),
                    false,
                )
            })?,
        );
        if let Some(idempotency_key) =
            optional_alias_string(effect, "idempotencyKey", "idempotency_key")?
        {
            parsed = parsed.with_idempotency_key(idempotency_key);
        }
        if let Some(provider_effect_id) =
            optional_alias_string(effect, "providerEffectId", "provider_effect_id")?
        {
            parsed = parsed.with_provider_effect_id(provider_effect_id);
        }
        effects.push(parsed);
    }
    Ok(effects)
}

fn parse_tool_effect_outcome(outcome: &str) -> Result<ToolEffectOutcome, &str> {
    match outcome {
        "no_external_effect" => Ok(ToolEffectOutcome::NoExternalEffect),
        "committed" => Ok(ToolEffectOutcome::Committed),
        "not_committed" => Ok(ToolEffectOutcome::NotCommitted),
        "unknown" => Ok(ToolEffectOutcome::Unknown),
        _ => Err(outcome),
    }
}

fn apply_async_result_projections(
    config: &serde_json::Map<String, Value>,
    block_label: &str,
    result: &mut AsyncOperationResult,
) -> Result<(), BlockError> {
    result.artifacts = parse_async_result_artifacts(config, block_label)?;
    result.diagnostics = parse_async_result_projection(config, "diagnostics", block_label)?;
    result.metrics = parse_async_result_projection(config, "metrics", block_label)?;
    result.checks = parse_async_result_projection(config, "checks", block_label)?;
    result.usage = parse_async_result_projection(config, "usage", block_label)?;
    Ok(())
}

fn parse_async_result_projection(
    config: &serde_json::Map<String, Value>,
    field: &str,
    block_label: &str,
) -> Result<Vec<Value>, BlockError> {
    let Some(raw_items) = config.get(field) else {
        return Ok(Vec::new());
    };
    if raw_items.is_null() {
        return Ok(Vec::new());
    }
    let Some(raw_items) = raw_items.as_array() else {
        return Err(BlockError::new(
            format!("{}.invalid_config", block_label.trim_end_matches("@1")),
            ErrorCategory::Configuration,
            format!("{block_label} config.{field} must be an array"),
            false,
        ));
    };
    for (index, raw_item) in raw_items.iter().enumerate() {
        if !raw_item.is_object() {
            return Err(BlockError::new(
                format!("{}.invalid_config", block_label.trim_end_matches("@1")),
                ErrorCategory::Configuration,
                format!("{block_label} config.{field}[{index}] must be an object"),
                false,
            ));
        }
    }
    Ok(raw_items.clone())
}

fn parse_async_result_artifacts(
    config: &serde_json::Map<String, Value>,
    block_label: &str,
) -> Result<Vec<CallbackArtifactRef>, BlockError> {
    let Some(raw_items) = config.get("artifacts") else {
        return Ok(Vec::new());
    };
    if raw_items.is_null() {
        return Ok(Vec::new());
    }
    let Some(raw_items) = raw_items.as_array() else {
        return Err(BlockError::new(
            format!("{}.invalid_config", block_label.trim_end_matches("@1")),
            ErrorCategory::Configuration,
            format!("{block_label} config.artifacts must be an array"),
            false,
        ));
    };
    let mut artifacts = Vec::with_capacity(raw_items.len());
    for (index, raw_item) in raw_items.iter().enumerate() {
        let Some(raw_item) = raw_item.as_object() else {
            return Err(BlockError::new(
                format!("{}.invalid_config", block_label.trim_end_matches("@1")),
                ErrorCategory::Configuration,
                format!("{block_label} config.artifacts[{index}] must be an object"),
                false,
            ));
        };
        let artifact_id =
            required_artifact_string(raw_item, "artifact_id", "artifactId", block_label, index)?;
        let uri = required_artifact_string(raw_item, "uri", "uri", block_label, index)?;
        let mut artifact = CallbackArtifactRef::new(artifact_id, uri);
        if let Some(media_type) =
            optional_artifact_string(raw_item, "media_type", "mediaType", block_label, index)?
        {
            artifact = artifact.with_media_type(media_type);
        }
        if let Some(checksum) =
            optional_artifact_string(raw_item, "checksum", "checksum", block_label, index)?
        {
            artifact = artifact.with_checksum(checksum);
        }
        artifacts.push(artifact);
    }
    Ok(artifacts)
}

fn required_artifact_string(
    artifact: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    block_label: &str,
    index: usize,
) -> Result<String, BlockError> {
    optional_artifact_string(artifact, primary, alternate, block_label, index)?.ok_or_else(|| {
        BlockError::new(
            format!("{}.invalid_config", block_label.trim_end_matches("@1")),
            ErrorCategory::Configuration,
            format!("{block_label} config.artifacts[{index}].{primary} is required"),
            false,
        )
    })
}

fn optional_artifact_string(
    artifact: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    block_label: &str,
    index: usize,
) -> Result<Option<String>, BlockError> {
    artifact
        .get(primary)
        .or_else(|| artifact.get(alternate))
        .filter(|value| !value.is_null())
        .map(|value| {
            value
                .as_str()
                .filter(|text| !text.trim().is_empty())
                .map(str::to_owned)
                .ok_or_else(|| {
                    BlockError::new(
                        format!("{}.invalid_config", block_label.trim_end_matches("@1")),
                        ErrorCategory::Configuration,
                        format!(
                            "{block_label} config.artifacts[{index}].{primary} must be a non-empty string"
                        ),
                        false,
                    )
                })
        })
        .transpose()
}

fn async_operation_result_json(
    result: AsyncOperationResult,
    completed_at_unix_ms: Option<u64>,
) -> Result<Value, BlockError> {
    result.validate().map_err(|error| {
        BlockError::new(
            "async.operation_result.invalid_result",
            ErrorCategory::Configuration,
            format!("async operation result is invalid: {error:?}"),
            false,
        )
    })?;
    let status = match result.status {
        AsyncOperationResultStatus::Completed => "completed",
        AsyncOperationResultStatus::Failed => "failed",
        AsyncOperationResultStatus::Cancelled => "cancelled",
        AsyncOperationResultStatus::Expired => "expired",
        AsyncOperationResultStatus::Incomplete => "incomplete",
    };
    Ok(json!({
        "operation_id": result.operation_id,
        "status": status,
        "output": result.output,
        "artifacts": result
            .artifacts
            .iter()
            .map(CallbackArtifactRef::canonical_value)
            .collect::<Vec<_>>(),
        "diagnostics": result.diagnostics,
        "metrics": result.metrics,
        "checks": result.checks,
        "usage": result.usage,
        "external_effects": result
            .external_effects
            .iter()
            .map(external_effect_json)
            .collect::<Vec<_>>(),
        "completed_at_unix_ms": completed_at_unix_ms,
    }))
}

fn optional_async_terminal_u64(
    config: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    code: &'static str,
) -> Result<Option<u64>, BlockError> {
    config
        .get(primary)
        .or_else(|| config.get(alternate))
        .filter(|value| !value.is_null())
        .map(|value| {
            value.as_u64().ok_or_else(|| {
                BlockError::new(
                    code,
                    ErrorCategory::Configuration,
                    format!("field {primary} must be an unsigned integer"),
                    false,
                )
            })
        })
        .transpose()
}

fn required_async_terminal_config<'a>(
    config: &'a Value,
    block_label: &str,
) -> Result<&'a serde_json::Map<String, Value>, BlockError> {
    config.as_object().ok_or_else(|| {
        BlockError::new(
            format!("{block_label}.invalid_config"),
            ErrorCategory::Configuration,
            format!("{block_label} config must be an object"),
            false,
        )
    })
}

fn validate_async_terminal_timestamp(
    operation: &Value,
    completed_at_unix_ms: Option<u64>,
    block_label: &str,
    error_code: &'static str,
) -> Result<(), BlockError> {
    let Some(completed_at_unix_ms) = completed_at_unix_ms else {
        return Ok(());
    };
    if completed_at_unix_ms == 0 {
        return Err(BlockError::new(
            error_code,
            ErrorCategory::Configuration,
            format!("{block_label} terminal timestamp must be positive"),
            false,
        ));
    }
    if let Some(submitted_at_unix_ms) = operation
        .get("submitted_at_unix_ms")
        .or_else(|| operation.get("submittedAtUnixMs"))
        .and_then(Value::as_u64)
        && completed_at_unix_ms < submitted_at_unix_ms
    {
        return Err(BlockError::new(
            error_code,
            ErrorCategory::Configuration,
            format!(
                "{block_label} terminal timestamp must not be earlier than submitted_at_unix_ms"
            ),
            false,
        ));
    }
    if let Some(expires_at_unix_ms) = operation
        .get("expires_at_unix_ms")
        .or_else(|| operation.get("expiresAtUnixMs"))
        .and_then(Value::as_u64)
        && completed_at_unix_ms >= expires_at_unix_ms
    {
        return Err(BlockError::new(
            error_code,
            ErrorCategory::Configuration,
            format!("{block_label} terminal timestamp must be earlier than expires_at_unix_ms"),
            false,
        ));
    }
    Ok(())
}

fn external_effect_json(effect: &ExternalEffectRecord) -> Value {
    json!({
        "effect_id": effect.effect_id,
        "target": effect.target,
        "operation": effect.operation,
        "outcome": effect.outcome.as_str(),
        "idempotency_key": effect.idempotency_key,
        "provider_effect_id": effect.provider_effect_id,
    })
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::application_event::{ApplicationProtocolEventKind, SqliteApplicationProtocolLog};
    use crate::journal::SqliteExecutionJournal;
    use crate::run_store::{RunStatus, SqliteRunStore};
    use crate::stdlib_blocks::{
        ModelGenerate, ModelGenerateConfig, ModelGenerateInputs, ModelResponseValue, PromptValue,
    };
    use crate::typed_graph::GraphBuilder;
    use serde_json::{Value, json};

    use super::{
        ExecutionPhase, StdlibRunOptions, StdlibRunStatus, optional_alias_duration_ms,
        run_stdlib_graph_with_options, run_stdlib_graph_with_options_json, stdlib_block_catalog,
        validate_stdlib_output_contract,
    };

    #[test]
    fn native_duration_parser_rounds_up_and_enforces_u64_milliseconds() {
        for (value, expected) in [
            (json!("5e-1ms"), 1),
            (json!("1.5s"), 1_500),
            (json!("1d"), 86_400_000),
            (json!(0.0005), 1),
            (json!("1 s"), 1_000),
            (json!("1e-1000ms"), 1),
            (json!(u64::MAX), u64::MAX),
            (json!("18446744073709551615ms"), u64::MAX),
        ] {
            let config = json!({"timeout": value});
            let parsed = optional_alias_duration_ms(
                config.as_object().expect("config is an object"),
                &["timeout"],
                "duration.invalid",
                "duration must be positive and fit in milliseconds",
            )
            .expect("duration is valid");
            assert_eq!(parsed, Some(expected));
        }

        let config = json!({"timeout": "18446744073709551616ms"});
        assert!(
            optional_alias_duration_ms(
                config.as_object().expect("config is an object"),
                &["timeout"],
                "duration.invalid",
                "duration must be positive and fit in milliseconds",
            )
            .is_err()
        );
    }

    fn unique_sqlite_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "graphblocks-stdlib-{label}-{}-{}.sqlite3",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("system clock should be after epoch")
                .as_nanos()
        ))
    }

    #[test]
    fn native_stdlib_output_contract_enforces_resumed_required_outputs() {
        let catalog = stdlib_block_catalog().expect("stdlib catalog is valid");
        let descriptor = catalog
            .get("async.await_callback@1")
            .expect("async await callback descriptor exists");
        let initial_outputs = json!({"wait": {}});
        let initial_outputs = initial_outputs
            .as_object()
            .expect("test outputs are an object");

        validate_stdlib_output_contract(
            "async.await_callback@1",
            descriptor,
            initial_outputs,
            &json!({}),
            ExecutionPhase::Initial,
        )
        .expect("conditional callback outputs are optional during initial execution");
        let error = validate_stdlib_output_contract(
            "async.await_callback@1",
            descriptor,
            initial_outputs,
            &json!({}),
            ExecutionPhase::Resumed,
        )
        .expect_err("callback and operation outputs are required after resume");
        assert!(error.message.contains("callback, operation"));
    }

    #[test]
    fn typed_runtime_returns_structured_result_without_json_adapter() {
        let mut graph = GraphBuilder::new("typed-runtime").expect("graph name is valid");
        let prompt = graph
            .input::<PromptValue>("prompt")
            .expect("input is unique");
        let generated = graph
            .add(
                "generate",
                ModelGenerate::new(ModelGenerateConfig::new(json!("typed response"))),
                ModelGenerateInputs { prompt },
            )
            .expect("node is unique");
        graph
            .bind_output::<ModelResponseValue>("response", &generated.response)
            .expect("output is unique");

        let result = run_stdlib_graph_with_options(
            &graph.build(),
            &json!({"prompt": "ignored by scripted response"}),
            &StdlibRunOptions::default().with_run_id("typed-run-1"),
        )
        .expect("typed runtime succeeds");

        assert_eq!(result.run_id, "typed-run-1");
        assert_eq!(result.status, StdlibRunStatus::Succeeded);
        assert_eq!(result.outputs, json!({"response": "typed response"}));
        assert!(result.graph_hash.starts_with("sha256:"));
    }

    #[test]
    fn json_runtime_compiles_with_stdlib_catalog() {
        let error = run_stdlib_graph_with_options_json(
            &json!({
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "invalid-stdlib-port"},
                "spec": {
                    "interface": {
                        "inputs": {"prompt": "graphblocks.ai/Prompt@1"},
                        "outputs": {"response": "graphblocks.ai/ModelResponse@1"}
                    },
                    "nodes": {
                        "generate": {
                            "block": "model.generate@1",
                            "inputs": {"wrong": "$input.prompt"},
                            "outputs": {"response": "$output.response"}
                        }
                    }
                }
            })
            .to_string(),
            &json!({"prompt": "hello"}).to_string(),
            "{}",
        )
        .expect_err("unknown stdlib input port should fail compilation");

        assert!(error.to_string().contains("GB1013"));
    }

    #[test]
    fn json_runtime_rejects_unregistered_blocks_before_execution() {
        let error = run_stdlib_graph_with_options_json(
            &json!({
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "unknown-runtime-block"},
                "spec": {
                    "nodes": {
                        "unknown": {"block": "custom.unknown@1"}
                    }
                }
            })
            .to_string(),
            "{}",
            "{}",
        )
        .expect_err("unregistered stdlib block should fail preflight");

        assert!(
            error
                .to_string()
                .contains("uses unregistered block \"custom.unknown@1\"")
        );
    }

    #[test]
    fn json_runtime_rejects_nominal_stdlib_port_type_mismatches() {
        let error = run_stdlib_graph_with_options_json(
            &json!({
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "mismatched-runtime-port"},
                "spec": {
                    "interface": {
                        "inputs": {"message": "graphblocks.ai/Text@1"},
                        "outputs": {"prompt": "graphblocks.ai/Prompt@1"}
                    },
                    "nodes": {
                        "render": {
                            "block": "prompt.render@1",
                            "inputs": {"message": "$input.message"},
                            "outputs": {"prompt": "$output.prompt"}
                        }
                    }
                }
            })
            .to_string(),
            &json!({"message": {"text": "hello"}}).to_string(),
            "{}",
        )
        .expect_err("nominal stdlib port mismatch should fail compilation");

        assert!(error.to_string().contains("GB1018"));
    }

    #[test]
    fn stdlib_retrieval_fusion_applies_algorithms_and_source_weights() {
        let inputs = json!({
            "sources": [
                {
                    "sourceId": "first",
                    "weight": 0.1,
                    "hits": [
                        {"hitId": "a", "rank": 1, "normalizedScore": 0.2, "item": {"itemId": "a"}},
                        {"hitId": "b", "rank": 2, "normalizedScore": 0.9, "item": {"itemId": "b"}}
                    ]
                },
                {
                    "sourceId": "second",
                    "weight": 2.0,
                    "hits": [
                        {"hitId": "c", "rank": 1, "normalizedScore": 0.5, "item": {"itemId": "c"}},
                        {"hitId": "d", "rank": 2, "normalizedScore": 0.4, "item": {"itemId": "d"}}
                    ]
                }
            ]
        });
        for (algorithm, expected_ids) in [
            ("concatenate", vec!["a", "b", "c", "d"]),
            ("interleave", vec!["a", "c", "b", "d"]),
            ("reciprocal_rank_fusion", vec!["c", "d", "a", "b"]),
            ("weighted_rank", vec!["c", "d", "a", "b"]),
            ("normalized_score", vec!["c", "d", "b", "a"]),
        ] {
            let output = super::execute_stdlib_block(
                "retrieve.fuse@1",
                &inputs,
                &json!({"algorithm": algorithm, "k": 60}),
            )
            .expect("supported fusion strategy should succeed");
            let ids = output["hits"]
                .as_array()
                .expect("fusion hits should be an array")
                .iter()
                .map(|hit| {
                    hit.pointer("/item/itemId")
                        .and_then(Value::as_str)
                        .expect("fused hit should preserve itemId")
                })
                .collect::<Vec<_>>();
            assert_eq!(ids, expected_ids, "unexpected order for {algorithm}");
            assert!(output["hits"].as_array().is_some_and(|hits| {
                hits.iter()
                    .all(|hit| hit["fusionStrategy"].as_str() == Some(algorithm))
            }));
        }
    }

    #[test]
    fn stdlib_retrieval_fusion_rejects_invalid_strategy_weight_and_score() {
        let invalid_algorithm = super::execute_stdlib_block(
            "retrieve.fuse@1",
            &json!({"sources": []}),
            &json!({"algorithm": "unknown"}),
        )
        .expect_err("unknown fusion strategy should fail");
        assert_eq!(invalid_algorithm.code, "retrieve.fuse.invalid_algorithm");

        let invalid_weight = super::execute_stdlib_block(
            "retrieve.fuse@1",
            &json!({"sources": [{"sourceId": "source", "weight": 0, "hits": []}]}),
            &json!({}),
        )
        .expect_err("non-positive source weight should fail");
        assert_eq!(invalid_weight.code, "retrieve.fuse.invalid_source_weight");

        let invalid_score = super::execute_stdlib_block(
            "retrieve.fuse@1",
            &json!({
                "sources": [{
                    "sourceId": "source",
                    "hits": [{"hitId": "hit", "normalizedScore": 1.1}]
                }]
            }),
            &json!({"algorithm": "normalized_score"}),
        )
        .expect_err("out-of-range normalized score should fail");
        assert_eq!(invalid_score.code, "retrieve.fuse.invalid_normalized_score");
    }

    #[test]
    fn stdlib_rank_and_context_apply_typed_camel_case_config() {
        let ranked = super::execute_stdlib_block(
            "rank.documents@1",
            &json!({
                "query": "needle",
                "hits": [{"hitId": "hit", "item": {"preview": ["needle"]}}]
            }),
            &json!({"rerankerId": "configured-reranker"}),
        )
        .expect("ranking should succeed");
        assert_eq!(ranked["result"]["reranker"], json!("configured-reranker"));
        assert_eq!(ranked["hits"][0]["reranker"], json!("configured-reranker"));

        let context = super::execute_stdlib_block(
            "context.build@1",
            &json!({"evidence": []}),
            &json!({"contextId": "configured-context", "maxTokens": 100}),
        )
        .expect("context build should succeed");
        assert_eq!(context["pack"]["contextId"], json!("configured-context"));
    }

    #[test]
    fn stdlib_grounding_policies_warn_abstain_and_report_unrepaired_issues() {
        let inputs = json!({
            "response": {"answerId": "answer-1", "text": "unsupported", "citations": []},
            "context": {"hits": []}
        });
        let warning = super::execute_stdlib_block(
            "answer.validate_grounding@1",
            &inputs,
            &json!({"requireCitation": true, "onInsufficientEvidence": "warn"}),
        )
        .expect("warning policy should return validation evidence");
        assert_eq!(warning["validation"]["ok"], json!(true));
        assert!(
            warning["validation"]["issues"]
                .as_array()
                .is_some_and(|issues| !issues.is_empty())
        );
        assert_eq!(warning["candidate"], inputs["response"]);

        let abstention = super::execute_stdlib_block(
            "answer.validate_grounding@1",
            &inputs,
            &json!({"requireCitation": true, "onInsufficientEvidence": "abstain"}),
        )
        .expect("abstain policy should return a safe candidate");
        assert_eq!(abstention["validation"]["ok"], json!(false));
        assert_eq!(abstention["validation"]["abstained"], json!(true));
        assert_ne!(abstention["candidate"], inputs["response"]);

        let repair = super::execute_stdlib_block(
            "answer.validate_grounding@1",
            &inputs,
            &json!({"requireCitation": true, "onInsufficientEvidence": "repair"}),
        )
        .expect("repair policy should report its result");
        assert_eq!(repair["validation"]["ok"], json!(false));
        assert_eq!(repair["validation"]["repairAttempted"], json!(true));
        assert_eq!(repair["validation"]["repaired"], json!(false));
        assert!(
            repair["validation"]["unrepairedIssues"]
                .as_array()
                .is_some_and(|issues| !issues.is_empty())
        );
        assert_eq!(repair["candidate"], inputs["response"]);
    }

    #[test]
    fn stdlib_grounding_rejects_unknown_failure_policy() {
        let error = super::execute_stdlib_block(
            "answer.validate_grounding@1",
            &json!({"response": {"text": "answer"}, "context": {"hits": []}}),
            &json!({"onInsufficientEvidence": "ignore"}),
        )
        .expect_err("unknown grounding policy should fail");

        assert_eq!(
            error.code,
            "answer.validate_grounding.invalid_failure_policy"
        );
    }

    #[test]
    fn stdlib_async_start_operation_rejects_ambiguous_wait_bounds() {
        let error = super::execute_stdlib_block(
            "async.start_operation@1",
            &json!({}),
            &json!({
                "operationId": "op-ci-1",
                "runId": "run-coding-1",
                "nodeId": "startCI",
                "attemptId": "attempt-1",
                "kind": "ci_job",
                "providerOperationId": "gha-run-1",
                "resumeTokenHash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "idempotencyKey": "idem-op-ci-1",
                "expectedSchema": "schemas/CICallback@1",
                "createdAtUnixMs": 1_000,
                "submittedAtUnixMs": 1_050,
                "timeoutMs": 1_800_000,
                "infiniteWaitPolicy": "operator_review_required",
                "resume": {
                    "requirePolicyReevaluation": true,
                    "requireBudgetReservation": true,
                    "requireReleaseCompatibility": true,
                    "requireOwnershipFence": true
                },
                "attemptFencing": true
            }),
        )
        .expect_err("ambiguous async start wait bounds should fail");

        assert_eq!(error.code, "async.start_operation.invalid_config");
        assert!(
            error
                .message
                .contains("must not define both timeout and infiniteWaitPolicy"),
            "unexpected error: {:?}",
            error
        );
    }

    #[test]
    fn stdlib_async_start_operation_rejects_absolute_and_relative_wait_bounds() {
        let error = super::execute_stdlib_block(
            "async.start_operation@1",
            &json!({}),
            &json!({
                "operationId": "op-ci-1",
                "runId": "run-coding-1",
                "nodeId": "startCI",
                "attemptId": "attempt-1",
                "kind": "ci_job",
                "providerOperationId": "gha-run-1",
                "resumeTokenHash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "idempotencyKey": "idem-op-ci-1",
                "expectedSchema": "schemas/CICallback@1",
                "createdAtUnixMs": 1_000,
                "submittedAtUnixMs": 1_050,
                "expiresAtUnixMs": 1_801_000,
                "timeoutMs": 1_800_000,
                "resume": {
                    "requirePolicyReevaluation": true,
                    "requireBudgetReservation": true,
                    "requireReleaseCompatibility": true,
                    "requireOwnershipFence": true
                },
                "attemptFencing": true
            }),
        )
        .expect_err("absolute and relative async start wait bounds should fail");

        assert_eq!(error.code, "async.start_operation.invalid_config");
        assert!(
            error
                .message
                .contains("must not define both expiresAtUnixMs and timeout"),
            "unexpected error: {:?}",
            error
        );
    }

    #[test]
    fn stdlib_async_await_callback_rejects_ambiguous_wait_bounds() {
        let started = super::execute_stdlib_block(
            "async.start_operation@1",
            &json!({}),
            &json!({
                "operationId": "op-ci-1",
                "runId": "run-coding-1",
                "nodeId": "startCI",
                "attemptId": "attempt-1",
                "kind": "ci_job",
                "providerOperationId": "gha-run-1",
                "resumeTokenHash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "idempotencyKey": "idem-op-ci-1",
                "expectedSchema": "schemas/CICallback@1",
                "createdAtUnixMs": 1_000,
                "submittedAtUnixMs": 1_050,
                "infiniteWaitPolicy": "operator_review_required",
                "resume": {
                    "requirePolicyReevaluation": true,
                    "requireBudgetReservation": true,
                    "requireReleaseCompatibility": true,
                    "requireOwnershipFence": true
                },
                "attemptFencing": true
            }),
        )
        .expect("valid infinite async start should succeed");
        let error = super::execute_stdlib_block(
            "async.await_callback@1",
            &json!({"operation": started["operation"].clone()}),
            &json!({
                "checkpoint": true,
                "onTimeout": "fail",
                "timeout": "30m",
                "infiniteWaitPolicy": "operator_review_required"
            }),
        )
        .expect_err("ambiguous async await wait bounds should fail");

        assert_eq!(error.code, "async.await_callback.invalid_config");
        assert!(
            error
                .message
                .contains("must not define both timeout and infiniteWaitPolicy"),
            "unexpected error: {:?}",
            error
        );
    }

    #[test]
    fn stdlib_async_await_callback_rejects_operation_without_expected_schema() {
        let operation = json!({
            "operation_id": "op-ci-1",
            "run_id": "run-coding-1",
            "node_id": "waitCI",
            "attempt_id": "attempt-1",
            "kind": "ci_job",
            "state": "waiting_callback",
            "resume_token_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "idempotency_key": "idem-op-ci-1"
        });
        let error = super::execute_stdlib_block(
            "async.await_callback@1",
            &json!({"operation": operation}),
            &json!({"checkpoint": true, "onTimeout": "fail", "timeout": "30m"}),
        )
        .expect_err("operation without expected schema should fail");

        assert_eq!(error.code, "async.await_callback@1.invalid_operation");
        assert!(
            error
                .message
                .contains("input operation.expected_schema must be a non-empty string"),
            "unexpected error: {:?}",
            error
        );
    }

    #[test]
    fn stdlib_async_terminal_timestamp_validation_accepts_protocol_operation_projection() {
        let operation = json!({
            "operationId": "op-ci-1",
            "runId": "run-coding-1",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "kind": "ci_job",
            "state": "waiting_callback",
            "resumeTokenHash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "idempotencyKey": "idem-op-ci-1",
            "expectedSchema": "schemas/CICallback@1",
            "submittedAtUnixMs": 1_050,
            "expiresAtUnixMs": 1_800,
        });
        let error = super::execute_stdlib_block(
            "async.complete_operation@1",
            &json!({"operation": operation}),
            &json!({"completedAtUnixMs": 1_801}),
        )
        .expect_err("camelCase operation expiration should be enforced");

        assert_eq!(error.code, "async.complete_operation.invalid_config");
        assert!(
            error
                .message
                .contains("terminal timestamp must be earlier than expires_at_unix_ms"),
            "unexpected error: {:?}",
            error
        );
    }

    #[test]
    fn stdlib_async_poll_operation_rejects_ambiguous_wait_bounds() {
        let started = super::execute_stdlib_block(
            "async.start_operation@1",
            &json!({}),
            &json!({
                "operationId": "op-poll-1",
                "runId": "run-coding-1",
                "nodeId": "startPoll",
                "attemptId": "attempt-1",
                "kind": "external_provider_job",
                "providerOperationId": "batch-1",
                "resumeTokenHash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "idempotencyKey": "idem-op-poll-1",
                "expectedSchema": "schemas/PollResult@1",
                "createdAtUnixMs": 1_000,
                "submittedAtUnixMs": 1_050,
                "timeoutMs": 1_800_000,
                "resume": {
                    "requirePolicyReevaluation": true,
                    "requireBudgetReservation": true,
                    "requireReleaseCompatibility": true,
                    "requireOwnershipFence": true
                },
                "attemptFencing": true
            }),
        )
        .expect("valid bounded async start should succeed");
        let error = super::execute_stdlib_block(
            "async.poll_operation@1",
            &json!({"operation": started["operation"].clone()}),
            &json!({
                "interval": "30s",
                "maxInterval": "5m",
                "timeout": "2h",
                "infiniteWaitPolicy": "provider_has_no_timeout"
            }),
        )
        .expect_err("ambiguous async poll wait bounds should fail");

        assert_eq!(error.code, "async.poll_operation.invalid_config");
        assert!(
            error
                .message
                .contains("must not define both timeout and infiniteWaitPolicy"),
            "unexpected error: {:?}",
            error
        );
    }

    #[test]
    fn stdlib_runtime_options_can_select_run_id() {
        let graph_json = r#"{
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "stdlib-run-id"},
            "spec": {
                "nodes": {
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Native {message.text}"},
                        "inputs": {"message": "$input.message"},
                        "outputs": {"prompt": "$output.prompt"}
                    }
                }
            }
        }"#;
        let result_json = run_stdlib_graph_with_options_json(
            graph_json,
            r#"{"message":{"text":"ok"}}"#,
            r#"{"runId":"run-native-requested-1"}"#,
        )
        .expect("stdlib runtime should execute");
        let result: Value = serde_json::from_str(&result_json).expect("result is JSON");

        assert_eq!(result["runId"], "run-native-requested-1");
        assert_eq!(result["outputs"]["prompt"], "Native ok");
        for record in result["journal"].as_array().expect("journal is array") {
            assert_eq!(record["runId"], "run-native-requested-1");
        }
    }

    #[test]
    fn stdlib_runtime_options_debug_redacts_callback_secrets() {
        let secret = "callback-admission-secret-that-must-not-leak";
        let receipt_sentinel = "callback-receipt-secret-that-must-not-leak";
        let options = StdlibRunOptions::default()
            .with_callback_admission_hmac_key(secret)
            .with_callback_receipt(json!({"sentinel": receipt_sentinel}));

        let rendered = format!("{options:?}");

        assert!(!rendered.contains(secret));
        assert!(!rendered.contains(receipt_sentinel));
        assert!(rendered.contains("<redacted>"));
    }

    #[test]
    fn stdlib_runtime_options_reject_blank_run_id() {
        let error = run_stdlib_graph_with_options_json("{}", "{}", r#"{"runId":" "}"#)
            .expect_err("blank run id should be rejected");

        assert_eq!(
            error.to_string(),
            "runtime options field runId must not be empty"
        );
    }

    #[test]
    fn stdlib_runtime_options_reject_incomplete_deployment_provenance() {
        let error = run_stdlib_graph_with_options_json(
            "{}",
            "{}",
            r#"{"deploymentProvenance":{"releaseDigest":"sha256:release"}}"#,
        )
        .expect_err("incomplete production provenance should be rejected");

        assert_eq!(
            error.to_string(),
            "runtime options deploymentProvenance field deploymentRevisionId is required"
        );
    }

    #[test]
    fn stdlib_runtime_options_reject_noncanonical_deployment_digest() {
        let error = run_stdlib_graph_with_options_json(
            "{}",
            "{}",
            r#"{"deploymentProvenance":{"releaseDigest":"not-a-digest","deploymentRevisionId":"revision-1","physicalPlanHash":"sha256:physical-plan","releaseSignatureDigest":"sha256:signature"}}"#,
        )
        .expect_err("noncanonical production provenance should be rejected");

        assert_eq!(
            error.to_string(),
            "runtime options deploymentProvenance field releaseDigest must be a canonical sha256 digest"
        );
    }

    #[test]
    fn stdlib_runtime_options_persist_sqlite_run_and_journal_evidence() {
        let run_store_path = unique_sqlite_path("run-store");
        let journal_store_path = unique_sqlite_path("journal-store");
        let release_digest = format!("sha256:{}", "1".repeat(64));
        let physical_plan_hash = format!("sha256:{}", "2".repeat(64));
        let release_signature_digest = format!("sha256:{}", "3".repeat(64));
        let graph_json = r#"{
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "stdlib-run-evidence"},
            "spec": {
                "nodes": {
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Native {message.text}"},
                        "inputs": {"message": "$input.message"},
                        "outputs": {"prompt": "$output.prompt"}
                    }
                }
            }
        }"#;
        let options = serde_json::json!({
            "runId": "run-native-evidence-1",
            "runStorePath": run_store_path.to_string_lossy(),
            "journalStorePath": journal_store_path.to_string_lossy(),
            "deploymentProvenance": {
                "releaseDigest": release_digest,
                "deploymentRevisionId": "revision-1",
                "physicalPlanHash": physical_plan_hash,
                "releaseSignatureDigest": release_signature_digest,
            },
        });
        let result_json = run_stdlib_graph_with_options_json(
            graph_json,
            r#"{"message":{"text":"ok"}}"#,
            &serde_json::to_string(&options).expect("options serialize"),
        )
        .expect("stdlib runtime should execute");
        let result: Value = serde_json::from_str(&result_json).expect("result is JSON");

        assert_eq!(result["runId"], "run-native-evidence-1");
        assert_eq!(result["status"], "succeeded");

        let store = SqliteRunStore::open(&run_store_path).expect("run store reopens");
        let run = store
            .get_run("run-native-evidence-1")
            .expect("run record is persisted");
        assert_eq!(run.status, RunStatus::Completed);
        assert_eq!(run.inputs["message"]["text"], "ok");
        assert_eq!(
            run.deployment_provenance.release_digest.as_deref(),
            Some(release_digest.as_str())
        );
        assert_eq!(
            run.deployment_provenance.deployment_revision_id.as_deref(),
            Some("revision-1")
        );
        assert_eq!(
            run.deployment_provenance.physical_plan_hash.as_deref(),
            Some(physical_plan_hash.as_str())
        );
        assert_eq!(
            run.deployment_provenance
                .release_signature_digest
                .as_deref(),
            Some(release_signature_digest.as_str())
        );

        let journal = SqliteExecutionJournal::open(&journal_store_path, "run-native-evidence-1")
            .expect("journal reopens");
        let records = journal.records().expect("journal records load");
        assert_eq!(
            records
                .iter()
                .map(|record| record.kind.as_str())
                .collect::<Vec<_>>(),
            vec![
                "run_started",
                "node_started",
                "node_completed",
                "run_succeeded"
            ]
        );
        assert_eq!(
            journal.terminal_kind().expect("terminal loads").as_deref(),
            Some("run_succeeded")
        );
        assert!(
            records
                .iter()
                .all(|record| record.run_id == "run-native-evidence-1")
        );

        let _ = std::fs::remove_file(run_store_path);
        let _ = std::fs::remove_file(journal_store_path);
    }

    #[test]
    fn stdlib_runtime_options_persist_sqlite_application_event_stream() {
        let application_event_store_path = unique_sqlite_path("application-event-store");
        let graph_json = r#"{
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "stdlib-application-events"},
            "spec": {
                "nodes": {
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Native {message.text}"},
                        "inputs": {"message": "$input.message"},
                        "outputs": {"prompt": "$output.prompt"}
                    }
                }
            }
        }"#;
        let options = serde_json::json!({
            "runId": "run-native-events-1",
            "applicationEventStorePath": application_event_store_path.to_string_lossy(),
        });
        let result_json = run_stdlib_graph_with_options_json(
            graph_json,
            r#"{"message":{"text":"ok"}}"#,
            &serde_json::to_string(&options).expect("options serialize"),
        )
        .expect("stdlib runtime should execute");
        let result: Value = serde_json::from_str(&result_json).expect("result is JSON");

        let log = SqliteApplicationProtocolLog::open(&application_event_store_path)
            .expect("application event log reopens");
        let events = log
            .replay_after(None, 10)
            .expect("application events replay");

        assert_eq!(
            events.iter().map(|event| event.kind).collect::<Vec<_>>(),
            vec![
                ApplicationProtocolEventKind::RunStarted,
                ApplicationProtocolEventKind::RunCompleted,
            ]
        );
        assert_eq!(events[0].metadata.run_id, "run-native-events-1");
        assert_eq!(events[0].metadata.sequence, 1);
        assert_eq!(events[0].metadata.cursor.as_deref(), Some("evt-000001"));
        assert_eq!(events[1].metadata.sequence, 2);
        assert_eq!(events[1].metadata.cursor.as_deref(), Some("evt-000002"));
        assert_eq!(events[1].payload["status"], "succeeded");
        assert_eq!(events[1].payload["outputs"]["prompt"], "Native ok");
        assert_eq!(events[1].metadata.release_id, result["graphHash"]);

        let replay = log
            .replay_after(Some("evt-000001"), 10)
            .expect("cursor replay succeeds");
        assert_eq!(replay.len(), 1);
        assert_eq!(replay[0].kind, ApplicationProtocolEventKind::RunCompleted);

        let _ = std::fs::remove_file(application_event_store_path);
    }

    #[test]
    fn stdlib_runtime_options_append_multiple_runs_to_one_application_event_store() {
        let application_event_store_path = unique_sqlite_path("application-event-store-shared");
        let graph_json = r#"{
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "stdlib-application-events-shared"},
            "spec": {
                "nodes": {
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Native {message.text}"},
                        "inputs": {"message": "$input.message"},
                        "outputs": {"prompt": "$output.prompt"}
                    }
                }
            }
        }"#;

        for (run_id, text) in [
            ("run-native-events-1", "first"),
            ("run-native-events-2", "second"),
        ] {
            let options = serde_json::json!({
                "runId": run_id,
                "applicationEventStorePath": application_event_store_path.to_string_lossy(),
            });
            run_stdlib_graph_with_options_json(
                graph_json,
                &serde_json::to_string(&json!({"message": {"text": text}}))
                    .expect("inputs serialize"),
                &serde_json::to_string(&options).expect("options serialize"),
            )
            .expect("stdlib runtime should execute");
        }

        let log = SqliteApplicationProtocolLog::open(&application_event_store_path)
            .expect("application event log reopens");
        let second_run = log
            .replay_after_for_run("run-native-events-2", Some("evt-000001"), 10)
            .expect("second run cursor replay succeeds");

        assert_eq!(log.len().expect("total event count loads"), 4);
        assert_eq!(second_run.len(), 1);
        assert_eq!(
            second_run[0].kind,
            ApplicationProtocolEventKind::RunCompleted
        );
        assert_eq!(second_run[0].payload["outputs"]["prompt"], "Native second");

        let _ = std::fs::remove_file(application_event_store_path);
    }
}
