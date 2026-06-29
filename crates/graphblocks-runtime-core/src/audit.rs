use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::policy::{PrincipalRef, ResourceRef};
use crate::tool::{ResolvedTool, ToolEffect, canonical_effect_names};
use crate::tool_call::{ToolCall, ToolCallStatus};
use crate::tool_result::{ToolEffectOutcome, ToolResult, ToolResultStatus};

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
    ToolEffect,
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
            Self::ToolEffect => "tool_effect",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolEffectAuditContext<'a> {
    pub event_id: &'a str,
    pub occurred_at: &'a str,
    pub actor: PrincipalRef,
    pub resolved_tool: &'a ResolvedTool,
    pub call: &'a ToolCall,
    pub result: &'a ToolResult,
    pub effect_key: Option<&'a str>,
    pub precondition_digest: Option<&'a str>,
    pub idempotency_key: Option<&'a str>,
    pub policy_decision_id: Option<&'a str>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolEffectPreconditionContext<'a> {
    pub resolved_tool: &'a ResolvedTool,
    pub call: &'a ToolCall,
    pub effect_key: Option<&'a str>,
    pub idempotency_key: Option<&'a str>,
    pub policy_decision_id: Option<&'a str>,
    pub execution_target: Option<&'a str>,
    pub sandbox_id: Option<&'a str>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolEffectPrecondition {
    pub payload: Value,
    pub digest: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ToolEffectAuditError {
    ResolvedToolMismatch {
        expected: String,
        actual: String,
    },
    ToolNameMismatch {
        expected: String,
        actual: String,
    },
    ToolResultMismatch {
        expected: String,
        actual: String,
    },
    ToolCallNotAdmitted {
        tool_call_id: String,
        current: ToolCallStatus,
    },
}

impl fmt::Display for ToolEffectAuditError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ResolvedToolMismatch { expected, actual } => write!(
                formatter,
                "tool call resolved tool {actual:?} does not match audited resolved tool {expected:?}"
            ),
            Self::ToolNameMismatch { expected, actual } => write!(
                formatter,
                "tool call name {actual:?} does not match audited tool {expected:?}"
            ),
            Self::ToolResultMismatch { expected, actual } => write!(
                formatter,
                "tool result call id {actual:?} does not match audited tool call {expected:?}"
            ),
            Self::ToolCallNotAdmitted {
                tool_call_id,
                current,
            } => write!(
                formatter,
                "tool call {tool_call_id:?} must be admitted before recording an effect precondition, current status is {current:?}"
            ),
        }
    }
}

impl Error for ToolEffectAuditError {}

