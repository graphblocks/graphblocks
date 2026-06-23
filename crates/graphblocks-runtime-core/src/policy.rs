use std::collections::BTreeMap;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EnforcementPoint {
    Compile,
    Release,
    Admission,
    BeforeNode,
    BeforeProviderCall,
    OnGenerationChunk,
    BeforeClientDelivery,
    BeforeOutputCommit,
    OnUsageDelta,
    BeforeToolOrEffect,
    BeforeCommit,
    BeforePublish,
    OnResume,
}

impl EnforcementPoint {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Compile => "compile",
            Self::Release => "release",
            Self::Admission => "admission",
            Self::BeforeNode => "before_node",
            Self::BeforeProviderCall => "before_provider_call",
            Self::OnGenerationChunk => "on_generation_chunk",
            Self::BeforeClientDelivery => "before_client_delivery",
            Self::BeforeOutputCommit => "before_output_commit",
            Self::OnUsageDelta => "on_usage_delta",
            Self::BeforeToolOrEffect => "before_tool_or_effect",
            Self::BeforeCommit => "before_commit",
            Self::BeforePublish => "before_publish",
            Self::OnResume => "on_resume",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PrincipalRef {
    pub principal_id: String,
    pub tenant_id: Option<String>,
    pub groups: Vec<String>,
    pub roles: Vec<String>,
    pub attributes: BTreeMap<String, Value>,
}

impl PrincipalRef {
    pub fn new(principal_id: impl Into<String>) -> Self {
        Self {
            principal_id: principal_id.into(),
            tenant_id: None,
            groups: Vec::new(),
            roles: Vec::new(),
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_tenant_id(mut self, tenant_id: impl Into<String>) -> Self {
        self.tenant_id = Some(tenant_id.into());
        self
    }

    pub fn with_group(mut self, group: impl Into<String>) -> Self {
        self.groups.push(group.into());
        self
    }

    pub fn with_role(mut self, role: impl Into<String>) -> Self {
        self.roles.push(role.into());
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: Value) -> Self {
        self.attributes.insert(key.into(), value);
        self
    }

    fn digest_value(&self) -> Value {
        json!({
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "groups": self.groups,
            "roles": self.roles,
            "attributes": self.attributes,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ResourceRef {
    pub resource_id: String,
    pub resource_kind: Option<String>,
    pub tenant_id: Option<String>,
    pub attributes: BTreeMap<String, Value>,
}

impl ResourceRef {
    pub fn new(resource_id: impl Into<String>) -> Self {
        Self {
            resource_id: resource_id.into(),
            resource_kind: None,
            tenant_id: None,
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_resource_kind(mut self, resource_kind: impl Into<String>) -> Self {
        self.resource_kind = Some(resource_kind.into());
        self
    }

    pub fn with_tenant_id(mut self, tenant_id: impl Into<String>) -> Self {
        self.tenant_id = Some(tenant_id.into());
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: Value) -> Self {
        self.attributes.insert(key.into(), value);
        self
    }

    fn digest_value(&self) -> Value {
        json!({
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
            "tenant_id": self.tenant_id,
            "attributes": self.attributes,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolicyRequest {
    pub request_id: String,
    pub enforcement_point: EnforcementPoint,
    pub action: String,
    pub resource: ResourceRef,
    pub occurred_at: String,
    pub principal: Option<PrincipalRef>,
    pub tenant: Option<ResourceRef>,
    pub release_id: Option<String>,
    pub deployment_revision_id: Option<String>,
    pub run_id: Option<String>,
    pub atomic_unit: Option<ResourceRef>,
    pub data_labels: Vec<String>,
    pub requested_usage: Vec<Value>,
    pub attributes: BTreeMap<String, Value>,
    pub policy_snapshot_id: Option<String>,
    pub input_digest: String,
}

impl PolicyRequest {
    pub fn new(
        request_id: impl Into<String>,
        enforcement_point: EnforcementPoint,
        action: impl Into<String>,
        resource: ResourceRef,
        occurred_at: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            enforcement_point,
            action: action.into(),
            resource,
            occurred_at: occurred_at.into(),
            principal: None,
            tenant: None,
            release_id: None,
            deployment_revision_id: None,
            run_id: None,
            atomic_unit: None,
            data_labels: Vec::new(),
            requested_usage: Vec::new(),
            attributes: BTreeMap::new(),
            policy_snapshot_id: None,
            input_digest: String::new(),
        }
    }

    pub fn with_request_id(mut self, request_id: impl Into<String>) -> Self {
        self.request_id = request_id.into();
        self
    }

    pub fn with_occurred_at(mut self, occurred_at: impl Into<String>) -> Self {
        self.occurred_at = occurred_at.into();
        self
    }

    pub fn with_action(mut self, action: impl Into<String>) -> Self {
        self.action = action.into();
        self
    }

    pub fn with_principal(mut self, principal: PrincipalRef) -> Self {
        self.principal = Some(principal);
        self
    }

    pub fn with_tenant(mut self, tenant: ResourceRef) -> Self {
        self.tenant = Some(tenant);
        self
    }

    pub fn with_release_id(mut self, release_id: impl Into<String>) -> Self {
        self.release_id = Some(release_id.into());
        self
    }

    pub fn with_deployment_revision_id(
        mut self,
        deployment_revision_id: impl Into<String>,
    ) -> Self {
        self.deployment_revision_id = Some(deployment_revision_id.into());
        self
    }

    pub fn with_run_id(mut self, run_id: impl Into<String>) -> Self {
        self.run_id = Some(run_id.into());
        self
    }

    pub fn with_atomic_unit(mut self, atomic_unit: ResourceRef) -> Self {
        self.atomic_unit = Some(atomic_unit);
        self
    }

    pub fn with_data_label(mut self, data_label: impl Into<String>) -> Self {
        self.data_labels.push(data_label.into());
        self
    }

    pub fn with_requested_usage(mut self, requested_usage: Value) -> Self {
        self.requested_usage.push(requested_usage);
        self
    }

    pub fn with_attribute(mut self, key: impl Into<String>, value: Value) -> Self {
        self.attributes.insert(key.into(), value);
        self
    }

    pub fn with_policy_snapshot_id(mut self, policy_snapshot_id: impl Into<String>) -> Self {
        self.policy_snapshot_id = Some(policy_snapshot_id.into());
        self
    }

    pub fn with_input_digest(mut self) -> Self {
        let payload = json!({
            "enforcement_point": self.enforcement_point.as_str(),
            "action": self.action,
            "principal": self.principal.as_ref().map(PrincipalRef::digest_value),
            "tenant": self.tenant.as_ref().map(ResourceRef::digest_value),
            "resource": self.resource.digest_value(),
            "release_id": self.release_id,
            "deployment_revision_id": self.deployment_revision_id,
            "run_id": self.run_id,
            "atomic_unit": self.atomic_unit.as_ref().map(ResourceRef::digest_value),
            "data_labels": self.data_labels,
            "requested_usage": self.requested_usage,
            "attributes": self.attributes,
            "policy_snapshot_id": self.policy_snapshot_id,
        });
        self.input_digest = canonical_hash(&payload);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolicyEnforcementRecord {
    pub record_id: String,
    pub decision_id: String,
    pub enforcement_point: EnforcementPoint,
    pub status: String,
    pub enforced_obligation_ids: Vec<String>,
    pub occurred_at: String,
    pub metadata: BTreeMap<String, Value>,
}

impl PolicyEnforcementRecord {
    pub fn new(
        record_id: impl Into<String>,
        decision_id: impl Into<String>,
        enforcement_point: EnforcementPoint,
        status: impl Into<String>,
    ) -> Self {
        Self {
            record_id: record_id.into(),
            decision_id: decision_id.into(),
            enforcement_point,
            status: status.into(),
            enforced_obligation_ids: Vec::new(),
            occurred_at: String::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_enforced_obligation_id(mut self, obligation_id: impl Into<String>) -> Self {
        self.enforced_obligation_ids.push(obligation_id.into());
        self
    }

    pub fn with_occurred_at(mut self, occurred_at: impl Into<String>) -> Self {
        self.occurred_at = occurred_at.into();
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }
}
