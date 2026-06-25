use graphblocks_runtime_core::policy::{
    EnforcementPoint, PolicyDecision, PolicyEffect, PolicyObligation, PrincipalRef,
};
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ResolvedTool, ToolApproval, ToolBinding, ToolCatalog, ToolDefinition,
    ToolEffect, ToolIdempotency, ToolImplementation, ToolResolutionScope,
};
use graphblocks_runtime_core::tool_admission::{
    ToolAdmission, ToolAdmissionError, ToolAdmissionRequest, ToolPolicyRequestContext,
};
use graphblocks_runtime_core::tool_approval::{
    ToolApprovalError, ToolApprovalRecord, ToolApprovalRequest,
};
use graphblocks_runtime_core::tool_call::{ToolCall, ToolCallDraft, ToolCallError, ToolCallStatus};
use graphblocks_runtime_core::tool_schema::{
    JsonSchema, JsonSchemaNode, ToolSchemaRegistry, ToolSchemaRegistryError,
};
use graphblocks_schema::SchemaIdError;
use serde_json::json;

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

fn allow_tool_policy_decision() -> PolicyDecision {
    PolicyDecision {
        decision_id: "decision-allow-tool".to_owned(),
        effect: PolicyEffect::Allow,
        reason_codes: vec!["allow-process".to_owned()],
        policy_refs: vec!["allow-process".to_owned()],
        obligations: Vec::new(),
        advice: Vec::new(),
        evaluated_at: "2026-06-23T00:00:01Z".to_owned(),
        valid_until: None,
        input_digest: "sha256:before-tool".to_owned(),
    }
}

fn deny_tool_policy_decision() -> PolicyDecision {
    PolicyDecision {
        decision_id: "decision-deny-tool".to_owned(),
        effect: PolicyEffect::Deny,
        reason_codes: vec!["process_not_allowed".to_owned()],
        policy_refs: vec!["deny-process".to_owned()],
        obligations: Vec::new(),
        advice: Vec::new(),
        evaluated_at: "2026-06-23T00:00:01Z".to_owned(),
        valid_until: None,
        input_digest: "sha256:before-tool".to_owned(),
    }
}

#[test]
fn before_tool_or_effect_policy_request_carries_tool_admission_context() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);

    let request = ToolAdmission::before_tool_or_effect_policy_request(ToolPolicyRequestContext {
        request_id: "policy-req-1",
        call: &call,
        resolved_tool: &resolved_tool,
        principal: PrincipalRef::new("user-1").with_tenant_id("tenant-1"),
        occurred_at: "2026-06-23T00:00:00Z",
        run_id: Some("run-1"),
        output_policy_state: Some(json!({"response_status": "generating"})),
    })
    .with_input_digest();

    assert_eq!(
        request.enforcement_point,
        EnforcementPoint::BeforeToolOrEffect
    );
    assert_eq!(request.action, "tool.run");
    assert_eq!(request.resource.resource_id, "tool:process.run");
    assert_eq!(request.resource.resource_kind.as_deref(), Some("tool"));
    assert_eq!(
        request
            .principal
            .as_ref()
            .map(|principal| principal.principal_id.as_str()),
        Some("user-1")
    );
    assert_eq!(request.run_id.as_deref(), Some("run-1"));
    assert_eq!(
        request.policy_snapshot_id.as_deref(),
        Some("policy-snapshot-1")
    );
    assert_eq!(
        request.attributes.get("arguments_digest"),
        Some(&json!(call.arguments_digest))
    );
    assert_eq!(
        request.attributes.get("definition_digest"),
        Some(&json!(resolved_tool.definition_digest))
    );
    assert_eq!(
        request.attributes.get("binding_digest"),
        Some(&json!(resolved_tool.binding_digest))
    );
    assert_eq!(request.attributes.get("effects"), Some(&json!(["process"])));
    assert_eq!(
        request.attributes.get("output_policy_state"),
        Some(&json!({"response_status": "generating"}))
    );
    assert!(request.input_digest.starts_with("sha256:"));
}

