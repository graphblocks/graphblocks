use std::collections::BTreeMap;

use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ToolApproval, ToolBinding, ToolCatalog, ToolDefinition, ToolEffect,
    ToolIdempotency, ToolImplementation, ToolResolutionScope, ToolResultMode,
};
use graphblocks_runtime_core::tool_call::ToolCallDraft;
use graphblocks_runtime_core::tool_result::{
    ContentPart, ContentPartKind, ToolResult, ToolResultValidation, ToolResultValidationError,
    ToolResultValidationRequest,
};
use serde_json::{Map, Value, json};

pub(crate) fn evaluate_case(case: &Value) -> Result<Value, String> {
    let case = case
        .as_object()
        .ok_or_else(|| "tool-result TCK case must be an object".to_owned())?;
    let case_name = required_str(case, "name")?;
    let kind = required_str(case, "kind")?;
    let expected = case
        .get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("tool-result TCK case {case_name} requires expected"))?;
    let observed = match kind {
        "prepare_for_model" => evaluate_prepare_for_model(case, case_name)?,
        "stream_state" => evaluate_stream_state(case, case_name)?,
        other => {
            return Err(format!(
                "tool-result TCK case {case_name} has unknown kind {other}"
            ));
        }
    };

    let mut contract = Map::new();
    for key in expected
        .keys()
        .filter(|key| key.as_str() != "errorContains")
    {
        contract.insert(
            key.clone(),
            observed
                .get(key)
                .cloned()
                .ok_or_else(|| format!("tool-result TCK case {case_name} did not observe {key}"))?,
        );
    }
    if expected.contains_key("errorContains") {
        contract.insert(
            "errorCategory".to_owned(),
            observed.get("errorCategory").cloned().ok_or_else(|| {
                format!("tool-result TCK case {case_name} did not classify its validation error")
            })?,
        );
    }
    Ok(Value::Object(contract))
}

