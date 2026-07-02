use graphblocks_runtime_core::async_operation::{CallbackArtifactRef, ExternalCallbackReceived};
use graphblocks_runtime_core::audit::{
    AuditEvent, AuditOutboxError, AuditQuery, AuditSinkError, AuditTargetKind,
    ExternalCallbackAuditContext, ExternalCallbackRejectionAuditContext, InMemoryAuditOutbox,
    InMemoryAuditSink, ToolEffectAuditContext, ToolEffectAuditError, ToolEffectPrecondition,
    ToolEffectPreconditionContext,
};
use graphblocks_runtime_core::policy::{PrincipalRef, ResourceRef};
use graphblocks_runtime_core::tool::{
    BlockToolImplementation, ToolBinding, ToolCatalog, ToolDefinition, ToolEffect,
    ToolImplementation, ToolResolutionScope,
};
use graphblocks_runtime_core::tool_call::{ToolCallDraft, ToolCallStatus};
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

fn callback_receipt() -> ExternalCallbackReceived {
    ExternalCallbackReceived {
        callback_id: "cb-1".to_owned(),
        operation_id: "op-ci-1".to_owned(),
        run_id: "run-1".to_owned(),
        node_id: "waitCI".to_owned(),
        attempt_id: "attempt-1".to_owned(),
        provider_operation_id: Some("gha-run-1".to_owned()),
        idempotency_key: "idem-callback-1".to_owned(),
        payload: json!({
            "status": "completed",
            "secret": "do-not-copy-to-audit",
        }),
        payload_digest: "sha256:payload".to_owned(),
        artifacts: vec![
            CallbackArtifactRef::new("artifact-ci-log", "blob://callbacks/op-ci-1/cb-1.json")
                .with_media_type("application/json"),
        ],
        received_at_unix_ms: 1_000,
        verified_by: "hmac:endpoint-ci".to_owned(),
        policy_snapshot_id: "policy-snapshot-1".to_owned(),
    }
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
fn external_callback_received_audit_event_records_digest_without_untrusted_payload() {
    let receipt = callback_receipt();
    let event = AuditEvent::external_callback_received(ExternalCallbackAuditContext {
        event_id: "audit-callback-1",
        occurred_at: "2026-07-02T00:00:00Z",
        actor: PrincipalRef::new("callback:endpoint-ci").with_tenant_id("tenant-a"),
        receipt: &receipt,
        release_id: "release-1",
    });

    assert_eq!(event.target_kind, AuditTargetKind::ExternalCallback);
    assert_eq!(
        event
            .resource
            .as_ref()
            .map(|resource| resource.resource_id.as_str()),
        Some("async_operation:op-ci-1")
    );
    assert_eq!(event.reason_codes, vec!["external_callback.received"]);
    assert_eq!(
        event.payload,
        json!({
            "callback_id": "cb-1",
            "operation_id": "op-ci-1",
            "run_id": "run-1",
            "node_id": "waitCI",
            "attempt_id": "attempt-1",
            "provider_operation_id": "gha-run-1",
            "idempotency_key": "idem-callback-1",
            "payload_digest": "sha256:payload",
            "artifact_count": 1,
            "artifact_ids": ["artifact-ci-log"],
            "received_at_unix_ms": 1_000,
            "verified_by": "hmac:endpoint-ci",
            "policy_snapshot_id": "policy-snapshot-1",
            "release_id": "release-1",
        })
    );
    assert!(!event.payload.to_string().contains("do-not-copy-to-audit"));
}

#[test]
fn external_callback_rejected_audit_event_records_reason_without_payload() {
    let event = AuditEvent::external_callback_rejected(ExternalCallbackRejectionAuditContext {
        event_id: "audit-callback-rejected-1",
        occurred_at: "2026-07-02T00:00:01Z",
        actor: PrincipalRef::new("callback:endpoint-ci").with_tenant_id("tenant-a"),
        operation_id: "op-ci-1",
        callback_id: "cb-bad",
        reason: "callback_schema_invalid",
        occurred_at_unix_ms: 1_010,
        verified_by: "hmac:endpoint-ci",
        policy_snapshot_id: "policy-snapshot-1",
        release_id: "release-1",
    });

    assert_eq!(event.target_kind, AuditTargetKind::ExternalCallback);
    assert_eq!(
        event
            .resource
            .as_ref()
            .map(|resource| resource.resource_id.as_str()),
        Some("async_operation:op-ci-1")
    );
    assert_eq!(event.reason_codes, vec!["external_callback.rejected"]);
    assert_eq!(
        event.payload,
        json!({
            "operation_id": "op-ci-1",
            "callback_id": "cb-bad",
            "reason": "callback_schema_invalid",
            "occurred_at_unix_ms": 1_010,
            "verified_by": "hmac:endpoint-ci",
            "policy_snapshot_id": "policy-snapshot-1",
            "release_id": "release-1",
        })
    );
}

#[test]
fn in_memory_audit_outbox_publishes_failed_records_and_excludes_published_from_pending()
-> Result<(), AuditOutboxError> {
    let mut outbox = InMemoryAuditOutbox::new();
    let first = outbox.append(
        "application_event",
        json!({"event_id": "event-1", "kind": "OutputPolicyAllowed"}),
        "2026-06-23T00:00:00Z",
        Some("audit-1".to_owned()),
    )?;
    let second = outbox.append(
        "policy_enforcement",
        json!({"record_id": "enforcement-1", "status": "blocked"}),
        "2026-06-23T00:00:01Z",
        Some("audit-2".to_owned()),
    )?;

    assert_eq!(first.status.as_str(), "pending");
    assert_eq!(
        outbox
            .pending(None)
            .iter()
            .map(|record| record.record_id.as_str())
            .collect::<Vec<_>>(),
        vec!["audit-1", "audit-2"]
    );

    let published = outbox.mark_published("audit-1", "2026-06-23T00:00:02Z")?;
    let failed = outbox.mark_failed("audit-2", "sink unavailable")?;

    assert_eq!(published.status.as_str(), "published");
    assert_eq!(
        published.published_at.as_deref(),
        Some("2026-06-23T00:00:02Z")
    );
    assert_eq!(failed.status.as_str(), "failed");
    assert_eq!(failed.attempts, second.attempts + 1);
    assert_eq!(failed.last_error.as_deref(), Some("sink unavailable"));
    assert_eq!(outbox.pending(None), vec![failed]);
    Ok(())
}

#[test]
fn in_memory_audit_outbox_treats_published_records_as_terminal() -> Result<(), AuditOutboxError> {
    let mut outbox = InMemoryAuditOutbox::new();
    outbox.append(
        "application_event",
        json!({"event_id": "event-1"}),
        "2026-06-23T00:00:00Z",
        Some("audit-1".to_owned()),
    )?;
    outbox.mark_published("audit-1", "2026-06-23T00:00:01Z")?;

    assert_eq!(
        outbox.mark_failed("audit-1", "sink unavailable"),
        Err(AuditOutboxError::RecordAlreadyPublished {
            record_id: "audit-1".to_owned(),
        })
    );
    assert_eq!(outbox.get("audit-1")?.status.as_str(), "published");
    assert!(outbox.pending(None).is_empty());
    Ok(())
}

#[test]
fn tool_effect_audit_event_records_precondition_outcome_and_immutable_digests() {
    let resolved_tool = resolved_ticket_tool();
    let mut call = ticket_call(&resolved_tool.resolved_tool_id);
    call.status = ToolCallStatus::Admitted;
    call.admitted_at_unix_ms = Some(1_050);
    let precondition = ToolEffectPrecondition::from_admitted_call(ToolEffectPreconditionContext {
        resolved_tool: &resolved_tool,
        call: &call,
        effect_key: Some("ticket.create:cust-1"),
        idempotency_key: Some("idem-ticket-1"),
        policy_decision_id: Some("decision-tool-1"),
        execution_target: Some("worker:local"),
        sandbox_id: Some("sandbox-1"),
    })
    .expect("admitted call precondition is recorded");
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
        precondition_digest: Some(&precondition.digest),
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
            "effects": ["destructive", "external_write", "network"],
            "effect_key": "ticket.create:cust-1",
            "precondition_digest": precondition.digest,
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
fn tool_effect_precondition_records_admitted_effect_context() {
    let resolved_tool = resolved_ticket_tool();
    let mut call = ticket_call(&resolved_tool.resolved_tool_id);
    call.status = ToolCallStatus::Admitted;
    call.admitted_at_unix_ms = Some(1_050);

    let precondition = ToolEffectPrecondition::from_admitted_call(ToolEffectPreconditionContext {
        resolved_tool: &resolved_tool,
        call: &call,
        effect_key: Some("ticket.create:cust-1"),
        idempotency_key: Some("idem-ticket-1"),
        policy_decision_id: Some("decision-tool-1"),
        execution_target: Some("worker:local"),
        sandbox_id: Some("sandbox-1"),
    })
    .expect("admitted call precondition is recorded");
    let same_precondition =
        ToolEffectPrecondition::from_admitted_call(ToolEffectPreconditionContext {
            resolved_tool: &resolved_tool,
            call: &call,
            effect_key: Some("ticket.create:cust-1"),
            idempotency_key: Some("idem-ticket-1"),
            policy_decision_id: Some("decision-tool-1"),
            execution_target: Some("worker:local"),
            sandbox_id: Some("sandbox-1"),
        })
        .expect("same admitted call precondition is recorded");

    assert_eq!(precondition.digest, same_precondition.digest);
    assert!(precondition.digest.starts_with("sha256:"));
    assert_eq!(
        precondition.payload,
        json!({
            "tool_call_id": "call-1",
            "response_id": "response-1",
            "resolved_tool_id": resolved_tool.resolved_tool_id,
            "binding_id": "binding-ticket-create",
            "tool_name": "ticket.create",
            "tool_call_revision": 1,
            "arguments_digest": call.arguments_digest,
            "definition_digest": resolved_tool.definition_digest,
            "binding_digest": resolved_tool.binding_digest,
            "effective_policy_snapshot_id": "policy-snapshot-1",
            "effects": ["destructive", "external_write", "network"],
            "effect_key": "ticket.create:cust-1",
            "idempotency_key": "idem-ticket-1",
            "policy_decision_id": "decision-tool-1",
            "execution_target": "worker:local",
            "sandbox_id": "sandbox-1",
            "admitted_at_unix_ms": 1050,
        })
    );
}

#[test]
fn tool_effect_precondition_rejects_non_admitted_calls() {
    let resolved_tool = resolved_ticket_tool();
    let call = ticket_call(&resolved_tool.resolved_tool_id);

    assert_eq!(
        ToolEffectPrecondition::from_admitted_call(ToolEffectPreconditionContext {
            resolved_tool: &resolved_tool,
            call: &call,
            effect_key: None,
            idempotency_key: None,
            policy_decision_id: None,
            execution_target: None,
            sandbox_id: None,
        }),
        Err(ToolEffectAuditError::ToolCallNotAdmitted {
            tool_call_id: "call-1".to_owned(),
            current: ToolCallStatus::Validated,
        })
    );
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
