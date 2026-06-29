use std::collections::{BTreeMap, BTreeSet};

use graphblocks_compiler::compiler::{BlockCatalog, compile_graph, compile_graph_with_catalog};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_protocol::{
    RemotePayload, RemotePayloadError, RemotePayloadLimits, WorkerAdmissionPolicy,
    WorkerAdvertisement, WorkerProtocolError, admit_worker_with_policy, validate_remote_payload,
};
use graphblocks_runtime_core::agent::{AgentLoopController, AgentLoopDecision, AgentSpec};
use graphblocks_runtime_core::budget::{BudgetPermit, UsageAmount};
use graphblocks_runtime_core::exhaustion::{
    ContinuationEnvelope, ExhaustionController, ExhaustionPolicy, ExhaustionPreset, ExhaustionUnit,
    WorkKind,
};
use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory, Outcome};
use graphblocks_runtime_core::output_policy::{
    DeclarativeOutputPolicyEvaluator, DeclarativeOutputPolicyRule, DraftDisposition, DurableResult,
    FlushBoundary, GenerationChunk, OutputCutoff, OutputDeliveryGate, OutputDeliveryPolicy,
    OutputDisposition, OutputPolicyDecision, PendingToolCallsDisposition, ProviderCancellation,
    RedactionInstruction, TerminalReason, ViolationAction,
};
use graphblocks_runtime_core::readiness::{InputDependency, PortRef, ResolvedInput};
use graphblocks_runtime_core::scheduler::{ScheduledNode, StartedNode};
use graphblocks_runtime_core::test_runtime::{InProcessTestRuntime, NodeExecutor, TestRunStatus};
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, GraphToolImplementation, McpToolImplementation,
    OpenApiToolImplementation, RemoteToolImplementation, ResolvedTool, ToolApproval, ToolBinding,
    ToolCancellation, ToolCatalog, ToolDefinition, ToolEffect, ToolIdempotency, ToolImplementation,
    ToolResolutionScope, ToolResultMode,
};
use graphblocks_runtime_core::tool_call::{ToolCallDraft, ToolCallDraftStatus, ToolCallStatus};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use serde_json::{Value, json};

#[pyfunction]
fn binding_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pyfunction]
fn finalize_tool_call_json(
    draft_json: &str,
    resolved_tool_id: &str,
    created_at_unix_ms: u64,
) -> PyResult<String> {
    let draft_value = parse_json_argument(draft_json, "tool call draft")?;
    let draft_object = json_object(&draft_value, "draft")?;
    let status = match required_alias_string(draft_object, "status", "status", "draft")? {
        "proposed" => ToolCallDraftStatus::Proposed,
        "arguments_streaming" => ToolCallDraftStatus::ArgumentsStreaming,
        "arguments_complete" => ToolCallDraftStatus::ArgumentsComplete,
        value => {
            return Err(PyValueError::new_err(format!(
                "draft.status has unknown status {value:?}"
            )));
        }
    };
    let expected_sequence = required_alias_u64(draft_object, "sequence", "sequence", "draft")?;
    let fragments = draft_object
        .get("argumentFragments")
        .or_else(|| draft_object.get("argument_fragments"))
        .ok_or_else(|| PyValueError::new_err("draft.argumentFragments is required"))?
        .as_array()
        .ok_or_else(|| PyValueError::new_err("draft.argumentFragments must be an array"))?;

    let mut draft = ToolCallDraft::proposed(
        required_alias_string(draft_object, "responseId", "response_id", "draft")?,
        required_alias_string(draft_object, "toolCallId", "tool_call_id", "draft")?,
        required_alias_string(draft_object, "toolName", "tool_name", "draft")?,
    );
    for (fragment_index, fragment) in fragments.iter().enumerate() {
        let Some(fragment) = fragment.as_str() else {
            return Err(PyValueError::new_err(format!(
                "draft.argumentFragments[{fragment_index}] must be a string"
            )));
        };
        draft.append_argument_fragment(fragment).map_err(|error| {
            PyValueError::new_err(format!(
                "failed to append tool argument fragment {fragment_index}: {error:?}"
            ))
        })?;
    }
    if status == ToolCallDraftStatus::ArgumentsComplete {
        draft.complete_arguments().map_err(|error| {
            PyValueError::new_err(format!("failed to complete tool arguments: {error:?}"))
        })?;
    }
    if draft.status != status {
        return Err(PyValueError::new_err(format!(
            "draft.status does not match argument fragments: expected {status:?}, reconstructed {:?}",
            draft.status
        )));
    }
    if draft.sequence != expected_sequence {
        return Err(PyValueError::new_err(format!(
            "draft.sequence does not match argument fragments: expected {expected_sequence}, reconstructed {}",
            draft.sequence
        )));
    }

    let call = draft
        .into_tool_call(resolved_tool_id, created_at_unix_ms)
        .map_err(|error| PyValueError::new_err(format!("invalid tool call draft: {error:?}")))?;
    let status = match call.status {
        ToolCallStatus::Validated => "validated",
        ToolCallStatus::PolicyPending => "policy_pending",
        ToolCallStatus::ApprovalPending => "approval_pending",
        ToolCallStatus::Admitted => "admitted",
        ToolCallStatus::Running => "running",
        ToolCallStatus::Completed => "completed",
        ToolCallStatus::Failed => "failed",
        ToolCallStatus::Denied => "denied",
        ToolCallStatus::Cancelled => "cancelled",
        ToolCallStatus::PolicyStopped => "policy_stopped",
        ToolCallStatus::Expired => "expired",
    };
    let payload = json!({
        "toolCallId": call.tool_call_id,
        "responseId": call.response_id,
        "resolvedToolId": call.resolved_tool_id,
        "name": call.name,
        "arguments": call.arguments,
        "argumentsDigest": call.arguments_digest,
        "revision": call.revision,
        "status": status,
        "dependsOn": call.depends_on,
        "createdAtUnixMs": call.created_at_unix_ms,
        "admittedAtUnixMs": call.admitted_at_unix_ms,
        "completedAtUnixMs": call.completed_at_unix_ms,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize finalized tool call: {error}"))
    })
}

#[pyfunction]
#[pyo3(signature = (document_json, block_catalog_json=None))]
fn compile_graph_json(document_json: &str, block_catalog_json: Option<&str>) -> PyResult<String> {
    let document = serde_json::from_str::<Value>(document_json)
        .map_err(|error| PyValueError::new_err(format!("invalid graph document JSON: {error}")))?;
    let block_catalog = block_catalog_json
        .map(|catalog_json| {
            let catalog = serde_json::from_str::<Value>(catalog_json).map_err(|error| {
                PyValueError::new_err(format!("invalid block catalog JSON: {error}"))
            })?;
            BlockCatalog::from_blocks(&catalog).map_err(PyValueError::new_err)
        })
        .transpose()?;
    let plan = if let Some(block_catalog) = &block_catalog {
        compile_graph_with_catalog(&document, block_catalog)
    } else {
        compile_graph(&document)
    };
    let diagnostics = plan
        .diagnostics
        .iter()
        .map(|diagnostic| {
            let severity = match diagnostic.severity {
                Severity::Error => "error",
                Severity::Warning => "warning",
                Severity::Info => "info",
            };
            json!({
                "code": diagnostic.code.as_str(),
                "message": diagnostic.message.as_str(),
                "path": diagnostic.path.as_str(),
                "severity": severity,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "hash": plan.graph_hash,
        "ok": plan.ok(),
        "diagnostics": diagnostics,
        "graph": plan.normalized,
    });

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize compiler result: {error}"))
    })
}

#[pyfunction]
fn validate_worker_advertisement_json(
    advertisement_json: &str,
    expected_package_lock_hash: Option<&str>,
) -> PyResult<String> {
    let advertisement =
        serde_json::from_str::<WorkerAdvertisement>(advertisement_json).map_err(|error| {
            PyValueError::new_err(format!("invalid worker advertisement JSON: {error}"))
        })?;
    let mut policy = WorkerAdmissionPolicy::current();
    if let Some(package_lock_hash) = expected_package_lock_hash {
        policy = policy.require_package_lock_hash(package_lock_hash);
    }
    let payload = match admit_worker_with_policy(&policy, &advertisement) {
        Ok(()) => json!({"ok": true}),
        Err(error) => {
            let error_payload = match error {
                WorkerProtocolError::IncompatibleVersion { expected, actual } => json!({
                    "code": "worker.incompatible_protocol_version",
                    "expected": expected,
                    "actual": actual,
                }),
                WorkerProtocolError::IncompatiblePackageLock { expected, actual } => json!({
                    "code": "worker.incompatible_package_lock",
                    "expected": expected,
                    "actual": actual,
                }),
                WorkerProtocolError::EmptyWorkerId => json!({"code": "worker.empty_worker_id"}),
                WorkerProtocolError::EmptyTargetId => json!({"code": "worker.empty_target_id"}),
                WorkerProtocolError::EmptyPackageLockHash => {
                    json!({"code": "worker.empty_package_lock_hash"})
                }
                WorkerProtocolError::EmptyImageDigest => {
                    json!({"code": "worker.empty_image_digest"})
                }
                WorkerProtocolError::EmptySupportedBlocks => {
                    json!({"code": "worker.empty_supported_blocks"})
                }
                WorkerProtocolError::MissingRequiredBlock { required_block } => json!({
                    "code": "worker.missing_required_block",
                    "requiredBlock": required_block,
                }),
            };
            json!({"ok": false, "error": error_payload})
        }
    };

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize worker protocol result: {error}"
        ))
    })
}

#[pyfunction]
fn validate_remote_payload_json(payload_json: &str, max_inline_bytes: usize) -> PyResult<String> {
    let payload = serde_json::from_str::<RemotePayload>(payload_json)
        .map_err(|error| PyValueError::new_err(format!("invalid remote payload JSON: {error}")))?;
    let limits = RemotePayloadLimits { max_inline_bytes };
    let result_payload = match validate_remote_payload(&payload, &limits) {
        Ok(()) => json!({"ok": true}),
        Err(error) => {
            let error_payload = match error {
                RemotePayloadError::OversizedInlinePayload {
                    max_inline_bytes,
                    actual_inline_bytes,
                } => json!({
                    "code": "remote_payload.oversized_inline",
                    "maxInlineBytes": max_inline_bytes,
                    "actualInlineBytes": actual_inline_bytes,
                }),
                RemotePayloadError::InvalidArtifactRef { field } => json!({
                    "code": "remote_payload.invalid_artifact_ref",
                    "field": field,
                }),
                RemotePayloadError::InlineJsonEncoding => {
                    json!({"code": "remote_payload.inline_json_encoding"})
                }
            };
            json!({"ok": false, "error": error_payload})
        }
    };

    serde_json::to_string(&result_payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize remote payload result: {error}"
        ))
    })
}

struct JsonNodeExecutor {
    outputs_by_node: BTreeMap<String, Value>,
}

impl NodeExecutor for JsonNodeExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        let Some(outputs) = self
            .outputs_by_node
            .get(&node.node_id)
            .and_then(Value::as_object)
        else {
            return Err(BlockError::new(
                format!("{}.missing_fixture", node.node_id),
                ErrorCategory::Configuration,
                "node output fixture must be an object",
                false,
            ));
        };

        Ok(outputs
            .iter()
            .map(|(port, value)| {
                (
                    PortRef::new(node.node_id.clone(), port.clone()),
                    Outcome::Value(value.clone()),
                )
            })
            .collect())
    }
}

struct StdlibExecutor {
    nodes: BTreeMap<String, Value>,
    outputs_by_node: BTreeMap<String, Value>,
}

impl NodeExecutor for StdlibExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError> {
        let inputs = resolved_inputs_to_json(&node.inputs)?;
        let Some(node_spec) = self.nodes.get(&node.node_id).and_then(Value::as_object) else {
            return Err(BlockError::new(
                format!("{}.missing_node", node.node_id),
                ErrorCategory::Configuration,
                "node spec must be an object",
                false,
            ));
        };
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
        let outputs = execute_stdlib_block(block_id, &inputs, &config)?;
        let Some(outputs_object) = outputs.as_object() else {
            return Err(BlockError::new(
                format!("{block_id}.invalid_outputs"),
                ErrorCategory::Internal,
                "stdlib block returned non-object outputs",
                false,
            ));
        };
        let port_outputs = outputs_object
            .iter()
            .map(|(port, value)| {
                (
                    PortRef::new(node.node_id.clone(), port.clone()),
                    Outcome::Value(value.clone()),
                )
            })
            .collect();
        self.outputs_by_node.insert(node.node_id, outputs);
        Ok(port_outputs)
    }
}

struct RuntimeBridgePlan {
    graph_hash: String,
    nodes: BTreeMap<String, Value>,
    edges: Vec<Value>,
    scheduled_nodes: Vec<ScheduledNode>,
}

fn parse_json_argument(text: &str, label: &str) -> PyResult<Value> {
    serde_json::from_str::<Value>(text)
        .map_err(|error| PyValueError::new_err(format!("invalid {label} JSON: {error}")))
}

fn json_object<'a>(value: &'a Value, label: &str) -> PyResult<&'a serde_json::Map<String, Value>> {
    value
        .as_object()
        .ok_or_else(|| PyValueError::new_err(format!("{label} must be an object")))
}

fn required_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<&'a str> {
    object
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.{field} must be a string")))
}

fn required_alias_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<&'a str> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .and_then(Value::as_str)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.{primary} must be a string")))
}

fn required_u64(
    object: &serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<u64> {
    object.get(field).and_then(Value::as_u64).ok_or_else(|| {
        PyValueError::new_err(format!("{label}.{field} must be an unsigned integer"))
    })
}

fn required_alias_u64(
    object: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<u64> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            PyValueError::new_err(format!("{label}.{primary} must be an unsigned integer"))
        })
}

fn optional_alias_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<Option<&'a str>> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| PyValueError::new_err(format!("{label}.{primary} must be a string")))
        })
        .transpose()
}

fn optional_alias_u64(
    object: &serde_json::Map<String, Value>,
    primary: &str,
    alternate: &str,
    label: &str,
) -> PyResult<Option<u64>> {
    object
        .get(primary)
        .or_else(|| object.get(alternate))
        .map(|value| {
            value.as_u64().ok_or_else(|| {
                PyValueError::new_err(format!("{label}.{primary} must be an unsigned integer"))
            })
        })
        .transpose()
}

fn parse_work_kind(value: &Value, label: &str) -> PyResult<WorkKind> {
    let Some(value) = value.as_str() else {
        return Err(PyValueError::new_err(format!("{label} must be a string")));
    };
    match value {
        "current_provider_call" => Ok(WorkKind::CurrentProviderCall),
        "already_admitted_child_work" => Ok(WorkKind::AlreadyAdmittedChildWork),
        "declared_finalization" => Ok(WorkKind::DeclaredFinalization),
        "checkpoint" => Ok(WorkKind::Checkpoint),
        "cleanup" => Ok(WorkKind::Cleanup),
        "read_only_tool" => Ok(WorkKind::ReadOnlyTool),
        "new_turn" => Ok(WorkKind::NewTurn),
        "plan_expansion" => Ok(WorkKind::PlanExpansion),
        "optional_task" => Ok(WorkKind::OptionalTask),
        "new_trial" => Ok(WorkKind::NewTrial),
        "state_changing_effect" => Ok(WorkKind::StateChangingEffect),
        "unreserved_provider_call" => Ok(WorkKind::UnreservedProviderCall),
        _ => Err(PyValueError::new_err(format!(
            "{label} has unknown work kind {value:?}"
        ))),
    }
}

