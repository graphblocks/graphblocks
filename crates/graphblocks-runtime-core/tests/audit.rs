use graphblocks_runtime_core::audit::{
    AuditEvent, AuditQuery, AuditSinkError, AuditTargetKind, InMemoryAuditSink,
    ToolEffectAuditContext, ToolEffectAuditError,
};
use graphblocks_runtime_core::policy::{PrincipalRef, ResourceRef};
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ToolBinding, ToolCatalog, ToolDefinition, ToolEffect,
    ToolImplementation, ToolResolutionScope,
};
use graphblocks_runtime_core::tool_call::ToolCallDraft;
use graphblocks_runtime_core::tool_result::{ContentPart, ToolEffectOutcome, ToolResult};
use serde_json::json;

fn resolved_ticket_tool() -> graphblocks_runtime_core::tool::ResolvedTool {
    let catalog = ToolCatalog::new(
        [ToolDefinition::new(
            "ticket.create",
            "Create a support ticket.",
            "schemas/TicketCreate@1",
        )],
        [ToolBinding::new(
            "binding-ticket-create",
            "ticket.create",
            ToolImplementation::Block(BlockToolImplementation::new("blocks.ticket_create")),
        )
        .with_effects([
            ToolEffect::ExternalWrite,
            ToolEffect::Network,
            ToolEffect::Destructive,
        ])],
    )
    .expect("catalog is valid");
    catalog
        .resolve(ToolResolutionScope::new(), "policy-snapshot-1")
        .expect("tool resolves")
        .remove(0)
}

fn ticket_call(resolved_tool_id: impl AsRef<str>) -> graphblocks_runtime_core::tool_call::ToolCall {
    let mut draft = ToolCallDraft::proposed("response-1", "call-1", "ticket.create");
    draft
        .append_argument_fragment("{\"customer_id\":\"cust-1\",\"title\":\"Help\"}")
        .expect("fragment appends");
    draft
        .into_completed_tool_call(resolved_tool_id.as_ref(), 1_000)
        .expect("arguments are valid")
}

#[test]
fn audit_event_payload_digest_is_stable_without_event_identity() {
    let actor = PrincipalRef::new("user-1").with_tenant_id("tenant-a");
    let resource = ResourceRef::new("tool:ticket.create").with_resource_kind("tool");
    let left = AuditEvent::new(
        "audit-1",
        AuditTargetKind::DestructiveEffect,
        "2026-06-23T00:00:00Z",
    )
    .with_actor(actor.clone())
    .with_resource(resource.clone())
    .with_reason_code("approval_granted")
    .with_payload(json!({"tool_call_id": "call-1", "effect": "external_write"}));
    let right = AuditEvent::new(
        "audit-2",
        AuditTargetKind::DestructiveEffect,
        "2026-06-23T00:00:01Z",
    )
    .with_actor(actor)
    .with_resource(resource)
    .with_reason_code("approval_granted")
    .with_payload(json!({"tool_call_id": "call-1", "effect": "external_write"}));

    assert_eq!(left.payload_digest(), right.payload_digest());
}

#[test]
fn in_memory_audit_sink_appends_immutably_and_rejects_duplicate_event_id()
-> Result<(), AuditSinkError> {
    let mut sink = InMemoryAuditSink::new();
    let first = AuditEvent::new(
        "audit-1",
        AuditTargetKind::PermissionDecision,
        "2026-06-23T00:00:00Z",
    )
    .with_actor(PrincipalRef::new("user-1"))
    .with_resource(ResourceRef::new("graph:support-agent"));

    sink.append(first.clone())?;

    assert_eq!(
        sink.append(first),
        Err(AuditSinkError::DuplicateEvent {
            event_id: "audit-1".to_owned()
        })
    );
    assert_eq!(sink.events().len(), 1);
    assert_eq!(sink.events()[0].event_id, "audit-1");
    Ok(())
}

#[test]
fn audit_query_filters_by_target_actor_and_resource() -> Result<(), AuditSinkError> {
    let mut sink = InMemoryAuditSink::new();
    sink.append(
        AuditEvent::new(
            "audit-1",
            AuditTargetKind::PermissionDecision,
            "2026-06-23T00:00:00Z",
        )
        .with_actor(PrincipalRef::new("user-1"))
        .with_resource(ResourceRef::new("tool:knowledge.search")),
    )?;
    sink.append(
        AuditEvent::new(
            "audit-2",
            AuditTargetKind::SecretAccess,
            "2026-06-23T00:01:00Z",
        )
        .with_actor(PrincipalRef::new("runtime"))
        .with_resource(ResourceRef::new("secret://env/OPENAI_API_KEY")),
    )?;
    sink.append(
        AuditEvent::new(
            "audit-3",
            AuditTargetKind::SecretAccess,
            "2026-06-23T00:02:00Z",
        )
        .with_actor(PrincipalRef::new("runtime"))
        .with_resource(ResourceRef::new("secret://vault/QDRANT")),
    )?;

    let matches = sink.query(
        AuditQuery::new()
            .with_target_kind(AuditTargetKind::SecretAccess)
            .with_actor("runtime")
            .with_resource("secret://env/OPENAI_API_KEY"),
    );

    assert_eq!(
        matches
            .iter()
            .map(|event| event.event_id.as_str())
            .collect::<Vec<_>>(),
        vec!["audit-2"]
    );
    Ok(())
}

