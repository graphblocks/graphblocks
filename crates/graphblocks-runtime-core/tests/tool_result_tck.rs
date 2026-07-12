use std::collections::{BTreeMap, BTreeSet};

use graphblocks_runtime_core::observability::CaptureDecision;
use graphblocks_runtime_core::outcome::{BlockError, ErrorCategory};
use graphblocks_runtime_core::output_policy::RedactionInstruction;
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ToolApproval, ToolBinding, ToolCatalog, ToolDefinition, ToolEffect,
    ToolIdempotency, ToolImplementation, ToolResolutionScope, ToolResultMode,
};
use graphblocks_runtime_core::tool_call::ToolCallDraft;
use graphblocks_runtime_core::tool_result::{
    ContentPart, ContentPartKind, ToolEffectOutcome, ToolResult, ToolResultContentPolicy,
    ToolResultEvent, ToolResultStreamError, ToolResultStreamState, ToolResultValidation,
    ToolResultValidationError, ToolResultValidationRequest,
};
use graphblocks_runtime_core::tool_schema::{JsonSchema, JsonSchemaNode, ToolSchemaRegistry};
use serde_json::{Map, Value, json};

#[test]
fn rust_tool_result_validation_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("fixtures/tool-result-cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "tool-result TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let case_name = required_str(case, "name")?;
    match required_str(case, "kind")? {
        "prepare_for_model" => run_prepare_for_model_case(case_name, case),
        "stream_state" => run_stream_state_case(case_name, case),
        other => Err(format!(
            "tool-result TCK case {case_name} has unknown kind {other}"
        )),
    }
}

fn run_prepare_for_model_case(case_name: &str, case: &Value) -> Result<(), String> {
    let raw_tool = required_object(case, "tool", case_name)?;
    let tool_name = required_map_str(raw_tool, "name")?;
    let mut definition = ToolDefinition::new(
        tool_name,
        optional_map_str(raw_tool, "description").unwrap_or("Execute a tool."),
        optional_map_str(raw_tool, "inputSchema")
            .or_else(|| optional_map_str(raw_tool, "input_schema"))
            .unwrap_or("schemas/ToolRequest@1"),
    );
    if let Some(output_schema) = optional_map_str(raw_tool, "outputSchema")
        .or_else(|| optional_map_str(raw_tool, "output_schema"))
    {
        definition = definition.with_output_schema(output_schema);
    }

    let binding = ToolBinding::new(
        optional_map_str(raw_tool, "bindingId")
            .or_else(|| optional_map_str(raw_tool, "binding_id"))
            .unwrap_or("binding-tool"),
        tool_name,
        ToolImplementation::Block(BlockToolImplementation::new(
            optional_map_str(raw_tool, "block").unwrap_or("blocks.tool"),
        )),
    )
    .with_effects(tool_effects_from_fixture(raw_tool)?)
    .with_approval(tool_approval_from_fixture(
        optional_map_str(raw_tool, "approval").unwrap_or("never"),
    )?)
    .with_idempotency(tool_idempotency_from_fixture(
        optional_map_str(raw_tool, "idempotency").unwrap_or("not_applicable"),
    )?)
    .with_result_mode(tool_result_mode_from_fixture(
        optional_map_str(raw_tool, "resultMode")
            .or_else(|| optional_map_str(raw_tool, "result_mode"))
            .unwrap_or("value"),
    )?);
    let catalog = ToolCatalog::new([definition], [binding])
        .map_err(|error| format!("tool-result TCK case {case_name} catalog failed: {error:?}"))?;
    let mut resolved = catalog
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")
        .map_err(|error| {
            format!("tool-result TCK case {case_name} resolution failed: {error:?}")
        })?;
    let resolved_tool = resolved.remove(0);

    let arguments = case
        .get("arguments")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", tool_name);
    draft
        .append_argument_fragment(arguments.to_string())
        .map_err(|error| format!("tool-result TCK case {case_name} draft failed: {error:?}"))?;
    let call = draft
        .into_completed_tool_call(&resolved_tool.resolved_tool_id, 1_000)
        .map_err(|error| {
            format!("tool-result TCK case {case_name} call finalization failed: {error:?}")
        })?;

    let schema_registry = schema_registry_from_fixture(case, case_name)?;
    let result = tool_result_from_fixture(case, case_name)?;
    let content_policy = content_policy_from_fixture(case)?;
    let output = ToolResultValidation::prepare_for_model_with_content_policy(
        ToolResultValidationRequest {
            call: &call,
            result: &result,
            resolved_tool: &resolved_tool,
            schema_registry: &schema_registry,
        },
        &content_policy,
    );
    let observed = match output {
        Ok(output) => observed_success(output),
        Err(error) => observed_error(&error),
    };
    let expected = expected(case, case_name)?;

    for (key, expected_value) in expected {
        if key == "errorContains" {
            let error = observed.get("error").and_then(Value::as_str).unwrap_or("");
            assert!(
                error.contains(expected_value.as_str().unwrap_or_default()),
                "{case_name}: expected error {error:?} to contain {expected_value:?}",
            );
        } else {
            assert_eq!(
                observed.get(key).unwrap_or(&Value::Null),
                expected_value,
                "{case_name}: observed {key} did not match",
            );
        }
    }
    if !expected.contains_key("errorContains") {
        assert!(
            observed.get("error").is_none(),
            "{case_name}: unexpected error {:?}",
            observed.get("error"),
        );
    }
    Ok(())
}