fn evaluate_prepare_for_model(
    case: &Map<String, Value>,
    case_name: &str,
) -> Result<Map<String, Value>, String> {
    let raw_tool = required_object(case, "tool", case_name)?;
    let tool_name = required_str(raw_tool, "name")?;
    let mut definition = ToolDefinition::new(
        tool_name,
        optional_str(raw_tool, "description").map_or("Execute a tool.", |value| value),
        optional_str(raw_tool, "inputSchema")
            .or_else(|| optional_str(raw_tool, "input_schema"))
            .map_or("schemas/ToolRequest@1", |value| value),
    );
    if let Some(output_schema) =
        optional_str(raw_tool, "outputSchema").or_else(|| optional_str(raw_tool, "output_schema"))
    {
        definition = definition.with_output_schema(output_schema);
    }

    let effects = raw_tool
        .get("effects")
        .and_then(Value::as_array)
        .map(|effects| {
            effects
                .iter()
                .enumerate()
                .map(|(index, effect)| {
                    let effect = effect.as_str().ok_or_else(|| {
                        format!(
                            "tool-result TCK case {case_name} tool.effects[{index}] must be a string"
                        )
                    })?;
                    match effect {
                        "none" => Ok(ToolEffect::None),
                        "external_read" => Ok(ToolEffect::ExternalRead),
                        "external_write" => Ok(ToolEffect::ExternalWrite),
                        "filesystem_read" => Ok(ToolEffect::FilesystemRead),
                        "filesystem_write" => Ok(ToolEffect::FilesystemWrite),
                        "process" => Ok(ToolEffect::Process),
                        "network" => Ok(ToolEffect::Network),
                        "destructive" => Ok(ToolEffect::Destructive),
                        other => Err(format!(
                            "tool-result TCK case {case_name} has unsupported tool effect {other}"
                        )),
                    }
                })
                .collect::<Result<Vec<_>, _>>()
        })
        .transpose()?
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
    let approval = match optional_str(raw_tool, "approval").map_or("never", |value| value) {
        "never" => ToolApproval::Never,
        "policy" => ToolApproval::Policy,
        "always" => ToolApproval::Always,
        other => {
            return Err(format!(
                "tool-result TCK case {case_name} has unsupported tool approval {other}"
            ));
        }
    };
    let idempotency =
        match optional_str(raw_tool, "idempotency").map_or("not_applicable", |value| value) {
            "not_applicable" => ToolIdempotency::NotApplicable,
            "optional" => ToolIdempotency::Optional,
            "required" => ToolIdempotency::Required,
            other => {
                return Err(format!(
                    "tool-result TCK case {case_name} has unsupported tool idempotency {other}"
                ));
            }
        };
    let result_mode = match optional_str(raw_tool, "resultMode")
        .or_else(|| optional_str(raw_tool, "result_mode"))
        .map_or("value", |value| value)
    {
        "value" => ToolResultMode::Value,
        "incremental" => ToolResultMode::Incremental,
        "bounded_sequence" => ToolResultMode::BoundedSequence,
        "artifact_reference" => ToolResultMode::ArtifactReference,
        other => {
            return Err(format!(
                "tool-result TCK case {case_name} has unsupported result mode {other}"
            ));
        }
    };
    let binding = ToolBinding::new(
        optional_str(raw_tool, "bindingId")
            .or_else(|| optional_str(raw_tool, "binding_id"))
            .map_or("binding-tool", |value| value),
        tool_name,
        ToolImplementation::Block(BlockToolImplementation::new(
            optional_str(raw_tool, "block").map_or("blocks.tool", |value| value),
        )),
    )
    .with_effects(effects)
    .with_approval(approval)
    .with_idempotency(idempotency)
    .with_result_mode(result_mode);
    let catalog = ToolCatalog::new([definition], [binding])
        .map_err(|error| format!("tool-result TCK case {case_name} catalog failed: {error:?}"))?;
    let resolved_tool = catalog
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")
        .map_err(|error| format!("tool-result TCK case {case_name} resolution failed: {error:?}"))?
        .into_iter()
        .next()
        .ok_or_else(|| format!("tool-result TCK case {case_name} did not resolve its tool"))?;

    let arguments = match case.get("arguments") {
        Some(arguments) => arguments.clone(),
        None => Value::Object(Map::new()),
    };
    let arguments_json = serde_json::to_string(&arguments).map_err(|error| {
        format!("tool-result TCK case {case_name} arguments failed to serialize: {error}")
    })?;
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", tool_name);
    draft
        .append_argument_fragment(arguments_json)
        .map_err(|error| format!("tool-result TCK case {case_name} draft failed: {error:?}"))?;
    let call = draft
        .into_completed_tool_call(&resolved_tool.resolved_tool_id, 1_000)
        .map_err(|error| {
            format!("tool-result TCK case {case_name} call finalization failed: {error:?}")
        })?;

    let schemas = case
        .get("schemas")
        .cloned()
        .map_or(Value::Null, |value| value);
    let schema_registry = super::parse_tool_schema_registry(&schemas, "tool-result TCK schemas")
        .map_err(|error| format!("tool-result TCK case {case_name}: {error}"))?;
    let raw_result = required_object(case, "result", case_name)?;
    if optional_str(raw_result, "status").map_or("completed", |value| value) != "completed" {
        return Err(format!(
            "tool-result TCK case {case_name} only supports completed result fixtures"
        ));
    }
    let raw_output = raw_result
        .get("output")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            format!("tool-result TCK case {case_name} result.output must be an array")
        })?;
    let mut output = Vec::with_capacity(raw_output.len());
    for (index, raw_part) in raw_output.iter().enumerate() {
        let raw_part = raw_part.as_object().ok_or_else(|| {
            format!("tool-result TCK case {case_name} result.output[{index}] must be an object")
        })?;
        let metadata = raw_part
            .get("metadata")
            .and_then(Value::as_object)
            .map(|metadata| {
                metadata
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect::<BTreeMap<_, _>>()
            })
            .into_iter()
            .flatten()
            .collect::<BTreeMap<_, _>>();
        let mut part = match optional_str(raw_part, "kind").map_or("text", |value| value) {
            "text" => ContentPart::text(required_str(raw_part, "text")?),
            "json" => ContentPart::json(raw_part.get("data").cloned().ok_or_else(|| {
                format!("tool-result TCK case {case_name} result.output[{index}].data is required")
            })?),
            "artifact_ref" => ContentPart {
                kind: ContentPartKind::ArtifactRef,
                text: None,
                data: Some(raw_part.get("data").cloned().ok_or_else(|| {
                    format!(
                        "tool-result TCK case {case_name} result.output[{index}].data is required"
                    )
                })?),
                metadata: BTreeMap::new(),
            },
            other => {
                return Err(format!(
                    "tool-result TCK case {case_name} result.output[{index}] has unsupported kind {other}"
                ));
            }
        };
        part.metadata = metadata;
        output.push(part);
    }
    let mut result = ToolResult::completed("call-1", output, 1_100, 1_200).map_err(|error| {
        format!("tool-result TCK case {case_name} result is not canonical: {error:?}")
    })?;
    if let Some(mutation) = raw_result
        .get("mutateAfterDigest")
        .and_then(Value::as_object)
    {
        let part_index = mutation
            .get("part")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                format!("tool-result TCK case {case_name} mutateAfterDigest.part is required")
            })? as usize;
        let part = result.output.get_mut(part_index).ok_or_else(|| {
            format!("tool-result TCK case {case_name} mutation part is out of bounds")
        })?;
        if let Some(data) = mutation.get("data") {
            part.data = Some(data.clone());
        }
    }
    let content_policy = super::parse_tool_result_content_policy(
        case.get("contentPolicy")
            .or_else(|| case.get("content_policy")),
        "tool-result TCK content policy",
    )
    .map_err(|error| format!("tool-result TCK case {case_name}: {error}"))?;

    match ToolResultValidation::prepare_for_model_with_content_policy(
        ToolResultValidationRequest {
            call: &call,
            result: &result,
            resolved_tool: &resolved_tool,
            schema_registry: &schema_registry,
        },
        &content_policy,
    ) {
        Ok(output) => Ok(observe_success(&output)),
        Err(error) => Ok(json!({
            "ok": false,
            "errorCategory": validation_error_category(&error),
        })
        .as_object()
        .cloned()
        .ok_or_else(|| format!("tool-result TCK case {case_name} result must be an object"))?),
    }
}

