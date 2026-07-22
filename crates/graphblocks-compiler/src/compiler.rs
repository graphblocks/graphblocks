use std::collections::{BTreeMap, BTreeSet};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use graphblocks_schema::{
    ResourceSchemaViolation, SchemaId, parse_duration_milliseconds, parse_duration_seconds,
    resource_depth_violation, resource_schema_errors,
};
use serde_json::{Map, Value};

use crate::canonical::canonical_hash;
use crate::diagnostics::{Diagnostic, Severity};
use crate::graph::{GRAPH_API_VERSION, PSEUDO_NODES, migrate_graph, normalize_graph};

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
    allow_unknown_blocks: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlockDescriptor {
    pub type_id: String,
    pub version: u64,
    pub config_schema: Value,
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
    required_when: Option<OutputRequirednessPredicate>,
}

impl PortDescriptor {
    pub fn required_for(&self, config: &Value, phase: ExecutionPhase) -> bool {
        self.required
            || self
                .required_when
                .as_ref()
                .is_some_and(|predicate| predicate.evaluate(config, phase))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExecutionPhase {
    Initial,
    Resumed,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum OutputRequirednessPredicate {
    ConfigEquals { pointer: String, expected: Value },
    Phase(ExecutionPhase),
    All(Vec<Self>),
    Any(Vec<Self>),
    Not(Box<Self>),
}

impl OutputRequirednessPredicate {
    fn evaluate(&self, config: &Value, phase: ExecutionPhase) -> bool {
        match self {
            Self::ConfigEquals { pointer, expected } => {
                resolve_json_pointer(config, pointer) == Some(expected)
            }
            Self::Phase(expected) => *expected == phase,
            Self::All(predicates) => predicates
                .iter()
                .all(|predicate| predicate.evaluate(config, phase)),
            Self::Any(predicates) => predicates
                .iter()
                .any(|predicate| predicate.evaluate(config, phase)),
            Self::Not(predicate) => !predicate.evaluate(config, phase),
        }
    }
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
const DEFAULT_CALLBACK_MAX_PAYLOAD_BYTES: u64 = 262_144;
pub const MAX_NODE_RETRY_ATTEMPTS: u64 = 100;
const MANDATORY_CALLBACK_FAILURE_POLICIES: [&str; 2] =
    ["pause_run_on_failure", "fail_run_on_failure"];
const ORDER_CAPABLE_CALLBACK_TARGETS: [&str; 3] = ["webhook", "websocket", "sse"];
const PRIMITIVE_TYPE_REFS: [&str; 7] = [
    "Any", "Boolean", "Bytes", "Integer", "Number", "Null", "String",
];
const MAX_TYPE_REF_DEPTH: usize = 32;

/// Returns whether a JSON integer is greater than an unsigned bound, including
/// arbitrary-precision integer tokens that do not fit in `u64`.
pub fn json_integer_exceeds_u64(value: &Value, maximum: u64) -> bool {
    let Some(number) = value.as_number() else {
        return false;
    };
    if let Some(value) = number.as_u64() {
        return value > maximum;
    }

    let token = number.to_string();
    if token.starts_with('-') || !token.bytes().all(|byte| byte.is_ascii_digit()) {
        return false;
    }
    let digits = token.trim_start_matches('0');
    let maximum = maximum.to_string();
    digits.len() > maximum.len() || (digits.len() == maximum.len() && digits > maximum.as_str())
}

fn validate_port_type_ref(type_ref: &str) -> Result<(), String> {
    validate_port_type_ref_at_depth(type_ref, 0)
}

fn validate_port_type_ref_at_depth(type_ref: &str, nesting_depth: usize) -> Result<(), String> {
    if type_ref.is_empty()
        || type_ref.trim() != type_ref
        || type_ref.chars().any(char::is_whitespace)
    {
        return Err("type reference must be non-empty and contain no whitespace".to_owned());
    }
    if PRIMITIVE_TYPE_REFS.contains(&type_ref) {
        return Ok(());
    }
    if !type_ref.contains(['<', '>']) {
        return SchemaId::parse(type_ref)
            .map(|_| ())
            .map_err(|error| error.to_string());
    }

    let Some(opening) = type_ref.find('<') else {
        return Err(format!("invalid type reference {type_ref:?}"));
    };
    if opening == 0 || !type_ref.ends_with('>') {
        return Err(format!("invalid type reference {type_ref:?}"));
    }
    let constructor = &type_ref[..opening];
    let expected_arity = match constructor {
        "List" | "Optional" => 1,
        "Map" => 2,
        _ => return Err(format!("unsupported type constructor {constructor:?}")),
    };
    if nesting_depth >= MAX_TYPE_REF_DEPTH {
        return Err(format!(
            "type reference nesting must not exceed {MAX_TYPE_REF_DEPTH} constructor levels"
        ));
    }
    let body = &type_ref[opening + 1..type_ref.len() - 1];
    let mut arguments = Vec::new();
    let mut depth = 0_i64;
    let mut start = 0;
    for (offset, character) in body.char_indices() {
        match character {
            '<' => depth += 1,
            '>' => {
                depth -= 1;
                if depth < 0 {
                    return Err(format!("invalid type reference {type_ref:?}"));
                }
            }
            ',' if depth == 0 => {
                arguments.push(&body[start..offset]);
                start = offset + 1;
            }
            _ => {}
        }
    }
    if depth != 0 {
        return Err(format!("invalid type reference {type_ref:?}"));
    }
    arguments.push(&body[start..]);
    if arguments.len() != expected_arity || arguments.iter().any(|argument| argument.is_empty()) {
        return Err(format!("invalid type reference {type_ref:?}"));
    }
    for argument in arguments {
        validate_port_type_ref_at_depth(argument, nesting_depth + 1)?;
    }
    Ok(())
}

fn validate_resource_type_ref(type_ref: &str) -> Result<(), String> {
    if type_ref.is_empty()
        || type_ref.trim() != type_ref
        || type_ref.chars().any(char::is_whitespace)
    {
        return Err(
            "resource type reference must be non-empty and contain no whitespace".to_owned(),
        );
    }
    if type_ref.contains(['@', '/']) {
        return SchemaId::parse(type_ref)
            .map(|_| ())
            .map_err(|error| error.to_string());
    }

    let segments = type_ref.split('.').collect::<Vec<_>>();
    let valid_segment = |segment: &str| {
        let mut characters = segment.chars();
        characters
            .next()
            .is_some_and(|character| character.is_ascii_alphabetic())
            && characters.all(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '_' | '-')
            })
    };
    if segments.len() < 2 || !segments.into_iter().all(valid_segment) {
        return Err(
            "opaque resource type reference must use dot-separated identifier segments".to_owned(),
        );
    }
    Ok(())
}

fn descriptor_bool(
    owner: &Map<String, Value>,
    field_name: &str,
    default: bool,
) -> Result<bool, String> {
    match owner.get(field_name) {
        None => Ok(default),
        Some(value) => value
            .as_bool()
            .ok_or_else(|| format!("{field_name} must be a boolean")),
    }
}

fn descriptor_type_ref(
    owner: &Map<String, Value>,
    context: &str,
    validator: fn(&str) -> Result<(), String>,
) -> Result<Option<String>, String> {
    match owner.get("type") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(type_ref)) => {
            validator(type_ref)
                .map_err(|error| format!("{context} has invalid type {type_ref}: {error}"))?;
            Ok(Some(type_ref.clone()))
        }
        Some(type_ref) => Err(format!(
            "{context} has invalid type {type_ref}: type reference must be a string"
        )),
    }
}

const MAX_OUTPUT_REQUIREDNESS_DEPTH: usize = 16;
const MAX_OUTPUT_REQUIREDNESS_OPERANDS: usize = 16;

fn validate_json_pointer(pointer: &str) -> Result<(), String> {
    if pointer.chars().count() > 512 {
        return Err("configEquals.pointer must contain at most 512 characters".to_owned());
    }
    if !pointer.is_empty() && !pointer.starts_with('/') {
        return Err("configEquals.pointer must be empty or start with '/'".to_owned());
    }
    for token in pointer.split('/').skip(1) {
        let bytes = token.as_bytes();
        let mut index = 0;
        while index < bytes.len() {
            if bytes[index] != b'~' {
                index += 1;
                continue;
            }
            if index + 1 >= bytes.len() || !matches!(bytes[index + 1], b'0' | b'1') {
                return Err(
                    "configEquals.pointer contains an invalid JSON Pointer escape".to_owned(),
                );
            }
            index += 2;
        }
    }
    Ok(())
}

fn validate_config_schema(schema: &Value) -> Result<(), String> {
    if !schema.is_object() {
        return Err("configSchema must be an object".to_owned());
    }
    jsonschema::draft202012::meta::validate(schema)
        .map_err(|error| format!("configSchema is not valid JSON Schema Draft 2020-12: {error}"))?;

    let mut pending = vec![schema];
    while let Some(value) = pending.pop() {
        match value {
            Value::Object(object) => {
                for (key, child) in object {
                    if matches!(key.as_str(), "$ref" | "$dynamicRef")
                        && child
                            .as_str()
                            .is_some_and(|reference| !reference.starts_with('#'))
                    {
                        return Err(format!(
                            "configSchema external reference {child} is not allowed"
                        ));
                    }
                    pending.push(child);
                }
            }
            Value::Array(array) => pending.extend(array),
            _ => {}
        }
    }

    jsonschema::draft202012::new(schema)
        .map(|_| ())
        .map_err(|error| format!("configSchema could not be compiled: {error}"))
}

fn validate_endpoint_identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes.next().is_some_and(|byte| byte.is_ascii_alphabetic())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn parse_output_requiredness(
    value: &Value,
    depth: usize,
) -> Result<OutputRequirednessPredicate, String> {
    if depth >= MAX_OUTPUT_REQUIREDNESS_DEPTH {
        return Err(format!(
            "requiredWhen nesting must not exceed {MAX_OUTPUT_REQUIREDNESS_DEPTH} levels"
        ));
    }
    let object = value
        .as_object()
        .ok_or_else(|| "requiredWhen must be an object".to_owned())?;
    if object.len() != 1 {
        return Err("requiredWhen must contain exactly one predicate operator".to_owned());
    }
    let (operator, operand) = object.iter().next().expect("one predicate operator");
    match operator.as_str() {
        "configEquals" => {
            let operand = operand
                .as_object()
                .ok_or_else(|| "configEquals must be an object".to_owned())?;
            if operand.len() != 2
                || !operand.contains_key("pointer")
                || !operand.contains_key("value")
            {
                return Err("configEquals must contain exactly pointer and value".to_owned());
            }
            let pointer = operand
                .get("pointer")
                .and_then(Value::as_str)
                .ok_or_else(|| "configEquals.pointer must be a string".to_owned())?;
            validate_json_pointer(pointer)?;
            Ok(OutputRequirednessPredicate::ConfigEquals {
                pointer: pointer.to_owned(),
                expected: operand["value"].clone(),
            })
        }
        "phase" => match operand.as_str() {
            Some("initial") => Ok(OutputRequirednessPredicate::Phase(ExecutionPhase::Initial)),
            Some("resumed") => Ok(OutputRequirednessPredicate::Phase(ExecutionPhase::Resumed)),
            _ => Err("phase must be initial or resumed".to_owned()),
        },
        "all" | "any" => {
            let operands = operand
                .as_array()
                .filter(|operands| !operands.is_empty())
                .ok_or_else(|| format!("{operator} must be a non-empty array"))?;
            if operands.len() > MAX_OUTPUT_REQUIREDNESS_OPERANDS {
                return Err(format!(
                    "{operator} must contain at most {MAX_OUTPUT_REQUIREDNESS_OPERANDS} predicates"
                ));
            }
            let parsed = operands
                .iter()
                .map(|operand| parse_output_requiredness(operand, depth + 1))
                .collect::<Result<Vec<_>, _>>()?;
            if operator == "all" {
                Ok(OutputRequirednessPredicate::All(parsed))
            } else {
                Ok(OutputRequirednessPredicate::Any(parsed))
            }
        }
        "not" => Ok(OutputRequirednessPredicate::Not(Box::new(
            parse_output_requiredness(operand, depth + 1)?,
        ))),
        _ => Err(format!(
            "requiredWhen uses unsupported operator {operator:?}"
        )),
    }
}