fn run_stream_state_case(case_name: &str, case: &Value) -> Result<(), String> {
    let operations = case
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("tool-result TCK case {case_name} operations must be an array"))?;
    let mut stream = ToolResultStreamState::new();
    let mut accepted = Vec::new();
    let mut errors = Vec::new();
    let mut tool_call_ids = BTreeSet::new();

    for (operation_index, operation) in operations.iter().enumerate() {
        let operation = operation.as_object().ok_or_else(|| {
            format!(
                "tool-result TCK case {case_name} operations[{operation_index}] must be an object"
            )
        })?;
        let raw_event = operation
            .get("event")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                format!(
                    "tool-result TCK case {case_name} operations[{operation_index}] missing event"
                )
            })?;
        let event = tool_result_event_from_fixture(raw_event)?;
        tool_call_ids.insert(event.tool_call_id().to_owned());
        match stream.accept(event) {
            Ok(accepted_event) => accepted.push(json!({
                "toolCallId": accepted_event.tool_call_id(),
                "kind": tool_result_event_kind(&accepted_event),
            })),
            Err(error) => errors.push(json!({
                "operation": operation_index,
                "code": tool_result_stream_error_code(&error),
            })),
        }
    }

    let final_statuses = tool_call_ids
        .iter()
        .filter_map(|tool_call_id| {
            stream.final_result_for(tool_call_id).map(|result| {
                (
                    tool_call_id.clone(),
                    Value::String(tool_result_status(&result.status).to_owned()),
                )
            })
        })
        .collect::<Map<_, _>>();
    let last_sequences = tool_call_ids
        .iter()
        .filter_map(|tool_call_id| {
            stream
                .last_sequence_for(tool_call_id)
                .map(|sequence| (tool_call_id.clone(), Value::Number(sequence.into())))
        })
        .collect::<Map<_, _>>();
    let observed = Value::Object(Map::from_iter([
        ("accepted".to_owned(), Value::Array(accepted)),
        ("errors".to_owned(), Value::Array(errors)),
        ("finalStatuses".to_owned(), Value::Object(final_statuses)),
        ("lastSequences".to_owned(), Value::Object(last_sequences)),
    ]));
    let expected = expected(case, case_name)?;

    for (key, expected_value) in expected {
        assert_eq!(
            observed.get(key).unwrap_or(&Value::Null),
            expected_value,
            "{case_name}: observed {key} did not match",
        );
    }
    Ok(())
}