fn evaluate_stream_state(
    case: &Map<String, Value>,
    case_name: &str,
) -> Result<Map<String, Value>, String> {
    let operations = case
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("tool-result TCK case {case_name} requires operations"))?;
    let mut normalized = Vec::with_capacity(operations.len());
    for (index, operation) in operations.iter().enumerate() {
        let operation = operation.as_object().ok_or_else(|| {
            format!("tool-result TCK case {case_name} operations[{index}] must be an object")
        })?;
        if required_str(operation, "op")? != "accept" {
            return Err(format!(
                "tool-result TCK case {case_name} operations[{index}] requires op accept"
            ));
        }
        let mut event = operation
            .get("event")
            .and_then(Value::as_object)
            .cloned()
            .ok_or_else(|| {
                format!("tool-result TCK case {case_name} operations[{index}] requires event")
            })?;
        let event_kind = required_str(&event, "kind")?.to_owned();
        let tool_call_id = required_str(&event, "toolCallId")?.to_owned();
        if event_kind == "started" {
            event
                .entry("startedAtUnixMs".to_owned())
                .or_insert_with(|| json!(1_000));
        }
        if matches!(event_kind.as_str(), "completed" | "denied") {
            let result = event
                .get_mut("result")
                .and_then(Value::as_object_mut)
                .ok_or_else(|| {
                    format!(
                        "tool-result TCK case {case_name} operations[{index}].event requires result"
                    )
                })?;
            result
                .entry("toolCallId".to_owned())
                .or_insert_with(|| Value::String(tool_call_id));
            result
                .entry("completedAtUnixMs".to_owned())
                .or_insert_with(|| json!(1_200));
            if event_kind == "completed" {
                result
                    .entry("startedAtUnixMs".to_owned())
                    .or_insert_with(|| json!(1_100));
            } else {
                result
                    .entry("effectOutcome".to_owned())
                    .or_insert_with(|| json!("not_committed"));
                let error = result
                    .get_mut("error")
                    .and_then(Value::as_object_mut)
                    .ok_or_else(|| {
                        format!(
                            "tool-result TCK case {case_name} operations[{index}].event.result requires error"
                        )
                    })?;
                error
                    .entry("category".to_owned())
                    .or_insert_with(|| json!("policy"));
                error
                    .entry("retryable".to_owned())
                    .or_insert_with(|| Value::Bool(false));
            }
        }
        normalized.push(json!({
            "kind": "event",
            "event": event,
        }));
    }
    let operations_json = serde_json::to_string(&normalized).map_err(|error| {
        format!("tool-result TCK case {case_name} operations failed to serialize: {error}")
    })?;
    let native_json = super::evaluate_tool_result_stream_json("{}", &operations_json)
        .map_err(|error| format!("tool-result TCK case {case_name}: {error}"))?;
    let native = serde_json::from_str::<Value>(&native_json).map_err(|error| {
        format!("tool-result TCK case {case_name} native stream result is invalid: {error}")
    })?;
    let native = native.as_object().ok_or_else(|| {
        format!("tool-result TCK case {case_name} native stream result must be an object")
    })?;
    let updates = native
        .get("updates")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            format!("tool-result TCK case {case_name} native stream result requires updates")
        })?;
    let mut accepted = Vec::new();
    let mut errors = Vec::new();
    for (index, update) in updates.iter().enumerate() {
        let update = update.as_object().ok_or_else(|| {
            format!("tool-result TCK case {case_name} native update {index} must be an object")
        })?;
        match required_str(update, "kind")? {
            "accepted" => {
                let event = update
                    .get("event")
                    .and_then(Value::as_object)
                    .ok_or_else(|| {
                        format!(
                            "tool-result TCK case {case_name} native update {index} requires event"
                        )
                    })?;
                accepted.push(json!({
                    "toolCallId": required_str(event, "toolCallId")?,
                    "kind": required_str(event, "kind")?,
                }));
            }
            "error" => {
                let operation = update
                    .get("operationIndex")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| {
                        format!(
                            "tool-result TCK case {case_name} native update {index} requires operationIndex"
                        )
                    })?;
                let error = update
                    .get("error")
                    .and_then(Value::as_object)
                    .ok_or_else(|| {
                        format!(
                            "tool-result TCK case {case_name} native update {index} requires error"
                        )
                    })?;
                let code = match required_str(error, "code")? {
                    "invalid_event" => "InvalidEvent",
                    "non_monotonic_sequence" => "NonMonotonicSequence",
                    "event_after_final_result" => "EventAfterFinalResult",
                    "duplicate_started" => "DuplicateStarted",
                    "event_before_started" => "EventBeforeStarted",
                    other => {
                        return Err(format!(
                            "tool-result TCK case {case_name} native update {index} has unknown error {other}"
                        ));
                    }
                };
                errors.push(json!({
                    "operation": operation,
                    "code": code,
                }));
            }
            other => {
                return Err(format!(
                    "tool-result TCK case {case_name} native update {index} has unknown kind {other}"
                ));
            }
        }
    }

    let state = native
        .get("state")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            format!("tool-result TCK case {case_name} native stream result requires state")
        })?;
    let last_sequences = state.get("lastSequences").cloned().ok_or_else(|| {
        format!("tool-result TCK case {case_name} native stream state requires lastSequences")
    })?;
    let final_results = state
        .get("finalResults")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            format!("tool-result TCK case {case_name} native stream state requires finalResults")
        })?;
    let mut final_statuses = Map::new();
    for (tool_call_id, result) in final_results {
        let status = result
            .get("status")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                format!(
                    "tool-result TCK case {case_name} native final result {tool_call_id} requires status"
                )
            })?;
        final_statuses.insert(tool_call_id.clone(), Value::String(status.to_owned()));
    }
    json!({
        "accepted": accepted,
        "errors": errors,
        "finalStatuses": final_statuses,
        "lastSequences": last_sequences,
    })
    .as_object()
    .cloned()
    .ok_or_else(|| format!("tool-result TCK case {case_name} result must be an object"))
}