fn parse_work_kind_list(
    object: &serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<Vec<WorkKind>> {
    let Some(value) = object.get(field) else {
        return Ok(Vec::new());
    };
    let Some(values) = value.as_array() else {
        return Err(PyValueError::new_err(format!(
            "{label}.{field} must be an array"
        )));
    };
    values
        .iter()
        .enumerate()
        .map(|(index, value)| parse_work_kind(value, &format!("{label}.{field}[{index}]")))
        .collect()
}

fn parse_usage_amount(value: &Value, label: &str) -> PyResult<UsageAmount> {
    let object = json_object(value, label)?;
    let kind = required_string(object, "kind", label)?;
    let amount = object
        .get("amount")
        .and_then(Value::as_i64)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.amount must be an integer")))?;
    let unit = required_string(object, "unit", label)?;
    let mut usage = UsageAmount::new(kind, amount, unit);
    if let Some(dimensions) = object.get("dimensions") {
        let dimensions = json_object(dimensions, &format!("{label}.dimensions"))?;
        for (key, value) in dimensions {
            let Some(value) = value.as_str() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.dimensions.{key} must be a string"
                )));
            };
            usage = usage.with_dimension(key.clone(), value);
        }
    }
    Ok(usage)
}

fn parse_usage_amounts(
    object: &serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<Vec<UsageAmount>> {
    let Some(value) = object.get(field) else {
        return Ok(Vec::new());
    };
    let Some(values) = value.as_array() else {
        return Err(PyValueError::new_err(format!(
            "{label}.{field} must be an array"
        )));
    };
    values
        .iter()
        .enumerate()
        .map(|(index, value)| parse_usage_amount(value, &format!("{label}.{field}[{index}]")))
        .collect()
}

fn parse_budget_permit(value: &Value, label: &str) -> PyResult<BudgetPermit> {
    let object = json_object(value, label)?;
    let reservation_refs = object
        .get("reservationRefs")
        .and_then(Value::as_array)
        .ok_or_else(|| PyValueError::new_err(format!("{label}.reservationRefs must be an array")))?
        .iter()
        .enumerate()
        .map(|(index, value)| {
            value.as_str().map(str::to_owned).ok_or_else(|| {
                PyValueError::new_err(format!("{label}.reservationRefs[{index}] must be a string"))
            })
        })
        .collect::<PyResult<Vec<_>>>()?;
    let mut fencing_tokens = BTreeMap::new();
    if let Some(tokens) = object.get("fencingTokens") {
        let tokens = json_object(tokens, &format!("{label}.fencingTokens"))?;
        for (budget_id, token) in tokens {
            let Some(token) = token.as_u64() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.fencingTokens.{budget_id} must be an unsigned integer"
                )));
            };
            fencing_tokens.insert(budget_id.clone(), token);
        }
    }

    Ok(BudgetPermit {
        permit_id: required_string(object, "permitId", label)?.to_owned(),
        reservation_refs,
        owner: required_string(object, "owner", label)?.to_owned(),
        atomic_unit: required_string(object, "atomicUnit", label)?.to_owned(),
        admission_epoch: required_u64(object, "admissionEpoch", label)?,
        authorized_amounts: parse_usage_amounts(object, "authorizedAmounts", label)?,
        continuation_profile: required_string(object, "continuationProfile", label)?.to_owned(),
        policy_snapshot_digest: required_string(object, "policySnapshotDigest", label)?.to_owned(),
        expires_at: required_string(object, "expiresAt", label)?.to_owned(),
        low_watermark: parse_usage_amounts(object, "lowWatermark", label)?,
        fencing_tokens,
    })
}

fn parse_continuation_envelope(value: &Value, label: &str) -> PyResult<ContinuationEnvelope> {
    let object = json_object(value, label)?;
    let max_steps = if let Some(value) = object.get("maxAdditionalSteps") {
        let Some(value) = value.as_u64() else {
            return Err(PyValueError::new_err(format!(
                "{label}.maxAdditionalSteps must be an unsigned integer"
            )));
        };
        Some(u32::try_from(value).map_err(|_| {
            PyValueError::new_err(format!("{label}.maxAdditionalSteps exceeds u32"))
        })?)
    } else {
        None
    };
    let deadline = object
        .get("deadline")
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| PyValueError::new_err(format!("{label}.deadline must be a string")))
        })
        .transpose()?;
    let mut envelope = ContinuationEnvelope::new()
        .with_allowed_work(parse_work_kind_list(object, "allowedWork", label)?)
        .with_forbidden_work(parse_work_kind_list(object, "forbiddenWork", label)?)
        .with_max_additional_usage(parse_usage_amounts(object, "maxAdditionalUsage", label)?);
    if let Some(max_steps) = max_steps {
        envelope = envelope.with_max_additional_steps(max_steps);
    }
    if let Some(deadline) = deadline {
        envelope = envelope.with_deadline(deadline);
    }
    Ok(envelope)
}

fn parse_exhaustion_policy(value: &Value) -> PyResult<ExhaustionPolicy> {
    let object = json_object(value, "policy")?;
    let preset = match required_string(object, "preset", "policy")? {
        "finish_current_turn" => ExhaustionPreset::FinishCurrentTurn,
        "finish_current_call" => ExhaustionPreset::FinishCurrentCall,
        "finish_current_step" => ExhaustionPreset::FinishCurrentStep,
        "checkpoint_and_pause" => ExhaustionPreset::CheckpointAndPause,
        "hard_stop" => ExhaustionPreset::HardStop,
        "degrade_then_finalize" => ExhaustionPreset::DegradeThenFinalize,
        "request_extension" => ExhaustionPreset::RequestExtension,
        preset => {
            return Err(PyValueError::new_err(format!(
                "policy.preset has unknown preset {preset:?}"
            )));
        }
    };
    let unit = match required_string(object, "unit", "policy")? {
        "provider_call" => ExhaustionUnit::ProviderCall,
        "node" => ExhaustionUnit::Node,
        "agent_step" => ExhaustionUnit::AgentStep,
        "turn" => ExhaustionUnit::Turn,
        "map_item" => ExhaustionUnit::MapItem,
        "task" => ExhaustionUnit::Task,
        "trial" => ExhaustionUnit::Trial,
        "run" => ExhaustionUnit::Run,
        unit => {
            return Err(PyValueError::new_err(format!(
                "policy.unit has unknown unit {unit:?}"
            )));
        }
    };
    let continuation = object
        .get("continuation")
        .map(|value| parse_continuation_envelope(value, "policy.continuation"))
        .transpose()?;
    Ok(ExhaustionPolicy::from_preset(preset, unit, continuation))
}

fn build_runtime_bridge_plan(graph: &Value) -> PyResult<RuntimeBridgePlan> {
    let plan = compile_graph(graph);
    if !plan.ok() {
        let error_codes = plan
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == Severity::Error)
            .map(|diagnostic| diagnostic.code.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        return Err(PyValueError::new_err(format!(
            "graph did not compile: {error_codes}"
        )));
    }

    let spec = plan
        .normalized
        .get("spec")
        .and_then(Value::as_object)
        .ok_or_else(|| PyValueError::new_err("normalized graph spec must be an object"))?;
    let nodes = spec
        .get("nodes")
        .and_then(Value::as_object)
        .ok_or_else(|| PyValueError::new_err("normalized graph nodes must be an object"))?;
    let node_specs = nodes
        .iter()
        .map(|(node_id, node)| (node_id.clone(), node.clone()))
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
        if target_owner.starts_with('$') {
            continue;
        }
        if source_owner.starts_with('$') && source_owner != "$input" {
            continue;
        }
        let Some(source_port) = source_path
            .split('.')
            .next()
            .filter(|port| !port.is_empty())
        else {
            return Err(PyValueError::new_err(format!(
                "edge source {source:?} must include a port"
            )));
        };
        let Some(target_input) = target_path
            .split('.')
            .next()
            .filter(|port| !port.is_empty())
        else {
            return Err(PyValueError::new_err(format!(
                "edge target {target:?} must include an input"
            )));
        };
        let Some(dependencies) = dependencies_by_node.get_mut(target_owner) else {
            return Err(PyValueError::new_err(format!(
                "edge target references unknown node {target_owner:?}"
            )));
        };
        dependencies.push(InputDependency::value(
            target_input,
            PortRef::new(source_owner, source_port),
        ));
    }

    let scheduled_nodes = dependencies_by_node
        .into_iter()
        .map(|(node_id, dependencies)| ScheduledNode::new(node_id, dependencies))
        .collect::<Vec<_>>();

    Ok(RuntimeBridgePlan {
        graph_hash: plan.graph_hash,
        nodes: node_specs,
        edges,
        scheduled_nodes,
    })
}

fn runtime_with_inputs(
    scheduled_nodes: Vec<ScheduledNode>,
    inputs: &Value,
) -> PyResult<InProcessTestRuntime> {
    let mut runtime =
        InProcessTestRuntime::new("run-000001", scheduled_nodes).map_err(|error| {
            PyValueError::new_err(format!("failed to create test runtime: {error:?}"))
        })?;
    if let Some(input_object) = inputs.as_object() {
        for (input_name, value) in input_object {
            runtime = runtime.with_initial_value(PortRef::new("$input", input_name), value.clone());
        }
    }
    Ok(runtime)
}

fn collect_output_values(
    edges: &[Value],
    inputs: &Value,
    outputs_by_node: &BTreeMap<String, Value>,
    status: TestRunStatus,
) -> PyResult<Value> {
    let mut output_values = json!({});

    if status == TestRunStatus::Succeeded {
        for edge in edges {
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
            if target_owner != "$output" {
                continue;
            }
            let mut value = if source_owner == "$input" {
                inputs.clone()
            } else {
                outputs_by_node.get(source_owner).cloned().ok_or_else(|| {
                    PyRuntimeError::new_err(format!(
                        "output edge references missing node output {source_owner:?}"
                    ))
                })?
            };
            if !source_path.is_empty() {
                for part in source_path.split('.') {
                    value = value.get(part).cloned().ok_or_else(|| {
                        PyRuntimeError::new_err(format!(
                            "output edge source {source:?} is missing path segment {part:?}"
                        ))
                    })?;
                }
            }
            let target_parts = target_path.split('.').collect::<Vec<_>>();
            if target_parts.is_empty() || target_parts.iter().any(|part| part.is_empty()) {
                return Err(PyValueError::new_err(format!(
                    "output edge target {target:?} must include an output path"
                )));
            }
            let mut current = &mut output_values;
            for part in &target_parts[..target_parts.len() - 1] {
                let Some(current_object) = current.as_object_mut() else {
                    return Err(PyRuntimeError::new_err(format!(
                        "output path conflict at {target:?}"
                    )));
                };
                current = current_object
                    .entry((*part).to_owned())
                    .or_insert_with(|| json!({}));
            }
            let Some(current_object) = current.as_object_mut() else {
                return Err(PyRuntimeError::new_err(format!(
                    "output path conflict at {target:?}"
                )));
            };
            current_object.insert(target_parts[target_parts.len() - 1].to_owned(), value);
        }
    }

    Ok(output_values)
}

fn serialize_runtime_result(
    result: graphblocks_runtime_core::test_runtime::TestRunResult,
    graph_hash: String,
    output_values: Value,
) -> PyResult<String> {
    let status = match result.status {
        TestRunStatus::Succeeded => "succeeded",
        TestRunStatus::Failed => "failed",
        TestRunStatus::Cancelled => "cancelled",
    };
    let journal = result
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
        .collect::<Vec<_>>();
    let payload = json!({
        "runId": result.run_id,
        "graphHash": graph_hash,
        "status": status,
        "outputs": output_values,
        "journal": journal,
    });

    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize runtime result: {error}"))
    })
}

