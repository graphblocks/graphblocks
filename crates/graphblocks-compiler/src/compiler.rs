use serde_json::Value;

use crate::canonical::canonical_hash;
use crate::diagnostics::{Diagnostic, Severity};
use crate::graph::{GRAPH_API_VERSION, PSEUDO_NODES, normalize_graph};

#[derive(Clone, Debug, PartialEq)]
pub struct Plan {
    pub normalized: Value,
    pub graph_hash: String,
    pub diagnostics: Vec<Diagnostic>,
}

impl Plan {
    pub fn ok(&self) -> bool {
        !self
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.severity == Severity::Error)
    }
}

pub fn compile_graph(document: &Value) -> Plan {
    let mut diagnostics = Vec::new();

    if document.get("kind").and_then(Value::as_str) != Some("Graph") {
        let normalized = normalize_graph(document);
        diagnostics.push(Diagnostic::error(
            "GB0001",
            "document kind must be Graph",
            "$.kind",
        ));
        return Plan {
            graph_hash: canonical_hash(&normalized),
            normalized,
            diagnostics,
        };
    }

    if document.get("apiVersion").and_then(Value::as_str) != Some(GRAPH_API_VERSION) {
        diagnostics.push(Diagnostic::error(
            "GB0002",
            format!(
                "unsupported Graph apiVersion {:?}",
                document.get("apiVersion")
            ),
            "$.apiVersion",
        ));
    }

    let metadata = document.get("metadata").and_then(Value::as_object);
    if metadata
        .and_then(|metadata| metadata.get("name"))
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        diagnostics.push(Diagnostic::error(
            "GB0003",
            "metadata.name is required",
            "$.metadata.name",
        ));
    }

    let spec = document.get("spec");
    if spec.is_none_or(|spec| !spec.is_object()) {
        let normalized = normalize_graph(document);
        diagnostics.push(Diagnostic::error(
            "GB0004",
            "spec must be a mapping",
            "$.spec",
        ));
        return Plan {
            graph_hash: canonical_hash(&normalized),
            normalized,
            diagnostics,
        };
    }

    let nodes = spec
        .and_then(|spec| spec.get("nodes"))
        .and_then(Value::as_object);
    if spec
        .and_then(|spec| spec.get("nodes"))
        .is_some_and(|nodes| !nodes.is_object())
    {
        diagnostics.push(Diagnostic::error(
            "GB0005",
            "spec.nodes must be a mapping",
            "$.spec.nodes",
        ));
    }

    if let Some(nodes) = nodes {
        for (node_name, node) in nodes {
            if node_name.is_empty() {
                diagnostics.push(Diagnostic::error(
                    "GB0006",
                    "node name must be a non-empty string",
                    "$.spec.nodes",
                ));
                continue;
            }
            if node_name.starts_with('$') {
                diagnostics.push(Diagnostic::error(
                    "GB0007",
                    "node names cannot use pseudo-node prefix '$'",
                    format!("$.spec.nodes.{node_name}"),
                ));
            }
            let Some(node) = node.as_object() else {
                diagnostics.push(Diagnostic::error(
                    "GB0008",
                    "node spec must be a mapping",
                    format!("$.spec.nodes.{node_name}"),
                ));
                continue;
            };
            if !node
                .get("block")
                .and_then(Value::as_str)
                .is_some_and(|block| block.contains('@') && !block.ends_with('@'))
            {
                diagnostics.push(Diagnostic::error(
                    "GB0009",
                    "node.block must use '<type>@<major>'",
                    format!("$.spec.nodes.{node_name}.block"),
                ));
            }
            if node.contains_key("connection") && node.contains_key("bindings") {
                diagnostics.push(Diagnostic::error(
                    "GB1006",
                    "connection shorthand cannot be combined with explicit bindings",
                    format!("$.spec.nodes.{node_name}"),
                ));
            }

            let effect_retry_requires_key = match node.get("effects") {
                Some(Value::String(effect)) => {
                    matches!(
                        effect.as_str(),
                        "external_write" | "destructive" | "process"
                    )
                }
                Some(Value::Array(effects)) => effects.iter().any(|effect| {
                    effect.as_str().is_some_and(|effect| {
                        matches!(effect, "external_write" | "destructive" | "process")
                    })
                }),
                _ => false,
            };
            let retry = node
                .get("flow")
                .and_then(Value::as_object)
                .and_then(|flow| flow.get("retry"));
            let max_attempts = match retry {
                Some(Value::Object(retry)) => retry
                    .get("maxAttempts")
                    .and_then(Value::as_i64)
                    .unwrap_or(1),
                Some(Value::Number(retry)) => retry.as_i64().unwrap_or(1),
                _ => 1,
            };
            let idempotency_key = retry.and_then(Value::as_object).and_then(|retry| {
                retry
                    .get("idempotencyKey")
                    .or_else(|| retry.get("idempotency_key"))
            });
            if effect_retry_requires_key && max_attempts > 1 && idempotency_key.is_none() {
                diagnostics.push(Diagnostic::error(
                    "GB1011",
                    "retrying effectful nodes requires an idempotency key",
                    format!("$.spec.nodes.{node_name}.flow.retry"),
                ));
            }
        }
    }

    if let Some(delivery) = spec
        .and_then(|spec| spec.get("outputPolicy"))
        .or_else(|| spec.and_then(|spec| spec.get("output_policy")))
        .and_then(|output_policy| output_policy.get("delivery"))
        .and_then(Value::as_object)
    {
        let mode = delivery.get("mode").and_then(Value::as_str);
        if mode == Some("bounded_holdback") {
            let has_token_bound = delivery
                .get("holdbackMaxTokens")
                .or_else(|| delivery.get("holdback_max_tokens"))
                .and_then(Value::as_u64)
                .is_some_and(|value| value > 0);
            let has_byte_bound = delivery
                .get("holdbackMaxBytes")
                .or_else(|| delivery.get("holdback_max_bytes"))
                .and_then(Value::as_u64)
                .is_some_and(|value| value > 0);
            let has_duration_bound = delivery
                .get("holdbackMaxDuration")
                .or_else(|| delivery.get("holdback_max_duration"))
                .or_else(|| delivery.get("holdbackMaxDurationMs"))
                .or_else(|| delivery.get("holdback_max_duration_ms"))
                .is_some_and(|duration| match duration {
                    Value::Number(duration) => duration.as_u64().is_some_and(|value| value > 0),
                    Value::String(duration) => !duration.trim().is_empty() && duration != "0ms",
                    _ => false,
                });
            if !has_token_bound && !has_byte_bound && !has_duration_bound {
                diagnostics.push(Diagnostic::error(
                    "UnboundedPolicyHoldback",
                    "bounded_holdback output delivery requires a token, byte, or duration bound",
                    "$.spec.outputPolicy.delivery",
                ));
            }
        }

        if mode == Some("immediate_draft") {
            let delivered_draft_disposition = delivery
                .get("deliveredDraftDisposition")
                .or_else(|| delivery.get("delivered_draft_disposition"))
                .and_then(Value::as_str)
                .unwrap_or("retract");
            if delivered_draft_disposition == "keep" {
                diagnostics.push(Diagnostic::error(
                    "ImmediateDraftWithoutRetractionSupport",
                    "immediate_draft output delivery requires incomplete or retracted draft semantics",
                    "$.spec.outputPolicy.delivery.deliveredDraftDisposition",
                ));
            }
        }
    }

    if let Some(tools) = spec
        .and_then(|spec| spec.get("bindings"))
        .and_then(|bindings| bindings.get("tools"))
        .and_then(Value::as_object)
    {
        let tool_execution = spec
            .and_then(|spec| spec.get("toolExecution"))
            .or_else(|| spec.and_then(|spec| spec.get("tool_execution")))
            .and_then(Value::as_object);
        let maximum_parallelism = tool_execution
            .and_then(|tool_execution| {
                tool_execution
                    .get("maximumParallelism")
                    .or_else(|| tool_execution.get("maximum_parallelism"))
            })
            .and_then(Value::as_u64)
            .unwrap_or(1);
        let parallel_tool_calls = tool_execution
            .and_then(|tool_execution| {
                tool_execution
                    .get("parallelToolCalls")
                    .or_else(|| tool_execution.get("parallel_tool_calls"))
            })
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let has_effect_serialization_key = tool_execution
            .and_then(|tool_execution| {
                tool_execution
                    .get("effectSerialization")
                    .or_else(|| tool_execution.get("effect_serialization"))
            })
            .and_then(Value::as_object)
            .and_then(|effect_serialization| {
                effect_serialization
                    .get("keyTemplate")
                    .or_else(|| effect_serialization.get("key_template"))
            })
            .and_then(Value::as_str)
            .is_some_and(|key_template| !key_template.trim().is_empty());
        let mut has_state_changing_tool = false;

        for (tool_key, tool) in tools {
            let Some(tool) = tool.as_object() else {
                continue;
            };
            let state_changing_tool = match tool.get("effects") {
                Some(Value::String(effect)) => {
                    matches!(
                        effect.as_str(),
                        "external_write" | "filesystem_write" | "process" | "destructive"
                    )
                }
                Some(Value::Array(effects)) => effects.iter().any(|effect| {
                    effect.as_str().is_some_and(|effect| {
                        matches!(
                            effect,
                            "external_write" | "filesystem_write" | "process" | "destructive"
                        )
                    })
                }),
                _ => false,
            };
            has_state_changing_tool |= state_changing_tool;
            let has_retry_policy_ref = tool
                .get("retryPolicyRef")
                .or_else(|| tool.get("retry_policy_ref"))
                .and_then(Value::as_str)
                .is_some_and(|retry_policy_ref| !retry_policy_ref.trim().is_empty());
            let idempotency = tool.get("idempotency").and_then(Value::as_str);
            if state_changing_tool && has_retry_policy_ref && idempotency != Some("required") {
                diagnostics.push(Diagnostic::error(
                    "NonIdempotentRetry",
                    "retrying state-changing tool effects requires required idempotency",
                    format!("$.spec.bindings.tools.{tool_key}.idempotency"),
                ));
            }
            if let Some(definition) = tool.get("definition").and_then(Value::as_object) {
                let has_input_schema = definition
                    .get("inputSchema")
                    .or_else(|| definition.get("input_schema"))
                    .and_then(Value::as_str)
                    .is_some_and(|schema| !schema.trim().is_empty());
                if !has_input_schema {
                    diagnostics.push(Diagnostic::error(
                        "ToolSchemaMissing",
                        "model-visible tool definitions require an input schema",
                        format!("$.spec.bindings.tools.{tool_key}.definition.inputSchema"),
                    ));
                }
                if !tool.contains_key("implementation") {
                    diagnostics.push(Diagnostic::error(
                        "ToolBindingMissing",
                        "model-visible tools require an executable binding implementation",
                        format!("$.spec.bindings.tools.{tool_key}.implementation"),
                    ));
                }
            }
        }

        if (maximum_parallelism > 1 || parallel_tool_calls)
            && has_state_changing_tool
            && !has_effect_serialization_key
        {
            diagnostics.push(Diagnostic::error(
                "UnsafeParallelEffects",
                "parallel state-changing tool execution requires an effect serialization key",
                "$.spec.toolExecution.effectSerialization",
            ));
        }
    }

    let normalized = normalize_graph(document);
    let normalized_nodes = normalized
        .get("spec")
        .and_then(|spec| spec.get("nodes"))
        .and_then(Value::as_object);
    if let Some(edges) = normalized
        .get("spec")
        .and_then(|spec| spec.get("edges"))
        .and_then(Value::as_array)
    {
        for (index, edge) in edges.iter().enumerate() {
            let Some(edge) = edge.as_object() else {
                diagnostics.push(Diagnostic::error(
                    "GB0010",
                    "edge must be a mapping",
                    format!("$.spec.edges[{index}]"),
                ));
                continue;
            };
            let source = edge.get("from").and_then(Value::as_str);
            let target = edge.get("to").and_then(Value::as_str);
            let (Some(source), Some(target)) = (source, target) else {
                diagnostics.push(Diagnostic::error(
                    "GB0011",
                    "edge.from and edge.to must be strings",
                    format!("$.spec.edges[{index}]"),
                ));
                continue;
            };
            for (key, endpoint) in [("from", source), ("to", target)] {
                let owner = endpoint
                    .split_once('.')
                    .map_or(endpoint, |(owner, _)| owner);
                if PSEUDO_NODES.contains(&owner) {
                    continue;
                }
                if normalized_nodes.is_none_or(|nodes| !nodes.contains_key(owner)) {
                    diagnostics.push(Diagnostic::error(
                        "GB1002",
                        format!("edge {key} endpoint references unknown node {owner:?}"),
                        format!("$.spec.edges[{index}].{key}"),
                    ));
                }
            }
        }
    }

    Plan {
        graph_hash: canonical_hash(&normalized),
        normalized,
        diagnostics,
    }
}
