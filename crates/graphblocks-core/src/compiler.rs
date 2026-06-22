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