fn observe_success(output: &[ContentPart]) -> Map<String, Value> {
    let output_kinds = output
        .iter()
        .map(|part| {
            Value::String(
                match part.kind {
                    ContentPartKind::Text => "text",
                    ContentPartKind::Json => "json",
                    ContentPartKind::ArtifactRef => "artifact_ref",
                }
                .to_owned(),
            )
        })
        .collect::<Vec<_>>();
    let texts = output
        .iter()
        .filter_map(|part| part.text.clone().map(Value::String))
        .collect::<Vec<_>>();
    let json_outputs = output
        .iter()
        .filter(|part| part.kind == ContentPartKind::Json)
        .filter_map(|part| part.data.clone())
        .collect::<Vec<_>>();
    let trust_designations = output
        .iter()
        .map(|part| {
            part.metadata
                .get("trust_designation")
                .cloned()
                .map_or(Value::Null, |value| value)
        })
        .collect::<Vec<_>>();
    let prompt_injection_labels = output
        .iter()
        .map(|part| {
            part.metadata
                .get("prompt_injection_label")
                .cloned()
                .map_or(Value::Null, |value| value)
        })
        .collect::<Vec<_>>();
    let content_classifications = output
        .iter()
        .map(|part| {
            part.metadata
                .get("content_classification")
                .cloned()
                .map_or(Value::Null, |value| value)
        })
        .collect::<Vec<_>>();
    let captures = output
        .iter()
        .filter_map(|part| part.metadata.get("capture"))
        .filter_map(Value::as_object)
        .collect::<Vec<_>>();
    let capture_modes = captures
        .iter()
        .filter_map(|capture| capture.get("mode").cloned())
        .collect::<Vec<_>>();
    let redaction_counts = captures
        .iter()
        .filter_map(|capture| capture.get("redaction_count").cloned())
        .collect::<Vec<_>>();
    Map::from_iter([
        ("ok".to_owned(), Value::Bool(true)),
        ("outputKinds".to_owned(), Value::Array(output_kinds)),
        ("texts".to_owned(), Value::Array(texts)),
        ("jsonOutputs".to_owned(), Value::Array(json_outputs)),
        (
            "trustDesignations".to_owned(),
            Value::Array(trust_designations),
        ),
        (
            "promptInjectionLabels".to_owned(),
            Value::Array(prompt_injection_labels),
        ),
        (
            "contentClassifications".to_owned(),
            Value::Array(content_classifications),
        ),
        ("captureModes".to_owned(), Value::Array(capture_modes)),
        ("redactionCounts".to_owned(), Value::Array(redaction_counts)),
    ])
}