fn resolved_inputs_to_json(inputs: &BTreeMap<String, ResolvedInput>) -> Result<Value, BlockError> {
    let mut object = serde_json::Map::new();
    for (name, input) in inputs {
        match input {
            ResolvedInput::Value(value) => {
                object.insert(name.clone(), value.clone());
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
        "tools.resolve@1" => execute_resolve_tools(inputs, config),
        "agent.run@1" => execute_scripted_agent_run(inputs, config),
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
    let Some(prompt) = inputs.get("prompt") else {
        return Err(BlockError::new(
            "model.generate.missing_prompt",
            ErrorCategory::Configuration,
            "model.generate@1 requires prompt input",
            false,
        ));
    };
    let prompt = json_display(prompt);
    let response = config
        .get("script")
        .and_then(Value::as_object)
        .and_then(|script| script.get(&prompt))
        .or_else(|| config.get("response"))
        .map(json_display)
        .unwrap_or(prompt);

    Ok(json!({ "response": response }))
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
    let parse_string_map =
        |value: Option<&Value>, field: &str| -> Result<BTreeMap<String, String>, BlockError> {
            let Some(value) = value else {
                return Ok(BTreeMap::new());
            };
            let Some(object) = value.as_object() else {
                return Err(BlockError::new(
                    format!("tools.resolve.invalid_{field}"),
                    ErrorCategory::Configuration,
                    format!("tools.resolve@1 {field} must be an object"),
                    false,
                ));
            };
            let mut parsed = BTreeMap::new();
            for (key, value) in object {
                let Some(value) = value.as_str() else {
                    return Err(BlockError::new(
                        format!("tools.resolve.invalid_{field}"),
                        ErrorCategory::Configuration,
                        format!("tools.resolve@1 {field} values must be strings"),
                        false,
                    ));
                };
                parsed.insert(key.clone(), value.to_owned());
            }
            Ok(parsed)
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
            let name = definition
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    BlockError::new(
                        "tools.resolve.invalid_definition",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.definitions[{index}].name must be a string"
                        ),
                        false,
                    )
                })?;
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
            let binding_id = binding
                .get("bindingId")
                .or_else(|| binding.get("binding_id"))
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].bindingId must be a string"
                        ),
                        false,
                    )
                })?;
            let tool_name = binding
                .get("toolName")
                .or_else(|| binding.get("tool_name"))
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].toolName must be a string"
                        ),
                        false,
                    )
                })?;
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
                    let block = implementation
                        .get("block")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].implementation.block must be a string"
                                ),
                                false,
                            )
                        })?;
                    let mut implementation = BlockToolImplementation::new(block);
                    implementation.input_mapping = parse_string_map(
                        binding
                            .get("implementation")
                            .and_then(|value| value.get("inputMapping").or_else(|| value.get("input_mapping"))),
                        "implementation.inputMapping",
                    )?;
                    implementation.output_mapping = parse_string_map(
                        binding
                            .get("implementation")
                            .and_then(|value| value.get("outputMapping").or_else(|| value.get("output_mapping"))),
                        "implementation.outputMapping",
                    )?;
                    ToolImplementation::Block(implementation)
                }
                "graph" => {
                    let graph = implementation
                        .get("graph")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].implementation.graph must be a string"
                                ),
                                false,
                            )
                        })?;
                    let mut implementation = GraphToolImplementation::new(graph);
                    implementation.input_mapping = parse_string_map(
                        binding
                            .get("implementation")
                            .and_then(|value| value.get("inputMapping").or_else(|| value.get("input_mapping"))),
                        "implementation.inputMapping",
                    )?;
                    implementation.output_mapping = parse_string_map(
                        binding
                            .get("implementation")
                            .and_then(|value| value.get("outputMapping").or_else(|| value.get("output_mapping"))),
                        "implementation.outputMapping",
                    )?;
                    ToolImplementation::Graph(implementation)
                }
                "remote" => ToolImplementation::Remote(RemoteToolImplementation::new(
                    implementation
                        .get("connection")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].implementation.connection must be a string"
                                ),
                                false,
                            )
                        })?,
                    implementation
                        .get("operation")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].implementation.operation must be a string"
                                ),
                                false,
                            )
                        })?,
                )),
                "mcp" => ToolImplementation::Mcp(McpToolImplementation::new(
                    implementation
                        .get("server")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].implementation.server must be a string"
                                ),
                                false,
                            )
                        })?,
                    implementation
                        .get("remoteName")
                        .or_else(|| implementation.get("remote_name"))
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].implementation.remoteName must be a string"
                                ),
                                false,
                            )
                        })?,
                )),
                "openapi" => ToolImplementation::OpenApi(OpenApiToolImplementation::new(
                    implementation
                        .get("connection")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].implementation.connection must be a string"
                                ),
                                false,
                            )
                        })?,
                    implementation
                        .get("operationId")
                        .or_else(|| implementation.get("operation_id"))
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].implementation.operationId must be a string"
                                ),
                                false,
                            )
                        })?,
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
                let Some(effects) = effects.as_array() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].effects must be an array"
                        ),
                        false,
                    ));
                };
                let mut parsed_effects = BTreeSet::new();
                for (effect_index, effect) in effects.iter().enumerate() {
                    let Some(effect) = effect.as_str() else {
                        return Err(BlockError::new(
                            "tools.resolve.invalid_binding",
                            ErrorCategory::Configuration,
                            format!(
                                "tools.resolve@1 config.bindings[{index}].effects[{effect_index}] must be a string"
                            ),
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
                parsed = parsed.with_effects(parsed_effects);
            }
            if let Some(approval) = binding.get("approval").filter(|value| !value.is_null()) {
                let Some(approval) = approval.as_str() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].approval must be a string"
                        ),
                        false,
                    ));
                };
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
            if let Some(idempotency) = binding.get("idempotency").filter(|value| !value.is_null()) {
                let Some(idempotency) = idempotency.as_str() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].idempotency must be a string"
                        ),
                        false,
                    ));
                };
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
            if let Some(cancellation) = binding.get("cancellation").filter(|value| !value.is_null())
            {
                let Some(cancellation) = cancellation.as_str() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].cancellation must be a string"
                        ),
                        false,
                    ));
                };
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
            if let Some(result_mode) = binding
                .get("resultMode")
                .or_else(|| binding.get("result_mode"))
                .filter(|value| !value.is_null())
            {
                let Some(result_mode) = result_mode.as_str() else {
                    return Err(BlockError::new(
                        "tools.resolve.invalid_binding",
                        ErrorCategory::Configuration,
                        format!(
                            "tools.resolve@1 config.bindings[{index}].resultMode must be a string"
                        ),
                        false,
                    ));
                };
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
            if let Some(retry_policy_ref) = binding
                .get("retryPolicyRef")
                .or_else(|| binding.get("retry_policy_ref"))
                .filter(|value| !value.is_null())
            {
                parsed.retry_policy_ref = Some(
                    retry_policy_ref
                        .as_str()
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].retryPolicyRef must be a string"
                                ),
                                false,
                            )
                        })?
                        .to_owned(),
                );
            }
            if let Some(policy_profile_ref) = binding
                .get("policyProfileRef")
                .or_else(|| binding.get("policy_profile_ref"))
                .filter(|value| !value.is_null())
            {
                parsed.policy_profile_ref = Some(
                    policy_profile_ref
                        .as_str()
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].policyProfileRef must be a string"
                                ),
                                false,
                            )
                        })?
                        .to_owned(),
                );
            }
            if let Some(execution_class) = binding
                .get("executionClass")
                .or_else(|| binding.get("execution_class"))
                .filter(|value| !value.is_null())
            {
                parsed.execution_class = Some(
                    execution_class
                        .as_str()
                        .ok_or_else(|| {
                            BlockError::new(
                                "tools.resolve.invalid_binding",
                                ErrorCategory::Configuration,
                                format!(
                                    "tools.resolve@1 config.bindings[{index}].executionClass must be a string"
                                ),
                                false,
                            )
                        })?
                        .to_owned(),
                );
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
        let parse_tool_names =
            |camel_key: &str, snake_key: &str| -> Result<Option<Vec<String>>, BlockError> {
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
                            format!(
                                "tools.resolve@1 config.scope.{camel_key}[{index}] must be a string"
                            ),
                            false,
                        ));
                    };
                    names.push(item.to_owned());
                }
                Ok(Some(names))
            };
        if let Some(tools) = parse_tool_names("applicationTools", "application_tools")? {
            scope = scope.with_application_tools(tools);
        }
        if let Some(tools) = parse_tool_names("graphTools", "graph_tools")? {
            scope = scope.with_graph_tools(tools);
        }
        if let Some(tools) = parse_tool_names("principalTools", "principal_tools")? {
            scope = scope.with_principal_tools(tools);
        }
        if let Some(tools) = parse_tool_names("tenantPolicyTools", "tenant_policy_tools")? {
            scope = scope.with_tenant_policy_tools(tools);
        }
        if let Some(tools) =
            parse_tool_names("conversationPolicyTools", "conversation_policy_tools")?
        {
            scope = scope.with_conversation_policy_tools(tools);
        }
        if let Some(tools) =
            parse_tool_names("dataClassificationTools", "data_classification_tools")?
        {
            scope = scope.with_data_classification_tools(tools);
        }
        if let Some(tools) = parse_tool_names("deploymentTools", "deployment_tools")? {
            scope = scope.with_deployment_tools(tools);
        }
        if let Some(tools) = parse_tool_names("budgetTools", "budget_tools")? {
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
    let resolved_tool_json = |tool: &ResolvedTool| {
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
    };
    let tools = resolved.iter().map(resolved_tool_json).collect::<Vec<_>>();
    Ok(json!({ "tools": tools }))
}

fn execute_scripted_agent_run(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let Some(tools) = inputs.get("tools").and_then(Value::as_array) else {
        return Err(BlockError::new(
            "agent.run.invalid_tools",
            ErrorCategory::Configuration,
            "agent.run@1 input 'tools' must be a list",
            false,
        ));
    };
    let Some(messages) = inputs.get("messages").and_then(Value::as_array) else {
        return Err(BlockError::new(
            "agent.run.invalid_messages",
            ErrorCategory::Configuration,
            "agent.run@1 input 'messages' must be a list",
            false,
        ));
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

    Ok(json!({
        "candidate": {
            "text": text,
            "finishReason": finish_reason,
            "toolCount": tools.len(),
        }
    }))
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
    let Some(transaction) = inputs.get("transaction").and_then(Value::as_object) else {
        return Err(BlockError::new(
            "conversation.commit_turn.missing_transaction",
            ErrorCategory::Configuration,
            "conversation.commit_turn@1 requires transaction input",
            false,
        ));
    };
    if transaction.get("status").and_then(Value::as_str) == Some("policy_stopped") {
        return Err(BlockError::new(
            "conversation.commit_turn.policy_stopped",
            ErrorCategory::Policy,
            "conversation.commit_turn@1 cannot commit policy-stopped turn",
            false,
        ));
    }
    let Some(candidate) = inputs.get("candidate") else {
        return Err(BlockError::new(
            "conversation.commit_turn.missing_candidate",
            ErrorCategory::Configuration,
            "conversation.commit_turn@1 requires candidate input",
            false,
        ));
    };
    let text = candidate
        .get("text")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| json_display(candidate));

    Ok(json!({
        "answer": {
            "conversationId": transaction
                .get("conversationId")
                .and_then(Value::as_str)
                .unwrap_or("conversation-default"),
            "text": text,
            "turnId": transaction
                .get("turnId")
                .and_then(Value::as_str)
                .unwrap_or("turn-000001"),
        }
    }))
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

#[pyfunction]
fn run_test_graph_json(
    graph_json: &str,
    inputs_json: &str,
    node_outputs_json: &str,
) -> PyResult<String> {
    let graph = parse_json_argument(graph_json, "graph document")?;
    let inputs = parse_json_argument(inputs_json, "runtime inputs")?;
    let node_outputs = parse_json_argument(node_outputs_json, "node outputs")?;
    let Some(node_outputs) = node_outputs.as_object() else {
        return Err(PyValueError::new_err(
            "node outputs JSON must be an object keyed by node id",
        ));
    };
    let bridge_plan = build_runtime_bridge_plan(&graph)?;
    let mut runtime = runtime_with_inputs(bridge_plan.scheduled_nodes, &inputs)?;
    let mut executor = JsonNodeExecutor {
        outputs_by_node: node_outputs
            .iter()
            .map(|(node_id, outputs)| (node_id.clone(), outputs.clone()))
            .collect(),
    };
    let result = runtime.run(&mut executor).map_err(|error| {
        PyRuntimeError::new_err(format!("test runtime execution failed: {error:?}"))
    })?;
    let output_values = collect_output_values(
        &bridge_plan.edges,
        &inputs,
        &executor.outputs_by_node,
        result.status,
    )?;
    serialize_runtime_result(result, bridge_plan.graph_hash, output_values)
}

#[pyfunction]
fn run_stdlib_graph_json(graph_json: &str, inputs_json: &str) -> PyResult<String> {
    let graph = parse_json_argument(graph_json, "graph document")?;
    let inputs = parse_json_argument(inputs_json, "runtime inputs")?;
    let bridge_plan = build_runtime_bridge_plan(&graph)?;
    let mut runtime = runtime_with_inputs(bridge_plan.scheduled_nodes, &inputs)?;
    let mut executor = StdlibExecutor {
        nodes: bridge_plan.nodes,
        outputs_by_node: BTreeMap::new(),
    };
    let result = runtime.run(&mut executor).map_err(|error| {
        PyRuntimeError::new_err(format!("stdlib runtime execution failed: {error:?}"))
    })?;
    let output_values = collect_output_values(
        &bridge_plan.edges,
        &inputs,
        &executor.outputs_by_node,
        result.status,
    )?;
    serialize_runtime_result(result, bridge_plan.graph_hash, output_values)
}

#[pyfunction]
fn decide_agent_step_json(spec_json: &str, request_json: &str) -> PyResult<String> {
    let spec_value = parse_json_argument(spec_json, "agent spec")?;
    let request_value = parse_json_argument(request_json, "agent step request")?;
    let spec_object = json_object(&spec_value, "agent spec")?;
    let request_object = json_object(&request_value, "agent step request")?;

    let mut spec = AgentSpec::new(required_string(spec_object, "modelPool", "agent spec")?);
    if let Some(tools) = spec_object.get("tools") {
        let tools = tools
            .as_array()
            .ok_or_else(|| PyValueError::new_err("agent spec.tools must be an array"))?;
        let mut parsed_tools = Vec::new();
        for (index, tool) in tools.iter().enumerate() {
            let Some(tool) = tool.as_str() else {
                return Err(PyValueError::new_err(format!(
                    "agent spec.tools[{index}] must be a string"
                )));
            };
            parsed_tools.push(tool.to_owned());
        }
        spec = spec.with_tools(parsed_tools);
    }
    if let Some(max_steps) = spec_object.get("maxSteps") {
        let Some(max_steps) = max_steps.as_u64() else {
            return Err(PyValueError::new_err(
                "agent spec.maxSteps must be an unsigned integer",
            ));
        };
        spec = spec
            .with_max_steps(usize::try_from(max_steps).map_err(|_| {
                PyValueError::new_err("agent spec.maxSteps exceeds platform usize")
            })?);
    }
    if let Some(completion_reserve_units) = spec_object.get("completionReserveUnits") {
        let Some(completion_reserve_units) = completion_reserve_units.as_i64() else {
            return Err(PyValueError::new_err(
                "agent spec.completionReserveUnits must be an integer",
            ));
        };
        spec = spec.with_completion_reserve_units(completion_reserve_units);
    }

    let completed_steps = required_u64(request_object, "completedSteps", "agent step request")?;
    let completed_steps = usize::try_from(completed_steps).map_err(|_| {
        PyValueError::new_err("agent step request.completedSteps exceeds platform usize")
    })?;
    let remaining_budget_units = request_object
        .get("remainingBudgetUnits")
        .and_then(Value::as_i64)
        .ok_or_else(|| {
            PyValueError::new_err("agent step request.remainingBudgetUnits must be an integer")
        })?;

    let decision =
        AgentLoopController::new(spec).decide_next_step(completed_steps, remaining_budget_units);
    let (disposition, reason) = match decision {
        AgentLoopDecision::Continue { reason } => ("continue", reason),
        AgentLoopDecision::Finalize { reason } => ("finalize", reason),
        AgentLoopDecision::Stop { reason } => ("stop", reason),
    };
    let payload = json!({
        "disposition": disposition,
        "reason": reason,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize agent step decision: {error}"))
    })
}

#[pyfunction]
fn admit_exhaustion_work_json(policy_json: &str, request_json: &str) -> PyResult<String> {
    let policy_value = parse_json_argument(policy_json, "exhaustion policy")?;
    let request_value = parse_json_argument(request_json, "exhaustion admission request")?;
    let policy = parse_exhaustion_policy(&policy_value)?;
    let request = json_object(&request_value, "request")?;
    let atomic_unit_id = required_string(request, "atomicUnitId", "request")?;
    let admission_epoch = required_u64(request, "admissionEpoch", "request")?;
    let work_kind = parse_work_kind(
        request
            .get("workKind")
            .ok_or_else(|| PyValueError::new_err("request.workKind is required"))?,
        "request.workKind",
    )?;
    let work_epoch = required_u64(request, "workEpoch", "request")?;
    let permit = request
        .get("permit")
        .map(|value| parse_budget_permit(value, "request.permit"))
        .transpose()?;
    let requested_usage = parse_usage_amounts(request, "requestedUsage", "request")?;
    let continuation_permit = request
        .get("continuationPermit")
        .map(|value| parse_budget_permit(value, "request.continuationPermit"))
        .transpose()?;
    let validation_time = request
        .get("validationTime")
        .map(|value| {
            value
                .as_str()
                .ok_or_else(|| PyValueError::new_err("request.validationTime must be a string"))
        })
        .transpose()?;
    let mut controller = ExhaustionController::new(policy, atomic_unit_id, admission_epoch);
    if let Some(continuation_permit) = continuation_permit {
        controller = controller.with_continuation_permit(continuation_permit);
    }
    if let Some(validation_time) = validation_time {
        controller = controller.with_validation_time(validation_time);
    }

    let decision =
        controller.admit_with_usage(work_kind, work_epoch, permit.as_ref(), requested_usage);
    let payload = json!({
        "allowed": decision.allowed,
        "reason": decision.reason,
        "usedAdditionalSteps": controller.used_additional_steps,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize exhaustion admission result: {error}"
        ))
    })
}