fn resolve_json_pointer<'a>(document: &'a Value, pointer: &str) -> Option<&'a Value> {
    if pointer.is_empty() {
        return Some(document);
    }
    let mut current = document;
    for encoded_token in pointer.split('/').skip(1) {
        let token = encoded_token.replace("~1", "/").replace("~0", "~");
        current = match current {
            Value::Object(object) => object.get(&token)?,
            Value::Array(array) => {
                if token.is_empty()
                    || !token.bytes().all(|byte| byte.is_ascii_digit())
                    || (token.len() > 1 && token.starts_with('0'))
                {
                    return None;
                }
                array.get(token.parse::<usize>().ok()?)?
            }
            _ => return None,
        };
    }
    Some(current)
}

impl BlockCatalog {
    pub fn with_unknown_blocks_allowed(mut self) -> Self {
        self.allow_unknown_blocks = true;
        self
    }

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
            if type_id.is_empty() {
                return Err(format!("block catalog entry {index} is missing typeId"));
            }
            let mut version = block
                .get("version")
                .map(|version| parse_block_catalog_version(version, index))
                .transpose()?;
            if version.is_none()
                && let Some((parsed_type_id, parsed_version)) = type_id.rsplit_once('@')
            {
                version = Some(parse_block_catalog_version_string(parsed_version, index)?);
                type_id = parsed_type_id.to_owned();
            }
            let version =
                version.ok_or_else(|| format!("block catalog entry {index} is missing version"))?;
            if version == 0 {
                return Err(format!("block catalog entry {index} is missing version"));
            }

            let mut inputs = Vec::new();
            let mut input_names = BTreeSet::new();
            let raw_inputs = match block.get("inputs") {
                None => &[][..],
                Some(Value::Array(inputs)) => inputs.as_slice(),
                Some(_) => {
                    return Err(format!(
                        "block catalog entry {index} inputs must be an array"
                    ));
                }
            };
            for (port_index, port) in raw_inputs.iter().enumerate() {
                let port = port.as_object().ok_or_else(|| {
                    format!("block catalog entry {index} input {port_index} must be an object")
                })?;
                let name = port
                    .get("name")
                    .and_then(Value::as_str)
                    .filter(|name| !name.trim().is_empty())
                    .ok_or_else(|| {
                        format!(
                            "block catalog entry {index} input {port_index} requires a non-empty name"
                        )
                    })?;
                if !validate_endpoint_identifier(name) {
                    return Err(format!(
                        "block catalog entry {index} input {port_index} requires a canonical endpoint name"
                    ));
                }
                if !input_names.insert(name) {
                    return Err(format!(
                        "block catalog entry {index} has duplicate input {name:?}"
                    ));
                }
                if port.contains_key("requiredWhen") {
                    return Err(format!(
                        "block catalog entry {index} input {name} must not declare requiredWhen"
                    ));
                }
                let context = format!("block catalog entry {index} input {name}");
                inputs.push(PortDescriptor {
                    name: name.to_owned(),
                    type_ref: descriptor_type_ref(port, &context, validate_port_type_ref)?,
                    required: descriptor_bool(port, "required", true)
                        .map_err(|error| format!("{context} {error}"))?,
                    required_when: None,
                });
            }

            let mut outputs = Vec::new();
            let mut output_names = BTreeSet::new();
            let raw_outputs = match block.get("outputs") {
                None => &[][..],
                Some(Value::Array(outputs)) => outputs.as_slice(),
                Some(_) => {
                    return Err(format!(
                        "block catalog entry {index} outputs must be an array"
                    ));
                }
            };
            for (port_index, port) in raw_outputs.iter().enumerate() {
                let port = port.as_object().ok_or_else(|| {
                    format!("block catalog entry {index} output {port_index} must be an object")
                })?;
                let name = port
                    .get("name")
                    .and_then(Value::as_str)
                    .filter(|name| !name.trim().is_empty())
                    .ok_or_else(|| {
                        format!(
                            "block catalog entry {index} output {port_index} requires a non-empty name"
                        )
                    })?;
                if !validate_endpoint_identifier(name) {
                    return Err(format!(
                        "block catalog entry {index} output {port_index} requires a canonical endpoint name"
                    ));
                }
                if !output_names.insert(name) {
                    return Err(format!(
                        "block catalog entry {index} has duplicate output {name:?}"
                    ));
                }
                let context = format!("block catalog entry {index} output {name}");
                let required_when = port
                    .get("requiredWhen")
                    .map(|value| parse_output_requiredness(value, 0))
                    .transpose()
                    .map_err(|error| format!("{context} has invalid requiredWhen: {error}"))?;
                outputs.push(PortDescriptor {
                    name: name.to_owned(),
                    type_ref: descriptor_type_ref(port, &context, validate_port_type_ref)?,
                    required: descriptor_bool(port, "required", true)
                        .map_err(|error| format!("{context} {error}"))?,
                    required_when,
                });
            }

            let resource_slots = match block.get("resourceSlots") {
                None => Vec::new(),
                Some(Value::Array(slots)) => {
                    let mut descriptors = Vec::new();
                    let mut names = BTreeSet::new();
                    for (slot_index, slot) in slots.iter().enumerate() {
                        let slot = slot.as_object().ok_or_else(|| {
                            format!(
                                "block catalog entry {index} resource slot {slot_index} must be an object"
                            )
                        })?;
                        let name = slot
                            .get("name")
                            .and_then(Value::as_str)
                            .filter(|name| !name.trim().is_empty())
                            .ok_or_else(|| {
                                format!(
                                    "block catalog entry {index} resource slot {slot_index} requires a non-empty name"
                                )
                            })?;
                        if !names.insert(name) {
                            return Err(format!(
                                "block catalog entry {index} has duplicate resource slot {name:?}"
                            ));
                        }
                        let context = format!("block catalog entry {index} resource slot {name}");
                        descriptors.push(ResourceSlotDescriptor {
                            name: name.to_owned(),
                            type_ref: descriptor_type_ref(
                                slot,
                                &context,
                                validate_resource_type_ref,
                            )?,
                            optional: descriptor_bool(slot, "optional", false)
                                .map_err(|error| format!("{context} {error}"))?,
                        });
                    }
                    descriptors
                }
                Some(Value::Object(slots)) => {
                    let mut descriptors = Vec::new();
                    for (slot_name, slot) in slots {
                        if slot_name.trim().is_empty() {
                            return Err(format!(
                                "block catalog entry {index} resource slot requires a non-empty name"
                            ));
                        }
                        let slot = slot.as_object().ok_or_else(|| {
                            format!(
                                "block catalog entry {index} resource slot {slot_name:?} must be an object"
                            )
                        })?;
                        let context =
                            format!("block catalog entry {index} resource slot {slot_name}");
                        descriptors.push(ResourceSlotDescriptor {
                            name: slot_name.to_owned(),
                            type_ref: descriptor_type_ref(
                                slot,
                                &context,
                                validate_resource_type_ref,
                            )?,
                            optional: descriptor_bool(slot, "optional", false)
                                .map_err(|error| format!("{context} {error}"))?,
                        });
                    }
                    descriptors
                }
                Some(_) => {
                    return Err(format!(
                        "block catalog entry {index} resourceSlots must be an array or object"
                    ));
                }
            };
            let config_schema = block
                .get("configSchema")
                .cloned()
                .unwrap_or_else(|| serde_json::json!({"type": "object"}));
            validate_config_schema(&config_schema)
                .map_err(|error| format!("block catalog entry {index} {error}"))?;
            let descriptor = BlockDescriptor {
                type_id,
                version,
                config_schema,
                inputs,
                outputs,
                resource_slots,
            };
            let block_id = descriptor.block_id();
            if descriptors.contains_key(&block_id) {
                return Err(format!("duplicate block catalog descriptor {block_id}"));
            }
            descriptors.insert(block_id, descriptor);
        }

        Ok(Self {
            descriptors,
            allow_unknown_blocks: false,
        })
    }

    pub fn get(&self, block_id: &str) -> Option<&BlockDescriptor> {
        self.descriptors.get(block_id)
    }
}

fn parse_block_catalog_version(version: &Value, index: usize) -> Result<u64, String> {
    match version {
        Value::Number(version) => version.as_u64().ok_or_else(|| {
            format!(
                "block catalog entry {index} version must be a positive integer no greater than {}",
                u64::MAX
            )
        }),
        Value::String(version) => parse_block_catalog_version_string(version, index),
        _ => Err(format!(
            "block catalog entry {index} version must be a positive integer or canonical decimal string"
        )),
    }
}

fn parse_block_catalog_version_string(version: &str, index: usize) -> Result<u64, String> {
    if version.is_empty()
        || version.starts_with('0')
        || !version.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(format!(
            "block catalog entry {index} version must be a canonical positive decimal string"
        ));
    }
    version.parse::<u64>().map_err(|_| {
        format!(
            "block catalog entry {index} version exceeds the maximum supported value {}",
            u64::MAX
        )
    })
}

fn positive_integer(value: Option<&Value>) -> Option<u64> {
    value.and_then(Value::as_u64).filter(|value| *value > 0)
}

fn has_non_empty_string(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_str)
        .is_some_and(|value| !value.trim().is_empty())
}

fn truthy_flag(config: &Map<String, Value>, names: &[&str]) -> bool {
    names
        .iter()
        .any(|name| config.get(*name).and_then(Value::as_bool) == Some(true))
}

fn duration_milliseconds(value: Option<&Value>) -> Option<u64> {
    value.and_then(parse_duration_milliseconds)
}

fn has_async_relative_timeout(config: &Map<String, Value>) -> bool {
    let timeout = config
        .get("timeout")
        .or_else(|| config.get("timeoutMs"))
        .or_else(|| config.get("timeout_ms"))
        .or_else(|| config.get("deadline"));
    duration_milliseconds(timeout).is_some_and(|timeout_ms| timeout_ms > 0)
}

fn has_async_absolute_deadline(config: &Map<String, Value>) -> bool {
    positive_integer(
        config
            .get("expiresAtUnixMs")
            .or_else(|| config.get("expires_at_unix_ms")),
    )
    .is_some()
}