fn schema_registry_from_fixture(
    case: &Value,
    case_name: &str,
) -> Result<ToolSchemaRegistry, String> {
    let Some(raw_schemas) = case.get("schemas") else {
        return ToolSchemaRegistry::new(Vec::<JsonSchema>::new())
            .map_err(|error| format!("tool-result TCK case {case_name} schema failed: {error:?}"));
    };
    let raw_schemas = raw_schemas
        .as_array()
        .ok_or_else(|| format!("tool-result TCK case {case_name} schemas must be an array"))?;
    let mut schemas = Vec::new();
    for (index, raw_schema) in raw_schemas.iter().enumerate() {
        let raw_schema = raw_schema.as_object().ok_or_else(|| {
            format!("tool-result TCK case {case_name} schemas[{index}] must be an object")
        })?;
        let schema_id = required_map_str(raw_schema, "schemaId")
            .or_else(|_| required_map_str(raw_schema, "schema_id"))?;
        let root = raw_schema
            .get("root")
            .cloned()
            .unwrap_or_else(|| Value::Object(raw_schema.clone()));
        schemas.push(JsonSchema::new(schema_id, schema_node_from_fixture(&root)));
    }
    ToolSchemaRegistry::new(schemas)
        .map_err(|error| format!("tool-result TCK case {case_name} schema failed: {error:?}"))
}

fn schema_node_from_fixture(raw_node: &Value) -> JsonSchemaNode {
    let Some(raw_node) = raw_node.as_object() else {
        return JsonSchemaNode::any();
    };
    let mut node = match raw_node
        .get("type")
        .or_else(|| raw_node.get("expectedType"))
        .or_else(|| raw_node.get("expected_type"))
        .and_then(Value::as_str)
    {
        Some("string") => JsonSchemaNode::string(),
        Some("integer") => JsonSchemaNode::integer(),
        Some("number") => JsonSchemaNode::number(),
        Some("boolean") => JsonSchemaNode::boolean(),
        Some("array") => JsonSchemaNode::array(
            raw_node
                .get("items")
                .map(schema_node_from_fixture)
                .unwrap_or_else(JsonSchemaNode::any),
        ),
        Some("object") => JsonSchemaNode::object(),
        _ => JsonSchemaNode::any(),
    };
    let required = raw_node
        .get("required")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).collect::<Vec<_>>())
        .unwrap_or_default();
    if let Some(properties) = raw_node.get("properties").and_then(Value::as_object) {
        for (property, property_schema) in properties {
            if required.contains(&property.as_str()) {
                node = node.required_property(property, schema_node_from_fixture(property_schema));
            } else {
                node = node.property(property, schema_node_from_fixture(property_schema));
            }
        }
    }
    node
}

fn tool_result_from_fixture(case: &Value, case_name: &str) -> Result<ToolResult, String> {
    let raw_result = required_object(case, "result", case_name)?;
    if optional_map_str(raw_result, "status").unwrap_or("completed") != "completed" {
        return Err(format!(
            "tool-result TCK case {case_name} only supports completed result fixtures"
        ));
    }
    let raw_output = raw_result
        .get("output")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            format!("tool-result TCK case {case_name} result output must be an array")
        })?;
    let output = raw_output
        .iter()
        .map(content_part_from_fixture)
        .collect::<Result<Vec<_>, _>>()?;
    let mut result = ToolResult::completed("call-1", output, 1_100, 1_200);

    if let Some(mutation) = raw_result
        .get("mutateAfterDigest")
        .and_then(Value::as_object)
    {
        let part_index = mutation
            .get("part")
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                format!("tool-result TCK case {case_name} mutateAfterDigest requires part")
            })? as usize;
        let part = result.output.get_mut(part_index).ok_or_else(|| {
            format!("tool-result TCK case {case_name} mutation part index is out of bounds")
        })?;
        if let Some(data) = mutation.get("data") {
            part.data = Some(data.clone());
        }
    }

    Ok(result)
}