#[pyfunction]
fn evaluate_output_gate_json(gate_json: &str, operations_json: &str) -> PyResult<String> {
    let gate_value = parse_json_argument(gate_json, "output gate")?;
    let operations_value = parse_json_argument(operations_json, "output gate operations")?;
    let gate_object = json_object(&gate_value, "gate")?;
    let stream_id = required_alias_string(gate_object, "streamId", "stream_id", "gate")?;
    let response_id = required_alias_string(gate_object, "responseId", "response_id", "gate")?;
    let last_generated_sequence = optional_alias_u64(
        gate_object,
        "lastGeneratedSequence",
        "last_generated_sequence",
        "gate",
    )?
    .unwrap_or(0);
    let last_policy_accepted_sequence = optional_alias_u64(
        gate_object,
        "lastPolicyAcceptedSequence",
        "last_policy_accepted_sequence",
        "gate",
    )?
    .unwrap_or(0);
    let last_client_delivered_sequence = optional_alias_u64(
        gate_object,
        "lastClientDeliveredSequence",
        "last_client_delivered_sequence",
        "gate",
    )?
    .unwrap_or(0);
    let mut pending_chunks = Vec::new();
    if let Some(pending) = gate_object.get("pending") {
        let Some(pending) = pending.as_array() else {
            return Err(PyValueError::new_err("gate.pending must be an array"));
        };
        for (pending_index, pending_chunk) in pending.iter().enumerate() {
            let pending_label = format!("gate.pending[{pending_index}]");
            let pending_chunk = json_object(pending_chunk, &pending_label)?;
            pending_chunks.push(GenerationChunk::text(
                optional_alias_string(pending_chunk, "streamId", "stream_id", &pending_label)?
                    .unwrap_or(stream_id),
                optional_alias_string(pending_chunk, "responseId", "response_id", &pending_label)?
                    .unwrap_or(response_id),
                required_u64(pending_chunk, "sequence", &pending_label)?,
                required_string(pending_chunk, "text", &pending_label)?,
            ));
        }
    }
    let mut gate = if let Some(cutoff) = gate_object.get("cutoff") {
        let cutoff = json_object(cutoff, "gate.cutoff")?;
        let terminal_reason = match required_alias_string(
            cutoff,
            "terminalReason",
            "terminal_reason",
            "gate.cutoff",
        )? {
            "policy_denied" => TerminalReason::PolicyDenied,
            "budget_exhausted" => TerminalReason::BudgetExhausted,
            "cancelled" => TerminalReason::Cancelled,
            "client_disconnected" => TerminalReason::ClientDisconnected,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.cutoff.terminalReason has unknown reason {value:?}"
                )));
            }
        };
        let draft_disposition = match required_alias_string(
            cutoff,
            "draftDisposition",
            "draft_disposition",
            "gate.cutoff",
        )? {
            "keep" => DraftDisposition::Keep,
            "mark_incomplete" => DraftDisposition::MarkIncomplete,
            "retract" => DraftDisposition::Retract,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.cutoff.draftDisposition has unknown disposition {value:?}"
                )));
            }
        };
        let durable_result = match required_alias_string(
            cutoff,
            "durableResult",
            "durable_result",
            "gate.cutoff",
        )? {
            "none" => DurableResult::None,
            "incomplete" => DurableResult::Incomplete,
            "partial" => DurableResult::Partial,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.cutoff.durableResult has unknown result {value:?}"
                )));
            }
        };
        let turn_id =
            optional_alias_string(cutoff, "turnId", "turn_id", "gate.cutoff")?.map(str::to_owned);
        let policy_decision_id = optional_alias_string(
            cutoff,
            "policyDecisionId",
            "policy_decision_id",
            "gate.cutoff",
        )?
        .map(str::to_owned);
        OutputDeliveryGate::from_cutoff(OutputCutoff {
            stream_id: optional_alias_string(cutoff, "streamId", "stream_id", "gate.cutoff")?
                .unwrap_or(stream_id)
                .to_owned(),
            response_id: optional_alias_string(cutoff, "responseId", "response_id", "gate.cutoff")?
                .unwrap_or(response_id)
                .to_owned(),
            turn_id,
            last_generated_sequence: required_alias_u64(
                cutoff,
                "lastGeneratedSequence",
                "last_generated_sequence",
                "gate.cutoff",
            )?,
            last_policy_accepted_sequence: required_alias_u64(
                cutoff,
                "lastPolicyAcceptedSequence",
                "last_policy_accepted_sequence",
                "gate.cutoff",
            )?,
            last_client_delivered_sequence: required_alias_u64(
                cutoff,
                "lastClientDeliveredSequence",
                "last_client_delivered_sequence",
                "gate.cutoff",
            )?,
            terminal_reason,
            draft_disposition,
            durable_result,
            policy_decision_id,
            occurred_at_unix_ms: required_alias_u64(
                cutoff,
                "occurredAtUnixMs",
                "occurred_at_unix_ms",
                "gate.cutoff",
            )?,
        })
    } else {
        OutputDeliveryGate::from_state(
            stream_id,
            response_id,
            pending_chunks,
            last_generated_sequence,
            last_policy_accepted_sequence,
            last_client_delivered_sequence,
        )
    }
    .map_err(|error| PyValueError::new_err(format!("invalid output gate state: {error:?}")))?;
    if let Some(turn_id) = optional_alias_string(gate_object, "turnId", "turn_id", "gate")? {
        gate = gate.with_turn_id(turn_id);
    }
    if let Some(delivery_policy) = gate_object
        .get("deliveryPolicy")
        .or_else(|| gate_object.get("delivery_policy"))
    {
        let delivery_policy = json_object(delivery_policy, "gate.deliveryPolicy")?;
        let on_violation = match optional_alias_string(
            delivery_policy,
            "onViolation",
            "on_violation",
            "gate.deliveryPolicy",
        )?
        .unwrap_or("abort_response")
        {
            "abort_response" => ViolationAction::AbortResponse,
            "abort_turn" => ViolationAction::AbortTurn,
            "redact" => ViolationAction::Redact,
            "replace" => ViolationAction::Replace,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.deliveryPolicy.onViolation has unknown action {value:?}"
                )));
            }
        };
        let delivered_draft_disposition = match optional_alias_string(
            delivery_policy,
            "deliveredDraftDisposition",
            "delivered_draft_disposition",
            "gate.deliveryPolicy",
        )?
        .unwrap_or("retract")
        {
            "keep" => DraftDisposition::Keep,
            "mark_incomplete" => DraftDisposition::MarkIncomplete,
            "retract" => DraftDisposition::Retract,
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.deliveryPolicy.deliveredDraftDisposition has unknown disposition {value:?}"
                )));
            }
        };
        let mut policy = match required_string(delivery_policy, "mode", "gate.deliveryPolicy")? {
            "buffer_until_commit" => OutputDeliveryPolicy::buffer_until_commit(on_violation),
            "bounded_holdback" => {
                OutputDeliveryPolicy::bounded_holdback(on_violation, delivered_draft_disposition)
            }
            "immediate_draft" => {
                OutputDeliveryPolicy::immediate_draft(on_violation, delivered_draft_disposition)
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "gate.deliveryPolicy.mode has unknown mode {value:?}"
                )));
            }
        };
        if let Some(value) = optional_alias_u64(
            delivery_policy,
            "holdbackMaxTokens",
            "holdback_max_tokens",
            "gate.deliveryPolicy",
        )? {
            policy = policy.with_holdback_max_tokens(value);
        }
        if let Some(value) = optional_alias_u64(
            delivery_policy,
            "holdbackMaxBytes",
            "holdback_max_bytes",
            "gate.deliveryPolicy",
        )? {
            policy = policy.with_holdback_max_bytes(value);
        }
        if let Some(value) = optional_alias_u64(
            delivery_policy,
            "holdbackMaxDurationMs",
            "holdback_max_duration_ms",
            "gate.deliveryPolicy",
        )? {
            policy = policy.with_holdback_max_duration_ms(value);
        }
        if let Some(boundaries) = delivery_policy
            .get("flushBoundaries")
            .or_else(|| delivery_policy.get("flush_boundaries"))
        {
            let Some(boundaries) = boundaries.as_array() else {
                return Err(PyValueError::new_err(
                    "gate.deliveryPolicy.flushBoundaries must be an array",
                ));
            };
            let mut parsed_boundaries = Vec::new();
            for (index, boundary) in boundaries.iter().enumerate() {
                let Some(boundary) = boundary.as_str() else {
                    return Err(PyValueError::new_err(format!(
                        "gate.deliveryPolicy.flushBoundaries[{index}] must be a string"
                    )));
                };
                parsed_boundaries.push(match boundary {
                    "token" => FlushBoundary::Token,
                    "sentence" => FlushBoundary::Sentence,
                    "paragraph" => FlushBoundary::Paragraph,
                    "content_part" => FlushBoundary::ContentPart,
                    "tool_call" => FlushBoundary::ToolCall,
                    "response" => FlushBoundary::Response,
                    value => {
                        return Err(PyValueError::new_err(format!(
                            "gate.deliveryPolicy.flushBoundaries[{index}] has unknown boundary {value:?}"
                        )));
                    }
                });
            }
            policy = policy.flush_on(parsed_boundaries);
        }
        gate = gate.with_delivery_policy(policy).map_err(|error| {
            PyValueError::new_err(format!("invalid output gate policy: {error:?}"))
        })?;
    }

    let operations = operations_value
        .as_array()
        .ok_or_else(|| PyValueError::new_err("output gate operations JSON must be an array"))?;
    let mut deliveries = Vec::new();
    let mut decisions = Vec::new();
    for (operation_index, operation) in operations.iter().enumerate() {
        let operation = json_object(operation, &format!("operations[{operation_index}]"))?;
        match required_string(operation, "kind", &format!("operations[{operation_index}]"))? {
            "chunk" => {
                let sequence = required_u64(
                    operation,
                    "sequence",
                    &format!("operations[{operation_index}]"),
                )?;
                let text =
                    required_string(operation, "text", &format!("operations[{operation_index}]"))?;
                let operation_stream_id = optional_alias_string(
                    operation,
                    "streamId",
                    "stream_id",
                    &format!("operations[{operation_index}]"),
                )?
                .unwrap_or(stream_id);
                let operation_response_id = optional_alias_string(
                    operation,
                    "responseId",
                    "response_id",
                    &format!("operations[{operation_index}]"),
                )?
                .unwrap_or(response_id);
                let deliverable = gate
                    .record_chunk(GenerationChunk::text(
                        operation_stream_id,
                        operation_response_id,
                        sequence,
                        text,
                    ))
                    .map_err(|error| {
                        PyValueError::new_err(format!(
                            "output gate operation {operation_index} failed: {error:?}"
                        ))
                    })?;
                if !deliverable.is_empty() {
                    deliveries.push(json!({
                        "operationIndex": operation_index,
                        "draft": true,
                        "chunks": deliverable
                            .iter()
                            .map(|chunk| json!({
                                "streamId": chunk.stream_id,
                                "responseId": chunk.response_id,
                                "sequence": chunk.sequence,
                                "text": chunk.text,
                            }))
                            .collect::<Vec<_>>(),
                    }));
                }
            }
            "commit" => {
                let deliverable = gate.commit_accepted_output();
                if !deliverable.is_empty() {
                    deliveries.push(json!({
                        "operationIndex": operation_index,
                        "chunks": deliverable
                            .iter()
                            .map(|chunk| json!({
                                "streamId": chunk.stream_id,
                                "responseId": chunk.response_id,
                                "sequence": chunk.sequence,
                                "text": chunk.text,
                            }))
                            .collect::<Vec<_>>(),
                    }));
                }
            }
            "decision" => {
                let label = format!("operations[{operation_index}]");
                let decision_id =
                    required_alias_string(operation, "decisionId", "decision_id", &label)?;
                let input_digest =
                    required_alias_string(operation, "inputDigest", "input_digest", &label)?;
                let disposition = required_string(operation, "disposition", &label)?;
                let accepted_through_sequence = optional_alias_u64(
                    operation,
                    "acceptedThroughSequence",
                    "accepted_through_sequence",
                    &label,
                )?;
                let mut decision = match disposition {
                    "allow" => OutputPolicyDecision::allow(
                        decision_id,
                        accepted_through_sequence,
                        input_digest,
                    ),
                    "hold" => OutputPolicyDecision::hold(decision_id, input_digest),
                    "redact" | "replace" => {
                        let mut replacement_chunks = Vec::new();
                        let replacement_parts = operation
                            .get("replacementParts")
                            .or_else(|| operation.get("replacement_parts"));
                        let replacement_chunks_value = operation
                            .get("replacementChunks")
                            .or_else(|| operation.get("replacement_chunks"));
                        if replacement_parts.is_some() && replacement_chunks_value.is_some() {
                            return Err(PyValueError::new_err(format!(
                                "{label} must not specify both replacementParts and replacementChunks"
                            )));
                        }
                        if let Some(parts) = replacement_parts {
                            let Some(parts) = parts.as_array() else {
                                return Err(PyValueError::new_err(format!(
                                    "{label}.replacementParts must be an array"
                                )));
                            };
                            let base_sequence = accepted_through_sequence
                                .unwrap_or_else(|| gate.last_generated_sequence());
                            for (part_index, part) in parts.iter().enumerate() {
                                let part_label = format!("{label}.replacementParts[{part_index}]");
                                let part = json_object(part, &part_label)?;
                                let kind = required_string(part, "kind", &part_label)?;
                                if kind != "text" {
                                    return Err(PyValueError::new_err(format!(
                                        "{part_label}.kind must be \"text\""
                                    )));
                                }
                                let sequence = base_sequence
                                    .checked_add(part_index as u64)
                                    .ok_or_else(|| {
                                        PyValueError::new_err(format!(
                                            "{part_label} replacement sequence overflowed"
                                        ))
                                    })?;
                                replacement_chunks.push(GenerationChunk::text(
                                    stream_id,
                                    response_id,
                                    sequence,
                                    required_string(part, "text", &part_label)?,
                                ));
                            }
                        } else if let Some(chunks) = replacement_chunks_value {
                            let Some(chunks) = chunks.as_array() else {
                                return Err(PyValueError::new_err(format!(
                                    "{label}.replacementChunks must be an array"
                                )));
                            };
                            for (chunk_index, chunk) in chunks.iter().enumerate() {
                                let chunk = json_object(
                                    chunk,
                                    &format!("{label}.replacementChunks[{chunk_index}]"),
                                )?;
                                let chunk_label =
                                    format!("{label}.replacementChunks[{chunk_index}]");
                                replacement_chunks.push(GenerationChunk::text(
                                    optional_alias_string(
                                        chunk,
                                        "streamId",
                                        "stream_id",
                                        &chunk_label,
                                    )?
                                    .unwrap_or(stream_id),
                                    optional_alias_string(
                                        chunk,
                                        "responseId",
                                        "response_id",
                                        &chunk_label,
                                    )?
                                    .unwrap_or(response_id),
                                    required_u64(chunk, "sequence", &chunk_label)?,
                                    required_string(chunk, "text", &chunk_label)?,
                                ));
                            }
                        }
                        if disposition == "redact" {
                            OutputPolicyDecision::redact(
                                decision_id,
                                accepted_through_sequence,
                                replacement_chunks,
                                input_digest,
                            )
                        } else {
                            OutputPolicyDecision::replace(
                                decision_id,
                                accepted_through_sequence,
                                replacement_chunks,
                                input_digest,
                            )
                        }
                    }
                    "abort_response" => {
                        let decision =
                            OutputPolicyDecision::abort_response(decision_id, input_digest);
                        if let Some(sequence) = accepted_through_sequence {
                            decision.with_accepted_through_sequence(sequence)
                        } else {
                            decision
                        }
                    }
                    "abort_turn" => {
                        let decision = OutputPolicyDecision::abort_turn(decision_id, input_digest);
                        if let Some(sequence) = accepted_through_sequence {
                            decision.with_accepted_through_sequence(sequence)
                        } else {
                            decision
                        }
                    }
                    "deny_commit" => {
                        let decision = OutputPolicyDecision::deny_commit(decision_id, input_digest);
                        if let Some(sequence) = accepted_through_sequence {
                            decision.with_accepted_through_sequence(sequence)
                        } else {
                            decision
                        }
                    }
                    value => {
                        return Err(PyValueError::new_err(format!(
                            "{label}.disposition has unknown disposition {value:?}"
                        )));
                    }
                };
                if let Some(redactions) = operation.get("redactions") {
                    let Some(redactions) = redactions.as_array() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.redactions must be an array"
                        )));
                    };
                    let mut parsed_redactions = Vec::new();
                    for (redaction_index, redaction) in redactions.iter().enumerate() {
                        let redaction_label = format!("{label}.redactions[{redaction_index}]");
                        let redaction = json_object(redaction, &redaction_label)?;
                        parsed_redactions.push(RedactionInstruction::text_range(
                            required_string(redaction, "path", &redaction_label)?,
                            required_u64(redaction, "start", &redaction_label)?,
                            required_u64(redaction, "end", &redaction_label)?,
                            required_string(redaction, "replacement", &redaction_label)?,
                        ));
                    }
                    decision = decision.with_redactions(parsed_redactions);
                }
                if let Some(values) = operation
                    .get("reasonCodes")
                    .or_else(|| operation.get("reason_codes"))
                {
                    let Some(values) = values.as_array() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.reasonCodes must be an array"
                        )));
                    };
                    let mut parsed_reason_codes = Vec::new();
                    for (value_index, value) in values.iter().enumerate() {
                        let Some(value) = value.as_str() else {
                            return Err(PyValueError::new_err(format!(
                                "{label}.reasonCodes[{value_index}] must be a string"
                            )));
                        };
                        parsed_reason_codes.push(value.to_owned());
                    }
                    decision = decision.with_reason_codes(parsed_reason_codes);
                }
                if let Some(values) = operation
                    .get("policyRefs")
                    .or_else(|| operation.get("policy_refs"))
                {
                    let Some(values) = values.as_array() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.policyRefs must be an array"
                        )));
                    };
                    let mut parsed_policy_refs = Vec::new();
                    for (value_index, value) in values.iter().enumerate() {
                        let Some(value) = value.as_str() else {
                            return Err(PyValueError::new_err(format!(
                                "{label}.policyRefs[{value_index}] must be a string"
                            )));
                        };
                        parsed_policy_refs.push(value.to_owned());
                    }
                    decision = decision.with_policy_refs(parsed_policy_refs);
                }
                if let Some(value) = operation
                    .get("providerCancellation")
                    .or_else(|| operation.get("provider_cancellation"))
                {
                    let Some(value) = value.as_str() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.providerCancellation must be a string"
                        )));
                    };
                    decision = decision.with_provider_cancellation(match value {
                        "none" => ProviderCancellation::None,
                        "request" => ProviderCancellation::Request,
                        "required_if_supported" => ProviderCancellation::RequiredIfSupported,
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{label}.providerCancellation has unknown mode {value:?}"
                            )));
                        }
                    });
                }
                if let Some(value) = operation
                    .get("draftDisposition")
                    .or_else(|| operation.get("draft_disposition"))
                {
                    let Some(value) = value.as_str() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.draftDisposition must be a string"
                        )));
                    };
                    decision = decision.with_draft_disposition(match value {
                        "keep" => DraftDisposition::Keep,
                        "mark_incomplete" => DraftDisposition::MarkIncomplete,
                        "retract" => DraftDisposition::Retract,
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{label}.draftDisposition has unknown disposition {value:?}"
                            )));
                        }
                    });
                }
                if let Some(value) = operation
                    .get("pendingToolCalls")
                    .or_else(|| operation.get("pending_tool_calls"))
                {
                    let Some(value) = value.as_str() else {
                        return Err(PyValueError::new_err(format!(
                            "{label}.pendingToolCalls must be a string"
                        )));
                    };
                    decision = decision.with_pending_tool_calls(match value {
                        "keep" => PendingToolCallsDisposition::Keep,
                        "deny" => PendingToolCallsDisposition::Deny,
                        "cancel_admitted" => PendingToolCallsDisposition::CancelAdmitted,
                        value => {
                            return Err(PyValueError::new_err(format!(
                                "{label}.pendingToolCalls has unknown disposition {value:?}"
                            )));
                        }
                    });
                }
                if let Some(value) = optional_alias_u64(
                    operation,
                    "evaluatedAtUnixMs",
                    "evaluated_at_unix_ms",
                    &label,
                )? {
                    decision = decision.evaluated_at_unix_ms(value);
                }
                let occurred_at_unix_ms = required_alias_u64(
                    operation,
                    "occurredAtUnixMs",
                    "occurred_at_unix_ms",
                    &label,
                )?;
                let decision_trace = json!({
                    "operationIndex": operation_index,
                    "decisionId": decision.decision_id.as_str(),
                    "disposition": disposition,
                    "acceptedThroughSequence": decision.accepted_through_sequence,
                    "reasonCodes": &decision.reason_codes,
                    "policyRefs": &decision.policy_refs,
                    "providerCancellation": match decision.provider_cancellation {
                        ProviderCancellation::None => "none",
                        ProviderCancellation::Request => "request",
                        ProviderCancellation::RequiredIfSupported => "required_if_supported",
                    },
                    "draftDisposition": match decision.draft_disposition {
                        DraftDisposition::Keep => "keep",
                        DraftDisposition::MarkIncomplete => "mark_incomplete",
                        DraftDisposition::Retract => "retract",
                    },
                    "pendingToolCalls": match decision.pending_tool_calls {
                        PendingToolCallsDisposition::Keep => "keep",
                        PendingToolCallsDisposition::Deny => "deny",
                        PendingToolCallsDisposition::CancelAdmitted => "cancel_admitted",
                    },
                    "evaluatedAtUnixMs": decision.evaluated_at_unix_ms,
                    "occurredAtUnixMs": occurred_at_unix_ms,
                    "inputDigest": decision.input_digest.as_str(),
                });
                let update =
                    gate.apply_decision(decision, occurred_at_unix_ms)
                        .map_err(|error| {
                            PyValueError::new_err(format!(
                                "output gate operation {operation_index} failed: {error:?}"
                            ))
                        })?;
                decisions.push(decision_trace);
                if !update.deliverable.is_empty() || update.cutoff.is_some() {
                    let cutoff = update.cutoff.as_ref().map(|cutoff| {
                        json!({
                            "streamId": cutoff.stream_id,
                            "responseId": cutoff.response_id,
                            "turnId": cutoff.turn_id,
                            "lastGeneratedSequence": cutoff.last_generated_sequence,
                            "lastPolicyAcceptedSequence": cutoff.last_policy_accepted_sequence,
                            "lastClientDeliveredSequence": cutoff.last_client_delivered_sequence,
                            "terminalReason": match cutoff.terminal_reason {
                                graphblocks_runtime_core::output_policy::TerminalReason::PolicyDenied => "policy_denied",
                                graphblocks_runtime_core::output_policy::TerminalReason::BudgetExhausted => "budget_exhausted",
                                graphblocks_runtime_core::output_policy::TerminalReason::Cancelled => "cancelled",
                                graphblocks_runtime_core::output_policy::TerminalReason::ClientDisconnected => "client_disconnected",
                            },
                            "draftDisposition": match cutoff.draft_disposition {
                                DraftDisposition::Keep => "keep",
                                DraftDisposition::MarkIncomplete => "mark_incomplete",
                                DraftDisposition::Retract => "retract",
                            },
                            "durableResult": match cutoff.durable_result {
                                graphblocks_runtime_core::output_policy::DurableResult::None => "none",
                                graphblocks_runtime_core::output_policy::DurableResult::Incomplete => "incomplete",
                                graphblocks_runtime_core::output_policy::DurableResult::Partial => "partial",
                            },
                            "policyDecisionId": cutoff.policy_decision_id,
                            "occurredAtUnixMs": cutoff.occurred_at_unix_ms,
                        })
                    });
                    deliveries.push(json!({
                        "operationIndex": operation_index,
                        "chunks": update
                            .deliverable
                            .iter()
                            .map(|chunk| json!({
                                "streamId": chunk.stream_id,
                                "responseId": chunk.response_id,
                                "sequence": chunk.sequence,
                                "text": chunk.text,
                            }))
                            .collect::<Vec<_>>(),
                        "cutoff": cutoff,
                        "pendingToolCalls": update.pending_tool_calls.map(|disposition| match disposition {
                            PendingToolCallsDisposition::Keep => "keep",
                            PendingToolCallsDisposition::Deny => "deny",
                            PendingToolCallsDisposition::CancelAdmitted => "cancel_admitted",
                        }),
                        "providerCancellation": update.provider_cancellation.map(|cancellation| match cancellation {
                            ProviderCancellation::None => "none",
                            ProviderCancellation::Request => "request",
                            ProviderCancellation::RequiredIfSupported => "required_if_supported",
                        }),
                    }));
                }
            }
            value => {
                return Err(PyValueError::new_err(format!(
                    "operations[{operation_index}].kind has unknown kind {value:?}"
                )));
            }
        }
    }

    let cutoff = gate.cutoff().map(|cutoff| {
        json!({
            "streamId": cutoff.stream_id,
            "responseId": cutoff.response_id,
            "turnId": cutoff.turn_id,
            "lastGeneratedSequence": cutoff.last_generated_sequence,
            "lastPolicyAcceptedSequence": cutoff.last_policy_accepted_sequence,
            "lastClientDeliveredSequence": cutoff.last_client_delivered_sequence,
            "terminalReason": match cutoff.terminal_reason {
                graphblocks_runtime_core::output_policy::TerminalReason::PolicyDenied => "policy_denied",
                graphblocks_runtime_core::output_policy::TerminalReason::BudgetExhausted => "budget_exhausted",
                graphblocks_runtime_core::output_policy::TerminalReason::Cancelled => "cancelled",
                graphblocks_runtime_core::output_policy::TerminalReason::ClientDisconnected => "client_disconnected",
            },
            "draftDisposition": match cutoff.draft_disposition {
                DraftDisposition::Keep => "keep",
                DraftDisposition::MarkIncomplete => "mark_incomplete",
                DraftDisposition::Retract => "retract",
            },
            "durableResult": match cutoff.durable_result {
                graphblocks_runtime_core::output_policy::DurableResult::None => "none",
                graphblocks_runtime_core::output_policy::DurableResult::Incomplete => "incomplete",
                graphblocks_runtime_core::output_policy::DurableResult::Partial => "partial",
            },
            "policyDecisionId": cutoff.policy_decision_id,
            "occurredAtUnixMs": cutoff.occurred_at_unix_ms,
        })
    });
    let payload = json!({
        "deliveries": deliveries,
        "decisions": decisions,
        "cutoff": cutoff,
        "pending": gate.pending_chunks()
            .map(|chunk| json!({
                "streamId": chunk.stream_id,
                "responseId": chunk.response_id,
                "sequence": chunk.sequence,
                "text": chunk.text,
            }))
            .collect::<Vec<_>>(),
        "lastGeneratedSequence": gate.last_generated_sequence(),
        "lastPolicyAcceptedSequence": gate.last_policy_accepted_sequence(),
        "lastClientDeliveredSequence": gate.last_client_delivered_sequence(),
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!("failed to serialize output gate result: {error}"))
    })
}