impl ToolEffectPrecondition {
    pub fn from_admitted_call(
        context: ToolEffectPreconditionContext<'_>,
    ) -> Result<Self, ToolEffectAuditError> {
        validate_tool_effect_context(context.resolved_tool, context.call)?;
        if context.call.status != ToolCallStatus::Admitted {
            return Err(ToolEffectAuditError::ToolCallNotAdmitted {
                tool_call_id: context.call.tool_call_id.clone(),
                current: context.call.status,
            });
        }

        let payload = json!({
            "tool_call_id": &context.call.tool_call_id,
            "response_id": &context.call.response_id,
            "resolved_tool_id": &context.resolved_tool.resolved_tool_id,
            "binding_id": &context.resolved_tool.binding.binding_id,
            "tool_name": &context.resolved_tool.definition.name,
            "tool_call_revision": context.call.revision,
            "arguments_digest": &context.call.arguments_digest,
            "definition_digest": &context.resolved_tool.definition_digest,
            "binding_digest": &context.resolved_tool.binding_digest,
            "effective_policy_snapshot_id": &context.resolved_tool.effective_policy_snapshot_id,
            "effects": canonical_effect_names(&context.resolved_tool.binding.effects),
            "effect_key": context.effect_key,
            "idempotency_key": context.idempotency_key,
            "policy_decision_id": context.policy_decision_id,
            "execution_target": context.execution_target,
            "sandbox_id": context.sandbox_id,
            "admitted_at_unix_ms": context.call.admitted_at_unix_ms,
        });
        let digest = canonical_hash(&payload);
        Ok(Self { payload, digest })
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

    pub fn tool_effect_outcome(
        context: ToolEffectAuditContext<'_>,
    ) -> Result<Self, ToolEffectAuditError> {
        validate_tool_effect_context(context.resolved_tool, context.call)?;
        if context.result.tool_call_id != context.call.tool_call_id {
            return Err(ToolEffectAuditError::ToolResultMismatch {
                expected: context.call.tool_call_id.clone(),
                actual: context.result.tool_call_id.clone(),
            });
        }

        let target_kind = if context
            .resolved_tool
            .binding
            .effects
            .contains(&ToolEffect::Destructive)
        {
            AuditTargetKind::DestructiveEffect
        } else {
            AuditTargetKind::ToolEffect
        };
        let effects = canonical_effect_names(&context.resolved_tool.binding.effects);
        let result_status = match context.result.status {
            ToolResultStatus::Completed => "completed",
            ToolResultStatus::Failed => "failed",
            ToolResultStatus::Denied => "denied",
            ToolResultStatus::Cancelled => "cancelled",
            ToolResultStatus::PolicyStopped => "policy_stopped",
            ToolResultStatus::Incomplete => "incomplete",
        };
        let effect_outcome = match context.result.effect_outcome {
            ToolEffectOutcome::NoExternalEffect => "no_external_effect",
            ToolEffectOutcome::Committed => "committed",
            ToolEffectOutcome::NotCommitted => "not_committed",
            ToolEffectOutcome::Unknown => "unknown",
        };

        Ok(AuditEvent::new(context.event_id, target_kind, context.occurred_at)
            .with_actor(context.actor)
            .with_resource(
                ResourceRef::new(format!(
                    "tool:{}",
                    context.resolved_tool.definition.name
                ))
                .with_resource_kind("tool"),
            )
            .with_reason_code(format!("tool_effect.{effect_outcome}"))
            .with_payload(json!({
                "tool_call_id": &context.call.tool_call_id,
                "response_id": &context.call.response_id,
                "resolved_tool_id": &context.resolved_tool.resolved_tool_id,
                "tool_name": &context.resolved_tool.definition.name,
                "tool_call_revision": context.call.revision,
                "arguments_digest": &context.call.arguments_digest,
                "definition_digest": &context.resolved_tool.definition_digest,
                "binding_digest": &context.resolved_tool.binding_digest,
                "effective_policy_snapshot_id": &context.resolved_tool.effective_policy_snapshot_id,
                "effects": effects,
                "effect_key": context.effect_key,
                "precondition_digest": context.precondition_digest,
                "idempotency_key": context.idempotency_key,
                "policy_decision_id": context.policy_decision_id,
                "result_status": result_status,
                "effect_outcome": effect_outcome,
                "output_digest": &context.result.output_digest,
                "started_at_unix_ms": context.result.started_at_unix_ms,
                "completed_at_unix_ms": context.result.completed_at_unix_ms,
            })))
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

fn validate_tool_effect_context(
    resolved_tool: &ResolvedTool,
    call: &ToolCall,
) -> Result<(), ToolEffectAuditError> {
    if call.resolved_tool_id != resolved_tool.resolved_tool_id {
        return Err(ToolEffectAuditError::ResolvedToolMismatch {
            expected: resolved_tool.resolved_tool_id.clone(),
            actual: call.resolved_tool_id.clone(),
        });
    }
    if call.name != resolved_tool.definition.name {
        return Err(ToolEffectAuditError::ToolNameMismatch {
            expected: resolved_tool.definition.name.clone(),
            actual: call.name.clone(),
        });
    }
    Ok(())
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AuditOutboxStatus {
    Pending,
    Published,
    Failed,
}

impl AuditOutboxStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Published => "published",
            Self::Failed => "failed",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AuditOutboxRecord {
    pub record_id: String,
    pub record_type: String,
    pub payload: Value,
    pub payload_digest: String,
    pub occurred_at: String,
    pub status: AuditOutboxStatus,
    pub attempts: u32,
    pub published_at: Option<String>,
    pub last_error: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AuditOutboxError {
    DuplicateRecord { record_id: String },
    RecordNotFound { record_id: String },
    RecordAlreadyPublished { record_id: String },
}

impl fmt::Display for AuditOutboxError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateRecord { record_id } => {
                write!(
                    formatter,
                    "audit outbox record {record_id:?} already exists"
                )
            }
            Self::RecordNotFound { record_id } => {
                write!(
                    formatter,
                    "audit outbox record {record_id:?} does not exist"
                )
            }
            Self::RecordAlreadyPublished { record_id } => {
                write!(
                    formatter,
                    "audit outbox record {record_id:?} is already published"
                )
            }
        }
    }
}

impl Error for AuditOutboxError {}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct InMemoryAuditOutbox {
    records: Vec<AuditOutboxRecord>,
    record_indexes: BTreeMap<String, usize>,
}

impl InMemoryAuditOutbox {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn append(
        &mut self,
        record_type: impl Into<String>,
        payload: Value,
        occurred_at: impl Into<String>,
        record_id: Option<String>,
    ) -> Result<AuditOutboxRecord, AuditOutboxError> {
        let payload_digest = canonical_hash(&payload);
        let actual_record_id = record_id.unwrap_or_else(|| format!("audit:{payload_digest}"));
        if self.record_indexes.contains_key(&actual_record_id) {
            return Err(AuditOutboxError::DuplicateRecord {
                record_id: actual_record_id,
            });
        }
        let record = AuditOutboxRecord {
            record_id: actual_record_id.clone(),
            record_type: record_type.into(),
            payload,
            payload_digest,
            occurred_at: occurred_at.into(),
            status: AuditOutboxStatus::Pending,
            attempts: 0,
            published_at: None,
            last_error: None,
        };
        self.record_indexes
            .insert(actual_record_id, self.records.len());
        self.records.push(record.clone());
        Ok(record)
    }

    pub fn get(&self, record_id: impl AsRef<str>) -> Result<AuditOutboxRecord, AuditOutboxError> {
        Ok(self.records[self.record_index(record_id.as_ref())?].clone())
    }

    pub fn pending(&self, limit: Option<usize>) -> Vec<AuditOutboxRecord> {
        let records = self
            .records
            .iter()
            .filter(|record| {
                matches!(
                    record.status,
                    AuditOutboxStatus::Pending | AuditOutboxStatus::Failed
                )
            })
            .cloned();
        match limit {
            Some(limit) => records.take(limit).collect(),
            None => records.collect(),
        }
    }

    pub fn mark_published(
        &mut self,
        record_id: impl AsRef<str>,
        published_at: impl Into<String>,
    ) -> Result<AuditOutboxRecord, AuditOutboxError> {
        let index = self.record_index(record_id.as_ref())?;
        let record = &mut self.records[index];
        record.status = AuditOutboxStatus::Published;
        record.published_at = Some(published_at.into());
        record.last_error = None;
        Ok(record.clone())
    }

    pub fn mark_failed(
        &mut self,
        record_id: impl AsRef<str>,
        error: impl Into<String>,
    ) -> Result<AuditOutboxRecord, AuditOutboxError> {
        let index = self.record_index(record_id.as_ref())?;
        let record = &mut self.records[index];
        if record.status == AuditOutboxStatus::Published {
            return Err(AuditOutboxError::RecordAlreadyPublished {
                record_id: record.record_id.clone(),
            });
        }
        record.status = AuditOutboxStatus::Failed;
        record.attempts += 1;
        record.last_error = Some(error.into());
        Ok(record.clone())
    }

    fn record_index(&self, record_id: &str) -> Result<usize, AuditOutboxError> {
        self.record_indexes.get(record_id).copied().ok_or_else(|| {
            AuditOutboxError::RecordNotFound {
                record_id: record_id.to_owned(),
            }
        })
    }
}

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