fn has_async_explicit_infinite_wait(config: &Map<String, Value>) -> bool {
    config
        .get("infiniteWait")
        .or_else(|| config.get("infinite_wait"))
        .and_then(Value::as_bool)
        == Some(true)
        || has_non_empty_string(
            config
                .get("infiniteWaitPolicy")
                .or_else(|| config.get("infinite_wait_policy")),
        )
}

fn invalid_optional_duration_field<'a>(
    config: &'a Map<String, Value>,
    names: &[&'a str],
) -> Option<&'a str> {
    names
        .iter()
        .find(|name| {
            config
                .get(**name)
                .is_some_and(|value| duration_milliseconds(Some(value)).is_none())
        })
        .copied()
}

fn has_async_idempotency_key(config: &Map<String, Value>) -> bool {
    has_non_empty_string(
        config
            .get("idempotencyKey")
            .or_else(|| config.get("idempotency_key")),
    )
}

fn callback_config(config: &Map<String, Value>) -> Option<&Map<String, Value>> {
    config.get("callback").and_then(Value::as_object)
}

fn callback_schema_required(config: &Map<String, Value>) -> bool {
    match callback_config(config) {
        Some(callback) => callback.get("required").and_then(Value::as_bool) != Some(false),
        None => {
            config.contains_key("callback")
                || config.contains_key("callbackSchema")
                || config.contains_key("callback_schema")
        }
    }
}

fn has_async_callback_schema(config: &Map<String, Value>) -> bool {
    if let Some(callback) = callback_config(config)
        && has_non_empty_string(
            callback
                .get("schema")
                .or_else(|| callback.get("acceptedSchema"))
                .or_else(|| callback.get("accepted_schema"))
                .or_else(|| callback.get("expectedSchema"))
                .or_else(|| callback.get("expected_schema")),
        )
    {
        return true;
    }
    has_non_empty_string(
        config
            .get("callbackSchema")
            .or_else(|| config.get("callback_schema")),
    )
}

fn has_async_callback_completion_ref(config: &Map<String, Value>) -> bool {
    callback_config(config).is_some()
        || has_non_empty_string(
            config
                .get("callbackRef")
                .or_else(|| config.get("callback_ref")),
        )
}

fn has_async_polling_completion_ref(config: &Map<String, Value>) -> bool {
    config.get("polling").and_then(Value::as_object).is_some()
        || has_non_empty_string(
            config
                .get("pollingRef")
                .or_else(|| config.get("polling_ref")),
        )
}

fn configured_positive_integer(config: &Map<String, Value>, names: &[&str]) -> Option<u64> {
    names
        .iter()
        .find_map(|name| positive_integer(config.get(*name)))
}

fn has_async_resume_reevaluation(config: &Map<String, Value>) -> bool {
    let resume = config.get("resume").and_then(Value::as_object);
    let policy_ok = resume.is_some_and(|resume| {
        truthy_flag(
            resume,
            &[
                "requirePolicyReevaluation",
                "require_policy_reevaluation",
                "policyReevaluation",
                "policy_reevaluation",
            ],
        )
    }) || truthy_flag(
        config,
        &["requirePolicyReevaluation", "require_policy_reevaluation"],
    );
    let budget_ok = resume.is_some_and(|resume| {
        truthy_flag(
            resume,
            &[
                "requireBudgetReservation",
                "require_budget_reservation",
                "budgetReservation",
                "budget_reservation",
            ],
        )
    }) || truthy_flag(
        config,
        &["requireBudgetReservation", "require_budget_reservation"],
    );
    let release_ok = resume.is_some_and(|resume| {
        truthy_flag(
            resume,
            &[
                "requireReleaseCompatibility",
                "require_release_compatibility",
                "releaseCompatibility",
                "release_compatibility",
            ],
        )
    }) || truthy_flag(
        config,
        &[
            "requireReleaseCompatibility",
            "require_release_compatibility",
        ],
    );
    policy_ok && budget_ok && release_ok
}

fn has_async_attempt_fencing(config: &Map<String, Value>) -> bool {
    truthy_flag(
        config,
        &[
            "attemptFencing",
            "attempt_fencing",
            "fencingTokenRequired",
            "fencing_token_required",
        ],
    ) || callback_config(config).is_some_and(|callback| {
        truthy_flag(
            callback,
            &[
                "attemptFencing",
                "attempt_fencing",
                "fencingTokenRequired",
                "fencing_token_required",
            ],
        )
    })
}

fn has_async_ownership_fence(config: &Map<String, Value>) -> bool {
    truthy_flag(
        config,
        &[
            "ownershipFence",
            "ownership_fence",
            "runOwnershipLease",
            "run_ownership_lease",
        ],
    ) || config
        .get("resume")
        .and_then(Value::as_object)
        .is_some_and(|resume| {
            truthy_flag(
                resume,
                &[
                    "requireOwnershipFence",
                    "require_ownership_fence",
                    "ownershipFence",
                    "ownership_fence",
                    "runOwnershipLease",
                    "run_ownership_lease",
                ],
            )
        })
}

fn diagnose_async_operation_config(
    diagnostics: &mut Vec<Diagnostic>,
    config: &Map<String, Value>,
    path: &str,
    require_callback_schema: bool,
) {
    if has_async_callback_completion_ref(config) && has_async_polling_completion_ref(config) {
        diagnostics.push(Diagnostic::error(
            "GB1026",
            "async operation must not define both callback and polling completion refs",
            path,
        ));
    }
    let has_relative_timeout = has_async_relative_timeout(config);
    let has_absolute_deadline = has_async_absolute_deadline(config);
    let has_bounded_timeout = has_relative_timeout || has_absolute_deadline;
    let has_infinite_wait = has_async_explicit_infinite_wait(config);
    if has_relative_timeout && has_absolute_deadline {
        diagnostics.push(Diagnostic::error(
            "GB1026",
            "async operation wait must not define both expiresAtUnixMs and timeout",
            path,
        ));
    }
    if has_bounded_timeout && has_infinite_wait {
        diagnostics.push(Diagnostic::error(
            "GB1026",
            "async operation wait must not define both timeout and infinite-wait policy",
            path,
        ));
    }
    if !has_bounded_timeout && !has_infinite_wait {
        diagnostics.push(Diagnostic::error(
            "GB6001",
            "async operation callback waits require a timeout or explicit infinite-wait policy",
            path,
        ));
    }
    if config
        .get("onTimeout")
        .or_else(|| config.get("on_timeout"))
        .and_then(Value::as_str)
        .is_some_and(|on_timeout| !matches!(on_timeout, "fail" | "cancel" | "expire"))
    {
        diagnostics.push(Diagnostic::error(
            "GB1026",
            "async await onTimeout must be one of fail, cancel, or expire",
            format!("{path}.onTimeout"),
        ));
    }
    for (field, names) in [
        (
            "interval",
            ["interval", "intervalMs", "interval_ms"].as_slice(),
        ),
        (
            "maxInterval",
            [
                "maxInterval",
                "max_interval",
                "maxIntervalMs",
                "max_interval_ms",
            ]
            .as_slice(),
        ),
    ] {
        if invalid_optional_duration_field(config, names).is_some() {
            diagnostics.push(Diagnostic::error(
                "GB1026",
                format!("async operation {field} must be a positive duration"),
                format!("{path}.{field}"),
            ));
        }
    }
    if !has_async_idempotency_key(config) {
        diagnostics.push(Diagnostic::error(
            "GB6003",
            "async operation callbacks require an idempotency key",
            path,
        ));
    }
    if (require_callback_schema || callback_schema_required(config))
        && !has_async_callback_schema(config)
    {
        diagnostics.push(Diagnostic::error(
            "GB6007",
            "async operation callbacks require an expected callback schema",
            format!("{path}.callback"),
        ));
    }
    let expected_payload_bytes = callback_config(config)
        .and_then(|callback| {
            configured_positive_integer(
                callback,
                &[
                    "expectedPayloadBytes",
                    "expected_payload_bytes",
                    "expectedMaxPayloadBytes",
                    "expected_max_payload_bytes",
                ],
            )
        })
        .or_else(|| {
            configured_positive_integer(
                config,
                &[
                    "expectedPayloadBytes",
                    "expected_payload_bytes",
                    "expectedMaxPayloadBytes",
                    "expected_max_payload_bytes",
                ],
            )
        });
    let max_payload_bytes = callback_config(config)
        .and_then(|callback| {
            configured_positive_integer(callback, &["maxPayloadBytes", "max_payload_bytes"])
        })
        .or_else(|| configured_positive_integer(config, &["maxPayloadBytes", "max_payload_bytes"]))
        .unwrap_or(DEFAULT_CALLBACK_MAX_PAYLOAD_BYTES);
    if expected_payload_bytes.is_some_and(|expected| expected > max_payload_bytes) {
        diagnostics.push(Diagnostic::error(
            "GB6010",
            "async callback payload contract exceeds the configured inline payload limit",
            format!("{path}.callback.maxPayloadBytes"),
        ));
    }
    if !has_async_resume_reevaluation(config) {
        diagnostics.push(Diagnostic::error(
            "GB6008",
            "callback resume must re-evaluate policy, budget, and release compatibility",
            format!("{path}.resume"),
        ));
    }
    if !has_async_attempt_fencing(config) {
        diagnostics.push(Diagnostic::error(
            "GB6015",
            "async callbacks require attempt fencing so stale callbacks cannot resume newer attempts",
            path,
        ));
    }
    if !has_async_ownership_fence(config) {
        diagnostics.push(Diagnostic::error(
            "GB6016",
            "callback resume requires run ownership lease or fencing protection",
            format!("{path}.resume"),
        ));
    }
}

fn is_background_run(execution: &Map<String, Value>) -> bool {
    execution
        .get("runLifetime")
        .or_else(|| execution.get("run_lifetime"))
        .or_else(|| execution.get("lifetime"))
        .or_else(|| execution.get("invocationMode"))
        .or_else(|| execution.get("invocation_mode"))
        .or_else(|| execution.get("responseMode"))
        .or_else(|| execution.get("response_mode"))
        .and_then(Value::as_str)
        .is_some_and(|mode| matches!(mode, "accepted" | "background" | "job"))
}

fn execution_is_client_bound(execution: &Map<String, Value>) -> bool {
    if truthy_flag(
        execution,
        &[
            "clientConnectionRequired",
            "client_connection_required",
            "websocketRequired",
            "websocket_required",
            "processBound",
            "process_bound",
        ],
    ) {
        return true;
    }
    execution
        .get("detach")
        .and_then(Value::as_object)
        .and_then(|detach| {
            detach
                .get("onClientDisconnect")
                .or_else(|| detach.get("on_client_disconnect"))
        })
        .and_then(Value::as_str)
        .is_some_and(|behavior| matches!(behavior, "cancel" | "cancel_run" | "client_connection"))
}

fn event_stream_is_replayable(event_stream: Option<&Map<String, Value>>) -> bool {
    event_stream.is_some_and(|event_stream| {
        truthy_flag(
            event_stream,
            &[
                "replayable",
                "cursorReplay",
                "cursor_replay",
                "authoritative",
            ],
        )
    })
}

