use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ResolvedTool, ToolApproval, ToolBinding, ToolCatalog, ToolDefinition,
    ToolEffect, ToolIdempotency, ToolImplementation, ToolResolutionScope,
};
use graphblocks_runtime_core::tool_admission::{
    ToolAdmission, ToolAdmissionError, ToolAdmissionRequest,
};
use graphblocks_runtime_core::tool_approval::{ToolApprovalRecord, ToolApprovalRequest};
use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft, ToolCallStatus};
use graphblocks_runtime_core::tool_schema::{
    JsonSchema, JsonSchemaNode, ToolSchemaRegistry, ToolSchemaRegistryError,
};

fn resolved_process_tool() -> ResolvedTool {
    let catalog = ToolCatalog::new(
        [ToolDefinition::new(
            "process.run",
            "Run an approved process.",
            "schemas/ProcessRun@1",
        )],
        [ToolBinding::new(
            "binding-process",
            "process.run",
            ToolImplementation::Block(BlockToolImplementation::new("blocks.process")),
        )
        .with_effects([ToolEffect::Process])
        .with_approval(ToolApproval::Always)
        .with_idempotency(ToolIdempotency::Required)],
    )
    .expect("catalog is valid");
    let mut resolved = catalog
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")
        .expect("tool resolves");
    resolved.remove(0)
}

fn process_call(resolved_tool: &ResolvedTool) -> ToolCall {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "process.run");
    draft
        .append_argument_fragment("{\"cmd\":[\"echo\",\"hello\"]}")
        .expect("fragment accepted");
    draft
        .into_completed_tool_call(&resolved_tool.resolved_tool_id, 1_000)
        .expect("tool call is valid")
}

fn process_schema_registry() -> ToolSchemaRegistry {
    ToolSchemaRegistry::new([JsonSchema::new(
        "schemas/ProcessRun@1",
        JsonSchemaNode::object()
            .required_property("cmd", JsonSchemaNode::array(JsonSchemaNode::string())),
    )])
    .expect("schema registry is valid")
}

#[test]
fn admission_requires_valid_approval_when_binding_requires_it() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call: call.clone(),
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::ApprovalRequired {
            tool_call_id: "call-1".to_owned()
        }),
    );

    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved_tool, &call, "user-1", 1_100, 2_000)
            .expect("approval request is valid");
    let approval = ToolApprovalRecord::approve(request, "admin-1", 1_150);
    let admitted = ToolAdmission::admit(ToolAdmissionRequest {
        call,
        resolved_tool: &resolved_tool,
        schema_registry: &schemas,
        approval: Some(&approval),
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    })
    .expect("approved call admits");

    assert_eq!(admitted.call.status, ToolCallStatus::Admitted);
    assert_eq!(admitted.call.admitted_at_unix_ms, Some(1_200));
    assert_eq!(admitted.idempotency_key.as_deref(), Some("idem-1"));
}

#[test]
fn admission_rejects_required_idempotency_without_key() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved_tool, &call, "user-1", 1_100, 2_000)
            .expect("approval request is valid");
    let approval = ToolApprovalRecord::approve(request, "admin-1", 1_150);

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            approval: Some(&approval),
            principal_id: "user-1",
            idempotency_key: None,
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::IdempotencyKeyRequired {
            tool_call_id: "call-1".to_owned()
        }),
    );
}

#[test]
fn admission_rejects_call_for_different_resolved_tool() {
    let resolved_tool = resolved_process_tool();
    let mut call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    call.resolved_tool_id = "sha256:other".to_owned();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::ResolvedToolMismatch {
            expected: resolved_tool.resolved_tool_id,
            actual: "sha256:other".to_owned()
        }),
    );
}

#[test]
fn admission_rejects_arguments_that_do_not_match_input_schema_before_approval() {
    let resolved_tool = resolved_process_tool();
    let schemas = process_schema_registry();
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "process.run");
    draft
        .append_argument_fragment("{\"cmd\":\"echo hello\"}")
        .expect("fragment accepted");
    let call = draft
        .into_completed_tool_call(&resolved_tool.resolved_tool_id, 1_000)
        .expect("JSON arguments are complete");

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::ArgumentsSchemaInvalid {
            tool_call_id: "call-1".to_owned(),
            schema_id: "schemas/ProcessRun@1".to_owned(),
            path: "$.cmd".to_owned(),
            expected: "array".to_owned()
        }),
    );
}

#[test]
fn admission_denies_tool_no_longer_allowed_for_principal() {
    let mut resolved_tool = resolved_process_tool();
    resolved_tool.allowed_for_principal = false;
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::ResolvedToolNotAllowed {
            resolved_tool_id: resolved_tool.resolved_tool_id,
            principal_id: "user-1".to_owned()
        }),
    );
}

#[test]
fn admission_denies_expired_resolved_tool() {
    let mut resolved_tool = resolved_process_tool();
    resolved_tool.valid_until_unix_ms = Some(1_199);
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::ResolvedToolExpired {
            resolved_tool_id: resolved_tool.resolved_tool_id,
            valid_until_unix_ms: 1_199,
            admitted_at_unix_ms: 1_200
        }),
    );
}

#[test]
fn admission_reports_missing_input_schema_before_effect_admission() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = ToolSchemaRegistry::new([]).expect("empty registry is valid");

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::InputSchemaMissing {
            schema_id: "schemas/ProcessRun@1".to_owned()
        }),
    );
}

#[test]
fn schema_registry_rejects_duplicate_schema_ids() {
    assert_eq!(
        ToolSchemaRegistry::new([
            JsonSchema::new("schemas/ProcessRun@1", JsonSchemaNode::object()),
            JsonSchema::new("schemas/ProcessRun@1", JsonSchemaNode::object()),
        ]),
        Err(ToolSchemaRegistryError::DuplicateSchema {
            schema_id: "schemas/ProcessRun@1".to_owned()
        }),
    );
}