fn validation_error_category(error: &ToolResultValidationError) -> &'static str {
    match error {
        ToolResultValidationError::InvalidToolResult { .. } => "invalid_tool_result",
        ToolResultValidationError::ToolCallMismatch { .. } => "tool_call_mismatch",
        ToolResultValidationError::ResolvedToolMismatch { .. } => "resolved_tool_mismatch",
        ToolResultValidationError::OutputSchemaMissing { .. } => "output_schema_missing",
        ToolResultValidationError::OutputContentMissing { .. } => "output_content_missing",
        ToolResultValidationError::OutputContentAmbiguous { .. } => "output_content_ambiguous",
        ToolResultValidationError::OutputSchemaInvalid { .. } => "output_schema_invalid",
        ToolResultValidationError::RequiredOutputMissing { .. } => "required_output_missing",
        ToolResultValidationError::OutputDigestMissing { .. } => "output_digest_missing",
        ToolResultValidationError::OutputDigestMismatch { .. } => "output_digest_mismatch",
        ToolResultValidationError::ModelOutputTooLarge { .. } => "model_output_too_large",
        ToolResultValidationError::ModelOutputRedactionInvalid { .. } => {
            "model_output_redaction_invalid"
        }
        ToolResultValidationError::ModelOutputLabelInvalid { .. } => "model_output_label_invalid",
        ToolResultValidationError::InlineOutputForbiddenForArtifactReference { .. } => {
            "inline_output_forbidden_for_artifact_reference"
        }
    }
}

fn required_object<'a>(
    value: &'a Map<String, Value>,
    key: &str,
    case_name: &str,
) -> Result<&'a Map<String, Value>, String> {
    value
        .get(key)
        .and_then(Value::as_object)
        .ok_or_else(|| format!("tool-result TCK case {case_name} requires object {key}"))
}

fn required_str<'a>(value: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("tool-result TCK case requires string {key}"))
}

fn optional_str<'a>(value: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}
