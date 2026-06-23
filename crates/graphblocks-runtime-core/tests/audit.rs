use graphblocks_runtime_core::audit::{
    AuditEvent, AuditQuery, AuditSinkError, AuditTargetKind, InMemoryAuditSink,
};
use graphblocks_runtime_core::policy::{PrincipalRef, ResourceRef};
use serde_json::json;

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