fn tool_result_event_from_fixture(
    raw_event: &Map<String, Value>,
) -> Result<ToolResultEvent, String> {
    let kind = required_map_str(raw_event, "kind")?;
    let tool_call_id = optional_map_str(raw_event, "toolCallId")
        .or_else(|| optional_map_str(raw_event, "tool_call_id"))
        .ok_or_else(|| "tool-result event requires toolCallId".to_owned())?;
    let sequence = required_map_u64(raw_event, "sequence")?;
    match kind {
        "started" => Ok(ToolResultEvent::started(
            tool_call_id,
            sequence,
            optional_map_u64(raw_event, "startedAtUnixMs")
                .or_else(|| optional_map_u64(raw_event, "started_at_unix_ms"))
                .unwrap_or(1_000),
        )),
        "delta" => {
            let output = raw_event
                .get("output")
                .and_then(Value::as_array)
                .ok_or_else(|| "tool-result delta event output must be an array".to_owned())?
                .iter()
                .map(content_part_from_fixture)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(ToolResultEvent::delta(tool_call_id, sequence, output))
        }
        "completed" => {
            let raw_result = raw_event
                .get("result")
                .and_then(Value::as_object)
                .ok_or_else(|| "tool-result completed event requires result".to_owned())?;
            let output = raw_result
                .get("output")
                .and_then(Value::as_array)
                .ok_or_else(|| "tool-result completed result output must be an array".to_owned())?
                .iter()
                .map(content_part_from_fixture)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(ToolResultEvent::completed(
                tool_call_id,
                sequence,
                ToolResult::completed(
                    tool_call_id,
                    output,
                    optional_map_u64(raw_result, "startedAtUnixMs")
                        .or_else(|| optional_map_u64(raw_result, "started_at_unix_ms"))
                        .unwrap_or(1_100),
                    optional_map_u64(raw_result, "completedAtUnixMs")
                        .or_else(|| optional_map_u64(raw_result, "completed_at_unix_ms"))
                        .unwrap_or(1_200),
                ),
            ))
        }
        "denied" => {
            let raw_result = raw_event
                .get("result")
                .and_then(Value::as_object)
                .ok_or_else(|| "tool-result denied event requires result".to_owned())?;
            let raw_error = raw_result
                .get("error")
                .and_then(Value::as_object)
                .ok_or_else(|| "tool-result denied result requires error".to_owned())?;
            let mut result = ToolResult::denied(
                tool_call_id,
                BlockError::new(
                    optional_map_str(raw_error, "code").unwrap_or("tool.denied"),
                    ErrorCategory::Policy,
                    optional_map_str(raw_error, "message").unwrap_or("tool denied"),
                    false,
                ),
                optional_map_u64(raw_result, "completedAtUnixMs")
                    .or_else(|| optional_map_u64(raw_result, "completed_at_unix_ms"))
                    .unwrap_or(1_200),
            );
            if let Some(effect_outcome) = optional_map_str(raw_result, "effectOutcome")
                .or_else(|| optional_map_str(raw_result, "effect_outcome"))
            {
                result =
                    result.with_effect_outcome(tool_effect_outcome_from_fixture(effect_outcome)?);
            }
            Ok(ToolResultEvent::denied(tool_call_id, sequence, result))
        }
        other => Err(format!("unsupported tool-result stream event kind {other}")),
    }
}

fn content_part_from_fixture(raw_part: &Value) -> Result<ContentPart, String> {
    let raw_part = raw_part
        .as_object()
        .ok_or_else(|| "tool-result output part must be an object".to_owned())?;
    let metadata = raw_part
        .get("metadata")
        .and_then(Value::as_object)
        .map(|metadata| {
            metadata
                .iter()
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect::<BTreeMap<_, _>>()
        })
        .unwrap_or_default();
    match optional_map_str(raw_part, "kind").unwrap_or("text") {
        "text" => {
            let text = required_map_str(raw_part, "text")?;
            Ok(ContentPart::text(text).with_metadata_map(metadata))
        }
        "json" => {
            let data = raw_part
                .get("data")
                .cloned()
                .ok_or_else(|| "tool-result json output part requires data".to_owned())?;
            Ok(ContentPart::json(data).with_metadata_map(metadata))
        }
        "artifact_ref" => {
            let data = raw_part
                .get("data")
                .cloned()
                .ok_or_else(|| "tool-result artifact_ref output part requires data".to_owned())?;
            Ok(ContentPart {
                kind: ContentPartKind::ArtifactRef,
                text: None,
                data: Some(data),
                metadata,
            })
        }
        other => Err(format!(
            "tool-result output part has unsupported kind {other}"
        )),
    }
}