fn diagnose_background_execution_config(
    diagnostics: &mut Vec<Diagnostic>,
    execution: &Map<String, Value>,
    event_stream: Option<&Map<String, Value>>,
) {
    if !is_background_run(execution) {
        return;
    }
    if !event_stream_is_replayable(event_stream) {
        diagnostics.push(Diagnostic::error(
            "GB6005",
            "background runs require a replayable ApplicationEventStream",
            "$.spec.eventStream",
        ));
    }
    if execution_is_client_bound(execution) {
        diagnostics.push(Diagnostic::error(
            "GB6009",
            "background or job runs must not be bound to a single client connection",
            "$.spec.execution",
        ));
    }
    let Some(event_stream) = event_stream else {
        return;
    };
    let retention = duration_milliseconds(
        event_stream
            .get("retention")
            .or_else(|| event_stream.get("eventRetention"))
            .or_else(|| event_stream.get("event_retention"))
            .or_else(|| event_stream.get("retentionDuration"))
            .or_else(|| event_stream.get("retention_duration")),
    );
    let replay_guarantee = duration_milliseconds(
        event_stream
            .get("reconnectReplayGuarantee")
            .or_else(|| event_stream.get("reconnect_replay_guarantee"))
            .or_else(|| event_stream.get("replayGuarantee"))
            .or_else(|| event_stream.get("replay_guarantee")),
    );
    if let (Some(retention), Some(replay_guarantee)) = (retention, replay_guarantee)
        && retention < replay_guarantee
    {
        diagnostics.push(Diagnostic::error(
            "GB6013",
            "event retention is shorter than the declared reconnect replay guarantee",
            "$.spec.eventStream.retention",
        ));
    }
}

fn has_callback_signing(delivery: &Map<String, Value>) -> bool {
    let Some(signing) = delivery.get("signing").and_then(Value::as_object) else {
        return false;
    };
    signing
        .get("algorithm")
        .and_then(Value::as_str)
        .is_some_and(|algorithm| matches!(algorithm, "hmac-sha256" | "ed25519"))
        && has_non_empty_string(
            signing
                .get("secretRef")
                .or_else(|| signing.get("secret_ref")),
        )
}

fn callback_url_is_unsafe(url: Option<&Value>) -> bool {
    let Some(url) = url.and_then(Value::as_str) else {
        return true;
    };
    if url.trim() != url {
        return true;
    }
    let Some(rest) = url.strip_prefix("https://") else {
        return true;
    };
    if rest.contains('#') || rest.bytes().any(|byte| byte <= 32 || byte == 127) {
        return true;
    }
    let authority = rest.split(&['/', '?'][..]).next().unwrap_or_default();
    if authority.is_empty() || authority.contains('@') {
        return true;
    }
    let host = if let Some(rest) = authority.strip_prefix('[') {
        let Some((host, suffix)) = rest.split_once(']') else {
            return true;
        };
        if host.contains('%') || host.parse::<Ipv6Addr>().is_err() {
            return true;
        }
        if !suffix.is_empty() {
            let Some(port) = suffix.strip_prefix(':') else {
                return true;
            };
            if port.is_empty() || port.parse::<u16>().is_err() {
                return true;
            }
        }
        host.trim_end_matches('.').to_ascii_lowercase()
    } else {
        let host = if let Some((host, port)) = authority.split_once(':') {
            if port.is_empty() || port.parse::<u16>().is_err() {
                return true;
            }
            host
        } else {
            authority
        };
        let host = host.trim_end_matches('.').to_ascii_lowercase();
        if host.contains(':')
            || !host
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.'))
            || host
                .split('.')
                .any(|label| label.is_empty() || label.starts_with('-') || label.ends_with('-'))
        {
            return true;
        }
        host
    };
    if host.is_empty()
        || host == "localhost"
        || host.ends_with(".localhost")
        || host == "metadata.google.internal"
    {
        return true;
    }
    let forbidden_ipv4 = |address: Ipv4Addr| {
        let octets = address.octets();
        address.is_loopback()
            || address.is_private()
            || address.is_link_local()
            || address.is_multicast()
            || address.is_unspecified()
            || octets[0] == 0
            || octets[0] >= 224
            || (octets[0] == 100 && (64..=127).contains(&octets[1]))
    };
    let Ok(address) = host.parse::<IpAddr>() else {
        let numeric_ipv4 = if let Some(hex) = host.strip_prefix("0x") {
            u32::from_str_radix(hex, 16).ok()
        } else if host.bytes().all(|byte| byte.is_ascii_digit()) {
            host.parse::<u32>().ok()
        } else {
            None
        };
        if numeric_ipv4.map(Ipv4Addr::from).is_some_and(forbidden_ipv4) {
            return true;
        }
        // Browsers, URL clients, and libc resolvers commonly accept inet_aton-style
        // hexadecimal, octal, and shortened IPv4 forms. Rust's canonical IP parser
        // intentionally does not, so reject such numeric-looking hosts instead of
        // allowing the downstream resolver to reinterpret them after validation.
        let looks_like_legacy_ipv4 = {
            let components = host.split('.').collect::<Vec<_>>();
            !components.is_empty()
                && components.len() <= 4
                && components.iter().all(|component| {
                    if component.is_empty() {
                        return false;
                    }
                    let digits = component
                        .strip_prefix("0x")
                        .or_else(|| component.strip_prefix("0X"));
                    digits.map_or_else(
                        || component.bytes().all(|byte| byte.is_ascii_digit()),
                        |digits| {
                            !digits.is_empty()
                                && digits.bytes().all(|byte| byte.is_ascii_hexdigit())
                        },
                    )
                })
        };
        return looks_like_legacy_ipv4;
    };
    match address {
        IpAddr::V4(address) => forbidden_ipv4(address),
        IpAddr::V6(address) => {
            if let Some(mapped_address) = address.to_ipv4_mapped() {
                return forbidden_ipv4(mapped_address);
            }

            let segments = address.segments();
            if segments[..6].iter().all(|segment| *segment == 0) {
                let compatible_address = Ipv4Addr::new(
                    (segments[6] >> 8) as u8,
                    segments[6] as u8,
                    (segments[7] >> 8) as u8,
                    segments[7] as u8,
                );
                return forbidden_ipv4(compatible_address);
            }

            address.is_loopback()
                || address.is_unique_local()
                || address.is_unicast_link_local()
                || address.is_multicast()
                || address.is_unspecified()
        }
    }
}

fn has_callback_dead_letter_behavior(
    config: &Map<String, Value>,
    delivery: &Map<String, Value>,
) -> bool {
    config
        .get("failurePolicy")
        .or_else(|| config.get("failure_policy"))
        .and_then(Value::as_str)
        == Some("retry_then_dead_letter")
        || has_non_empty_string(
            config
                .get("deadLetterPolicy")
                .or_else(|| config.get("dead_letter_policy"))
                .or_else(|| config.get("deadLetterRef"))
                .or_else(|| config.get("dead_letter_ref"))
                .or_else(|| delivery.get("deadLetterPolicy"))
                .or_else(|| delivery.get("dead_letter_policy"))
                .or_else(|| delivery.get("deadLetterRef"))
                .or_else(|| delivery.get("dead_letter_ref")),
        )
        || config
            .get("deadLetterPolicy")
            .or_else(|| config.get("dead_letter_policy"))
            .or_else(|| delivery.get("deadLetterPolicy"))
            .or_else(|| delivery.get("dead_letter_policy"))
            .is_some_and(Value::is_object)
        || has_non_empty_string(
            config
                .get("fallbackPolicy")
                .or_else(|| config.get("fallback_policy"))
                .or_else(|| config.get("fallbackRef"))
                .or_else(|| config.get("fallback_ref"))
                .or_else(|| delivery.get("fallbackPolicy"))
                .or_else(|| delivery.get("fallback_policy"))
                .or_else(|| delivery.get("fallbackRef"))
                .or_else(|| delivery.get("fallback_ref")),
        )
        || config
            .get("fallbackPolicy")
            .or_else(|| config.get("fallback_policy"))
            .or_else(|| delivery.get("fallbackPolicy"))
            .or_else(|| delivery.get("fallback_policy"))
            .is_some_and(Value::is_object)
}

fn diagnose_callback_subscription_config(
    diagnostics: &mut Vec<Diagnostic>,
    config: &Map<String, Value>,
    path: &str,
) {
    let Some(delivery) = config.get("delivery").and_then(Value::as_object) else {
        return;
    };
    let delivery_kind = delivery.get("kind").and_then(Value::as_str);
    if delivery_kind == Some("webhook") {
        if !has_callback_signing(delivery) {
            diagnostics.push(Diagnostic::error(
                "GB6002",
                "webhook callback subscriptions require signing configuration",
                format!("{path}.delivery.signing"),
            ));
        }
        if callback_url_is_unsafe(delivery.get("url")) {
            diagnostics.push(Diagnostic::error(
                "GB6011",
                "webhook callback endpoint is unsafe or forbidden by default egress policy",
                format!("{path}.delivery.url"),
            ));
        }
    }
    if config.get("sourceOfTruth").and_then(Value::as_bool) == Some(true)
        || config.get("source_of_truth").and_then(Value::as_bool) == Some(true)
        || config
            .get("authoritativeFor")
            .or_else(|| config.get("authoritative_for"))
            .is_some()
    {
        diagnostics.push(Diagnostic::error(
            "GB6004",
            "callback delivery must not be used as the source of truth for run correctness or accounting",
            path,
        ));
    }

    let failure_policy = config
        .get("failurePolicy")
        .or_else(|| config.get("failure_policy"))
        .and_then(Value::as_str);
    let mandatory = config.get("mandatory").and_then(Value::as_bool) == Some(true)
        || delivery.get("mandatory").and_then(Value::as_bool) == Some(true)
        || failure_policy.is_some_and(|failure_policy| {
            MANDATORY_CALLBACK_FAILURE_POLICIES.contains(&failure_policy)
        });
    if mandatory && failure_policy.is_none() {
        diagnostics.push(Diagnostic::error(
            "GB6006",
            "mandatory callback delivery requires retry, dead-letter, or fallback failure policy",
            format!("{path}.failurePolicy"),
        ));
    }

    let ordering = delivery
        .get("ordering")
        .and_then(Value::as_object)
        .or_else(|| config.get("ordering").and_then(Value::as_object));
    if ordering
        .and_then(|ordering| ordering.get("mode"))
        .and_then(Value::as_str)
        == Some("ordered")
        && !delivery_kind.is_some_and(|kind| ORDER_CAPABLE_CALLBACK_TARGETS.contains(&kind))
    {
        diagnostics.push(Diagnostic::error(
            "GB6012",
            "callback subscription requests ordered delivery on a target that cannot guarantee it",
            format!("{path}.delivery.ordering"),
        ));
    }

    if failure_policy
        .is_some_and(|failure_policy| MANDATORY_CALLBACK_FAILURE_POLICIES.contains(&failure_policy))
        && !has_callback_dead_letter_behavior(config, delivery)
    {
        diagnostics.push(Diagnostic::error(
            "GB6014",
            "mandatory callback failure policy requires dead-letter or fallback behavior",
            format!("{path}.deadLetterPolicy"),
        ));
    }
}

pub fn compile_graph(document: &Value) -> Plan {
    compile_graph_with_catalog(document, &BlockCatalog::default())
}

pub fn compile_graph_for_discovery(document: &Value) -> Plan {
    compile_graph_with_catalog(
        document,
        &BlockCatalog::default().with_unknown_blocks_allowed(),
    )
}