#[test]
fn tool_effect_audit_event_records_precondition_outcome_and_immutable_digests() {
    let resolved_tool = resolved_ticket_tool();
    let call = ticket_call(&resolved_tool.resolved_tool_id);
    let result = ToolResult::completed(
        "call-1",
        [ContentPart::json(json!({"ticket_id": "T-1"}))],
        1_100,
        1_250,
    )
    .with_effect_outcome(ToolEffectOutcome::Committed);

    let event = AuditEvent::tool_effect_outcome(ToolEffectAuditContext {
        event_id: "audit-effect-1",
        occurred_at: "2026-06-23T00:00:02Z",
        actor: PrincipalRef::new("user-1").with_tenant_id("tenant-a"),
        resolved_tool: &resolved_tool,
        call: &call,
        result: &result,
        effect_key: Some("ticket.create:cust-1"),
        precondition_digest: Some("sha256:precondition"),
        idempotency_key: Some("idem-ticket-1"),
        policy_decision_id: Some("decision-tool-1"),
    })
    .expect("tool effect audit event is valid");

    assert_eq!(event.target_kind, AuditTargetKind::DestructiveEffect);
    assert_eq!(
        event
            .resource
            .as_ref()
            .map(|resource| resource.resource_id.as_str()),
        Some("tool:ticket.create")
    );
    assert_eq!(
        event.payload,
        json!({
            "tool_call_id": "call-1",
            "response_id": "response-1",
            "resolved_tool_id": resolved_tool.resolved_tool_id,
            "tool_name": "ticket.create",
            "tool_call_revision": 1,
            "arguments_digest": call.arguments_digest,
            "definition_digest": resolved_tool.definition_digest,
            "binding_digest": resolved_tool.binding_digest,
            "effective_policy_snapshot_id": "policy-snapshot-1",
            "effects": ["external_write", "network", "destructive"],
            "effect_key": "ticket.create:cust-1",
            "precondition_digest": "sha256:precondition",
            "idempotency_key": "idem-ticket-1",
            "policy_decision_id": "decision-tool-1",
            "result_status": "completed",
            "effect_outcome": "committed",
            "output_digest": result.output_digest,
            "started_at_unix_ms": 1100,
            "completed_at_unix_ms": 1250,
        })
    );
    assert!(
        event
            .reason_codes
            .contains(&"tool_effect.committed".to_owned())
    );
    assert!(event.payload_digest().starts_with("sha256:"));
}

#[test]
fn tool_effect_audit_event_rejects_mismatched_call_result_or_resolved_tool() {
    let resolved_tool = resolved_ticket_tool();
    let call = ticket_call(&resolved_tool.resolved_tool_id);
    let result = ToolResult::completed("other-call", [ContentPart::text("ok")], 1_100, 1_250)
        .with_effect_outcome(ToolEffectOutcome::NoExternalEffect);

    assert_eq!(
        AuditEvent::tool_effect_outcome(ToolEffectAuditContext {
            event_id: "audit-effect-1",
            occurred_at: "2026-06-23T00:00:02Z",
            actor: PrincipalRef::new("user-1"),
            resolved_tool: &resolved_tool,
            call: &call,
            result: &result,
            effect_key: None,
            precondition_digest: None,
            idempotency_key: None,
            policy_decision_id: None,
        }),
        Err(ToolEffectAuditError::ToolResultMismatch {
            expected: "call-1".to_owned(),
            actual: "other-call".to_owned(),
        })
    );

    let mut mismatched_call = call.clone();
    mismatched_call.resolved_tool_id = "other-tool".to_owned();

    assert_eq!(
        AuditEvent::tool_effect_outcome(ToolEffectAuditContext {
            event_id: "audit-effect-2",
            occurred_at: "2026-06-23T00:00:02Z",
            actor: PrincipalRef::new("user-1"),
            resolved_tool: &resolved_tool,
            call: &mismatched_call,
            result: &ToolResult::completed("call-1", [ContentPart::text("ok")], 1_100, 1_250),
            effect_key: None,
            precondition_digest: None,
            idempotency_key: None,
            policy_decision_id: None,
        }),
        Err(ToolEffectAuditError::ResolvedToolMismatch {
            expected: resolved_tool.resolved_tool_id,
            actual: "other-tool".to_owned(),
        })
    );
}