fn content_policy_from_fixture(case: &Value) -> Result<ToolResultContentPolicy, String> {
    let content_policy = case
        .get("contentPolicy")
        .or_else(|| case.get("content_policy"))
        .and_then(Value::as_object);
    let Some(content_policy) = content_policy else {
        return Ok(ToolResultContentPolicy::new());
    };

    let mut policy = ToolResultContentPolicy::new();
    if let Some(max_output_bytes) = content_policy.get("maxOutputBytes").and_then(Value::as_u64) {
        policy = policy.with_max_output_bytes(max_output_bytes as usize);
    }
    if let Some(redactions) = content_policy.get("redactions").and_then(Value::as_array) {
        let redactions = redactions
            .iter()
            .map(redaction_from_fixture)
            .collect::<Result<Vec<_>, _>>()?;
        policy = policy.with_redactions(redactions);
    }
    if let Some(capture_policy) = content_policy
        .get("capturePolicy")
        .or_else(|| content_policy.get("capture_policy"))
        .and_then(Value::as_object)
    {
        policy = policy.with_capture_decision(capture_decision_from_fixture(capture_policy)?);
    }
    policy = policy.with_model_output_labels(
        optional_map_str(content_policy, "trustDesignation").unwrap_or("untrusted_external"),
        optional_map_str(content_policy, "promptInjectionLabel").unwrap_or("untrusted_tool_output"),
        optional_map_str(content_policy, "contentClassification").unwrap_or("external_tool_output"),
    );
    Ok(policy)
}

fn redaction_from_fixture(redaction: &Value) -> Result<RedactionInstruction, String> {
    let redaction = redaction
        .as_object()
        .ok_or_else(|| "tool-result redaction must be an object".to_owned())?;
    Ok(RedactionInstruction::text_range(
        required_map_str(redaction, "path")?,
        required_map_u64(redaction, "start")?,
        required_map_u64(redaction, "end")?,
        optional_map_str(redaction, "replacement").unwrap_or("[redacted]"),
    ))
}

fn capture_decision_from_fixture(
    capture_policy: &Map<String, Value>,
) -> Result<CaptureDecision, String> {
    let retention_policy = optional_map_str(capture_policy, "retention_policy")
        .or_else(|| optional_map_str(capture_policy, "retentionPolicy"))
        .unwrap_or("default");
    let mut decision = match optional_map_str(capture_policy, "mode").unwrap_or("hash_only") {
        "none" => CaptureDecision::none(retention_policy),
        "hash_only" => CaptureDecision::hash_only(retention_policy),
        "reference_only" => CaptureDecision::reference_only(retention_policy),
        "redacted_preview" => CaptureDecision::redacted_preview(retention_policy),
        "full" => CaptureDecision::full(retention_policy),
        other => return Err(format!("unsupported capture policy mode {other}")),
    };
    if let Some(consent_ref) = optional_map_str(capture_policy, "consent_ref")
        .or_else(|| optional_map_str(capture_policy, "consentRef"))
    {
        decision = decision.with_consent_ref(consent_ref);
    }
    Ok(decision)
}

fn observed_success(output: Vec<ContentPart>) -> Value {
    let output_kinds = output
        .iter()
        .map(|part| Value::String(content_part_kind_str(part.kind).to_owned()))
        .collect::<Vec<_>>();
    let texts = output
        .iter()
        .filter_map(|part| part.text.as_ref())
        .map(|text| Value::String(text.clone()))
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
                .unwrap_or(Value::Null)
        })
        .collect::<Vec<_>>();
    let prompt_injection_labels = output
        .iter()
        .map(|part| {
            part.metadata
                .get("prompt_injection_label")
                .cloned()
                .unwrap_or(Value::Null)
        })
        .collect::<Vec<_>>();
    let content_classifications = output
        .iter()
        .map(|part| {
            part.metadata
                .get("content_classification")
                .cloned()
                .unwrap_or(Value::Null)
        })
        .collect::<Vec<_>>();
    let capture_metadata = output
        .iter()
        .filter_map(|part| part.metadata.get("capture"))
        .filter_map(Value::as_object)
        .collect::<Vec<_>>();
    let capture_modes = capture_metadata
        .iter()
        .filter_map(|capture| capture.get("mode").cloned())
        .collect::<Vec<_>>();
    let redaction_counts = capture_metadata
        .iter()
        .filter_map(|capture| capture.get("redaction_count").cloned())
        .collect::<Vec<_>>();

    Value::Object(Map::from_iter([
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
    ]))
}

fn observed_error(error: &ToolResultValidationError) -> Value {
    Value::Object(Map::from_iter([
        ("ok".to_owned(), Value::Bool(false)),
        (
            "error".to_owned(),
            Value::String(validation_error_text(error)),
        ),
    ]))
}