pub fn compile_graph_with_catalog(document: &Value, block_catalog: &BlockCatalog) -> Plan {
    if let Some(violation) = resource_depth_violation(document) {
        let diagnostic = Diagnostic::error(
            violation.code.as_str(),
            violation.message.as_str(),
            violation.path.as_str(),
        );
        let normalized = serde_json::json!({
            "invalidResource": [{
                "code": violation.code,
                "keyword": violation.keyword,
                "message": violation.message,
                "path": violation.path,
            }]
        });
        return Plan {
            graph_hash: canonical_hash(&normalized),
            normalized,
            diagnostics: vec![diagnostic],
        };
    }

    let source_uses_stable_graph_api =
        document.get("apiVersion").and_then(Value::as_str) == Some(GRAPH_API_VERSION);
    let mut diagnostics = Vec::new();
    let migrated = migrate_graph(document);
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

    let schema_violations = if matches!(
        document.get("apiVersion").and_then(Value::as_str),
        Some(
            GRAPH_API_VERSION
                | "graphblocks.ai/v1alpha1"
                | "graphblocks.ai/v1alpha2"
                | "graphblocks.ai/v1alpha3"
        )
    ) {
        match resource_schema_errors(document) {
            Ok(violations) => violations,
            Err(error) => {
                diagnostics.push(Diagnostic::error(
                    "GB9001",
                    format!("failed to validate graph resource schema: {error}"),
                    "$",
                ));
                Vec::new()
            }
        }
    } else {
        Vec::new()
    };

    if !matches!(
        document.get("apiVersion").and_then(Value::as_str),
        Some(
            GRAPH_API_VERSION
                | "graphblocks.ai/v1alpha1"
                | "graphblocks.ai/v1alpha2"
                | "graphblocks.ai/v1alpha3"
        )
    ) {
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
        prepend_schema_diagnostics(&mut diagnostics, &schema_violations);
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

    if spec.and_then(|spec| spec.get("composition")).is_some() {
        diagnostics.push(Diagnostic::error(
            "GB1052",
            "graph composition must be materialized before compilation",
            "$.spec.composition",
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
                            "GB0015",
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
                            "GB0015",
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
            if node.contains_key("slot") {
                diagnostics.push(Diagnostic::error(
                    "GB1052",
                    "slot placeholders must be materialized before compilation",
                    format!("$.spec.nodes.{node_name}.slot"),
                ));
                continue;
            }
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
            if let Some(async_block_type) = node
                .get("block")
                .and_then(Value::as_str)
                .and_then(|block| block.split_once('@').map(|(block_type, _)| block_type))
                .filter(|block_type| {
                    matches!(
                        *block_type,
                        "async.start_operation" | "async.await_callback" | "async.poll_operation"
                    )
                })
            {
                match node.get("config") {
                    Some(Value::Object(config)) => {
                        diagnose_async_operation_config(
                            &mut diagnostics,
                            config,
                            &format!("$.spec.nodes.{node_name}.config"),
                            async_block_type == "async.await_callback",
                        );
                    }
                    Some(_) => diagnostics.push(Diagnostic::error(
                        "GB1026",
                        "async operation node config must be a mapping",
                        format!("$.spec.nodes.{node_name}.config"),
                    )),
                    None => {
                        let empty_config = Map::new();
                        diagnose_async_operation_config(
                            &mut diagnostics,
                            &empty_config,
                            &format!("$.spec.nodes.{node_name}.config"),
                            async_block_type == "async.await_callback",
                        );
                    }
                }
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
                        "external_write" | "filesystem_write" | "destructive" | "process"
                    )
                }
                Some(Value::Array(effects)) => effects.iter().any(|effect| {
                    effect.as_str().is_some_and(|effect| {
                        matches!(
                            effect,
                            "external_write" | "filesystem_write" | "destructive" | "process"
                        )
                    })
                }),
                _ => false,
            };
            let flow = node.get("flow").and_then(Value::as_object);
            if let Some(timeout) = flow.and_then(|flow| flow.get("timeout")) {
                let valid_timeout = parse_duration_seconds(timeout).is_some();
                if !valid_timeout {
                    diagnostics.push(Diagnostic::error(
                        "GB1019",
                        "flow.timeout must be a positive finite duration",
                        format!("$.spec.nodes.{node_name}.flow.timeout"),
                    ));
                }
            }
            let retry = flow.and_then(|flow| flow.get("retry"));
            let retry_path = format!("$.spec.nodes.{node_name}.flow.retry");
            let (max_attempts, exceeds_max_attempts, max_attempts_path) = match retry {
                Some(Value::Object(retry)) => {
                    let (key, configured) = if let Some(configured) = retry.get("maxAttempts") {
                        ("maxAttempts", Some(configured))
                    } else {
                        ("max_attempts", retry.get("max_attempts"))
                    };
                    (
                        configured.and_then(Value::as_i64).unwrap_or(1),
                        configured.is_some_and(|value| {
                            json_integer_exceeds_u64(value, MAX_NODE_RETRY_ATTEMPTS)
                        }),
                        format!("{retry_path}.{key}"),
                    )
                }
                Some(value @ Value::Number(retry)) => (
                    retry.as_i64().unwrap_or(1),
                    json_integer_exceeds_u64(value, MAX_NODE_RETRY_ATTEMPTS),
                    retry_path.clone(),
                ),
                _ => (1, false, retry_path.clone()),
            };
            if exceeds_max_attempts && !source_uses_stable_graph_api {
                diagnostics.push(Diagnostic::error(
                    "GB1008",
                    format!("node retry attempts must not exceed {MAX_NODE_RETRY_ATTEMPTS}"),
                    max_attempts_path,
                ));
            }
            let has_valid_idempotency_key = retry
                .and_then(Value::as_object)
                .and_then(|retry| {
                    retry
                        .get("idempotencyKey")
                        .or_else(|| retry.get("idempotency_key"))
                })
                .and_then(Value::as_str)
                .is_some_and(|idempotency_key| {
                    !idempotency_key.is_empty() && idempotency_key == idempotency_key.trim()
                });
            if effect_retry_requires_key && max_attempts > 1 && !has_valid_idempotency_key {
                diagnostics.push(Diagnostic::error(
                    "GB1011",
                    "retrying effectful nodes requires an idempotency key",
                    format!("$.spec.nodes.{node_name}.flow.retry"),
                ));
            }
        }
    }

    if let Some(Value::Object(execution)) = spec.and_then(|spec| spec.get("execution")) {
        let event_stream = spec.and_then(|spec| {
            spec.get("eventStream")
                .or_else(|| spec.get("event_stream"))
                .and_then(Value::as_object)
        });
        diagnose_background_execution_config(&mut diagnostics, execution, event_stream);
    }

    let async_operations_value = spec.and_then(|spec| {
        spec.get("asyncOperations")
            .map(|value| ("asyncOperations", value))
            .or_else(|| {
                spec.get("async_operations")
                    .map(|value| ("async_operations", value))
            })
    });
    match async_operations_value {
        Some((async_operations_key, Value::Object(async_operations))) => {
            for (operation_key, operation_config) in async_operations {
                let operation_path = format!("$.spec.{async_operations_key}.{operation_key}");
                let Some(operation_config) = operation_config.as_object() else {
                    diagnostics.push(Diagnostic::error(
                        "GB1026",
                        "async operation config must be a mapping",
                        operation_path,
                    ));
                    continue;
                };
                diagnose_async_operation_config(
                    &mut diagnostics,
                    operation_config,
                    &operation_path,
                    false,
                );
            }
        }
        Some((async_operations_key, Value::Array(async_operations))) => {
            for (operation_index, operation_config) in async_operations.iter().enumerate() {
                let operation_path = format!("$.spec.{async_operations_key}[{operation_index}]");
                let Some(operation_config) = operation_config.as_object() else {
                    diagnostics.push(Diagnostic::error(
                        "GB1026",
                        "async operation config must be a mapping",
                        operation_path,
                    ));
                    continue;
                };
                diagnose_async_operation_config(
                    &mut diagnostics,
                    operation_config,
                    &operation_path,
                    false,
                );
            }
        }
        Some((async_operations_key, _)) => diagnostics.push(Diagnostic::error(
            "GB1026",
            "asyncOperations must be a mapping or list",
            format!("$.spec.{async_operations_key}"),
        )),
        None => {}
    }

    let callback_subscriptions_value = spec.and_then(|spec| {
        spec.get("callbackSubscriptions")
            .map(|value| ("callbackSubscriptions", value))
            .or_else(|| {
                spec.get("callback_subscriptions")
                    .map(|value| ("callback_subscriptions", value))
            })
    });
    match callback_subscriptions_value {
        Some((callback_subscriptions_key, Value::Object(callback_subscriptions))) => {
            for (subscription_key, subscription_config) in callback_subscriptions {
                let subscription_path =
                    format!("$.spec.{callback_subscriptions_key}.{subscription_key}");
                let Some(subscription_config) = subscription_config.as_object() else {
                    diagnostics.push(Diagnostic::error(
                        "GB1027",
                        "callback subscription config must be a mapping",
                        subscription_path,
                    ));
                    continue;
                };
                diagnose_callback_subscription_config(
                    &mut diagnostics,
                    subscription_config,
                    &subscription_path,
                );
            }
        }
        Some((callback_subscriptions_key, Value::Array(callback_subscriptions))) => {
            for (subscription_index, subscription_config) in
                callback_subscriptions.iter().enumerate()
            {
                let subscription_path =
                    format!("$.spec.{callback_subscriptions_key}[{subscription_index}]");
                let Some(subscription_config) = subscription_config.as_object() else {
                    diagnostics.push(Diagnostic::error(
                        "GB1027",
                        "callback subscription config must be a mapping",
                        subscription_path,
                    ));
                    continue;
                };
                diagnose_callback_subscription_config(
                    &mut diagnostics,
                    subscription_config,
                    &subscription_path,
                );
            }
        }
        Some((callback_subscriptions_key, _)) => diagnostics.push(Diagnostic::error(
            "GB1027",
            "callbackSubscriptions must be a mapping or list",
            format!("$.spec.{callback_subscriptions_key}"),
        )),
        None => {}
    }

    let output_policy_value = spec.and_then(|spec| {
        spec.get("outputPolicy")
            .map(|value| ("outputPolicy", value))
            .or_else(|| {
                spec.get("output_policy")
                    .map(|value| ("output_policy", value))
            })
    });
    let output_policy = match output_policy_value {
        Some((output_policy_key, Value::Object(output_policy))) => {
            Some((output_policy_key, output_policy))
        }
        Some((output_policy_key, _)) => {
            diagnostics.push(Diagnostic::error(
                "GB1034",
                "outputPolicy must be a mapping",
                format!("$.spec.{output_policy_key}"),
            ));
            None
        }
        None => None,
    };

    let delivery = output_policy.and_then(|(output_policy_key, output_policy)| match output_policy
        .get("delivery")
    {
        Some(Value::Object(delivery)) => Some(delivery),
        Some(_) => {
            diagnostics.push(Diagnostic::error(
                "GB1034",
                "outputPolicy delivery must be a mapping",
                format!("$.spec.{output_policy_key}.delivery"),
            ));
            None
        }
        None => None,
    });

    if let Some(delivery) = delivery {
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
                "GB1030",
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
                "GB1044",
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
                "GB1028",
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
                            "GB1029",
                            format!("invalid flush boundary {boundary}"),
                            format!("$.spec.outputPolicy.delivery.{path_key}[{boundary_index}]"),
                        ));
                    }
                }
            } else {
                diagnostics.push(Diagnostic::error(
                    "GB1029",
                    "flush boundaries must be a list of strings",
                    format!("$.spec.outputPolicy.delivery.{path_key}"),
                ));
            }
        }

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

        if mode == Some("bounded_holdback") {
            if !has_token_bound && !has_byte_bound && !has_duration_bound {
                diagnostics.push(Diagnostic::error(
                    "GB1051",
                    "bounded_holdback output delivery requires a token, byte, or duration bound",
                    "$.spec.outputPolicy.delivery",
                ));
            }
        } else if matches!(mode, Some("buffer_until_commit" | "immediate_draft"))
            && (has_token_bound || has_byte_bound || has_duration_bound)
        {
            diagnostics.push(Diagnostic::error(
                "GB1054",
                "holdback limits require bounded_holdback output delivery mode",
                "$.spec.outputPolicy.delivery",
            ));
        }

        if mode == Some("immediate_draft") {
            let delivered_draft_disposition = delivery
                .get("deliveredDraftDisposition")
                .or_else(|| delivery.get("delivered_draft_disposition"))
                .and_then(Value::as_str)
                .unwrap_or("retract");
            if delivered_draft_disposition == "keep" {
                diagnostics.push(Diagnostic::error(
                    "GB1025",
                    "immediate_draft output delivery requires incomplete or retracted draft semantics",
                    "$.spec.outputPolicy.delivery.deliveredDraftDisposition",
                ));
            }
        }
    }

    if let Some((output_policy_key, output_policy)) = output_policy {
        let evaluation = match output_policy
            .get("evaluation")
            .or_else(|| output_policy.get("outputEvaluation"))
            .or_else(|| output_policy.get("output_evaluation"))
        {
            Some(Value::Object(evaluation)) => Some(evaluation),
            Some(_) => {
                diagnostics.push(Diagnostic::error(
                    "GB1034",
                    "outputPolicy evaluation must be a mapping",
                    format!("$.spec.{output_policy_key}.evaluation"),
                ));
                None
            }
            None => None,
        };
        let enforcement_points_value = evaluation.and_then(|evaluation| {
            evaluation
                .get("enforcementPoints")
                .or_else(|| evaluation.get("enforcement_points"))
        });

        if let Some(enforcement_points) = enforcement_points_value.and_then(Value::as_array) {
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
                        "GB1033",
                        format!("invalid output policy enforcement point {enforcement_point}"),
                        format!("$.spec.outputPolicy.evaluation.enforcementPoints[{index}]"),
                    ));
                }
            }

            if before_client_delivery_index.is_none() {
                diagnostics.push(Diagnostic::error(
                    "GB1046",
                    "output policy enforcement must include the before_client_delivery gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                ));
            }
            if on_generation_chunk_index.is_none() {
                diagnostics.push(Diagnostic::error(
                    "GB1046",
                    "output policy enforcement must include the on_generation_chunk gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                ));
            }
            if before_output_commit_index.is_none() {
                diagnostics.push(Diagnostic::error(
                    "GB1046",
                    "output policy enforcement must include the before_output_commit gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                ));
            }

            if let (Some(before_client_delivery_index), Some(on_generation_chunk_index)) =
                (before_client_delivery_index, on_generation_chunk_index)
                && before_client_delivery_index < on_generation_chunk_index
            {
                diagnostics.push(Diagnostic::error(
                    "GB1048",
                    "on_generation_chunk policy evaluation must precede before_client_delivery",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                ));
            }
        } else if enforcement_points_value.is_some() {
            diagnostics.push(Diagnostic::error(
                "GB1033",
                "output policy enforcementPoints must be a list of strings",
                "$.spec.outputPolicy.evaluation.enforcementPoints",
            ));
            diagnostics.push(Diagnostic::error(
                "GB1046",
                "output policy enforcement must include the before_client_delivery gate",
                "$.spec.outputPolicy.evaluation.enforcementPoints",
            ));
        } else {
            diagnostics.push(Diagnostic::error(
                "GB1046",
                "output policy enforcement must include the before_client_delivery gate",
                "$.spec.outputPolicy.evaluation.enforcementPoints",
            ));
        }

        let on_violation = match output_policy
            .get("onViolation")
            .or_else(|| output_policy.get("on_violation"))
        {
            Some(Value::Object(on_violation)) => Some(on_violation),
            Some(_) => {
                diagnostics.push(Diagnostic::error(
                    "GB1034",
                    "outputPolicy onViolation must be a mapping",
                    format!("$.spec.{output_policy_key}.onViolation"),
                ));
                None
            }
            None => None,
        };
        if let Some(on_violation) = on_violation {
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
                    "GB1031",
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
                            "GB1036",
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
                        "GB1036",
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
                    "GB1035",
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
                    "GB1028",
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
                    "GB1032",
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
                        "GB1047",
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
                        "GB1024",
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
        let tool_execution_value = spec.and_then(|spec| {
            spec.get("toolExecution")
                .map(|value| ("toolExecution", value))
                .or_else(|| {
                    spec.get("tool_execution")
                        .map(|value| ("tool_execution", value))
                })
        });
        let tool_execution = match tool_execution_value {
            Some((tool_execution_key, Value::Object(tool_execution))) => {
                Some((tool_execution_key, tool_execution))
            }
            Some((tool_execution_key, _)) => {
                diagnostics.push(Diagnostic::error(
                    "GB1041",
                    "toolExecution must be a mapping",
                    format!("$.spec.{tool_execution_key}"),
                ));
                None
            }
            None => None,
        };
        let mut maximum_parallelism = 1;
        let mut parallel_tool_calls = false;
        let mut has_effect_serialization_key = false;
        if let Some((tool_execution_key, tool_execution)) = tool_execution {
            let maximum_parallelism_value = tool_execution
                .get("maximumParallelism")
                .map(|value| ("maximumParallelism", value))
                .or_else(|| {
                    tool_execution
                        .get("maximum_parallelism")
                        .map(|value| ("maximum_parallelism", value))
                });
            if let Some((maximum_parallelism_key, configured_parallelism)) =
                maximum_parallelism_value
            {
                if let Some(configured_parallelism) = configured_parallelism.as_u64()
                    && configured_parallelism > 0
                {
                    maximum_parallelism = configured_parallelism;
                } else {
                    diagnostics.push(Diagnostic::error(
                        "GB1041",
                        "toolExecution maximumParallelism must be a positive integer",
                        format!("$.spec.{tool_execution_key}.{maximum_parallelism_key}"),
                    ));
                }
            }

            let parallel_tool_calls_value = tool_execution
                .get("parallelToolCalls")
                .map(|value| ("parallelToolCalls", value))
                .or_else(|| {
                    tool_execution
                        .get("parallel_tool_calls")
                        .map(|value| ("parallel_tool_calls", value))
                });
            if let Some((parallel_tool_calls_key, configured_parallel_tool_calls)) =
                parallel_tool_calls_value
            {
                if let Some(configured_parallel_tool_calls) =
                    configured_parallel_tool_calls.as_bool()
                {
                    parallel_tool_calls = configured_parallel_tool_calls;
                } else {
                    diagnostics.push(Diagnostic::error(
                        "GB1041",
                        "toolExecution parallelToolCalls must be a boolean",
                        format!("$.spec.{tool_execution_key}.{parallel_tool_calls_key}"),
                    ));
                }
            }

            let effect_serialization_value = tool_execution
                .get("effectSerialization")
                .map(|value| ("effectSerialization", value))
                .or_else(|| {
                    tool_execution
                        .get("effect_serialization")
                        .map(|value| ("effect_serialization", value))
                });
            if let Some((effect_serialization_key, effect_serialization)) =
                effect_serialization_value
            {
                if let Some(effect_serialization) = effect_serialization.as_object() {
                    let key_template_value = effect_serialization
                        .get("keyTemplate")
                        .map(|value| ("keyTemplate", value))
                        .or_else(|| {
                            effect_serialization
                                .get("key_template")
                                .map(|value| ("key_template", value))
                        });
                    if let Some((key_template_key, key_template)) = key_template_value {
                        if let Some(key_template) = key_template.as_str() {
                            has_effect_serialization_key = !key_template.trim().is_empty();
                        }
                        if !has_effect_serialization_key {
                            diagnostics.push(Diagnostic::error(
                                "GB1041",
                                "toolExecution effectSerialization keyTemplate must be a non-empty string",
                                format!(
                                    "$.spec.{tool_execution_key}.{effect_serialization_key}.{key_template_key}"
                                ),
                            ));
                        }
                    }
                } else {
                    diagnostics.push(Diagnostic::error(
                        "GB1041",
                        "toolExecution effectSerialization must be a mapping",
                        format!("$.spec.{tool_execution_key}.{effect_serialization_key}"),
                    ));
                }
            }
        }
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
                            "GB1040",
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
                            "GB1040",
                            format!("invalid tool effect {effect}"),
                            format!("$.spec.bindings.tools.{tool_key}.effects[{effect_index}]"),
                        ));
                    }
                }
                Some(_) => {
                    diagnostics.push(Diagnostic::error(
                        "GB1040",
                        "tool effects must be a string or list of strings",
                        format!("$.spec.bindings.tools.{tool_key}.effects"),
                    ));
                }
                None => {}
            };
            if valid_effects.contains("none") && valid_effects.len() > 1 {
                diagnostics.push(Diagnostic::error(
                    "GB1040",
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
                            "GB1037",
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
                            "GB1023",
                            "explicit tool approval must be bound to immutable argument digest",
                            format!("$.spec.bindings.tools.{tool_key}.approval"),
                        ));
                    }
                } else if let Some(approval) = approval.as_str() {
                    if !matches!(approval, "never" | "policy" | "always") {
                        diagnostics.push(Diagnostic::error(
                            "GB1037",
                            format!("invalid tool approval {approval}"),
                            format!("$.spec.bindings.tools.{tool_key}.approval"),
                        ));
                    } else if approval == "always" {
                        diagnostics.push(Diagnostic::error(
                            "GB1023",
                            "explicit tool approval must be bound to immutable argument digest",
                            format!("$.spec.bindings.tools.{tool_key}.approval"),
                        ));
                    }
                } else {
                    diagnostics.push(Diagnostic::error(
                        "GB1037",
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
                    "GB1042",
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
                    "GB1038",
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
                    "GB1043",
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
                    "GB1045",
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
                            "GB1039",
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
                        "GB1039",
                        "tool definition version must be a non-empty string",
                        format!("$.spec.bindings.tools.{tool_key}.definition.version"),
                    ));
                }
                if let Some(tags) = definition.get("tags") {
                    if let Some(tags) = tags.as_array() {
                        for (tag_index, tag) in tags.iter().enumerate() {
                            if tag.as_str().is_none_or(|value| value.trim().is_empty()) {
                                diagnostics.push(Diagnostic::error(
                                    "GB1039",
                                    "tool definition tags must be non-empty strings",
                                    format!(
                                        "$.spec.bindings.tools.{tool_key}.definition.tags[{tag_index}]"
                                    ),
                                ));
                            }
                        }
                    } else {
                        diagnostics.push(Diagnostic::error(
                            "GB1039",
                            "tool definition tags must be a list of non-empty strings",
                            format!("$.spec.bindings.tools.{tool_key}.definition.tags"),
                        ));
                    }
                }
                for forbidden_field in FORBIDDEN_TOOL_DEFINITION_FIELDS {
                    if definition.contains_key(forbidden_field) {
                        diagnostics.push(Diagnostic::error(
                            "GB1039",
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
                        "GB1050",
                        "model-visible tool definitions require an input schema",
                        format!("$.spec.bindings.tools.{tool_key}.definition.inputSchema"),
                    ));
                } else if let Some(input_schema) = input_schema
                    && let Err(error) = SchemaId::parse(input_schema)
                {
                    diagnostics.push(Diagnostic::error(
                        "GB0015",
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
                        "GB0015",
                        format!("tool output schema id is invalid: {error}"),
                        format!("$.spec.bindings.tools.{tool_key}.definition.outputSchema"),
                    ));
                }
            } else {
                diagnostics.push(Diagnostic::error(
                    "GB1050",
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
                            "GB1049",
                            "tool implementation kind must be one of block, graph, remote, mcp, or openapi",
                            format!("$.spec.bindings.tools.{tool_key}.implementation.kind"),
                        ));
                        None
                    }
                };
                if let Some(missing_implementation_field) = missing_implementation_field {
                    diagnostics.push(Diagnostic::error(
                        "GB1049",
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
                    "GB1049",
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
                "GB1053",
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
                        "GB1055",
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
    let normalized_spec = normalized.get("spec");
    let allows_duplex_voice_feedback = normalized_spec
        .and_then(|spec| spec.get("extensions"))
        .and_then(Value::as_array)
        .is_some_and(|extensions| {
            extensions
                .iter()
                .any(|extension| extension.as_str() == Some("graphblocks.voice/v1alpha1"))
        })
        && normalized_spec
            .and_then(|spec| spec.get("execution"))
            .and_then(|execution| execution.get("lifetime"))
            .and_then(Value::as_str)
            == Some("session")
        && normalized_spec
            .and_then(|spec| spec.get("execution"))
            .and_then(|execution| execution.get("interaction"))
            .and_then(Value::as_str)
            == Some("duplex")
        && normalized_spec
            .and_then(|spec| spec.get("execution"))
            .and_then(|execution| execution.get("durability"))
            .and_then(Value::as_str)
            == Some("checkpointed")
        && normalized_spec
            .and_then(|spec| spec.get("voice"))
            .and_then(|voice| voice.get("pipeline"))
            .and_then(|pipeline| pipeline.get("kind"))
            .and_then(Value::as_str)
            == Some("realtime");
    let normalized_nodes = normalized
        .get("spec")
        .and_then(|spec| spec.get("nodes"))
        .and_then(Value::as_object);
    let interface_inputs = normalized
        .get("spec")
        .and_then(|spec| spec.get("interface"))
        .and_then(|interface| interface.get("inputs"))
        .and_then(Value::as_object);
    let interface_outputs = normalized
        .get("spec")
        .and_then(|spec| spec.get("interface"))
        .and_then(|interface| interface.get("outputs"))
        .and_then(Value::as_object);
    let mut produced_nodes = BTreeSet::<String>::new();
    let mut consumed_nodes = BTreeSet::<String>::new();
    let mut dependency_graph = normalized_nodes
        .into_iter()
        .flat_map(|nodes| nodes.keys())
        .map(|node_name| (node_name.to_owned(), BTreeSet::<String>::new()))
        .collect::<BTreeMap<_, _>>();
    let mut edge_dependency_endpoints = BTreeSet::<(String, String)>::new();
    let mut guard_dependencies = BTreeSet::<(String, String)>::new();
    if let Some(edges) = normalized
        .get("spec")
        .and_then(|spec| spec.get("edges"))
        .and_then(Value::as_array)
    {
        let mut seen_edge_identities = BTreeSet::<(String, String)>::new();
        let mut source_by_target = BTreeMap::<String, String>::new();
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
            let duplicate_identity =
                !seen_edge_identities.insert((source.to_owned(), target.to_owned()));
            if duplicate_identity {
                diagnostics.push(Diagnostic::error(
                    "GB1005",
                    format!("duplicate edge identity '{source}' -> '{target}'"),
                    format!("$.spec.edges[{index}]"),
                ));
            } else if let Some(existing_source) = source_by_target.get(target) {
                if existing_source != source {
                    diagnostics.push(Diagnostic::error(
                        "GB1007",
                        format!(
                            "multiple distinct edge sources write target '{target}': '{existing_source}' and '{source}'"
                        ),
                        format!("$.spec.edges[{index}]"),
                    ));
                }
            } else {
                source_by_target.insert(target.to_owned(), source.to_owned());
            }
            for (key, endpoint) in [("from", source), ("to", target)] {
                let Some((owner, endpoint_path)) = endpoint.split_once('.') else {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        format!("edge {key} endpoint must include a port path"),
                        format!("$.spec.edges[{index}].{key}"),
                    ));
                    continue;
                };
                if owner.is_empty()
                    || endpoint_path.is_empty()
                    || endpoint_path.split('.').any(str::is_empty)
                {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        format!("edge {key} endpoint must include a port path"),
                        format!("$.spec.edges[{index}].{key}"),
                    ));
                    continue;
                }
                if endpoint_path
                    .split('.')
                    .skip(1)
                    .any(|part| part.bytes().all(|byte| byte.is_ascii_digit()))
                {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        format!(
                            "edge {key} endpoint must not contain numeric nested path segments"
                        ),
                        format!("$.spec.edges[{index}].{key}"),
                    ));
                    continue;
                }
                if key == "from" && owner == "$output" {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        "$output cannot be used as an edge source",
                        format!("$.spec.edges[{index}].from"),
                    ));
                    continue;
                }
                if matches!(owner, "$context" | "$execution" | "$state") {
                    let endpoint_direction = if key == "from" { "source" } else { "target" };
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        format!(
                            "{owner} is not supported as an edge {endpoint_direction} by the local runtime"
                        ),
                        format!("$.spec.edges[{index}].{key}"),
                    ));
                    continue;
                }
                if key == "to" && owner == "$input" {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        "$input cannot be used as an edge target",
                        format!("$.spec.edges[{index}].to"),
                    ));
                    continue;
                }
                if key == "from" && owner == "$input" {
                    let port_name = endpoint
                        .split_once('.')
                        .map(|(_, path)| path.split_once('.').map_or(path, |(port, _)| port))
                        .unwrap_or_default();
                    if interface_inputs.is_some_and(|ports| !ports.contains_key(port_name)) {
                        diagnostics.push(Diagnostic::error(
                            "GB1014",
                            format!("graph interface has no input port {port_name:?}"),
                            format!("$.spec.edges[{index}].from"),
                        ));
                    }
                    continue;
                }
                if key == "to" && owner == "$output" {
                    let port_name = endpoint
                        .split_once('.')
                        .map(|(_, path)| path.split_once('.').map_or(path, |(port, _)| port))
                        .unwrap_or_default();
                    if interface_outputs.is_some_and(|ports| !ports.contains_key(port_name)) {
                        diagnostics.push(Diagnostic::error(
                            "GB1013",
                            format!("graph interface has no output port {port_name:?}"),
                            format!("$.spec.edges[{index}].to"),
                        ));
                    }
                    continue;
                }
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
            if let (Some((source_owner, source_path)), Some((target_owner, target_path))) =
                (source.split_once('.'), target.split_once('.'))
                && !source_path.is_empty()
                && !target_path.is_empty()
                && source_path.split('.').all(|part| !part.is_empty())
                && target_path.split('.').all(|part| !part.is_empty())
                && dependency_graph.contains_key(source_owner)
                && dependency_graph.contains_key(target_owner)
                && let Some(dependents) = dependency_graph.get_mut(source_owner)
            {
                dependents.insert(target_owner.to_owned());
                edge_dependency_endpoints.insert((source.to_owned(), target.to_owned()));
            }
        }
    }

    if let Some(normalized_nodes) = normalized_nodes {
        let empty_config = Value::Object(Map::new());
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
                    let (port_name, nested) = source_path
                        .split_once('.')
                        .map_or((source_path, false), |(port_name, _)| (port_name, true));
                    (source_owner, port_name, nested)
                });
                let target_port = target.split_once('.').map(|(target_owner, target_path)| {
                    let (port_name, nested) = target_path
                        .split_once('.')
                        .map_or((target_path, false), |(port_name, _)| (port_name, true));
                    (target_owner, port_name, nested)
                });

                let mut source_type = None;
                let mut target_type = None;
                let mut source_required = None;
                let mut target_required = None;
                if let Some((source_owner, port_name, nested)) = source_port {
                    if source_owner == "$input" && !nested {
                        source_type = interface_inputs
                            .and_then(|ports| ports.get(port_name))
                            .and_then(Value::as_str);
                    } else if !PSEUDO_NODES.contains(&source_owner)
                        && let Some(source_node) = normalized_nodes.get(source_owner)
                        && let Some(descriptor) = source_node
                            .as_object()
                            .and_then(|node| node.get("block"))
                            .and_then(Value::as_str)
                            .and_then(|block_id| block_catalog.get(block_id))
                    {
                        if let Some(port) = descriptor
                            .outputs
                            .iter()
                            .find(|port| port.name == port_name)
                        {
                            let source_config = source_node
                                .get("config")
                                .filter(|config| config.is_object())
                                .unwrap_or(&empty_config);
                            source_required =
                                Some(port.required_for(source_config, ExecutionPhase::Initial));
                            if !nested {
                                source_type = port.type_ref.as_deref();
                            }
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
                if let Some((target_owner, port_name, nested)) = target_port {
                    if target_owner == "$output" {
                        if let Some(output_type) =
                            interface_outputs.and_then(|ports| ports.get(port_name))
                        {
                            target_required = Some(true);
                            if !nested {
                                target_type = output_type.as_str();
                            }
                        }
                    } else if !PSEUDO_NODES.contains(&target_owner)
                        && let Some(target_node) = normalized_nodes.get(target_owner)
                        && let Some(descriptor) = target_node
                            .as_object()
                            .and_then(|node| node.get("block"))
                            .and_then(Value::as_str)
                            .and_then(|block_id| block_catalog.get(block_id))
                    {
                        if let Some(port) =
                            descriptor.inputs.iter().find(|port| port.name == port_name)
                        {
                            target_required = Some(port.required);
                            if !nested {
                                target_type = port.type_ref.as_deref();
                            }
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
                if !block_catalog.allow_unknown_blocks {
                    diagnostics.push(Diagnostic::error(
                        "GB1022",
                        format!("block {block_id:?} is not declared in the block catalog"),
                        format!("$.spec.nodes.{node_name}.block"),
                    ));
                }
                continue;
            };
            let config = node.get("config").unwrap_or(&empty_config);
            let validator = jsonschema::draft202012::new(&descriptor.config_schema)
                .expect("block catalog configSchema was validated when constructed");
            let mut config_errors = validator
                .iter_errors(config)
                .map(|error| {
                    (
                        error.instance_path().as_str().to_owned(),
                        error.schema_path().as_str().to_owned(),
                        error.to_string(),
                    )
                })
                .collect::<Vec<_>>();
            config_errors.sort();
            for (instance_path, _, message) in config_errors {
                diagnostics.push(Diagnostic::error(
                    "GB2019",
                    format!(
                        "node config does not satisfy {} configSchema: {message}",
                        descriptor.block_id()
                    ),
                    config_diagnostic_path(node_name, &instance_path),
                ));
            }
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
            if let Some(when) = node.as_object().and_then(|node| node.get("when")) {
                let Some(when) = when.as_str() else {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        "node when reference must be a string",
                        format!("$.spec.nodes.{node_name}.when"),
                    ));
                    continue;
                };
                let Some((owner, when_path)) = when.split_once('.') else {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        "node when reference must include a port path",
                        format!("$.spec.nodes.{node_name}.when"),
                    ));
                    continue;
                };
                if owner.is_empty()
                    || when_path.is_empty()
                    || when_path.split('.').any(str::is_empty)
                {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        "node when reference must include a port path",
                        format!("$.spec.nodes.{node_name}.when"),
                    ));
                } else if owner == "$output" {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        "$output cannot be used as a when source",
                        format!("$.spec.nodes.{node_name}.when"),
                    ));
                } else if owner == "$input" {
                    let port_name = when_path
                        .split_once('.')
                        .map_or(when_path, |(port_name, _)| port_name);
                    if interface_inputs.is_some_and(|ports| !ports.contains_key(port_name)) {
                        diagnostics.push(Diagnostic::error(
                            "GB1014",
                            format!("graph interface has no input port {port_name:?}"),
                            format!("$.spec.nodes.{node_name}.when"),
                        ));
                    }
                } else if matches!(owner, "$context" | "$execution" | "$state") {
                    diagnostics.push(Diagnostic::error(
                        "GB1020",
                        format!("{owner} is not supported as a when source by the local runtime"),
                        format!("$.spec.nodes.{node_name}.when"),
                    ));
                } else if PSEUDO_NODES.contains(&owner) {
                    continue;
                } else if !normalized_nodes.contains_key(owner) {
                    diagnostics.push(Diagnostic::error(
                        "GB1002",
                        format!("when references unknown node {owner:?}"),
                        format!("$.spec.nodes.{node_name}.when"),
                    ));
                } else {
                    let port_name = when_path
                        .split_once('.')
                        .map_or(when_path, |(port_name, _)| port_name);
                    let descriptor = normalized_nodes
                        .get(owner)
                        .and_then(Value::as_object)
                        .and_then(|node| node.get("block"))
                        .and_then(Value::as_str)
                        .and_then(|block_id| block_catalog.get(block_id));
                    if let Some(descriptor) = descriptor
                        && !descriptor.outputs.iter().any(|port| port.name == port_name)
                    {
                        diagnostics.push(Diagnostic::error(
                            "GB1014",
                            format!(
                                "block {} has no output port {port_name:?}",
                                descriptor.block_id()
                            ),
                            format!("$.spec.nodes.{node_name}.when"),
                        ));
                        continue;
                    }
                    produced_nodes.insert(owner.to_owned());
                    consumed_nodes.insert(node_name.to_owned());
                    if let Some(dependents) = dependency_graph.get_mut(owner) {
                        dependents.insert(node_name.to_owned());
                    }
                    guard_dependencies.insert((owner.to_owned(), node_name.to_owned()));
                }
            }
        }

        if allows_duplex_voice_feedback {
            let mut reverse_dependency_graph = dependency_graph
                .keys()
                .map(|node_name| (node_name.clone(), BTreeSet::<String>::new()))
                .collect::<BTreeMap<_, _>>();
            for (source_owner, targets) in &dependency_graph {
                for target_owner in targets {
                    if let Some(sources) = reverse_dependency_graph.get_mut(target_owner) {
                        sources.insert(source_owner.clone());
                    }
                }
            }

            let mut allowed_feedback_dependencies = BTreeSet::<(String, String)>::new();
            for (source_endpoint, target_endpoint) in &edge_dependency_endpoints {
                let Some((source_owner, source_path)) = source_endpoint.split_once('.') else {
                    continue;
                };
                let Some((target_owner, target_path)) = target_endpoint.split_once('.') else {
                    continue;
                };
                if source_path != "results" || target_path != "toolResults" {
                    continue;
                }
                let source_block = normalized_nodes
                    .get(source_owner)
                    .and_then(Value::as_object)
                    .and_then(|node| node.get("block"))
                    .and_then(Value::as_str);
                let target_block = normalized_nodes
                    .get(target_owner)
                    .and_then(Value::as_object)
                    .and_then(|node| node.get("block"))
                    .and_then(Value::as_str);
                if source_block != Some("tools.dispatch@1")
                    || target_block != Some("realtime.session@1")
                {
                    continue;
                }
                let reverse_endpoint = (
                    format!("{target_owner}.toolCalls"),
                    format!("{source_owner}.calls"),
                );
                if !edge_dependency_endpoints.contains(&reverse_endpoint) {
                    continue;
                }

                let mut reachable_from_session = BTreeSet::<String>::new();
                let mut stack = vec![target_owner.to_owned()];
                while let Some(current) = stack.pop() {
                    if !reachable_from_session.insert(current.clone()) {
                        continue;
                    }
                    if let Some(neighbors) = dependency_graph.get(&current) {
                        stack.extend(
                            neighbors
                                .iter()
                                .filter(|neighbor| !reachable_from_session.contains(*neighbor))
                                .cloned(),
                        );
                    }
                }

                let mut can_reach_session = BTreeSet::<String>::new();
                let mut stack = vec![target_owner.to_owned()];
                while let Some(current) = stack.pop() {
                    if !can_reach_session.insert(current.clone()) {
                        continue;
                    }
                    if let Some(neighbors) = reverse_dependency_graph.get(&current) {
                        stack.extend(
                            neighbors
                                .iter()
                                .filter(|neighbor| !can_reach_session.contains(*neighbor))
                                .cloned(),
                        );
                    }
                }

                let component = reachable_from_session
                    .intersection(&can_reach_session)
                    .cloned()
                    .collect::<BTreeSet<_>>();
                if component != BTreeSet::from([source_owner.to_owned(), target_owner.to_owned()]) {
                    continue;
                }
                let internal_endpoints = edge_dependency_endpoints
                    .iter()
                    .filter(|(edge_source, edge_target)| {
                        edge_source
                            .split_once('.')
                            .is_some_and(|(owner, _)| component.contains(owner))
                            && edge_target
                                .split_once('.')
                                .is_some_and(|(owner, _)| component.contains(owner))
                    })
                    .cloned()
                    .collect::<BTreeSet<_>>();
                if internal_endpoints
                    != BTreeSet::from([
                        (source_endpoint.clone(), target_endpoint.clone()),
                        reverse_endpoint,
                    ])
                {
                    continue;
                }
                if guard_dependencies
                    .iter()
                    .any(|(guard_source, guard_target)| {
                        component.contains(guard_source) && component.contains(guard_target)
                    })
                {
                    continue;
                }
                allowed_feedback_dependencies
                    .insert((source_owner.to_owned(), target_owner.to_owned()));
            }

            for (source_owner, target_owner) in allowed_feedback_dependencies {
                if let Some(dependents) = dependency_graph.get_mut(&source_owner) {
                    dependents.remove(&target_owner);
                }
            }
        }

        let dependencies_by_node = dependency_graph
            .iter()
            .map(|(node_name, dependencies)| {
                (
                    node_name.clone(),
                    dependencies.iter().cloned().collect::<Vec<_>>(),
                )
            })
            .collect::<BTreeMap<_, _>>();
        let mut dependency_states = BTreeMap::<String, u8>::new();
        let mut dependency_cycle = None;
        'roots: for root in dependencies_by_node.keys() {
            if dependency_states.contains_key(root) {
                continue;
            }
            dependency_states.insert(root.clone(), 1);
            let mut path = vec![root.clone()];
            let mut path_positions = BTreeMap::from([(root.clone(), 0_usize)]);
            let mut stack = vec![(root.clone(), 0_usize)];
            while let Some((current, next_index)) = stack.last_mut() {
                let neighbor = dependencies_by_node
                    .get(current)
                    .and_then(|neighbors| neighbors.get(*next_index))
                    .cloned();
                let Some(neighbor) = neighbor else {
                    dependency_states.insert(current.clone(), 2);
                    path_positions.remove(current);
                    path.pop();
                    stack.pop();
                    continue;
                };
                *next_index += 1;
                match dependency_states.get(&neighbor).copied() {
                    None => {
                        dependency_states.insert(neighbor.clone(), 1);
                        path_positions.insert(neighbor.clone(), path.len());
                        path.push(neighbor.clone());
                        stack.push((neighbor, 0));
                    }
                    Some(1) => {
                        if let Some(cycle_start) = path_positions.get(&neighbor).copied()
                            && let Some(cycle_path) = path.get(cycle_start..)
                        {
                            let mut cycle = cycle_path.to_vec();
                            cycle.push(neighbor);
                            dependency_cycle = Some(cycle);
                            break 'roots;
                        }
                    }
                    Some(_) => {}
                }
            }
        }
        if let Some(cycle) = dependency_cycle {
            diagnostics.push(Diagnostic::error(
                "GB1021",
                format!("graph dependency cycle detected: {}", cycle.join(" -> ")),
                "$.spec",
            ));
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
                "GB1004",
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

    prepend_schema_diagnostics(&mut diagnostics, &schema_violations);
    Plan {
        graph_hash: canonical_hash(&normalized),
        normalized,
        diagnostics,
    }
}

