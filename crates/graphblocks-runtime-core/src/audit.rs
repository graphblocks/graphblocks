use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::policy::{PrincipalRef, ResourceRef};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AuditTargetKind {
    PermissionDecision,
    ApprovalDecision,
    ReviewDecision,
    PolicyOverride,
    EntitlementChange,
    BudgetReconciliation,
    DestructiveEffect,
    DocumentAclChange,
    IndexPublish,
    IndexDelete,
    SecretAccess,
    PluginLoad,
    GraphDeployment,
}

impl AuditTargetKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::PermissionDecision => "permission_decision",
            Self::ApprovalDecision => "approval_decision",
            Self::ReviewDecision => "review_decision",
            Self::PolicyOverride => "policy_override",
            Self::EntitlementChange => "entitlement_change",
            Self::BudgetReconciliation => "budget_reconciliation",
            Self::DestructiveEffect => "destructive_effect",
            Self::DocumentAclChange => "document_acl_change",
            Self::IndexPublish => "index_publish",
            Self::IndexDelete => "index_delete",
            Self::SecretAccess => "secret_access",
            Self::PluginLoad => "plugin_load",
            Self::GraphDeployment => "graph_deployment",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AuditEvent {
    pub event_id: String,
    pub target_kind: AuditTargetKind,
    pub occurred_at: String,
    pub actor: Option<PrincipalRef>,
    pub resource: Option<ResourceRef>,
    pub reason_codes: Vec<String>,
    pub payload: Value,
    pub metadata: BTreeMap<String, Value>,
}

impl AuditEvent {
    pub fn new(
        event_id: impl Into<String>,
        target_kind: AuditTargetKind,
        occurred_at: impl Into<String>,
    ) -> Self {
        Self {
            event_id: event_id.into(),
            target_kind,
            occurred_at: occurred_at.into(),
            actor: None,
            resource: None,
            reason_codes: Vec::new(),
            payload: json!({}),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_actor(mut self, actor: PrincipalRef) -> Self {
        self.actor = Some(actor);
        self
    }

    pub fn with_resource(mut self, resource: ResourceRef) -> Self {
        self.resource = Some(resource);
        self
    }

    pub fn with_reason_code(mut self, reason_code: impl Into<String>) -> Self {
        self.reason_codes.push(reason_code.into());
        self
    }

    pub fn with_payload(mut self, payload: Value) -> Self {
        self.payload = payload;
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }

    pub fn payload_digest(&self) -> String {
        canonical_hash(&json!({
            "target_kind": self.target_kind.as_str(),
            "actor": self.actor.as_ref().map(|actor| json!({
                "principal_id": actor.principal_id,
                "tenant_id": actor.tenant_id,
                "groups": actor.groups,
                "roles": actor.roles,
                "attributes": actor.attributes,
            })),
            "resource": self.resource.as_ref().map(|resource| json!({
                "resource_id": resource.resource_id,
                "resource_kind": resource.resource_kind,
                "tenant_id": resource.tenant_id,
                "attributes": resource.attributes,
            })),
            "reason_codes": self.reason_codes,
            "payload": self.payload,
            "metadata": self.metadata,
        }))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AuditSinkError {
    DuplicateEvent { event_id: String },
}

impl fmt::Display for AuditSinkError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateEvent { event_id } => {
                write!(formatter, "audit event {event_id:?} already exists")
            }
        }
    }
}

impl Error for AuditSinkError {}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct AuditQuery {
    pub target_kind: Option<AuditTargetKind>,
    pub actor: Option<String>,
    pub resource: Option<String>,
}

impl AuditQuery {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_target_kind(mut self, target_kind: AuditTargetKind) -> Self {
        self.target_kind = Some(target_kind);
        self
    }

    pub fn with_actor(mut self, actor: impl Into<String>) -> Self {
        self.actor = Some(actor.into());
        self
    }

    pub fn with_resource(mut self, resource: impl Into<String>) -> Self {
        self.resource = Some(resource.into());
        self
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct InMemoryAuditSink {
    events: Vec<AuditEvent>,
    event_ids: BTreeSet<String>,
}

impl InMemoryAuditSink {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn append(&mut self, event: AuditEvent) -> Result<(), AuditSinkError> {
        if !self.event_ids.insert(event.event_id.clone()) {
            return Err(AuditSinkError::DuplicateEvent {
                event_id: event.event_id,
            });
        }
        self.events.push(event);
        Ok(())
    }

    pub fn events(&self) -> &[AuditEvent] {
        &self.events
    }

    pub fn query(&self, query: AuditQuery) -> Vec<AuditEvent> {
        self.events
            .iter()
            .filter(|event| {
                query
                    .target_kind
                    .is_none_or(|target_kind| event.target_kind == target_kind)
            })
            .filter(|event| {
                query.actor.as_ref().is_none_or(|actor| {
                    event
                        .actor
                        .as_ref()
                        .is_some_and(|principal| &principal.principal_id == actor)
                })
            })
            .filter(|event| {
                query.resource.as_ref().is_none_or(|resource_id| {
                    event
                        .resource
                        .as_ref()
                        .is_some_and(|resource| &resource.resource_id == resource_id)
                })
            })
            .cloned()
            .collect()
    }
}