fn validation_error_text(error: &ToolResultValidationError) -> String {
    match error {
        ToolResultValidationError::InvalidToolResult { source } => {
            format!("invalid tool result: {source:?}")
        }
        ToolResultValidationError::ToolCallMismatch { expected, actual } => {
            format!("tool result call mismatch: expected {expected}, got {actual}")
        }
        ToolResultValidationError::ResolvedToolMismatch { expected, actual } => {
            format!("resolved tool mismatch: expected {expected}, got {actual}")
        }
        ToolResultValidationError::OutputSchemaMissing { schema_id } => {
            format!("output schema {schema_id} is not registered")
        }
        ToolResultValidationError::OutputContentMissing { tool_call_id } => {
            format!("tool result {tool_call_id} output content is missing")
        }
        ToolResultValidationError::OutputContentAmbiguous {
            tool_call_id,
            count,
        } => {
            format!("tool result {tool_call_id} output content is ambiguous: {count} parts")
        }
        ToolResultValidationError::OutputSchemaInvalid {
            schema_id,
            path,
            expected,
            ..
        } => {
            format!("{schema_id} expected {expected} at {path}")
        }
        ToolResultValidationError::RequiredOutputMissing {
            schema_id,
            path,
            property,
            ..
        } => {
            format!("{schema_id} missing required property {property} at {path}")
        }
        ToolResultValidationError::OutputDigestMissing { tool_call_id } => {
            format!("tool result {tool_call_id} output digest is missing")
        }
        ToolResultValidationError::OutputDigestMismatch { tool_call_id } => {
            format!("tool result {tool_call_id} output digest does not match output")
        }
        ToolResultValidationError::ModelOutputTooLarge {
            tool_call_id,
            max_bytes,
            actual_bytes,
        } => {
            format!("tool result {tool_call_id} output exceeds {max_bytes} bytes: {actual_bytes}")
        }
        ToolResultValidationError::ModelOutputRedactionInvalid { tool_call_id, path } => {
            format!("tool result {tool_call_id} has invalid redaction at {path}")
        }
        ToolResultValidationError::ModelOutputLabelInvalid { field } => {
            format!("tool result model output label {field} must not be empty")
        }
        ToolResultValidationError::InlineOutputForbiddenForArtifactReference { tool_call_id } => {
            format!(
                "tool result {tool_call_id} uses artifact_reference mode but contains inline output"
            )
        }
    }
}

fn tool_effects_from_fixture(tool: &Map<String, Value>) -> Result<Vec<ToolEffect>, String> {
    tool.get("effects")
        .and_then(Value::as_array)
        .map(|effects| {
            effects
                .iter()
                .map(|effect| {
                    let effect = effect
                        .as_str()
                        .ok_or_else(|| "tool effect must be a string".to_owned())?;
                    tool_effect_from_fixture(effect)
                })
                .collect()
        })
        .unwrap_or_else(|| Ok(Vec::new()))
}

fn tool_effect_from_fixture(effect: &str) -> Result<ToolEffect, String> {
    match effect {
        "none" => Ok(ToolEffect::None),
        "external_read" => Ok(ToolEffect::ExternalRead),
        "external_write" => Ok(ToolEffect::ExternalWrite),
        "filesystem_read" => Ok(ToolEffect::FilesystemRead),
        "filesystem_write" => Ok(ToolEffect::FilesystemWrite),
        "process" => Ok(ToolEffect::Process),
        "network" => Ok(ToolEffect::Network),
        "destructive" => Ok(ToolEffect::Destructive),
        other => Err(format!("unsupported tool effect {other}")),
    }
}

fn tool_approval_from_fixture(approval: &str) -> Result<ToolApproval, String> {
    match approval {
        "never" => Ok(ToolApproval::Never),
        "policy" => Ok(ToolApproval::Policy),
        "always" => Ok(ToolApproval::Always),
        other => Err(format!("unsupported tool approval {other}")),
    }
}

fn tool_idempotency_from_fixture(idempotency: &str) -> Result<ToolIdempotency, String> {
    match idempotency {
        "not_applicable" => Ok(ToolIdempotency::NotApplicable),
        "optional" => Ok(ToolIdempotency::Optional),
        "required" => Ok(ToolIdempotency::Required),
        other => Err(format!("unsupported tool idempotency {other}")),
    }
}

