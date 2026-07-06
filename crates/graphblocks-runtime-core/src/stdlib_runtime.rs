use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::compiler::compile_graph;
use graphblocks_compiler::diagnostics::Severity;
use serde_json::{Value, json};

use crate::async_operation::{
    AsyncOperation, AsyncOperationKind, AsyncOperationResult, AsyncOperationResultStatus,
    AsyncOperationState,
    ExternalEffectRecord,
};
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
use crate::tool_result::ToolEffectOutcome;

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
    let expires_at_unix_ms =
        if let Some(expires_at_unix_ms) =
            optional_alias_u64(config, "expiresAtUnixMs", "expires_at_unix_ms")?
        {
            Some(expires_at_unix_ms)
        } else {
            optional_alias_duration_ms(
                config,
                &["timeoutMs", "timeout_ms", "timeout"],
                "async.start_operation.invalid_config",
                "async.start_operation@1 timeout must be a positive duration",
            )?
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
        "operation": async_operation_json(&operation, inputs.get("subject").cloned()),
    }))
}

fn execute_async_await_callback(inputs: &Value, config: &Value) -> Result<Value, BlockError> {
    let operation = inputs.get("operation").ok_or_else(|| {
        BlockError::new(
            "async.await_callback.missing_operation",
            ErrorCategory::Configuration,
            "async.await_callback@1 requires operation input",
            false,
        )
    })?;
    let Some(operation_object) = operation.as_object() else {
        return Err(BlockError::new(
            "async.await_callback.invalid_operation",
            ErrorCategory::Configuration,
            "async.await_callback@1 input operation must be an object",
            false,
        ));
    };
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
        "wait": wait
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
        &["maxIntervalMs", "max_interval_ms", "maxInterval", "max_interval"],
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
        return Err(
            BlockError::new(
                "async.poll_operation.missing_timeout",
                ErrorCategory::Configuration,
                "async.poll_operation@1 requires timeoutMs",
                false,
            )
        );
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
    let external_effects =
        parse_async_external_effects(config, "async.complete_operation@1")?;

    Ok(json!({
        "result": async_operation_result_json(
            AsyncOperationResult::completed(operation_id)
                .with_output(output)
                .with_external_effects(external_effects),
            completed_at_unix_ms,
        )?,
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
    let external_effects =
        parse_async_external_effects(config, "async.cancel_operation@1")?;

    Ok(json!({
        "result": async_operation_result_json(
            AsyncOperationResult::cancelled(operation_id).with_external_effects(external_effects),
            completed_at_unix_ms,
        )?,
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
    let external_effects =
        parse_async_external_effects(config, "async.expire_operation@1")?;

    Ok(json!({
        "result": async_operation_result_json(
            AsyncOperationResult::expired(operation_id).with_external_effects(external_effects),
            completed_at_unix_ms,
        )?,
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
    let Some(tools) = inputs.get("tools").and_then(Value::as_array) else {
        return Err(BlockError::new(
            "agent.run.invalid_tools",
            ErrorCategory::Configuration,
            "agent.run@1 input 'tools' must be a list",
            false,
        ));
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
    if let Some(duration_ms) = value.as_u64().filter(|duration_ms| *duration_ms > 0) {
        return Ok(Some(duration_ms));
    }
    if let Some(text) = value.as_str() {
        let text = text.trim();
        for (suffix, multiplier) in [
            ("ms", 1_u64),
            ("s", 1_000),
            ("m", 60_000),
            ("h", 3_600_000),
            ("d", 86_400_000),
        ] {
            let Some(amount) = text.strip_suffix(suffix) else {
                continue;
            };
            if amount.as_bytes().iter().all(u8::is_ascii_digit)
                && let Ok(amount) = amount.parse::<u64>()
                && amount > 0
                && let Some(duration_ms) = amount.checked_mul(multiplier)
            {
                return Ok(Some(duration_ms));
            }
        }
    }
    Err(BlockError::new(
        code,
        ErrorCategory::Configuration,
        message,
        false,
    ))
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
            value.as_str().filter(|text| !text.trim().is_empty()).ok_or_else(|| {
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
                &format!("{block_label}.invalid_config"),
            )?,
            required_object_str(effect, "target", &format!("{block_label}.invalid_config"))?,
            required_object_str(effect, "operation", &format!("{block_label}.invalid_config"))?,
            parse_tool_effect_outcome(required_object_str(
                effect,
                "outcome",
                &format!("{block_label}.invalid_config"),
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
        "artifacts": [],
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
    if let Some(expires_at_unix_ms) = operation.get("expires_at_unix_ms").and_then(Value::as_u64)
        && completed_at_unix_ms > expires_at_unix_ms
    {
        return Err(BlockError::new(
            error_code,
            ErrorCategory::Configuration,
            format!("{block_label} terminal timestamp must not exceed expires_at_unix_ms"),
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