#[test]
fn admission_requires_valid_approval_when_binding_requires_it() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let policy_decision = allow_tool_policy_decision();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call: call.clone(),
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
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
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
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
fn approval_records_validate_decision_metadata() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved_tool, &call, "user-1", 1_100, 2_000)
            .expect("approval request is valid");

    assert_eq!(
        ToolApprovalRecord::approve(request.clone(), " ", 1_150).validate(),
        Err(ToolApprovalError::EmptyField {
            field: "approver_id",
        })
    );

    let mut missing_decision_time = ToolApprovalRecord::approve(request.clone(), "admin-1", 1_150);
    missing_decision_time.decided_at_unix_ms = None;
    assert_eq!(
        missing_decision_time.validate(),
        Err(ToolApprovalError::MissingField {
            field: "decided_at_unix_ms",
        })
    );

    let mut mismatched_record = ToolApprovalRecord::approve(request.clone(), "admin-1", 1_150);
    mismatched_record.approval_id = "approval-other".to_owned();
    assert_eq!(
        mismatched_record.validate(),
        Err(ToolApprovalError::ApprovalIdMismatch {
            expected: "approval-1".to_owned(),
            actual: "approval-other".to_owned(),
        })
    );
    assert!(!mismatched_record.is_valid_for(&resolved_tool, &call, "user-1", 1_200));
}

#[test]
fn admission_rejects_invalid_tool_call_model() {
    let resolved_tool = resolved_process_tool();
    let mut call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let policy_decision = allow_tool_policy_decision();
    call.revision = 0;

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::InvalidToolCall {
            source: ToolCallError::InvalidRevision { revision: 0 }
        }),
    );
}

#[test]
fn admission_rejects_stale_argument_digest() {
    let resolved_tool = resolved_process_tool();
    let mut call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let policy_decision = allow_tool_policy_decision();
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved_tool, &call, "user-1", 1_100, 2_000)
            .expect("approval request is valid");
    let approval = ToolApprovalRecord::approve(request, "admin-1", 1_150);
    call.arguments = json!({"cmd": ["echo", "hello", "mutated"]});

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: Some(&approval),
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::ArgumentsDigestMismatch {
            tool_call_id: "call-1".to_owned()
        }),
    );
}

#[test]
fn admission_requires_approval_when_policy_obligates_it() {
    let mut resolved_tool = resolved_process_tool();
    resolved_tool.binding.approval = ToolApproval::Policy;
    resolved_tool.binding_digest = resolved_tool.binding.digest();
    resolved_tool.resolved_tool_id = "resolved-policy-process".to_owned();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.effect = PolicyEffect::AllowWithObligations;
    policy_decision.obligations = vec![PolicyObligation::new(
        "obl-approval",
        "require_tool_approval",
    )];

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call: call.clone(),
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
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
        policy_decision: &policy_decision,
        expected_policy_input_digest: &policy_decision.input_digest,
        approval: Some(&approval),
        principal_id: "user-1",
        idempotency_key: Some("idem-1".to_owned()),
        admitted_at_unix_ms: 1_200,
    })
    .expect("policy-obligated approval admits");

    assert_eq!(admitted.call.status, ToolCallStatus::Admitted);
}

#[test]
fn admission_denies_before_approval_when_policy_denies_tool_effect() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let policy_decision = deny_tool_policy_decision();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::PolicyDenied {
            decision_id: "decision-deny-tool".to_owned(),
            reason_codes: vec!["process_not_allowed".to_owned()],
        }),
    );
}

#[test]
fn admission_rejects_policy_decision_without_input_digest() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.input_digest.clear();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::PolicyDecisionMissingInputDigest {
            decision_id: "decision-allow-tool".to_owned(),
        }),
    );
}

#[test]
fn admission_rejects_policy_decision_for_different_input_digest() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.input_digest = "sha256:stale-before-tool".to_owned();
    let expected_request =
        ToolAdmission::before_tool_or_effect_policy_request(ToolPolicyRequestContext {
            request_id: "policy-req-1",
            call: &call,
            resolved_tool: &resolved_tool,
            principal: PrincipalRef::new("user-1"),
            occurred_at: "2026-06-23T00:00:00Z",
            run_id: None,
            output_policy_state: None,
        })
        .with_input_digest();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &expected_request.input_digest,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::PolicyInputDigestMismatch {
            decision_id: "decision-allow-tool".to_owned(),
            expected: expected_request.input_digest,
            actual: "sha256:stale-before-tool".to_owned(),
        }),
    );
}