fn tool_result_mode_from_fixture(result_mode: &str) -> Result<ToolResultMode, String> {
    match result_mode {
        "value" => Ok(ToolResultMode::Value),
        "incremental" => Ok(ToolResultMode::Incremental),
        "bounded_sequence" => Ok(ToolResultMode::BoundedSequence),
        "artifact_reference" => Ok(ToolResultMode::ArtifactReference),
        other => Err(format!("unsupported tool result mode {other}")),
    }
}

fn tool_effect_outcome_from_fixture(effect_outcome: &str) -> Result<ToolEffectOutcome, String> {
    match effect_outcome {
        "no_external_effect" => Ok(ToolEffectOutcome::NoExternalEffect),
        "committed" => Ok(ToolEffectOutcome::Committed),
        "not_committed" => Ok(ToolEffectOutcome::NotCommitted),
        "unknown" => Ok(ToolEffectOutcome::Unknown),
        other => Err(format!("unsupported tool effect outcome {other}")),
    }
}

fn content_part_kind_str(kind: ContentPartKind) -> &'static str {
    match kind {
        ContentPartKind::Text => "text",
        ContentPartKind::Json => "json",
        ContentPartKind::ArtifactRef => "artifact_ref",
    }
}

fn tool_result_event_kind(event: &ToolResultEvent) -> &'static str {
    match event {
        ToolResultEvent::Started { .. } => "started",
        ToolResultEvent::Delta { .. } => "delta",
        ToolResultEvent::ArtifactReady { .. } => "artifact_ready",
        ToolResultEvent::Completed { .. } => "completed",
        ToolResultEvent::Failed { .. } => "failed",
        ToolResultEvent::Denied { .. } => "denied",
        ToolResultEvent::Cancelled { .. } => "cancelled",
        ToolResultEvent::PolicyStopped { .. } => "policy_stopped",
        ToolResultEvent::Incomplete { .. } => "incomplete",
    }
}

fn tool_result_status(
    status: &graphblocks_runtime_core::tool_result::ToolResultStatus,
) -> &'static str {
    match status {
        graphblocks_runtime_core::tool_result::ToolResultStatus::Completed => "completed",
        graphblocks_runtime_core::tool_result::ToolResultStatus::Failed => "failed",
        graphblocks_runtime_core::tool_result::ToolResultStatus::Denied => "denied",
        graphblocks_runtime_core::tool_result::ToolResultStatus::Cancelled => "cancelled",
        graphblocks_runtime_core::tool_result::ToolResultStatus::PolicyStopped => "policy_stopped",
        graphblocks_runtime_core::tool_result::ToolResultStatus::Incomplete => "incomplete",
    }
}

fn tool_result_stream_error_code(error: &ToolResultStreamError) -> &'static str {
    match error {
        ToolResultStreamError::InvalidEvent { .. } => "InvalidEvent",
        ToolResultStreamError::NonMonotonicSequence { .. } => "NonMonotonicSequence",
        ToolResultStreamError::EventAfterFinalResult { .. } => "EventAfterFinalResult",
        ToolResultStreamError::DuplicateStarted { .. } => "DuplicateStarted",
        ToolResultStreamError::EventBeforeStarted { .. } => "EventBeforeStarted",
    }
}

trait ContentPartMetadataExt {
    fn with_metadata_map(self, metadata: BTreeMap<String, Value>) -> Self;
}

impl ContentPartMetadataExt for ContentPart {
    fn with_metadata_map(mut self, metadata: BTreeMap<String, Value>) -> Self {
        self.metadata = metadata;
        self
    }
}

fn expected<'a>(case: &'a Value, case_name: &str) -> Result<&'a Map<String, Value>, String> {
    case.get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("tool-result TCK case {case_name} missing expected"))
}

fn required_object<'a>(
    value: &'a Value,
    key: &str,
    case_name: &str,
) -> Result<&'a Map<String, Value>, String> {
    value
        .get(key)
        .and_then(Value::as_object)
        .ok_or_else(|| format!("tool-result TCK case {case_name} missing object field {key}"))
}

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing required string field {key}"))
}

fn required_map_str<'a>(value: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing required string field {key}"))
}

fn optional_map_str<'a>(value: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn optional_map_u64(value: &Map<String, Value>, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}

fn required_map_u64(value: &Map<String, Value>, key: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("missing required u64 field {key}"))
}