fn prepend_schema_diagnostics(
    diagnostics: &mut Vec<Diagnostic>,
    schema_violations: &[ResourceSchemaViolation],
) {
    let mut schema_diagnostics = schema_violations
        .iter()
        .filter(|violation| {
            (violation.keyword == "additionalProperties" && violation.path == "$")
                || !diagnostics.iter().any(|diagnostic| {
                    diagnostic.severity == Severity::Error
                        && (diagnostic.path == violation.path
                            || diagnostic.path.starts_with(&format!("{}.", violation.path))
                            || diagnostic.path.starts_with(&format!("{}[", violation.path))
                            || violation.path.starts_with(&format!("{}.", diagnostic.path))
                            || violation.path.starts_with(&format!("{}[", diagnostic.path)))
                })
        })
        .map(|violation| {
            Diagnostic::error(
                violation.code.clone(),
                violation.message.clone(),
                violation.path.clone(),
            )
        })
        .collect::<Vec<_>>();
    schema_diagnostics.append(diagnostics);
    *diagnostics = schema_diagnostics;
}

fn config_diagnostic_path(node_name: &str, pointer: &str) -> String {
    let mut path = format!("$.spec.nodes.{node_name}.config");
    for encoded in pointer.strip_prefix('/').unwrap_or(pointer).split('/') {
        if encoded.is_empty() {
            continue;
        }
        let segment = encoded.replace("~1", "/").replace("~0", "~");
        if segment
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
            && segment
                .as_bytes()
                .first()
                .is_some_and(|byte| byte.is_ascii_alphabetic() || *byte == b'_')
        {
            path.push('.');
            path.push_str(&segment);
        } else if segment.bytes().all(|byte| byte.is_ascii_digit()) {
            path.push('[');
            path.push_str(&segment);
            path.push(']');
        } else {
            path.push('[');
            path.push_str(&serde_json::to_string(&segment).expect("string JSON serialization"));
            path.push(']');
        }
    }
    path
}