#[test]
fn admission_rejects_empty_principal_and_blank_policy_digest() {
    let mut resolved_tool = resolved_process_tool();
    resolved_tool.binding.approval = ToolApproval::Never;
    resolved_tool.binding.idempotency = ToolIdempotency::Optional;
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let policy_decision = allow_tool_policy_decision();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call: call.clone(),
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: None,
            principal_id: " ",
            idempotency_key: None,
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::EmptyPrincipalId),
    );

    let mut blank_digest = policy_decision;
    blank_digest.input_digest = " ".to_owned();
    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &blank_digest,
            expected_policy_input_digest: &blank_digest.input_digest,
            approval: None,
            principal_id: "user-1",
            idempotency_key: None,
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::PolicyDecisionMissingInputDigest {
            decision_id: "decision-allow-tool".to_owned(),
        }),
    );
}

#[test]
fn admission_defers_before_approval_when_policy_defers_tool_effect() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let mut policy_decision = allow_tool_policy_decision();
    policy_decision.decision_id = "decision-defer-tool".to_owned();
    policy_decision.effect = PolicyEffect::Defer;
    policy_decision.reason_codes = vec!["needs_external_pdp".to_owned()];

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some("idem-1".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::PolicyDeferred {
            decision_id: "decision-defer-tool".to_owned(),
            reason_codes: vec!["needs_external_pdp".to_owned()],
        }),
    );
}

#[test]
fn admission_rejects_required_idempotency_without_key() {
    let resolved_tool = resolved_process_tool();
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let policy_decision = allow_tool_policy_decision();
    let request =
        ToolApprovalRequest::for_call("approval-1", &resolved_tool, &call, "user-1", 1_100, 2_000)
            .expect("approval request is valid");
    let approval = ToolApprovalRecord::approve(request, "admin-1", 1_150);

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call: call.clone(),
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: Some(&approval),
            principal_id: "user-1",
            idempotency_key: None,
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::IdempotencyKeyRequired {
            tool_call_id: "call-1".to_owned()
        }),
    );

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: Some(&approval),
            principal_id: "user-1",
            idempotency_key: Some(" ".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::EmptyIdempotencyKey {
            tool_call_id: "call-1".to_owned()
        }),
    );
}

#[test]
fn admission_rejects_blank_provided_optional_idempotency_key() {
    let mut resolved_tool = resolved_process_tool();
    resolved_tool.binding.approval = ToolApproval::Never;
    resolved_tool.binding.idempotency = ToolIdempotency::Optional;
    let call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let policy_decision = allow_tool_policy_decision();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
            approval: None,
            principal_id: "user-1",
            idempotency_key: Some(" ".to_owned()),
            admitted_at_unix_ms: 1_200,
        }),
        Err(ToolAdmissionError::EmptyIdempotencyKey {
            tool_call_id: "call-1".to_owned()
        }),
    );
}

#[test]
fn admission_rejects_call_for_different_resolved_tool() {
    let resolved_tool = resolved_process_tool();
    let mut call = process_call(&resolved_tool);
    let schemas = process_schema_registry();
    let policy_decision = allow_tool_policy_decision();
    call.resolved_tool_id = "sha256:other".to_owned();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
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
    let policy_decision = allow_tool_policy_decision();
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
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
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
    let policy_decision = allow_tool_policy_decision();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
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
    let policy_decision = allow_tool_policy_decision();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
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
    let policy_decision = allow_tool_policy_decision();

    assert_eq!(
        ToolAdmission::admit(ToolAdmissionRequest {
            call,
            resolved_tool: &resolved_tool,
            schema_registry: &schemas,
            policy_decision: &policy_decision,
            expected_policy_input_digest: &policy_decision.input_digest,
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

#[test]
fn schema_registry_rejects_invalid_schema_ids() {
    assert_eq!(
        ToolSchemaRegistry::new([JsonSchema::new(
            "schemas/ProcessRun",
            JsonSchemaNode::object(),
        )]),
        Err(ToolSchemaRegistryError::InvalidSchemaId {
            schema_id: "schemas/ProcessRun".to_owned(),
            error: SchemaIdError::MissingVersion,
        }),
    );
}
