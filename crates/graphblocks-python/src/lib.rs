use std::collections::BTreeMap;

use graphblocks_compiler::compiler::compile_graph;
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_protocol::{
    RemotePayload, RemotePayloadError, RemotePayloadLimits, WorkerAdmissionPolicy,
    WorkerAdvertisement, WorkerProtocolError, admit_worker_with_policy, validate_remote_payload,
};
use graphblocks_runtime_core::budget::{BudgetPermit, UsageAmount};
use graphblocks_runtime_core::exhaustion::{
    ContinuationEnvelope, ExhaustionController, ExhaustionPolicy, ExhaustionPreset, ExhaustionUnit,
    WorkKind,
};
use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory, Outcome};
use graphblocks_runtime_core::readiness::{InputDependency, PortRef, ResolvedInput};
use graphblocks_runtime_core::scheduler::{ScheduledNode, StartedNode};
use graphblocks_runtime_core::test_runtime::{InProcessTestRuntime, NodeExecutor, TestRunStatus};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use serde_json::{Value, json};

#[pyfunction]
fn binding_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pyfunction]
fn compile_graph_json(document_json: &str) -> PyResult<String> {
    let document = serde_json::from_str::<Value>(document_json)
        .map_err(|error| PyValueError::new_err(format!("invalid graph document JSON: {error}")))?;
    let plan = compile_graph(&document);
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
        let outputs = match block_id {
            "conversation.begin_turn@1" => execute_begin_turn(&inputs, &config),
            "prompt.render@1" => execute_prompt_render(&inputs, &config),
            "model.generate@1" => execute_scripted_generate(&inputs, &config),
            "conversation.commit_turn@1" => execute_commit_turn(&inputs),
            _ => Err(BlockError::new(
                format!("{block_id}.unsupported"),
                ErrorCategory::Configuration,
                "unsupported stdlib block",
                false,
            )),
        }?;
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

fn required_u64(
    object: &serde_json::Map<String, Value>,
    field: &str,
    label: &str,
) -> PyResult<u64> {
    object.get(field).and_then(Value::as_u64).ok_or_else(|| {
        PyValueError::new_err(format!("{label}.{field} must be an unsigned integer"))
    })
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

fn execute_commit_turn(inputs: &Value) -> Result<Value, BlockError> {
    let Some(transaction) = inputs.get("transaction").and_then(Value::as_object) else {
        return Err(BlockError::new(
            "conversation.commit_turn.missing_transaction",
            ErrorCategory::Configuration,
            "conversation.commit_turn@1 requires transaction input",
            false,
        ));
    };
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
    let continuation_permit = request
        .get("continuationPermit")
        .map(|value| parse_budget_permit(value, "request.continuationPermit"))
        .transpose()?;
    let mut controller = ExhaustionController::new(policy, atomic_unit_id, admission_epoch);
    if let Some(continuation_permit) = continuation_permit {
        controller = controller.with_continuation_permit(continuation_permit);
    }

    let decision = controller.admit(work_kind, work_epoch, permit.as_ref());
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

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add_function(wrap_pyfunction!(binding_version, module)?)?;
    module.add_function(wrap_pyfunction!(compile_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(
        validate_worker_advertisement_json,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(validate_remote_payload_json, module)?)?;
    module.add_function(wrap_pyfunction!(run_test_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(run_stdlib_graph_json, module)?)?;
    module.add_function(wrap_pyfunction!(admit_exhaustion_work_json, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::{
        admit_exhaustion_work_json, compile_graph_json, run_stdlib_graph_json, run_test_graph_json,
        validate_remote_payload_json, validate_worker_advertisement_json,
    };

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
            let compiled_json =
                compile_graph_json(&document_json).map_err(|error| error.to_string())?;
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
}
