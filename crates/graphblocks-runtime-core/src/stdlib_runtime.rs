use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::compiler::compile_graph;
use graphblocks_compiler::diagnostics::Severity;
use serde_json::{Value, json};

use crate::outcome::{BlockError, ErrorCategory, Outcome};
use crate::readiness::{InputDependency, PortRef, ResolvedInput};
use crate::scheduler::{ScheduledNode, StartedNode};
use crate::test_runtime::{InProcessTestRuntime, NodeExecutor, TestRunResult, TestRunStatus};
use crate::tool::{
    BlockToolImplementation, GraphToolImplementation, McpToolImplementation,
    OpenApiToolImplementation, RemoteToolImplementation, ResolvedTool, ToolApproval, ToolBinding,
    ToolCancellation, ToolCatalog, ToolDefinition, ToolEffect, ToolIdempotency, ToolImplementation,
    ToolResolutionScope, ToolResultMode,
};

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

struct RuntimeBridgePlan {
    graph_hash: String,
    nodes: BTreeMap<String, Value>,
    edges: Vec<Value>,
    scheduled_nodes: Vec<ScheduledNode>,
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

pub fn run_stdlib_graph_json(
    graph_json: &str,
    inputs_json: &str,
) -> Result<String, StdlibRuntimeError> {
    let graph = parse_json_argument(graph_json, "graph document")?;
    let inputs = parse_json_argument(inputs_json, "runtime inputs")?;
    let bridge_plan = build_runtime_bridge_plan(&graph)?;
    let mut runtime = runtime_with_inputs(bridge_plan.scheduled_nodes, &inputs)?;
    let mut executor = StdlibExecutor {
        nodes: bridge_plan.nodes,
        outputs_by_node: BTreeMap::new(),
    };
    let result = runtime.run(&mut executor).map_err(|error| {
        StdlibRuntimeError::runtime(format!("stdlib runtime execution failed: {error:?}"))
    })?;
    let output_values = collect_output_values(
        &bridge_plan.edges,
        &inputs,
        &executor.outputs_by_node,
        result.status,
    )?;
    serialize_runtime_result(result, bridge_plan.graph_hash, output_values)
}

fn parse_json_argument(text: &str, label: &str) -> Result<Value, StdlibRuntimeError> {
    serde_json::from_str::<Value>(text)
        .map_err(|error| StdlibRuntimeError::invalid(format!("invalid {label} JSON: {error}")))
}

fn build_runtime_bridge_plan(graph: &Value) -> Result<RuntimeBridgePlan, StdlibRuntimeError> {
    let plan = compile_graph(graph);
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
            return Err(StdlibRuntimeError::invalid(format!(
                "edge source {source:?} must include a port"
            )));
        };
        let Some(target_input) = target_path
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
) -> Result<InProcessTestRuntime, StdlibRuntimeError> {
    let mut runtime =
        InProcessTestRuntime::new("run-000001", scheduled_nodes).map_err(|error| {
            StdlibRuntimeError::invalid(format!("failed to create test runtime: {error:?}"))
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
) -> Result<Value, StdlibRuntimeError> {
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
                    StdlibRuntimeError::runtime(format!(
                        "output edge references missing node output {source_owner:?}"
                    ))
                })?
            };
            if !source_path.is_empty() {
                for part in source_path.split('.') {
                    value = value.get(part).cloned().ok_or_else(|| {
                        StdlibRuntimeError::runtime(format!(
                            "output edge source {source:?} is missing path segment {part:?}"
                        ))
                    })?;
                }
            }
            let target_parts = target_path.split('.').collect::<Vec<_>>();
            if target_parts.is_empty() || target_parts.iter().any(|part| part.is_empty()) {
                return Err(StdlibRuntimeError::invalid(format!(
                    "output edge target {target:?} must include an output path"
                )));
            }
            let mut current = &mut output_values;
            for part in &target_parts[..target_parts.len() - 1] {
                let Some(current_object) = current.as_object_mut() else {
                    return Err(StdlibRuntimeError::runtime(format!(
                        "output path conflict at {target:?}"
                    )));
                };
                current = current_object
                    .entry((*part).to_owned())
                    .or_insert_with(|| json!({}));
            }
            let Some(current_object) = current.as_object_mut() else {
                return Err(StdlibRuntimeError::runtime(format!(
                    "output path conflict at {target:?}"
                )));
            };
            current_object.insert(target_parts[target_parts.len() - 1].to_owned(), value);
        }
    }

    Ok(output_values)
}

fn serialize_runtime_result(
    result: TestRunResult,
    graph_hash: String,
    output_values: Value,
) -> Result<String, StdlibRuntimeError> {
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

    serde_json::to_string(&payload).map_err(StdlibRuntimeError::serialization)
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
                parsed = parsed.with_tags(tags.iter().map(json_display));
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
                "block" => ToolImplementation::Block(BlockToolImplementation::new(
                    required_object_str(implementation, "block", "tools.resolve.invalid_binding")?,
                )),
                "graph" => ToolImplementation::Graph(GraphToolImplementation::new(
                    required_object_str(implementation, "graph", "tools.resolve.invalid_binding")?,
                )),
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
