use std::collections::{BTreeMap, BTreeSet};

use graphblocks_schema::SchemaId;
use serde_json::{Map, Value};

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

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BlockCatalog {
    descriptors: BTreeMap<String, BlockDescriptor>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlockDescriptor {
    pub type_id: String,
    pub version: u64,
    pub inputs: Vec<PortDescriptor>,
    pub outputs: Vec<PortDescriptor>,
    pub resource_slots: Vec<ResourceSlotDescriptor>,
}

impl BlockDescriptor {
    fn block_id(&self) -> String {
        format!("{}@{}", self.type_id, self.version)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PortDescriptor {
    pub name: String,
    pub type_ref: Option<String>,
    pub required: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResourceSlotDescriptor {
    pub name: String,
    pub type_ref: Option<String>,
    pub optional: bool,
}

const FORBIDDEN_TOOL_DEFINITION_FIELDS: [&str; 9] = [
    "credentials",
    "credential",
    "secret",
    "secrets",
    "connection",
    "transport",
    "providerSdk",
    "provider_sdk",
    "implementation",
];

impl BlockCatalog {
    pub fn from_blocks(blocks: &Value) -> Result<Self, String> {
        let blocks = blocks
            .as_array()
            .ok_or_else(|| "block catalog must be an array".to_owned())?;
        let mut descriptors = BTreeMap::new();

        for (index, block) in blocks.iter().enumerate() {
            let block = block
                .as_object()
                .ok_or_else(|| format!("block catalog entry {index} must be an object"))?;
            let mut type_id = block
                .get("typeId")
                .or_else(|| block.get("type_id"))
                .or_else(|| block.get("block"))
                .and_then(Value::as_str)
                .ok_or_else(|| format!("block catalog entry {index} is missing typeId"))?
                .to_owned();
            let mut version = block.get("version").and_then(Value::as_u64);
            if version.is_none()
                && let Some((parsed_type_id, parsed_version)) = type_id.rsplit_once('@')
            {
                version = parsed_version.parse::<u64>().ok();
                type_id = parsed_type_id.to_owned();
            }
            let version =
                version.ok_or_else(|| format!("block catalog entry {index} is missing version"))?;
            let inputs = block
                .get("inputs")
                .and_then(Value::as_array)
                .map(|inputs| {
                    inputs
                        .iter()
                        .filter_map(|port| {
                            let port = port.as_object()?;
                            let name = port.get("name").and_then(Value::as_str)?;
                            Some(PortDescriptor {
                                name: name.to_owned(),
                                type_ref: port
                                    .get("type")
                                    .and_then(Value::as_str)
                                    .map(str::to_owned),
                                required: port
                                    .get("required")
                                    .and_then(Value::as_bool)
                                    .unwrap_or(true),
                            })
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let outputs = block
                .get("outputs")
                .and_then(Value::as_array)
                .map(|outputs| {
                    outputs
                        .iter()
                        .filter_map(|port| {
                            let port = port.as_object()?;
                            let name = port.get("name").and_then(Value::as_str)?;
                            Some(PortDescriptor {
                                name: name.to_owned(),
                                type_ref: port
                                    .get("type")
                                    .and_then(Value::as_str)
                                    .map(str::to_owned),
                                required: port
                                    .get("required")
                                    .and_then(Value::as_bool)
                                    .unwrap_or(true),
                            })
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let resource_slots = match block.get("resourceSlots") {
                Some(Value::Array(slots)) => slots
                    .iter()
                    .filter_map(|slot| {
                        let slot = slot.as_object()?;
                        let name = slot.get("name").and_then(Value::as_str)?;
                        Some(ResourceSlotDescriptor {
                            name: name.to_owned(),
                            type_ref: slot.get("type").and_then(Value::as_str).map(str::to_owned),
                            optional: slot
                                .get("optional")
                                .and_then(Value::as_bool)
                                .unwrap_or(false),
                        })
                    })
                    .collect::<Vec<_>>(),
                Some(Value::Object(slots)) => slots
                    .iter()
                    .map(|(slot_name, slot)| {
                        let slot = slot.as_object();
                        ResourceSlotDescriptor {
                            name: slot_name.to_owned(),
                            type_ref: slot
                                .and_then(|slot| slot.get("type"))
                                .and_then(Value::as_str)
                                .map(str::to_owned),
                            optional: slot
                                .and_then(|slot| slot.get("optional"))
                                .and_then(Value::as_bool)
                                .unwrap_or(false),
                        }
                    })
                    .collect::<Vec<_>>(),
                _ => Vec::new(),
            };
            let is_direct_schema_type_ref = |type_ref: &str| {
                (type_ref.contains('@') || type_ref.contains('/'))
                    && !type_ref.contains('<')
                    && !type_ref.contains('>')
            };
            for port in &inputs {
                if let Some(type_ref) = &port.type_ref
                    && is_direct_schema_type_ref(type_ref)
                    && let Err(error) = SchemaId::parse(type_ref)
                {
                    return Err(format!(
                        "block catalog entry {index} input {} has invalid type {type_ref}: {error}",
                        port.name
                    ));
                }
            }
            for port in &outputs {
                if let Some(type_ref) = &port.type_ref
                    && is_direct_schema_type_ref(type_ref)
                    && let Err(error) = SchemaId::parse(type_ref)
                {
                    return Err(format!(
                        "block catalog entry {index} output {} has invalid type {type_ref}: {error}",
                        port.name
                    ));
                }
            }
            for slot in &resource_slots {
                if let Some(type_ref) = &slot.type_ref
                    && is_direct_schema_type_ref(type_ref)
                    && let Err(error) = SchemaId::parse(type_ref)
                {
                    return Err(format!(
                        "block catalog entry {index} resource slot {} has invalid type {type_ref}: {error}",
                        slot.name
                    ));
                }
            }
            let descriptor = BlockDescriptor {
                type_id,
                version,
                inputs,
                outputs,
                resource_slots,
            };
            descriptors.insert(descriptor.block_id(), descriptor);
        }

        Ok(Self { descriptors })
    }

    pub fn get(&self, block_id: &str) -> Option<&BlockDescriptor> {
        self.descriptors.get(block_id)
    }
}

pub fn compile_graph(document: &Value) -> Plan {
    compile_graph_with_catalog(document, &BlockCatalog::default())
}

pub fn compile_graph_with_catalog(document: &Value, block_catalog: &BlockCatalog) -> Plan {
    let mut diagnostics = Vec::new();
    let mut migrated = document.clone();
    if migrated.get("kind").and_then(Value::as_str) == Some("Graph")
        && let Some(api_version @ ("graphblocks.ai/v1alpha1" | "graphblocks.ai/v1alpha2")) =
            migrated.get("apiVersion").and_then(Value::as_str)
    {
        let previous = api_version.to_owned();
        if let Some(root) = migrated.as_object_mut() {
            root.insert(
                "apiVersion".to_owned(),
                Value::String(GRAPH_API_VERSION.to_owned()),
            );
            if !root.contains_key("metadata") {
                root.insert("metadata".to_owned(), Value::Object(Map::new()));
            }
            if let Some(metadata) = root.get_mut("metadata").and_then(Value::as_object_mut) {
                if !metadata.contains_key("annotations") {
                    metadata.insert("annotations".to_owned(), Value::Object(Map::new()));
                }
                if let Some(annotations) = metadata
                    .get_mut("annotations")
                    .and_then(Value::as_object_mut)
                {
                    annotations
                        .entry("graphblocks.ai/migratedFrom")
                        .or_insert(Value::String(previous));
                }
            }
        }
    }
    let document = &migrated;

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

    if let Some(interface) = spec
        .and_then(|spec| spec.get("interface"))
        .and_then(Value::as_object)
    {
        for direction in ["inputs", "outputs"] {
            if let Some(ports) = interface.get(direction).and_then(Value::as_object) {
                for (port_name, schema_id) in ports {
                    let path = format!("$.spec.interface.{direction}.{port_name}");
                    let Some(schema_id) = schema_id.as_str() else {
                        diagnostics.push(Diagnostic::error(
                            "InvalidSchemaId",
                            format!(
                                "graph interface {} schema id must be a string",
                                direction.trim_end_matches('s')
                            ),
                            path,
                        ));
                        continue;
                    };
                    if let Err(error) = SchemaId::parse(schema_id) {
                        diagnostics.push(Diagnostic::error(
                            "InvalidSchemaId",
                            format!(
                                "graph interface {} schema id is invalid: {error}",
                                direction.trim_end_matches('s')
                            ),
                            path,
                        ));
                    }
                }
            }
        }
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

    let output_policy = spec
        .and_then(|spec| spec.get("outputPolicy"))
        .or_else(|| spec.and_then(|spec| spec.get("output_policy")))
        .and_then(Value::as_object);

    if let Some(delivery) = output_policy
        .and_then(|output_policy| output_policy.get("delivery"))
        .and_then(Value::as_object)
    {
        let mode = delivery.get("mode").and_then(Value::as_str);
        if let Some(mode) = delivery.get("mode")
            && !mode.as_str().is_some_and(|mode| {
                matches!(
                    mode,
                    "buffer_until_commit" | "bounded_holdback" | "immediate_draft"
                )
            })
        {
            diagnostics.push(Diagnostic::error(
                "InvalidOutputDeliveryMode",
                format!("invalid output delivery mode {mode}"),
                "$.spec.outputPolicy.delivery.mode",
            ));
        }

        if let Some(on_violation) = delivery
            .get("onViolation")
            .or_else(|| delivery.get("on_violation"))
            && !on_violation.as_str().is_some_and(|on_violation| {
                matches!(
                    on_violation,
                    "abort_response" | "abort_turn" | "redact" | "replace"
                )
            })
        {
            diagnostics.push(Diagnostic::error(
                "InvalidViolationAction",
                format!("invalid violation action {on_violation}"),
                "$.spec.outputPolicy.delivery.onViolation",
            ));
        }

        let delivered_draft_disposition = delivery
            .get("deliveredDraftDisposition")
            .map(|value| ("deliveredDraftDisposition", value))
            .or_else(|| {
                delivery
                    .get("delivered_draft_disposition")
                    .map(|value| ("delivered_draft_disposition", value))
            });
        if let Some((path_key, delivered_draft_disposition)) = delivered_draft_disposition
            && !delivered_draft_disposition
                .as_str()
                .is_some_and(|disposition| {
                    matches!(disposition, "keep" | "mark_incomplete" | "retract")
                })
        {
            diagnostics.push(Diagnostic::error(
                "InvalidDraftDisposition",
                format!("invalid draft disposition {delivered_draft_disposition}"),
                format!("$.spec.outputPolicy.delivery.{path_key}"),
            ));
        }

        let flush_boundaries = delivery
            .get("flushBoundaries")
            .map(|value| ("flushBoundaries", value))
            .or_else(|| {
                delivery
                    .get("flush_boundaries")
                    .map(|value| ("flush_boundaries", value))
            });
        if let Some((path_key, flush_boundaries)) = flush_boundaries {
            if let Some(flush_boundaries) = flush_boundaries.as_array() {
                for (boundary_index, boundary) in flush_boundaries.iter().enumerate() {
                    if !boundary.as_str().is_some_and(|boundary| {
                        matches!(
                            boundary,
                            "token"
                                | "sentence"
                                | "paragraph"
                                | "content_part"
                                | "tool_call"
                                | "response"
                        )
                    }) {
                        diagnostics.push(Diagnostic::error(
                            "InvalidFlushBoundary",
                            format!("invalid flush boundary {boundary}"),
                            format!("$.spec.outputPolicy.delivery.{path_key}[{boundary_index}]"),
                        ));
                    }
                }
            } else {
                diagnostics.push(Diagnostic::error(
                    "InvalidFlushBoundary",
                    "flush boundaries must be a list of strings",
                    format!("$.spec.outputPolicy.delivery.{path_key}"),
                ));
            }
        }

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
                    Value::String(duration) => {
                        let duration = duration.trim();
                        let mut valid_duration = false;
                        for unit in ["ms", "s", "m", "h"] {
                            if let Some(amount) = duration.strip_suffix(unit) {
                                valid_duration = !amount.is_empty()
                                    && amount.chars().all(|character| character.is_ascii_digit())
                                    && match amount.parse::<u64>() {
                                        Ok(value) => value > 0,
                                        Err(_) => false,
                                    };
                                break;
                            }
                        }
                        valid_duration
                    }
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

    if let Some(output_policy) = output_policy {
        let enforcement_points = output_policy
            .get("evaluation")
            .or_else(|| output_policy.get("outputEvaluation"))
            .or_else(|| output_policy.get("output_evaluation"))
            .and_then(Value::as_object)
            .and_then(|evaluation| {
                evaluation
                    .get("enforcementPoints")
                    .or_else(|| evaluation.get("enforcement_points"))
            })
            .and_then(Value::as_array);

        if let Some(enforcement_points) = enforcement_points {
            let mut on_generation_chunk_index = None;
            let mut before_client_delivery_index = None;
            let mut before_output_commit_index = None;

            for (index, enforcement_point) in enforcement_points.iter().enumerate() {
                match enforcement_point.as_str() {
                    Some("on_generation_chunk") => on_generation_chunk_index = Some(index),
                    Some("before_client_delivery") => before_client_delivery_index = Some(index),
                    Some("before_output_commit") => before_output_commit_index = Some(index),
                    _ => {}
                }
                if !enforcement_point.as_str().is_some_and(|enforcement_point| {
                    matches!(
                        enforcement_point,
                        "compile"
                            | "release"
                            | "admission"
                            | "before_node"
                            | "before_provider_call"
                            | "on_generation_chunk"
                            | "before_client_delivery"
                            | "before_output_commit"
                            | "on_usage_delta"
                            | "before_tool_or_effect"
                            | "before_commit"
                            | "before_publish"
                            | "on_resume"
                    )
                }) {
                    diagnostics.push(Diagnostic::error(
                        "InvalidOutputEnforcementPoint",
                        format!("invalid output policy enforcement point {enforcement_point}"),
                        format!("$.spec.outputPolicy.evaluation.enforcementPoints[{index}]"),
                    ));
                }
            }

            if before_client_delivery_index.is_none() {
                diagnostics.push(Diagnostic::error(
                    "OutputPolicyBypass",
                    "output policy enforcement must include the before_client_delivery gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                ));
            } else if on_generation_chunk_index.is_none() {
                diagnostics.push(Diagnostic::error(
                    "OutputPolicyBypass",
                    "output policy enforcement must include the on_generation_chunk gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                ));
            } else if before_output_commit_index.is_none() {
                diagnostics.push(Diagnostic::error(
                    "OutputPolicyBypass",
                    "output policy enforcement must include the before_output_commit gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                ));
            }

            if let (Some(before_client_delivery_index), Some(on_generation_chunk_index)) =
                (before_client_delivery_index, on_generation_chunk_index)
                && before_client_delivery_index < on_generation_chunk_index
            {
                diagnostics.push(Diagnostic::error(
                    "PolicyGateAfterDelivery",
                    "on_generation_chunk policy evaluation must precede before_client_delivery",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                ));
            }
        } else {
            diagnostics.push(Diagnostic::error(
                "OutputPolicyBypass",
                "output policy enforcement must include the before_client_delivery gate",
                "$.spec.outputPolicy.evaluation.enforcementPoints",
            ));
        }

        if let Some(on_violation) = output_policy
            .get("onViolation")
            .or_else(|| output_policy.get("on_violation"))
            .and_then(Value::as_object)
        {
            let disposition = on_violation
                .get("disposition")
                .and_then(Value::as_str)
                .unwrap_or("abort_response");
            let valid_disposition = on_violation.get("disposition").is_none_or(|disposition| {
                disposition.as_str().is_some_and(|disposition| {
                    matches!(
                        disposition,
                        "allow"
                            | "hold"
                            | "redact"
                            | "replace"
                            | "abort_response"
                            | "abort_turn"
                            | "deny_commit"
                    )
                })
            });
            if !valid_disposition {
                let invalid_disposition = on_violation
                    .get("disposition")
                    .map(Value::to_string)
                    .unwrap_or_default();
                diagnostics.push(Diagnostic::error(
                    "InvalidOutputDisposition",
                    format!("invalid output disposition {invalid_disposition}"),
                    "$.spec.outputPolicy.onViolation.disposition",
                ));
            }

            if let Some(provider_cancellation) = on_violation
                .get("providerCancellation")
                .or_else(|| on_violation.get("provider_cancellation"))
            {
                if let Some(provider_cancellation) = provider_cancellation.as_object() {
                    if let Some(mode) = provider_cancellation.get("mode")
                        && !mode.as_str().is_some_and(|mode| {
                            matches!(mode, "none" | "request" | "required_if_supported")
                        })
                    {
                        diagnostics.push(Diagnostic::error(
                            "InvalidProviderCancellation",
                            format!("invalid provider cancellation {mode}"),
                            "$.spec.outputPolicy.onViolation.providerCancellation.mode",
                        ));
                    }
                } else if !provider_cancellation
                    .as_str()
                    .is_some_and(|provider_cancellation| {
                        matches!(
                            provider_cancellation,
                            "none" | "request" | "required_if_supported"
                        )
                    })
                {
                    diagnostics.push(Diagnostic::error(
                        "InvalidProviderCancellation",
                        format!("invalid provider cancellation {provider_cancellation}"),
                        "$.spec.outputPolicy.onViolation.providerCancellation",
                    ));
                }
            }

            let pending_tool_calls_disposition_value = on_violation
                .get("pendingToolCalls")
                .or_else(|| on_violation.get("pending_tool_calls"))
                .and_then(Value::as_object)
                .and_then(|pending_tool_calls| pending_tool_calls.get("disposition"));
            let valid_pending_tool_calls_disposition = pending_tool_calls_disposition_value
                .is_none_or(|disposition| {
                    disposition.as_str().is_some_and(|disposition| {
                        matches!(disposition, "keep" | "deny" | "cancel_admitted")
                    })
                });
            if !valid_pending_tool_calls_disposition {
                let invalid_disposition = pending_tool_calls_disposition_value
                    .map(Value::to_string)
                    .unwrap_or_default();
                diagnostics.push(Diagnostic::error(
                    "InvalidPendingToolCallsDisposition",
                    format!("invalid pending tool calls disposition {invalid_disposition}"),
                    "$.spec.outputPolicy.onViolation.pendingToolCalls.disposition",
                ));
            }

            let delivered_draft_disposition_value = on_violation
                .get("deliveredDraft")
                .or_else(|| on_violation.get("delivered_draft"))
                .and_then(Value::as_object)
                .and_then(|delivered_draft| delivered_draft.get("disposition"));
            if let Some(delivered_draft_disposition) = delivered_draft_disposition_value
                && !delivered_draft_disposition
                    .as_str()
                    .is_some_and(|disposition| {
                        matches!(disposition, "keep" | "mark_incomplete" | "retract")
                    })
            {
                diagnostics.push(Diagnostic::error(
                    "InvalidDraftDisposition",
                    format!("invalid draft disposition {delivered_draft_disposition}"),
                    "$.spec.outputPolicy.onViolation.deliveredDraft.disposition",
                ));
            }

            let durable_result_disposition_value = on_violation
                .get("durableResult")
                .or_else(|| on_violation.get("durable_result"))
                .and_then(Value::as_object)
                .and_then(|durable_result| durable_result.get("disposition"));
            let valid_durable_result_disposition =
                durable_result_disposition_value.is_none_or(|disposition| {
                    disposition.as_str().is_some_and(|disposition| {
                        matches!(disposition, "none" | "incomplete" | "partial")
                    })
                });
            if !valid_durable_result_disposition {
                let invalid_disposition = durable_result_disposition_value
                    .map(Value::to_string)
                    .unwrap_or_default();
                diagnostics.push(Diagnostic::error(
                    "InvalidOutputDurableResult",
                    format!("invalid output durable result {invalid_disposition}"),
                    "$.spec.outputPolicy.onViolation.durableResult.disposition",
                ));
            }

            if valid_disposition && matches!(disposition, "abort_response" | "abort_turn") {
                let pending_tool_calls_disposition = on_violation
                    .get("pendingToolCalls")
                    .or_else(|| on_violation.get("pending_tool_calls"))
                    .and_then(Value::as_object)
                    .and_then(|pending_tool_calls| pending_tool_calls.get("disposition"))
                    .and_then(Value::as_str)
                    .unwrap_or("deny");
                if valid_pending_tool_calls_disposition && pending_tool_calls_disposition == "keep"
                {
                    diagnostics.push(Diagnostic::error(
                        "PendingToolCallAfterAbort",
                        "policy-aborted responses must deny or cancel pending tool calls",
                        "$.spec.outputPolicy.onViolation.pendingToolCalls.disposition",
                    ));
                }

                let durable_result_disposition = on_violation
                    .get("durableResult")
                    .or_else(|| on_violation.get("durable_result"))
                    .and_then(Value::as_object)
                    .and_then(|durable_result| durable_result.get("disposition"))
                    .and_then(Value::as_str)
                    .unwrap_or("none");
                if valid_durable_result_disposition && durable_result_disposition != "none" {
                    diagnostics.push(Diagnostic::error(
                        "CommitAfterPolicyStop",
                        "policy-stopped responses must not commit a durable result",
                        "$.spec.outputPolicy.onViolation.durableResult.disposition",
                    ));
                }
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
            let mut valid_effects = BTreeSet::new();
            match tool.get("effects") {
                Some(Value::String(effect)) => {
                    if matches!(
                        effect.as_str(),
                        "none"
                            | "external_read"
                            | "external_write"
                            | "filesystem_read"
                            | "filesystem_write"
                            | "process"
                            | "network"
                            | "destructive"
                    ) {
                        valid_effects.insert(effect.as_str());
                    } else {
                        diagnostics.push(Diagnostic::error(
                            "InvalidToolEffect",
                            format!("invalid tool effect {effect}"),
                            format!("$.spec.bindings.tools.{tool_key}.effects"),
                        ));
                    }
                }
                Some(Value::Array(effects)) => {
                    for (effect_index, effect) in effects.iter().enumerate() {
                        if let Some(effect) = effect.as_str()
                            && matches!(
                                effect,
                                "none"
                                    | "external_read"
                                    | "external_write"
                                    | "filesystem_read"
                                    | "filesystem_write"
                                    | "process"
                                    | "network"
                                    | "destructive"
                            )
                        {
                            valid_effects.insert(effect);
                            continue;
                        }
                        diagnostics.push(Diagnostic::error(
                            "InvalidToolEffect",
                            format!("invalid tool effect {effect}"),
                            format!("$.spec.bindings.tools.{tool_key}.effects[{effect_index}]"),
                        ));
                    }
                }
                Some(_) => {
                    diagnostics.push(Diagnostic::error(
                        "InvalidToolEffect",
                        "tool effects must be a string or list of strings",
                        format!("$.spec.bindings.tools.{tool_key}.effects"),
                    ));
                }
                None => {}
            };
            if valid_effects.contains("none") && valid_effects.len() > 1 {
                diagnostics.push(Diagnostic::error(
                    "InvalidToolEffect",
                    "tool effect none cannot be combined with other effects",
                    format!("$.spec.bindings.tools.{tool_key}.effects"),
                ));
            }
            let state_changing_tool = valid_effects.iter().any(|effect| {
                matches!(
                    *effect,
                    "external_write" | "filesystem_write" | "process" | "destructive"
                )
            });
            has_state_changing_tool |= state_changing_tool;
            if let Some(approval) = tool.get("approval") {
                if let Some(approval) = approval.as_object() {
                    let mode_value = approval.get("mode");
                    let mode = mode_value.and_then(Value::as_str).unwrap_or("policy");
                    let valid_mode = mode_value.is_none_or(|mode| {
                        mode.as_str()
                            .is_some_and(|mode| matches!(mode, "never" | "policy" | "always"))
                    });
                    if !valid_mode {
                        let invalid_mode = mode_value
                            .and_then(Value::as_str)
                            .map(str::to_owned)
                            .unwrap_or_else(|| {
                                mode_value.map(Value::to_string).unwrap_or_default()
                            });
                        diagnostics.push(Diagnostic::error(
                            "InvalidToolApproval",
                            format!("invalid tool approval {invalid_mode}"),
                            format!("$.spec.bindings.tools.{tool_key}.approval.mode"),
                        ));
                    }
                    let binds_arguments_digest = approval
                        .get("bindArgumentsDigest")
                        .or_else(|| approval.get("bind_arguments_digest"))
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                        || approval
                            .get("argumentsDigest")
                            .or_else(|| approval.get("arguments_digest"))
                            .or_else(|| approval.get("argumentsDigestRef"))
                            .or_else(|| approval.get("arguments_digest_ref"))
                            .and_then(Value::as_str)
                            .is_some_and(|arguments_digest| !arguments_digest.trim().is_empty());
                    if valid_mode && matches!(mode, "policy" | "always") && !binds_arguments_digest
                    {
                        diagnostics.push(Diagnostic::error(
                            "ApprovalWithoutArgumentDigest",
                            "explicit tool approval must be bound to immutable argument digest",
                            format!("$.spec.bindings.tools.{tool_key}.approval"),
                        ));
                    }
                } else if let Some(approval) = approval.as_str() {
                    if !matches!(approval, "never" | "policy" | "always") {
                        diagnostics.push(Diagnostic::error(
                            "InvalidToolApproval",
                            format!("invalid tool approval {approval}"),
                            format!("$.spec.bindings.tools.{tool_key}.approval"),
                        ));
                    } else if approval == "always" {
                        diagnostics.push(Diagnostic::error(
                            "ApprovalWithoutArgumentDigest",
                            "explicit tool approval must be bound to immutable argument digest",
                            format!("$.spec.bindings.tools.{tool_key}.approval"),
                        ));
                    }
                } else {
                    diagnostics.push(Diagnostic::error(
                        "InvalidToolApproval",
                        format!("invalid tool approval {approval}"),
                        format!("$.spec.bindings.tools.{tool_key}.approval"),
                    ));
                }
            }
            if let Some(idempotency) = tool.get("idempotency")
                && !idempotency.as_str().is_some_and(|idempotency| {
                    matches!(idempotency, "not_applicable" | "optional" | "required")
                })
            {
                diagnostics.push(Diagnostic::error(
                    "InvalidToolIdempotency",
                    format!("invalid tool idempotency {idempotency}"),
                    format!("$.spec.bindings.tools.{tool_key}.idempotency"),
                ));
            }
            if let Some(cancellation) = tool.get("cancellation")
                && !cancellation.as_str().is_some_and(|cancellation| {
                    matches!(
                        cancellation,
                        "unsupported" | "cooperative" | "force_terminable"
                    )
                })
            {
                diagnostics.push(Diagnostic::error(
                    "InvalidToolCancellation",
                    format!("invalid tool cancellation {cancellation}"),
                    format!("$.spec.bindings.tools.{tool_key}.cancellation"),
                ));
            }
            let result_mode = tool
                .get("resultMode")
                .map(|result_mode| ("resultMode", result_mode))
                .or_else(|| {
                    tool.get("result_mode")
                        .map(|result_mode| ("result_mode", result_mode))
                });
            if let Some((result_mode_key, result_mode)) = result_mode
                && !result_mode.as_str().is_some_and(|result_mode| {
                    matches!(
                        result_mode,
                        "value" | "incremental" | "bounded_sequence" | "artifact_reference"
                    )
                })
            {
                diagnostics.push(Diagnostic::error(
                    "InvalidToolResultMode",
                    format!("invalid tool result mode {result_mode}"),
                    format!("$.spec.bindings.tools.{tool_key}.{result_mode_key}"),
                ));
            }
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
                for definition_field in ["name", "description"] {
                    if definition
                        .get(definition_field)
                        .and_then(Value::as_str)
                        .is_none_or(|value| value.trim().is_empty())
                    {
                        diagnostics.push(Diagnostic::error(
                            "InvalidToolDefinition",
                            format!(
                                "tool definition {definition_field} must be a non-empty string"
                            ),
                            format!(
                                "$.spec.bindings.tools.{tool_key}.definition.{definition_field}"
                            ),
                        ));
                    }
                }
                if let Some(version) = definition.get("version")
                    && version.as_str().is_none_or(|value| value.trim().is_empty())
                {
                    diagnostics.push(Diagnostic::error(
                        "InvalidToolDefinition",
                        "tool definition version must be a non-empty string",
                        format!("$.spec.bindings.tools.{tool_key}.definition.version"),
                    ));
                }
                if let Some(tags) = definition.get("tags") {
                    if let Some(tags) = tags.as_array() {
                        for (tag_index, tag) in tags.iter().enumerate() {
                            if tag.as_str().is_none_or(|value| value.trim().is_empty()) {
                                diagnostics.push(Diagnostic::error(
                                    "InvalidToolDefinition",
                                    "tool definition tags must be non-empty strings",
                                    format!(
                                        "$.spec.bindings.tools.{tool_key}.definition.tags[{tag_index}]"
                                    ),
                                ));
                            }
                        }
                    } else {
                        diagnostics.push(Diagnostic::error(
                            "InvalidToolDefinition",
                            "tool definition tags must be a list of non-empty strings",
                            format!("$.spec.bindings.tools.{tool_key}.definition.tags"),
                        ));
                    }
                }
                for forbidden_field in FORBIDDEN_TOOL_DEFINITION_FIELDS {
                    if definition.contains_key(forbidden_field) {
                        diagnostics.push(Diagnostic::error(
                            "InvalidToolDefinition",
                            format!(
                                "tool definition must not contain execution detail {forbidden_field}"
                            ),
                            format!(
                                "$.spec.bindings.tools.{tool_key}.definition.{forbidden_field}"
                            ),
                        ));
                    }
                }
                let input_schema = definition
                    .get("inputSchema")
                    .or_else(|| definition.get("input_schema"))
                    .and_then(Value::as_str);
                if input_schema.is_none_or(|schema| schema.trim().is_empty()) {
                    diagnostics.push(Diagnostic::error(
                        "ToolSchemaMissing",
                        "model-visible tool definitions require an input schema",
                        format!("$.spec.bindings.tools.{tool_key}.definition.inputSchema"),
                    ));
                } else if let Some(input_schema) = input_schema
                    && let Err(error) = SchemaId::parse(input_schema)
                {
                    diagnostics.push(Diagnostic::error(
                        "InvalidSchemaId",
                        format!("tool input schema id is invalid: {error}"),
                        format!("$.spec.bindings.tools.{tool_key}.definition.inputSchema"),
                    ));
                }

                if let Some(output_schema) = definition
                    .get("outputSchema")
                    .or_else(|| definition.get("output_schema"))
                    .and_then(Value::as_str)
                    .filter(|schema| !schema.trim().is_empty())
                    && let Err(error) = SchemaId::parse(output_schema)
                {
                    diagnostics.push(Diagnostic::error(
                        "InvalidSchemaId",
                        format!("tool output schema id is invalid: {error}"),
                        format!("$.spec.bindings.tools.{tool_key}.definition.outputSchema"),
                    ));
                }
            } else {
                diagnostics.push(Diagnostic::error(
                    "ToolSchemaMissing",
                    "model-visible tool definitions require an input schema",
                    format!("$.spec.bindings.tools.{tool_key}.definition.inputSchema"),
                ));
            }
            if let Some(implementation) = tool.get("implementation").and_then(Value::as_object) {
                let implementation_kind = implementation.get("kind").and_then(Value::as_str);
                let missing_implementation_field = match implementation_kind {
                    Some("block") => implementation
                        .get("block")
                        .and_then(Value::as_str)
                        .is_none_or(|value| value.trim().is_empty())
                        .then_some("block"),
                    Some("graph") => implementation
                        .get("graph")
                        .and_then(Value::as_str)
                        .is_none_or(|value| value.trim().is_empty())
                        .then_some("graph"),
                    Some("remote") => {
                        if implementation
                            .get("connection")
                            .and_then(Value::as_str)
                            .is_none_or(|value| value.trim().is_empty())
                        {
                            Some("connection")
                        } else if implementation
                            .get("operation")
                            .and_then(Value::as_str)
                            .is_none_or(|value| value.trim().is_empty())
                        {
                            Some("operation")
                        } else {
                            None
                        }
                    }
                    Some("mcp") => {
                        if implementation
                            .get("server")
                            .and_then(Value::as_str)
                            .is_none_or(|value| value.trim().is_empty())
                        {
                            Some("server")
                        } else if implementation
                            .get("remoteName")
                            .or_else(|| implementation.get("remote_name"))
                            .and_then(Value::as_str)
                            .is_none_or(|value| value.trim().is_empty())
                        {
                            Some("remoteName")
                        } else {
                            None
                        }
                    }
                    Some("openapi") => {
                        if implementation
                            .get("connection")
                            .and_then(Value::as_str)
                            .is_none_or(|value| value.trim().is_empty())
                        {
                            Some("connection")
                        } else if implementation
                            .get("operationId")
                            .or_else(|| implementation.get("operation_id"))
                            .and_then(Value::as_str)
                            .is_none_or(|value| value.trim().is_empty())
                        {
                            Some("operationId")
                        } else {
                            None
                        }
                    }
                    _ => {
                        diagnostics.push(Diagnostic::error(
                            "ToolBindingMissing",
                            "tool implementation kind must be one of block, graph, remote, mcp, or openapi",
                            format!("$.spec.bindings.tools.{tool_key}.implementation.kind"),
                        ));
                        None
                    }
                };
                if let Some(missing_implementation_field) = missing_implementation_field {
                    diagnostics.push(Diagnostic::error(
                        "ToolBindingMissing",
                        format!(
                            "{} tool implementation requires {missing_implementation_field}",
                            implementation_kind.unwrap_or("unknown")
                        ),
                        format!(
                            "$.spec.bindings.tools.{tool_key}.implementation.{missing_implementation_field}"
                        ),
                    ));
                }
            } else {
                diagnostics.push(Diagnostic::error(
                    "ToolBindingMissing",
                    "model-visible tools require an executable binding implementation",
                    format!("$.spec.bindings.tools.{tool_key}.implementation"),
                ));
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

    if let Some(remote_payloads) = spec
        .and_then(|spec| {
            spec.get("remotePayloads")
                .or_else(|| spec.get("remote_payloads"))
        })
        .and_then(Value::as_array)
    {
        let max_inline_bytes = spec
            .and_then(|spec| {
                spec.get("remotePayloadLimits")
                    .or_else(|| spec.get("remote_payload_limits"))
            })
            .and_then(Value::as_object)
            .and_then(|limits| {
                limits
                    .get("maxInlineBytes")
                    .or_else(|| limits.get("max_inline_bytes"))
            })
            .and_then(Value::as_u64)
            .unwrap_or(64 * 1024) as usize;

        for (index, payload) in remote_payloads.iter().enumerate() {
            let Some(payload) = payload.as_object() else {
                continue;
            };
            let mode = payload.get("mode").and_then(Value::as_str);
            if mode == Some("inline") {
                let value = payload.get("value").unwrap_or(&Value::Null);
                if let Ok(encoded) = serde_json::to_vec(value)
                    && encoded.len() > max_inline_bytes
                {
                    diagnostics.push(Diagnostic::error(
                        "RemoteInlinePayloadTooLarge",
                        format!(
                            "remote inline payload is {} bytes, exceeding maxInlineBytes {}",
                            encoded.len(),
                            max_inline_bytes
                        ),
                        format!("$.spec.remotePayloads[{index}].value"),
                    ));
                }
            }
        }
    }

    let normalized = normalize_graph(document);
    let normalized_nodes = normalized
        .get("spec")
        .and_then(|spec| spec.get("nodes"))
        .and_then(Value::as_object);
    let mut produced_nodes = BTreeSet::<String>::new();
    let mut consumed_nodes = BTreeSet::<String>::new();
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
                } else if key == "from" {
                    produced_nodes.insert(owner.to_owned());
                } else {
                    consumed_nodes.insert(owner.to_owned());
                }
            }
        }
    }

    if let Some(normalized_nodes) = normalized_nodes
        && !block_catalog.descriptors.is_empty()
    {
        let mut inbound_by_node = normalized_nodes
            .keys()
            .map(|node_name| (node_name.as_str(), BTreeSet::<String>::new()))
            .collect::<BTreeMap<_, _>>();
        let mut invalid_input_port_nodes = BTreeSet::<String>::new();
        if let Some(edges) = normalized
            .get("spec")
            .and_then(|spec| spec.get("edges"))
            .and_then(Value::as_array)
        {
            for (index, edge) in edges.iter().enumerate() {
                let Some(source) = edge.get("from").and_then(Value::as_str) else {
                    continue;
                };
                let Some(target) = edge.get("to").and_then(Value::as_str) else {
                    continue;
                };
                let source_port = source.split_once('.').map(|(source_owner, source_path)| {
                    let port_name = source_path
                        .split_once('.')
                        .map_or(source_path, |(port_name, _)| port_name);
                    (source_owner, port_name)
                });
                let target_port = target.split_once('.').map(|(target_owner, target_path)| {
                    let port_name = target_path
                        .split_once('.')
                        .map_or(target_path, |(port_name, _)| port_name);
                    (target_owner, port_name)
                });

                let mut source_type = None;
                let mut target_type = None;
                let mut source_required = None;
                let mut target_required = None;
                if let Some((source_owner, port_name)) = source_port
                    && !PSEUDO_NODES.contains(&source_owner)
                    && let Some(source_node) = normalized_nodes.get(source_owner)
                    && let Some(descriptor) = source_node
                        .as_object()
                        .and_then(|node| node.get("block"))
                        .and_then(Value::as_str)
                        .and_then(|block_id| block_catalog.get(block_id))
                    && !descriptor.outputs.is_empty()
                {
                    if let Some(port) = descriptor
                        .outputs
                        .iter()
                        .find(|port| port.name == port_name)
                    {
                        source_type = port.type_ref.as_deref();
                        source_required = Some(port.required);
                    } else {
                        diagnostics.push(Diagnostic::error(
                            "GB1014",
                            format!(
                                "block {} has no output port {:?}",
                                descriptor.block_id(),
                                port_name
                            ),
                            format!("$.spec.edges[{index}].from"),
                        ));
                    }
                }

                let Some((target_owner, target_path)) = target.split_once('.') else {
                    continue;
                };
                let port_name = target_path
                    .split_once('.')
                    .map_or(target_path, |(port_name, _)| port_name);
                if let Some(inbound_ports) = inbound_by_node.get_mut(target_owner) {
                    inbound_ports.insert(port_name.to_owned());
                }
                if let Some((target_owner, port_name)) = target_port
                    && !PSEUDO_NODES.contains(&target_owner)
                    && let Some(target_node) = normalized_nodes.get(target_owner)
                    && let Some(descriptor) = target_node
                        .as_object()
                        .and_then(|node| node.get("block"))
                        .and_then(Value::as_str)
                        .and_then(|block_id| block_catalog.get(block_id))
                    && !descriptor.inputs.is_empty()
                {
                    if let Some(port) = descriptor.inputs.iter().find(|port| port.name == port_name)
                    {
                        target_type = port.type_ref.as_deref();
                        target_required = Some(port.required);
                    } else {
                        invalid_input_port_nodes.insert(target_owner.to_owned());
                        diagnostics.push(Diagnostic::error(
                            "GB1013",
                            format!(
                                "block {} has no input port {:?}",
                                descriptor.block_id(),
                                port_name
                            ),
                            format!("$.spec.edges[{index}].to"),
                        ));
                    }
                }

                if let (Some(source_type), Some(target_type)) = (source_type, target_type)
                    && source_type != "Any"
                    && target_type != "Any"
                    && source_type != target_type
                {
                    diagnostics.push(Diagnostic::error(
                        "GB1018",
                        format!("port type mismatch: {source_type} cannot feed {target_type}"),
                        format!("$.spec.edges[{index}]"),
                    ));
                }

                if source_required == Some(false) && target_required == Some(true) {
                    diagnostics.push(Diagnostic::error(
                        "GB1015",
                        "optional branch output cannot feed required input",
                        format!("$.spec.edges[{index}]"),
                    ));
                }
            }
        }

        for (node_name, node) in normalized_nodes {
            let Some(node) = node.as_object() else {
                continue;
            };
            let Some(block_id) = node.get("block").and_then(Value::as_str) else {
                continue;
            };
            let Some(descriptor) = block_catalog.get(block_id) else {
                continue;
            };
            if !descriptor.resource_slots.is_empty() {
                let bindings = node.get("bindings");
                let mut bindings_object = bindings.and_then(Value::as_object);
                let mut invalid_resource_binding_node = false;
                if bindings.is_some() && bindings_object.is_none() {
                    diagnostics.push(Diagnostic::error(
                        "GB1017",
                        "node bindings must be a mapping",
                        format!("$.spec.nodes.{node_name}.bindings"),
                    ));
                    bindings_object = None;
                }
                let slot_names = descriptor
                    .resource_slots
                    .iter()
                    .map(|slot| slot.name.as_str())
                    .collect::<BTreeSet<_>>();
                if let Some(bindings_object) = bindings_object {
                    for binding_name in bindings_object.keys() {
                        if !slot_names.contains(binding_name.as_str()) {
                            invalid_resource_binding_node = true;
                            diagnostics.push(Diagnostic::error(
                                "GB1017",
                                format!(
                                    "block {} has no resource slot {:?}",
                                    descriptor.block_id(),
                                    binding_name
                                ),
                                format!("$.spec.nodes.{node_name}.bindings.{binding_name}"),
                            ));
                        }
                    }
                }
                for slot in &descriptor.resource_slots {
                    if !invalid_resource_binding_node
                        && !slot.optional
                        && !bindings_object
                            .is_some_and(|bindings| bindings.contains_key(slot.name.as_str()))
                    {
                        diagnostics.push(Diagnostic::error(
                            "GB1016",
                            format!(
                                "required resource slot {:?} is not bound for node {:?}",
                                slot.name, node_name
                            ),
                            format!("$.spec.nodes.{node_name}.bindings"),
                        ));
                    }
                }
            }
            if invalid_input_port_nodes.contains(node_name.as_str()) {
                continue;
            }
            let produced_inputs = inbound_by_node.get(node_name.as_str());
            for port in &descriptor.inputs {
                if port.required
                    && !produced_inputs.is_some_and(|inputs| inputs.contains(port.name.as_str()))
                {
                    diagnostics.push(Diagnostic::error(
                        "GB1003",
                        format!(
                            "required input {:?} is never produced for node {:?}",
                            port.name, node_name
                        ),
                        format!("$.spec.nodes.{node_name}"),
                    ));
                }
            }
        }
    }

    if let Some(normalized_nodes) = normalized_nodes {
        for (node_name, node) in normalized_nodes {
            if let Some(owner) = node
                .as_object()
                .and_then(|node| node.get("when"))
                .and_then(Value::as_str)
                .map(|when| when.split_once('.').map_or(when, |(owner, _)| owner))
            {
                if PSEUDO_NODES.contains(&owner) {
                    continue;
                }
                if !normalized_nodes.contains_key(owner) {
                    diagnostics.push(Diagnostic::error(
                        "GB1002",
                        format!("when references unknown node {owner:?}"),
                        format!("$.spec.nodes.{node_name}.when"),
                    ));
                } else {
                    produced_nodes.insert(owner.to_owned());
                    consumed_nodes.insert(node_name.to_owned());
                }
            }
        }

        let interface_outputs = normalized
            .get("spec")
            .and_then(|spec| spec.get("interface"))
            .and_then(|interface| interface.get("outputs"))
            .and_then(Value::as_object);
        let has_declared_output = interface_outputs.is_some_and(|outputs| !outputs.is_empty());
        let output_edges = normalized
            .get("spec")
            .and_then(|spec| spec.get("edges"))
            .and_then(Value::as_array)
            .map(|edges| {
                edges
                    .iter()
                    .filter(|edge| {
                        edge.get("to")
                            .and_then(Value::as_str)
                            .is_some_and(|target| target.starts_with("$output."))
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if has_declared_output && output_edges.is_empty() {
            diagnostics.push(Diagnostic::warning(
                "GB1003",
                "graph declares outputs but no edge writes to $output",
                "$.spec.interface.outputs",
            ));
        }

        if !output_edges.is_empty() {
            let mut reachable = BTreeSet::<String>::new();
            let mut stack = output_edges
                .iter()
                .filter_map(|edge| edge.get("from").and_then(Value::as_str))
                .map(|source| {
                    source
                        .split_once('.')
                        .map_or(source, |(owner, _)| owner)
                        .to_owned()
                })
                .collect::<Vec<_>>();
            let mut reverse_edges = BTreeMap::<String, Vec<String>>::new();
            if let Some(edges) = normalized
                .get("spec")
                .and_then(|spec| spec.get("edges"))
                .and_then(Value::as_array)
            {
                for edge in edges {
                    let Some(source) = edge.get("from").and_then(Value::as_str) else {
                        continue;
                    };
                    let Some(target) = edge.get("to").and_then(Value::as_str) else {
                        continue;
                    };
                    let source_owner = source.split_once('.').map_or(source, |(owner, _)| owner);
                    let target_owner = target.split_once('.').map_or(target, |(owner, _)| owner);
                    reverse_edges
                        .entry(target_owner.to_owned())
                        .or_default()
                        .push(source_owner.to_owned());
                }
            }
            while let Some(owner) = stack.pop() {
                if reachable.contains(owner.as_str()) || PSEUDO_NODES.contains(&owner.as_str()) {
                    continue;
                }
                reachable.insert(owner.clone());
                if let Some(upstream) = reverse_edges.get(owner.as_str()) {
                    stack.extend(upstream.iter().cloned());
                }
            }
            for node_name in normalized_nodes.keys() {
                if !reachable.contains(node_name.as_str())
                    && !produced_nodes.contains(node_name.as_str())
                    && !consumed_nodes.contains(node_name.as_str())
                {
                    diagnostics.push(Diagnostic::warning(
                        "GB1001",
                        format!("node {node_name:?} is not connected"),
                        format!("$.spec.nodes.{node_name}"),
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