#[pyfunction]
fn evaluate_declarative_output_policy_json(
    rules_json: &str,
    chunk_json: &str,
    evaluated_at_unix_ms: u64,
) -> PyResult<String> {
    let rules_value = parse_json_argument(rules_json, "declarative output policy rules")?;
    let chunk_value = parse_json_argument(chunk_json, "generation chunk")?;
    let rules_array = rules_value.as_array().ok_or_else(|| {
        PyValueError::new_err("declarative output policy rules JSON must be an array")
    })?;
    let parse_string_list = |value: Option<&Value>, label: &str| -> PyResult<Vec<String>> {
        let Some(value) = value else {
            return Ok(Vec::new());
        };
        let Some(values) = value.as_array() else {
            return Err(PyValueError::new_err(format!("{label} must be an array")));
        };
        let mut parsed = Vec::new();
        for (index, value) in values.iter().enumerate() {
            let Some(value) = value.as_str() else {
                return Err(PyValueError::new_err(format!(
                    "{label}[{index}] must be a string"
                )));
            };
            parsed.push(value.to_owned());
        }
        Ok(parsed)
    };

    let mut rules = Vec::new();
    for (rule_index, rule) in rules_array.iter().enumerate() {
        let label = format!("rules[{rule_index}]");
        let rule = json_object(rule, &label)?;
        let rule_id = rule
            .get("ruleId")
            .or_else(|| rule.get("rule_id"))
            .and_then(Value::as_str)
            .ok_or_else(|| PyValueError::new_err(format!("{label}.ruleId is required")))?;
        let literal = required_string(rule, "literal", &label)?;
        let disposition = match required_string(rule, "disposition", &label)? {
            "allow" => OutputDisposition::Allow,
            "hold" => OutputDisposition::Hold,
            "redact" => OutputDisposition::Redact,
            "replace" => OutputDisposition::Replace,
            "abort_response" => OutputDisposition::AbortResponse,
            "abort_turn" => OutputDisposition::AbortTurn,
            "deny_commit" => OutputDisposition::DenyCommit,
            value => {
                return Err(PyValueError::new_err(format!(
                    "{label}.disposition has unknown disposition {value:?}"
                )));
            }
        };
        let mut parsed_rule = DeclarativeOutputPolicyRule::new(rule_id, literal, disposition);
        if let Some(replacement) = rule.get("replacement") {
            let Some(replacement) = replacement.as_str() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.replacement must be a string"
                )));
            };
            parsed_rule = parsed_rule.with_replacement(replacement);
        }
        parsed_rule = parsed_rule.with_reason_codes(parse_string_list(
            rule.get("reasonCodes"),
            &format!("{label}.reasonCodes"),
        )?);
        parsed_rule = parsed_rule.with_policy_refs(parse_string_list(
            rule.get("policyRefs"),
            &format!("{label}.policyRefs"),
        )?);
        if let Some(priority) = rule.get("priority") {
            let Some(priority) = priority.as_i64() else {
                return Err(PyValueError::new_err(format!(
                    "{label}.priority must be an integer"
                )));
            };
            parsed_rule = parsed_rule.with_priority(priority);
        }
        rules.push(parsed_rule);
    }

    let chunk = json_object(&chunk_value, "chunk")?;
    let chunk = GenerationChunk::text(
        required_string(chunk, "streamId", "chunk")?,
        required_string(chunk, "responseId", "chunk")?,
        required_u64(chunk, "sequence", "chunk")?,
        required_string(chunk, "text", "chunk")?,
    );
    let decision =
        DeclarativeOutputPolicyEvaluator::new(rules).evaluate_chunk(&chunk, evaluated_at_unix_ms);
    let disposition = match decision.disposition {
        OutputDisposition::Allow => "allow",
        OutputDisposition::Hold => "hold",
        OutputDisposition::Redact => "redact",
        OutputDisposition::Replace => "replace",
        OutputDisposition::AbortResponse => "abort_response",
        OutputDisposition::AbortTurn => "abort_turn",
        OutputDisposition::DenyCommit => "deny_commit",
    };
    let provider_cancellation = match decision.provider_cancellation {
        ProviderCancellation::None => "none",
        ProviderCancellation::Request => "request",
        ProviderCancellation::RequiredIfSupported => "required_if_supported",
    };
    let draft_disposition = match decision.draft_disposition {
        DraftDisposition::Keep => "keep",
        DraftDisposition::MarkIncomplete => "mark_incomplete",
        DraftDisposition::Retract => "retract",
    };
    let pending_tool_calls = match decision.pending_tool_calls {
        PendingToolCallsDisposition::Keep => "keep",
        PendingToolCallsDisposition::Deny => "deny",
        PendingToolCallsDisposition::CancelAdmitted => "cancel_admitted",
    };
    let payload = json!({
        "decisionId": decision.decision_id,
        "disposition": disposition,
        "acceptedThroughSequence": decision.accepted_through_sequence,
        "replacementParts": decision
            .replacement_chunks
            .iter()
            .map(|chunk| json!({
                "kind": "text",
                "text": chunk.text,
                "metadata": {},
            }))
            .collect::<Vec<_>>(),
        "replacementChunks": decision
            .replacement_chunks
            .iter()
            .map(|chunk| json!({
                "streamId": chunk.stream_id,
                "responseId": chunk.response_id,
                "sequence": chunk.sequence,
                "text": chunk.text,
            }))
            .collect::<Vec<_>>(),
        "redactions": decision
            .redactions
            .iter()
            .map(|redaction| json!({
                "path": redaction.path,
                "start": redaction.start,
                "end": redaction.end,
                "replacement": redaction.replacement,
            }))
            .collect::<Vec<_>>(),
        "reasonCodes": decision.reason_codes,
        "policyRefs": decision.policy_refs,
        "providerCancellation": provider_cancellation,
        "draftDisposition": draft_disposition,
        "pendingToolCalls": pending_tool_calls,
        "evaluatedAtUnixMs": decision.evaluated_at_unix_ms,
        "inputDigest": decision.input_digest,
    });
    serde_json::to_string(&payload).map_err(|error| {
        PyRuntimeError::new_err(format!(
            "failed to serialize declarative output policy decision: {error}"
        ))
    })
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add_function(wrap_pyfunction!(binding_version, module)?)?;
    module.add_function(wrap_pyfunction!(finalize_tool_call_json, module)?)?;
    module.add_function(wrap_pyfunction!(compile_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        validate_worker_advertisement_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(validate_remote_payload_json, module)?)?;
    module.add_function(wrap_pyfunction!(run_test_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(run_stdlib_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(decide_agent_step_json, module)?)?;
    module.add_function(wrap_pyfunction!(admit_exhaustion_work_json, module)?)?;
    module.add_function(wrap_pyfunction!(evaluate_output_gate_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        evaluate_declarative_output_policy_json,
        module
    )?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::{
        admit_exhaustion_work_json, compile_graph_json, decide_agent_step_json,
        evaluate_declarative_output_policy_json, evaluate_output_gate_json,
        finalize_tool_call_json, run_stdlib_graph_json, run_test_graph_json,
        validate_remote_payload_json, validate_worker_advertisement_json,
    };

    #[test]
    fn finalize_tool_call_json_assembles_validated_call_with_canonical_digest() -> Result<(), String>
    {
        let draft = json!({
            "responseId": "response-1",
            "toolCallId": "call-1",
            "toolName": "knowledge.search",
            "argumentFragments": ["{\"b\":2,", "\"a\":1}"],
            "sequence": 2,
            "status": "arguments_complete"
        });
        let reversed_draft = json!({
            "response_id": "response-1",
            "tool_call_id": "call-2",
            "tool_name": "knowledge.search",
            "argument_fragments": ["{\"a\":1,", "\"b\":2}"],
            "sequence": 2,
            "status": "arguments_complete"
        });
        let draft_json = serde_json::to_string(&draft).map_err(|error| error.to_string())?;
        let reversed_draft_json =
            serde_json::to_string(&reversed_draft).map_err(|error| error.to_string())?;

        let result_json = finalize_tool_call_json(&draft_json, "resolved-tool-1", 1_000)
            .map_err(|error| error.to_string())?;
        let reversed_result_json =
            finalize_tool_call_json(&reversed_draft_json, "resolved-tool-1", 1_001)
                .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let reversed_result = serde_json::from_str::<Value>(&reversed_result_json)
            .map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("toolCallId").and_then(Value::as_str),
            Some("call-1")
        );
        assert_eq!(
            result.get("responseId").and_then(Value::as_str),
            Some("response-1")
        );
        assert_eq!(
            result.get("resolvedToolId").and_then(Value::as_str),
            Some("resolved-tool-1")
        );
        assert_eq!(
            result.get("name").and_then(Value::as_str),
            Some("knowledge.search")
        );
        assert_eq!(result.get("arguments"), Some(&json!({"a": 1, "b": 2})));
        assert_eq!(result.get("revision").and_then(Value::as_u64), Some(1));
        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("validated")
        );
        assert_eq!(result.get("dependsOn"), Some(&json!([])));
        assert_eq!(
            result.get("createdAtUnixMs").and_then(Value::as_u64),
            Some(1_000)
        );
        assert_eq!(
            result.get("argumentsDigest").and_then(Value::as_str),
            reversed_result
                .get("argumentsDigest")
                .and_then(Value::as_str)
        );

        Ok(())
    }

    #[test]
    fn finalize_tool_call_json_rejects_incomplete_arguments() -> Result<(), String> {
        pyo3::Python::initialize();

        let draft = json!({
            "responseId": "response-1",
            "toolCallId": "call-1",
            "toolName": "knowledge.search",
            "argumentFragments": ["{\"query\":\"runtime\"}"],
            "sequence": 1,
            "status": "arguments_streaming"
        });
        let draft_json = serde_json::to_string(&draft).map_err(|error| error.to_string())?;

        let error = finalize_tool_call_json(&draft_json, "resolved-tool-1", 1_000)
            .expect_err("streaming arguments must not finalize")
            .to_string();

        assert!(error.contains("ArgumentsNotComplete"));
        Ok(())
    }

    #[test]
    fn compile_graph_json_matches_shared_tck_cases() -> Result<(), String> {
        let cases = serde_json::from_str::<Value>(include_str!("../../../tck/compiler/cases.json"))
            .map_err(|error| error.to_string())?;
        let cases = cases
            .as_array()
            .ok_or_else(|| "compiler TCK root must be an array".to_owned())?;

        for case in cases {
            let name = case
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(|| "compiler TCK case is missing name".to_owned())?;
            let document = case
                .get("document")
                .ok_or_else(|| format!("compiler TCK case {name} is missing document"))?;
            let expected = case
                .get("expected")
                .ok_or_else(|| format!("compiler TCK case {name} is missing expected result"))?;
            let expected_hash = expected
                .get("graph_hash")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    format!("compiler TCK case {name} is missing expected graph_hash")
                })?;
            let expected_error_codes = expected
                .get("error_codes")
                .and_then(Value::as_array)
                .ok_or_else(|| format!("compiler TCK case {name} is missing expected error_codes"))?
                .iter()
                .map(|code| {
                    code.as_str().ok_or_else(|| {
                        format!("compiler TCK case {name} has a non-string error code")
                    })
                })
                .collect::<Result<Vec<_>, _>>()?;

            let document_json =
                serde_json::to_string(document).map_err(|error| error.to_string())?;
            let block_catalog_json = case
                .get("block_catalog")
                .map(serde_json::to_string)
                .transpose()
                .map_err(|error| error.to_string())?;
            let compiled_json = compile_graph_json(&document_json, block_catalog_json.as_deref())
                .map_err(|error| error.to_string())?;
            let compiled =
                serde_json::from_str::<Value>(&compiled_json).map_err(|error| error.to_string())?;
            let diagnostics = compiled
                .get("diagnostics")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    format!("compiler bridge result for {name} is missing diagnostics")
                })?;
            let actual_error_codes = diagnostics
                .iter()
                .filter(|diagnostic| {
                    diagnostic.get("severity").and_then(Value::as_str) == Some("error")
                })
                .map(|diagnostic| {
                    diagnostic
                        .get("code")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            format!("compiler bridge result for {name} has an invalid code")
                        })
                })
                .collect::<Result<Vec<_>, _>>()?;

            assert_eq!(
                compiled.get("hash").and_then(Value::as_str),
                Some(expected_hash),
                "{name}"
            );
            assert_eq!(actual_error_codes, expected_error_codes, "{name}");
        }

        Ok(())
    }

    #[test]
    fn validate_worker_advertisement_json_reports_package_lock_mismatch() -> Result<(), String> {
        let advertisement = json!({
            "protocolVersion": 1,
            "workerId": "worker-local-1",
            "targetId": "doc-cpu",
            "packageLockHash": "sha256:actual",
            "imageDigest": "sha256:image",
            "supportedBlocks": [{"block": "prompt.render@1"}],
            "state": "ready"
        });
        let advertisement_json =
            serde_json::to_string(&advertisement).map_err(|error| error.to_string())?;
        let result_json =
            validate_worker_advertisement_json(&advertisement_json, Some("sha256:expected"))
                .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(false)));
        assert_eq!(
            result.pointer("/error/code").and_then(Value::as_str),
            Some("worker.incompatible_package_lock"),
        );
        assert_eq!(
            result.pointer("/error/expected").and_then(Value::as_str),
            Some("sha256:expected"),
        );
        Ok(())
    }

    #[test]
    fn validate_remote_payload_json_rejects_oversized_inline_payload() -> Result<(), String> {
        let payload = json!({
            "mode": "inline",
            "schema": "graphblocks.ai/Message@1",
            "value": {"body": "this inline payload is too large"}
        });
        let payload_json = serde_json::to_string(&payload).map_err(|error| error.to_string())?;
        let result_json =
            validate_remote_payload_json(&payload_json, 8).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("ok"), Some(&json!(false)));
        assert_eq!(
            result.pointer("/error/code").and_then(Value::as_str),
            Some("remote_payload.oversized_inline"),
        );
        assert_eq!(
            result
                .pointer("/error/maxInlineBytes")
                .and_then(Value::as_u64),
            Some(8),
        );
        Ok(())
    }

    #[test]
    fn admit_exhaustion_work_json_allows_bounded_finalization_with_matching_permit()
    -> Result<(), String> {
        let policy = json!({
            "preset": "finish_current_turn",
            "unit": "turn",
            "continuation": {
                "maxAdditionalUsage": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "maxAdditionalSteps": 1
            }
        });
        let request = json!({
            "atomicUnitId": "turn:1",
            "admissionEpoch": 7,
            "workKind": "declared_finalization",
            "workEpoch": 8,
            "permit": {
                "permitId": "permit-1",
                "reservationRefs": ["reservation-1"],
                "owner": "worker:1",
                "atomicUnit": "turn:1",
                "admissionEpoch": 7,
                "authorizedAmounts": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "continuationProfile": "finish_current_turn",
                "policySnapshotDigest": "sha256:policy",
                "expiresAt": "2026-06-22T01:00:00Z",
                "fencingTokens": {"budget-1": 1}
            }
        });
        let policy_json = serde_json::to_string(&policy).map_err(|error| error.to_string())?;
        let request_json = serde_json::to_string(&request).map_err(|error| error.to_string())?;
        let result_json = admit_exhaustion_work_json(&policy_json, &request_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("allowed"), Some(&json!(true)));
        assert_eq!(
            result.get("reason").and_then(Value::as_str),
            Some("allowed")
        );
        assert_eq!(
            result.get("usedAdditionalSteps").and_then(Value::as_u64),
            Some(1),
        );
        Ok(())
    }

    #[test]
    fn decide_agent_step_json_uses_rust_agent_loop_boundaries() -> Result<(), String> {
        let spec = json!({
            "modelPool": "support-models",
            "maxSteps": 4,
            "completionReserveUnits": 100
        });
        let spec_json = serde_json::to_string(&spec).map_err(|error| error.to_string())?;

        let stop_request = json!({"completedSteps": 4, "remainingBudgetUnits": 1_000});
        let stop_json = serde_json::to_string(&stop_request).map_err(|error| error.to_string())?;
        let stop = serde_json::from_str::<Value>(
            &decide_agent_step_json(&spec_json, &stop_json).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;

        assert_eq!(
            stop.get("disposition").and_then(Value::as_str),
            Some("stop")
        );
        assert_eq!(
            stop.get("reason").and_then(Value::as_str),
            Some("max_steps_reached")
        );

        let finalize_request = json!({"completedSteps": 3, "remainingBudgetUnits": 100});
        let finalize_json =
            serde_json::to_string(&finalize_request).map_err(|error| error.to_string())?;
        let finalize = serde_json::from_str::<Value>(
            &decide_agent_step_json(&spec_json, &finalize_json)
                .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;

        assert_eq!(
            finalize.get("disposition").and_then(Value::as_str),
            Some("finalize")
        );
        assert_eq!(
            finalize.get("reason").and_then(Value::as_str),
            Some("completion_reserve_reached")
        );

        let continue_request = json!({"completedSteps": 3, "remainingBudgetUnits": 101});
        let continue_json =
            serde_json::to_string(&continue_request).map_err(|error| error.to_string())?;
        let continued = serde_json::from_str::<Value>(
            &decide_agent_step_json(&spec_json, &continue_json)
                .map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;

        assert_eq!(
            continued.get("disposition").and_then(Value::as_str),
            Some("continue")
        );
        assert_eq!(
            continued.get("reason").and_then(Value::as_str),
            Some("admitted")
        );
        Ok(())
    }

    #[test]
    fn admit_exhaustion_work_json_rejects_mismatched_permit() -> Result<(), String> {
        let policy = json!({
            "preset": "finish_current_turn",
            "unit": "turn",
            "continuation": {
                "maxAdditionalUsage": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "maxAdditionalSteps": 1
            }
        });
        let request = json!({
            "atomicUnitId": "turn:1",
            "admissionEpoch": 7,
            "workKind": "declared_finalization",
            "workEpoch": 8,
            "permit": {
                "permitId": "permit-2",
                "reservationRefs": ["reservation-1"],
                "owner": "worker:1",
                "atomicUnit": "turn:other",
                "admissionEpoch": 7,
                "authorizedAmounts": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "continuationProfile": "finish_current_turn",
                "policySnapshotDigest": "sha256:policy",
                "expiresAt": "2026-06-22T01:00:00Z",
                "fencingTokens": {"budget-1": 1}
            }
        });
        let policy_json = serde_json::to_string(&policy).map_err(|error| error.to_string())?;
        let request_json = serde_json::to_string(&request).map_err(|error| error.to_string())?;
        let result_json = admit_exhaustion_work_json(&policy_json, &request_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("allowed"), Some(&json!(false)));
        assert_eq!(
            result.get("reason").and_then(Value::as_str),
            Some("invalid_permit"),
        );
        Ok(())
    }

    #[test]
    fn admit_exhaustion_work_json_rejects_expired_permit_at_validation_time() -> Result<(), String>
    {
        let policy = json!({
            "preset": "finish_current_turn",
            "unit": "turn",
            "continuation": {
                "maxAdditionalUsage": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "maxAdditionalSteps": 1
            }
        });
        let request = json!({
            "atomicUnitId": "turn:1",
            "admissionEpoch": 7,
            "workKind": "declared_finalization",
            "workEpoch": 8,
            "validationTime": "2026-06-22T01:00:00Z",
            "permit": {
                "permitId": "permit-1",
                "reservationRefs": ["reservation-1"],
                "owner": "worker:1",
                "atomicUnit": "turn:1",
                "admissionEpoch": 7,
                "authorizedAmounts": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "continuationProfile": "finish_current_turn",
                "policySnapshotDigest": "sha256:policy",
                "expiresAt": "2026-06-22T01:00:00Z",
                "fencingTokens": {"budget-1": 1}
            }
        });
        let policy_json = serde_json::to_string(&policy).map_err(|error| error.to_string())?;
        let request_json = serde_json::to_string(&request).map_err(|error| error.to_string())?;
        let result_json = admit_exhaustion_work_json(&policy_json, &request_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("allowed"), Some(&json!(false)));
        assert_eq!(
            result.get("reason").and_then(Value::as_str),
            Some("invalid_permit"),
        );
        Ok(())
    }

    #[test]
    fn admit_exhaustion_work_json_rejects_requested_usage_above_permit() -> Result<(), String> {
        let policy = json!({
            "preset": "finish_current_turn",
            "unit": "turn",
            "continuation": {
                "maxAdditionalUsage": [
                    {"kind": "model_output_tokens", "amount": 200, "unit": "tokens"}
                ],
                "maxAdditionalSteps": 1
            }
        });
        let request = json!({
            "atomicUnitId": "turn:1",
            "admissionEpoch": 7,
            "workKind": "declared_finalization",
            "workEpoch": 8,
            "requestedUsage": [
                {"kind": "model_output_tokens", "amount": 101, "unit": "tokens"}
            ],
            "permit": {
                "permitId": "permit-1",
                "reservationRefs": ["reservation-1"],
                "owner": "worker:1",
                "atomicUnit": "turn:1",
                "admissionEpoch": 7,
                "authorizedAmounts": [
                    {"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}
                ],
                "continuationProfile": "finish_current_turn",
                "policySnapshotDigest": "sha256:policy",
                "expiresAt": "2026-06-22T01:00:00Z",
                "fencingTokens": {"budget-1": 1}
            }
        });
        let policy_json = serde_json::to_string(&policy).map_err(|error| error.to_string())?;
        let request_json = serde_json::to_string(&request).map_err(|error| error.to_string())?;
        let result_json = admit_exhaustion_work_json(&policy_json, &request_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("allowed"), Some(&json!(false)));
        assert_eq!(
            result.get("reason").and_then(Value::as_str),
            Some("usage_exceeds_permit"),
        );
        Ok(())
    }

    #[test]
    fn run_test_graph_json_executes_compiled_graph_with_fixture_outputs() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-runtime-bridge"},
            "spec": {
                "nodes": {
                    "model": {
                        "block": "model.generate@1",
                        "inputs": {"prompt": "render.prompt"},
                        "outputs": {"response": "$output.answer"}
                    },
                    "render": {
                        "block": "prompt.render@1",
                        "inputs": {"message": "$input.message"}
                    }
                }
            }
        });
        let node_outputs = json!({
            "render": {"prompt": "rendered"},
            "model": {"response": "generated"}
        });

        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let node_outputs_json =
            serde_json::to_string(&node_outputs).map_err(|error| error.to_string())?;
        let result_json =
            run_test_graph_json(&graph_json, r#"{"message":"hello"}"#, &node_outputs_json)
                .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let journal = result
            .get("journal")
            .and_then(Value::as_array)
            .ok_or_else(|| "runtime bridge result is missing journal".to_owned())?;
        let completed_nodes = journal
            .iter()
            .filter(|record| record.get("kind").and_then(Value::as_str) == Some("node_completed"))
            .map(|record| {
                record
                    .get("nodeId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "node_completed record is missing nodeId".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("answer"))
                .and_then(Value::as_str),
            Some("generated")
        );
        assert_eq!(completed_nodes, vec!["render", "model"]);

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_delivers_accepted_chunks_and_records_cutoff() -> Result<(), String>
    {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "turnId": "turn-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 2,
                "onViolation": "abort_response",
                "deliveredDraftDisposition": "retract"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe "
            },
            {
                "kind": "chunk",
                "sequence": 2,
                "text": "blocked"
            },
            {
                "kind": "decision",
                "decisionId": "decision-allow",
                "disposition": "allow",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:allow",
                "occurredAtUnixMs": 1_000
            },
            {
                "kind": "decision",
                "decisionId": "decision-abort",
                "disposition": "abort_response",
                "inputDigest": "sha256:abort",
                "providerCancellation": "required_if_supported",
                "occurredAtUnixMs": 1_010
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("safe ")
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastGeneratedSequence"))
                .and_then(Value::as_u64),
            Some(2)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastPolicyAcceptedSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("draftDisposition"))
                .and_then(Value::as_str),
            Some("retract")
        );
        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.last())
                .and_then(|delivery| delivery.get("pendingToolCalls"))
                .and_then(Value::as_str),
            Some("deny")
        );
        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.last())
                .and_then(|delivery| delivery.get("providerCancellation"))
                .and_then(Value::as_str),
            Some("required_if_supported")
        );
        assert_eq!(
            result
                .get("lastClientDeliveredSequence")
                .and_then(Value::as_u64),
            Some(1)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_records_terminal_accepted_prefix() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 2,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe "
            },
            {
                "kind": "chunk",
                "sequence": 2,
                "text": "blocked"
            },
            {
                "kind": "decision",
                "decisionId": "decision-abort",
                "disposition": "abort_response",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:abort",
                "occurredAtUnixMs": 1_010
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastPolicyAcceptedSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(0)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_resumes_pending_holdback_state() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "turnId": "turn-1",
            "lastGeneratedSequence": 2,
            "lastPolicyAcceptedSequence": 1,
            "lastClientDeliveredSequence": 1,
            "pending": [
                {
                    "sequence": 2,
                    "text": "held"
                }
            ],
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 4,
                "onViolation": "abort_response",
                "deliveredDraftDisposition": "retract"
            }
        });
        let operations = json!([
            {
                "kind": "decision",
                "decisionId": "decision-2",
                "disposition": "allow",
                "acceptedThroughSequence": 2,
                "inputDigest": "sha256:second",
                "occurredAtUnixMs": 1_010
            },
            {
                "kind": "chunk",
                "sequence": 3,
                "text": " next"
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("held")
        );
        assert_eq!(
            result.get("lastGeneratedSequence").and_then(Value::as_u64),
            Some(3)
        );
        assert_eq!(
            result
                .get("lastClientDeliveredSequence")
                .and_then(Value::as_u64),
            Some(2)
        );
        assert_eq!(
            result
                .get("pending")
                .and_then(Value::as_array)
                .and_then(|pending| pending.first())
                .and_then(|chunk| chunk.get("sequence"))
                .and_then(Value::as_u64),
            Some(3)
        );
        assert_eq!(
            result
                .get("pending")
                .and_then(Value::as_array)
                .and_then(|pending| pending.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some(" next")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_resumes_terminal_cutoff_state() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "cutoff": {
                "streamId": "stream-1",
                "responseId": "response-1",
                "turnId": "turn-1",
                "lastGeneratedSequence": 2,
                "lastPolicyAcceptedSequence": 1,
                "lastClientDeliveredSequence": 1,
                "terminalReason": "policy_denied",
                "draftDisposition": "retract",
                "durableResult": "none",
                "policyDecisionId": "decision-abort",
                "occurredAtUnixMs": 1_100
            }
        });
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let empty_operations =
            serde_json::to_string(&json!([])).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &empty_operations)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("policyDecisionId"))
                .and_then(Value::as_str),
            Some("decision-abort")
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );

        let late_operations = serde_json::to_string(&json!([
            {
                "kind": "chunk",
                "sequence": 3,
                "text": "late"
            }
        ]))
        .map_err(|error| error.to_string())?;
        let error = evaluate_output_gate_json(&gate_json, &late_operations)
            .expect_err("late chunks must remain blocked after a resumed cutoff");
        assert!(error.to_string().contains("PolicyStopped"));

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_accepts_snake_case_cutoff_state() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "stream_id": "stream-1",
            "response_id": "response-1",
            "cutoff": {
                "stream_id": "stream-1",
                "response_id": "response-1",
                "turn_id": "turn-1",
                "last_generated_sequence": 2,
                "last_policy_accepted_sequence": 1,
                "last_client_delivered_sequence": 1,
                "terminal_reason": "policy_denied",
                "draft_disposition": "retract",
                "durable_result": "none",
                "policy_decision_id": "decision-abort",
                "occurred_at_unix_ms": 1_100
            }
        });
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let empty_operations =
            serde_json::to_string(&json!([])).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &empty_operations)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("policyDecisionId"))
                .and_then(Value::as_str),
            Some("decision-abort")
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_accepts_snake_case_decision_operation() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "onViolation": "abort_response",
                "holdbackMaxTokens": 16
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe draft"
            },
            {
                "kind": "decision",
                "decision_id": "decision-allow",
                "disposition": "allow",
                "accepted_through_sequence": 1,
                "input_digest": "sha256:accepted",
                "reason_codes": ["safe"],
                "policy_refs": ["policy/output"],
                "provider_cancellation": "none",
                "draft_disposition": "keep",
                "pending_tool_calls": "keep",
                "evaluated_at_unix_ms": 1_000,
                "occurred_at_unix_ms": 1_010
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("safe draft")
        );
        assert_eq!(
            result
                .get("decisions")
                .and_then(Value::as_array)
                .and_then(|decisions| decisions.first())
                .and_then(|decision| decision.get("decisionId"))
                .and_then(Value::as_str),
            Some("decision-allow")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_accepts_snake_case_delivery_policy() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "delivery_policy": {
                "mode": "immediate_draft",
                "on_violation": "abort_response",
                "delivered_draft_disposition": "retract",
                "flush_boundaries": ["sentence"]
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe draft"
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("draft"))
                .and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("safe draft")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_honors_snake_case_chunk_identity() -> Result<(), String> {
        pyo3::Python::initialize();
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1"
        });
        let operations = json!([
            {
                "kind": "chunk",
                "stream_id": "stream-other",
                "response_id": "response-1",
                "sequence": 1,
                "text": "misrouted"
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let error = evaluate_output_gate_json(&gate_json, &operations_json)
            .expect_err("snake_case chunk stream mismatch should be rejected");

        assert!(error.to_string().contains("StreamMismatch"));

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_emits_immediate_draft_before_retraction() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "immediate_draft",
                "onViolation": "abort_response",
                "deliveredDraftDisposition": "retract"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "provisional draft"
            },
            {
                "kind": "decision",
                "decisionId": "decision-abort",
                "disposition": "abort_response",
                "inputDigest": "sha256:abort",
                "occurredAtUnixMs": 1_010
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let deliveries = result
            .get("deliveries")
            .and_then(Value::as_array)
            .ok_or_else(|| "output gate result is missing deliveries".to_owned())?;

        assert_eq!(
            deliveries
                .first()
                .and_then(|delivery| delivery.get("operationIndex"))
                .and_then(Value::as_u64),
            Some(0)
        );
        assert_eq!(
            deliveries
                .first()
                .and_then(|delivery| delivery.get("draft"))
                .and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(
            deliveries
                .first()
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("provisional draft")
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastPolicyAcceptedSequence"))
                .and_then(Value::as_u64),
            Some(0)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("lastClientDeliveredSequence"))
                .and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            result
                .get("cutoff")
                .and_then(|cutoff| cutoff.get("draftDisposition"))
                .and_then(Value::as_str),
            Some("retract")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_applies_redaction_instructions() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 8,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "hello secret world"
            },
            {
                "kind": "decision",
                "decisionId": "decision-redact",
                "disposition": "redact",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:redact",
                "redactions": [
                    {
                        "path": "/chunks/1/text",
                        "start": 6,
                        "end": 12,
                        "replacement": "[redacted]"
                    }
                ],
                "occurredAtUnixMs": 1_000
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result
                .get("deliveries")
                .and_then(Value::as_array)
                .and_then(|deliveries| deliveries.first())
                .and_then(|delivery| delivery.get("chunks"))
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("hello [redacted] world")
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_accepts_replacement_parts() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 8,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "blocked draft"
            },
            {
                "kind": "decision",
                "decisionId": "decision-replace",
                "disposition": "replace",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:replace",
                "replacementParts": [
                    {"kind": "text", "text": "policy-approved "},
                    {"kind": "text", "text": "replacement"}
                ],
                "occurredAtUnixMs": 1_000
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let chunks = result
            .get("deliveries")
            .and_then(Value::as_array)
            .and_then(|deliveries| deliveries.first())
            .and_then(|delivery| delivery.get("chunks"))
            .and_then(Value::as_array)
            .ok_or_else(|| "missing replacement delivery chunks".to_owned())?;

        assert_eq!(
            chunks
                .iter()
                .map(|chunk| (
                    chunk.get("sequence").and_then(Value::as_u64),
                    chunk.get("text").and_then(Value::as_str),
                ))
                .collect::<Vec<_>>(),
            vec![
                (Some(1), Some("policy-approved ")),
                (Some(2), Some("replacement")),
            ],
        );
        assert_eq!(
            result
                .get("lastPolicyAcceptedSequence")
                .and_then(Value::as_u64),
            Some(2)
        );
        assert_eq!(
            result
                .get("lastClientDeliveredSequence")
                .and_then(Value::as_u64),
            Some(2)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_preserves_pending_prefix_on_replacement_parts()
    -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 8,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe "
            },
            {
                "kind": "chunk",
                "sequence": 2,
                "text": "context "
            },
            {
                "kind": "chunk",
                "sequence": 3,
                "text": "secret"
            },
            {
                "kind": "decision",
                "decisionId": "decision-replace",
                "disposition": "replace",
                "acceptedThroughSequence": 3,
                "inputDigest": "sha256:replace",
                "replacementParts": [
                    {"kind": "text", "text": "[redacted]"}
                ],
                "occurredAtUnixMs": 1_000
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let chunks = result
            .get("deliveries")
            .and_then(Value::as_array)
            .and_then(|deliveries| deliveries.first())
            .and_then(|delivery| delivery.get("chunks"))
            .and_then(Value::as_array)
            .ok_or_else(|| "missing replacement delivery chunks".to_owned())?;

        assert_eq!(
            chunks
                .iter()
                .map(|chunk| (
                    chunk.get("sequence").and_then(Value::as_u64),
                    chunk.get("text").and_then(Value::as_str),
                ))
                .collect::<Vec<_>>(),
            vec![
                (Some(1), Some("safe ")),
                (Some(2), Some("context ")),
                (Some(3), Some("[redacted]")),
            ],
        );
        assert_eq!(
            result
                .get("lastPolicyAcceptedSequence")
                .and_then(Value::as_u64),
            Some(3)
        );
        assert_eq!(
            result
                .get("lastClientDeliveredSequence")
                .and_then(Value::as_u64),
            Some(3)
        );

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_enforces_token_holdback_limit() -> Result<(), String> {
        pyo3::Python::initialize();

        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 3,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe text"
            },
            {
                "kind": "chunk",
                "sequence": 2,
                "text": "still"
            },
            {
                "kind": "chunk",
                "sequence": 3,
                "text": "blocked"
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let error = evaluate_output_gate_json(&gate_json, &operations_json)
            .expect_err("token holdback overflow should be rejected")
            .to_string();

        assert!(error.contains("BoundedHoldbackTokensExceeded"));
        assert!(error.contains("max_tokens: 3"));

        Ok(())
    }

    #[test]
    fn evaluate_output_gate_json_preserves_decision_metadata() -> Result<(), String> {
        let gate = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "deliveryPolicy": {
                "mode": "bounded_holdback",
                "holdbackMaxTokens": 8,
                "onViolation": "abort_response"
            }
        });
        let operations = json!([
            {
                "kind": "chunk",
                "sequence": 1,
                "text": "safe"
            },
            {
                "kind": "decision",
                "decisionId": "decision-allow",
                "disposition": "allow",
                "acceptedThroughSequence": 1,
                "inputDigest": "sha256:allow",
                "reasonCodes": ["pii.clear"],
                "policyRefs": ["policy/output-standard"],
                "providerCancellation": "required_if_supported",
                "draftDisposition": "mark_incomplete",
                "pendingToolCalls": "cancel_admitted",
                "evaluatedAtUnixMs": 995,
                "occurredAtUnixMs": 1_000
            }
        ]);
        let gate_json = serde_json::to_string(&gate).map_err(|error| error.to_string())?;
        let operations_json =
            serde_json::to_string(&operations).map_err(|error| error.to_string())?;

        let result_json = evaluate_output_gate_json(&gate_json, &operations_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let decision = result
            .get("decisions")
            .and_then(Value::as_array)
            .and_then(|decisions| decisions.first())
            .ok_or_else(|| "missing output decision trace".to_owned())?;

        assert_eq!(
            decision.get("decisionId").and_then(Value::as_str),
            Some("decision-allow")
        );
        assert_eq!(decision.get("reasonCodes"), Some(&json!(["pii.clear"])));
        assert_eq!(
            decision.get("policyRefs"),
            Some(&json!(["policy/output-standard"]))
        );
        assert_eq!(
            decision.get("evaluatedAtUnixMs").and_then(Value::as_u64),
            Some(995)
        );
        assert_eq!(
            decision.get("providerCancellation").and_then(Value::as_str),
            Some("required_if_supported")
        );
        assert_eq!(
            decision.get("draftDisposition").and_then(Value::as_str),
            Some("mark_incomplete")
        );
        assert_eq!(
            decision.get("pendingToolCalls").and_then(Value::as_str),
            Some("cancel_admitted")
        );

        Ok(())
    }

    #[test]
    fn evaluate_declarative_output_policy_json_returns_redaction_decision() -> Result<(), String> {
        let rules = json!([
            {
                "ruleId": "redact-secret",
                "literal": "secret",
                "disposition": "redact",
                "replacement": "[redacted]",
                "reasonCodes": ["pii.secret"],
                "policyRefs": ["policy/output-standard#redact-secret"],
                "priority": 10
            }
        ]);
        let chunk = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "sequence": 7,
            "text": "hello secret"
        });
        let rules_json = serde_json::to_string(&rules).map_err(|error| error.to_string())?;
        let chunk_json = serde_json::to_string(&chunk).map_err(|error| error.to_string())?;

        let result_json = evaluate_declarative_output_policy_json(&rules_json, &chunk_json, 1_000)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("disposition").and_then(Value::as_str),
            Some("redact")
        );
        assert_eq!(
            result
                .get("acceptedThroughSequence")
                .and_then(Value::as_u64),
            Some(7)
        );
        assert_eq!(result.get("reasonCodes"), Some(&json!(["pii.secret"])));
        assert_eq!(
            result.get("policyRefs"),
            Some(&json!(["policy/output-standard#redact-secret"]))
        );
        assert_eq!(
            result
                .get("redactions")
                .and_then(Value::as_array)
                .and_then(|redactions| redactions.first()),
            Some(&json!({
                "path": "/chunks/7/text",
                "start": 6,
                "end": 12,
                "replacement": "[redacted]"
            }))
        );
        assert!(
            result
                .get("decisionId")
                .and_then(Value::as_str)
                .is_some_and(|decision_id| decision_id.starts_with("output-decision:sha256:"))
        );
        assert!(
            result
                .get("inputDigest")
                .and_then(Value::as_str)
                .is_some_and(|digest| digest.starts_with("sha256:"))
        );
        assert_eq!(
            result.get("evaluatedAtUnixMs").and_then(Value::as_u64),
            Some(1_000)
        );

        Ok(())
    }

    #[test]
    fn evaluate_declarative_output_policy_json_returns_replacement_parts() -> Result<(), String> {
        let rules = json!([
            {
                "ruleId": "blocked",
                "literal": "blocked",
                "disposition": "replace",
                "replacement": "policy-approved",
                "reasonCodes": ["content.replaced"]
            }
        ]);
        let chunk = json!({
            "streamId": "stream-1",
            "responseId": "response-1",
            "sequence": 1,
            "text": "blocked"
        });
        let rules_json = serde_json::to_string(&rules).map_err(|error| error.to_string())?;
        let chunk_json = serde_json::to_string(&chunk).map_err(|error| error.to_string())?;

        let result_json = evaluate_declarative_output_policy_json(&rules_json, &chunk_json, 1_000)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("disposition").and_then(Value::as_str),
            Some("replace")
        );
        assert_eq!(
            result
                .get("replacementParts")
                .and_then(Value::as_array)
                .and_then(|parts| parts.first())
                .and_then(|part| part.get("kind"))
                .and_then(Value::as_str),
            Some("text")
        );
        assert_eq!(
            result
                .get("replacementParts")
                .and_then(Value::as_array)
                .and_then(|parts| parts.first())
                .and_then(|part| part.get("text"))
                .and_then(Value::as_str),
            Some("policy-approved")
        );
        assert_eq!(
            result
                .get("replacementChunks")
                .and_then(Value::as_array)
                .and_then(|chunks| chunks.first())
                .and_then(|chunk| chunk.get("text"))
                .and_then(Value::as_str),
            Some("policy-approved")
        );

        Ok(())
    }

    #[test]
    fn run_test_graph_json_blocks_missing_external_inputs() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-runtime-missing-input"},
            "spec": {
                "nodes": {
                    "render": {
                        "block": "prompt.render@1",
                        "inputs": {"message": "$input.message"},
                        "outputs": {"prompt": "$output.prompt"}
                    }
                }
            }
        });
        let node_outputs = json!({"render": {"prompt": "rendered"}});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let node_outputs_json =
            serde_json::to_string(&node_outputs).map_err(|error| error.to_string())?;

        let result_json = run_test_graph_json(&graph_json, "{}", &node_outputs_json)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(result.get("status").and_then(Value::as_str), Some("failed"));
        assert_eq!(
            result
                .get("journal")
                .and_then(Value::as_array)
                .and_then(|journal| journal.last())
                .and_then(|record| record.get("kind"))
                .and_then(Value::as_str),
            Some("run_failed")
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_executes_conversation_vertical_slice() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-conversation-slice"},
            "spec": {
                "interface": {
                    "inputs": {"message": "graphblocks.ai/Message@1"},
                    "outputs": {"answer": "graphblocks.ai/Answer@1"}
                },
                "nodes": {
                    "begin": {"block": "conversation.begin_turn@1"},
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Answer: {message.text}"},
                        "inputs": {"message": "$input.message"}
                    },
                    "generate": {
                        "block": "model.generate@1",
                        "config": {"script": {"Answer: Hello": "Hello from Rust."}},
                        "inputs": {"prompt": "render.prompt"}
                    },
                    "commit": {
                        "block": "conversation.commit_turn@1",
                        "inputs": {
                            "transaction": "begin.transaction",
                            "candidate": "generate.response"
                        },
                        "outputs": {"answer": "$output.answer"}
                    }
                }
            }
        });
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let result_json = run_stdlib_graph_json(&graph_json, r#"{"message":{"text":"Hello"}}"#)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let journal = result
            .get("journal")
            .and_then(Value::as_array)
            .ok_or_else(|| "stdlib runtime result is missing journal".to_owned())?;
        let completed_nodes = journal
            .iter()
            .filter(|record| record.get("kind").and_then(Value::as_str) == Some("node_completed"))
            .map(|record| {
                record
                    .get("nodeId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "node_completed record is missing nodeId".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("answer")),
            Some(&json!({
                "conversationId": "conversation-default",
                "text": "Hello from Rust.",
                "turnId": "turn-000001"
            }))
        );
        assert_eq!(
            completed_nodes,
            vec!["begin", "render", "generate", "commit"]
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_executes_scripted_agent_run() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-scripted-agent"},
            "spec": {
                "interface": {
                    "inputs": {
                        "messages": "graphblocks.ai/Messages@1",
                        "tools": "graphblocks.ai/ResolvedTools@1"
                    },
                    "outputs": {"candidate": "graphblocks.ai/AssistantCandidate@1"}
                },
                "nodes": {
                    "agent": {
                        "block": "agent.run@1",
                        "config": {"response": "native scripted response"},
                        "inputs": {
                            "messages": "$input.messages",
                            "tools": "$input.tools"
                        },
                        "outputs": {"candidate": "$output.candidate"}
                    }
                }
            }
        });
        let inputs = json!({
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{"definition": {"name": "knowledge.search"}}]
        });
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("candidate")),
            Some(&json!({
                "finishReason": "scripted",
                "text": "native scripted response",
                "toolCount": 1
            }))
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_resolves_tools_for_scripted_agent() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-agent-tools"},
            "spec": {
                "interface": {
                    "inputs": {"messages": "graphblocks.ai/Messages@1"},
                    "outputs": {
                        "candidate": "graphblocks.ai/AssistantCandidate@1",
                        "tools": "graphblocks.ai/ResolvedTools@1"
                    }
                },
                "nodes": {
                    "resolve": {
                        "block": "tools.resolve@1",
                        "config": {
                            "effectivePolicySnapshotId": "policy-snapshot-1",
                            "definitions": [
                                {
                                    "name": "knowledge.search",
                                    "description": "Search support documentation.",
                                    "inputSchema": "schemas/SearchRequest@1"
                                }
                            ],
                            "bindings": [
                                {
                                    "bindingId": "binding-search",
                                    "toolName": "knowledge.search",
                                    "implementation": {"kind": "block", "block": "knowledge.search@1"},
                                    "effects": ["external_read"],
                                    "approval": "never",
                                    "timeoutMs": 250
                                }
                            ],
                            "scope": {"principalTools": ["knowledge.search"]}
                        },
                        "outputs": {"tools": "$output.tools"}
                    },
                    "agent": {
                        "block": "agent.run@1",
                        "config": {"response": "Hello from native agent."},
                        "inputs": {
                            "messages": "$input.messages",
                            "tools": "resolve.tools"
                        },
                        "outputs": {"candidate": "$output.candidate"}
                    }
                }
            }
        });
        let inputs = json!({"messages": [{"role": "user", "content": "Hello"}]});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("candidate")),
            Some(&json!({
                "finishReason": "scripted",
                "text": "Hello from native agent.",
                "toolCount": 1
            }))
        );
        let resolved_tool = result
            .get("outputs")
            .and_then(|outputs| outputs.get("tools"))
            .and_then(Value::as_array)
            .and_then(|tools| tools.first())
            .ok_or_else(|| "resolved tools output missing first tool".to_owned())?;
        assert_eq!(
            resolved_tool
                .get("definition")
                .and_then(|definition| definition.get("name"))
                .and_then(Value::as_str),
            Some("knowledge.search")
        );
        assert_eq!(
            resolved_tool
                .get("allowed_for_principal")
                .and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(
            resolved_tool
                .get("binding")
                .and_then(|binding| binding.get("timeout_ms"))
                .and_then(Value::as_u64),
            Some(250)
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_maps_items_with_native_block() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-map-prompts"},
            "spec": {
                "interface": {
                    "inputs": {"items": "graphblocks.ai/Items@1"},
                    "outputs": {"values": "graphblocks.ai/Values@1"}
                },
                "nodes": {
                    "map": {
                        "block": "control.map@2",
                        "inputs": {"items": "$input.items"},
                        "outputs": {"values": "$output.values"},
                        "config": {
                            "block": "prompt.render@1",
                            "inputName": "message",
                            "outputName": "prompt",
                            "config": {"template": "Item {message.index}: {message.text}"}
                        }
                    }
                }
            }
        });
        let inputs = json!({
            "items": [
                {"index": 1, "text": "alpha"},
                {"index": 2, "text": "beta"}
            ]
        });
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("values")),
            Some(&json!(["Item 1: alpha", "Item 2: beta"]))
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_collects_native_map_errors() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-map-collect-errors"},
            "spec": {
                "interface": {
                    "inputs": {"items": "graphblocks.ai/Items@1"},
                    "outputs": {"outcomes": "graphblocks.ai/MapOutcomes@1"}
                },
                "nodes": {
                    "map": {
                        "block": "control.map@2",
                        "inputs": {"items": "$input.items"},
                        "outputs": {"outcomes": "$output.outcomes"},
                        "config": {
                            "block": "prompt.render@1",
                            "inputName": "message",
                            "outputName": "prompt",
                            "onError": "collect",
                            "config": {"template": "Value {message.text}"}
                        }
                    }
                }
            }
        });
        let inputs = json!({"items": [{"text": "ok"}, {}]});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let outcomes = result
            .get("outputs")
            .and_then(|outputs| outputs.get("outcomes"))
            .and_then(Value::as_array)
            .ok_or_else(|| "native map result is missing outcomes".to_owned())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            outcomes.first(),
            Some(&json!({"status": "succeeded", "value": "Value ok"}))
        );
        assert_eq!(
            outcomes
                .get(1)
                .and_then(|outcome| outcome.get("status"))
                .and_then(Value::as_str),
            Some("failed")
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_selects_native_cases() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-select-first"},
            "spec": {
                "interface": {
                    "inputs": {"cases": "graphblocks.ai/Cases@1"},
                    "outputs": {
                        "value": "graphblocks.ai/SelectedValue@1",
                        "selected": "graphblocks.ai/SelectedKey@1"
                    }
                },
                "nodes": {
                    "select": {
                        "block": "control.select@1",
                        "inputs": {"cases": "$input.cases"},
                        "outputs": {
                            "value": "$output.value",
                            "selected": "$output.selected"
                        },
                        "config": {"order": ["preferred", "fallback"], "default": "unused"}
                    }
                }
            }
        });
        let inputs = json!({"cases": {"preferred": null, "fallback": "fallback-value"}});
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let inputs_json = serde_json::to_string(&inputs).map_err(|error| error.to_string())?;
        let result_json =
            run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            Some("succeeded")
        );
        assert_eq!(
            result.get("outputs"),
            Some(&json!({"selected": "preferred", "value": null}))
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_blocks_commit_after_policy_stop() -> Result<(), String> {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "native-policy-stopped-turn"},
            "spec": {
                "interface": {
                    "inputs": {"message": "graphblocks.ai/Message@1"},
                    "outputs": {"answer": "graphblocks.ai/Answer@1"}
                },
                "nodes": {
                    "begin": {"block": "conversation.begin_turn@1"},
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Answer: {message.text}"},
                        "inputs": {"message": "$input.message"}
                    },
                    "generate": {
                        "block": "model.generate@1",
                        "config": {"script": {"Answer: Hello": "blocked answer"}},
                        "inputs": {"prompt": "render.prompt"}
                    },
                    "stop": {
                        "block": "conversation.policy_stop_turn@1",
                        "inputs": {"transaction": "begin.transaction"}
                    },
                    "commit": {
                        "block": "conversation.commit_turn@1",
                        "inputs": {
                            "transaction": "stop.transaction",
                            "candidate": "generate.response"
                        },
                        "outputs": {"answer": "$output.answer"}
                    }
                }
            }
        });
        let graph_json = serde_json::to_string(&graph).map_err(|error| error.to_string())?;
        let result_json = run_stdlib_graph_json(&graph_json, r#"{"message":{"text":"Hello"}}"#)
            .map_err(|error| error.to_string())?;
        let result =
            serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
        let journal = result
            .get("journal")
            .and_then(Value::as_array)
            .ok_or_else(|| "stdlib runtime result is missing journal".to_owned())?;
        let failed = journal
            .iter()
            .find(|record| record.get("kind").and_then(Value::as_str) == Some("node_failed"))
            .ok_or_else(|| "stdlib runtime result is missing node_failed".to_owned())?;

        assert_eq!(result.get("status").and_then(Value::as_str), Some("failed"));
        assert_eq!(
            result
                .get("outputs")
                .and_then(|outputs| outputs.get("answer")),
            None
        );
        assert_eq!(failed.get("nodeId").and_then(Value::as_str), Some("commit"));
        assert_eq!(
            failed
                .get("payload")
                .and_then(|payload| payload.get("message"))
                .and_then(Value::as_str),
            Some("conversation.commit_turn@1 cannot commit policy-stopped turn")
        );

        Ok(())
    }

    #[test]
    fn run_stdlib_graph_json_passes_shared_runtime_tck_cases() -> Result<(), String> {
        let cases_path =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tck/runtime/cases.json");
        let cases_text = std::fs::read_to_string(&cases_path).map_err(|error| error.to_string())?;
        let cases =
            serde_json::from_str::<Value>(&cases_text).map_err(|error| error.to_string())?;
        let cases = cases
            .as_array()
            .ok_or_else(|| "runtime TCK root must be an array".to_owned())?;

        for case in cases {
            let case_name = case
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(|| "runtime TCK case missing name".to_owned())?;
            let document = case
                .get("document")
                .ok_or_else(|| format!("runtime TCK case {case_name} missing document"))?;
            let expected = case
                .get("expected")
                .and_then(Value::as_object)
                .ok_or_else(|| format!("runtime TCK case {case_name} missing expected object"))?;
            let inputs = case.get("inputs").cloned().unwrap_or_else(|| json!({}));
            let result_json = run_stdlib_graph_json(
                &serde_json::to_string(document).map_err(|error| error.to_string())?,
                &serde_json::to_string(&inputs).map_err(|error| error.to_string())?,
            )
            .map_err(|error| error.to_string())?;
            let result =
                serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())?;
            let terminal_kind = result
                .get("journal")
                .and_then(Value::as_array)
                .and_then(|journal| {
                    journal.iter().rev().find(|record| {
                        record.get("terminal").and_then(Value::as_bool) == Some(true)
                    })
                })
                .and_then(|record| record.get("kind"))
                .and_then(Value::as_str);

            assert_eq!(
                result.get("status").and_then(Value::as_str),
                expected.get("status").and_then(Value::as_str),
                "runtime TCK case {case_name} status mismatch",
            );
            assert_eq!(
                terminal_kind,
                expected.get("terminal_kind").and_then(Value::as_str),
                "runtime TCK case {case_name} terminal kind mismatch",
            );
            assert_eq!(
                result.get("outputs"),
                expected.get("outputs"),
                "runtime TCK case {case_name} outputs mismatch",
            );
        }

        Ok(())
    }
}
