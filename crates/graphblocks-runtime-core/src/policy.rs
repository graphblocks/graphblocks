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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PolicyEffect {
    Allow,
    Deny,
    AllowWithObligations,
    Defer,
}

impl PolicyEffect {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Deny => "deny",
            Self::AllowWithObligations => "allow_with_obligations",
            Self::Defer => "defer",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuleEffect {
    Allow,
    Deny,
    Obligate,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolicyObligation {
    pub obligation_id: String,
    pub obligation_type: String,
    pub parameters: BTreeMap<String, Value>,
}

impl PolicyObligation {
    pub fn new(obligation_id: impl Into<String>, obligation_type: impl Into<String>) -> Self {
        Self {
            obligation_id: obligation_id.into(),
            obligation_type: obligation_type.into(),
            parameters: BTreeMap::new(),
        }
    }

    pub fn with_parameter(mut self, key: impl Into<String>, value: Value) -> Self {
        self.parameters.insert(key.into(), value);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolicyRule {
    pub rule_id: String,
    pub effect: RuleEffect,
    pub actions: Vec<String>,
    pub resource_selectors: Vec<String>,
    pub principal_selectors: Vec<String>,
    pub obligations: Vec<PolicyObligation>,
    pub priority: i32,
}

impl PolicyRule {
    pub fn new<A, R>(
        rule_id: impl Into<String>,
        effect: RuleEffect,
        actions: A,
        resource_selectors: R,
    ) -> Self
    where
        A: IntoIterator,
        A::Item: Into<String>,
        R: IntoIterator,
        R::Item: Into<String>,
    {
        Self {
            rule_id: rule_id.into(),
            effect,
            actions: actions.into_iter().map(Into::into).collect(),
            resource_selectors: resource_selectors.into_iter().map(Into::into).collect(),
            principal_selectors: Vec::new(),
            obligations: Vec::new(),
            priority: 0,
        }
    }

    pub fn with_principal_selector(mut self, selector: impl Into<String>) -> Self {
        self.principal_selectors.push(selector.into());
        self
    }

    pub fn with_obligation(mut self, obligation: PolicyObligation) -> Self {
        self.obligations.push(obligation);
        self
    }

    pub fn with_priority(mut self, priority: i32) -> Self {
        self.priority = priority;
        self
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
pub struct PolicyDecision {
    pub decision_id: String,
    pub effect: PolicyEffect,
    pub reason_codes: Vec<String>,
    pub policy_refs: Vec<String>,
    pub obligations: Vec<PolicyObligation>,
    pub advice: Vec<Value>,
    pub evaluated_at: String,
    pub valid_until: Option<String>,
    pub input_digest: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct StaticPolicyEvaluator {
    pub rules: Vec<PolicyRule>,
}

impl StaticPolicyEvaluator {
    pub fn new<I>(rules: I) -> Self
    where
        I: IntoIterator<Item = PolicyRule>,
    {
        Self {
            rules: rules.into_iter().collect(),
        }
    }

    pub fn evaluate(
        &self,
        request: &PolicyRequest,
        evaluated_at: impl Into<String>,
    ) -> PolicyDecision {
        let digested_request = request.clone().with_input_digest();
        let mut matching_deny = Vec::new();
        let mut matching_allow = Vec::new();
        let mut matching_obligate = Vec::new();

        for rule in &self.rules {
            let action_matches = rule.actions.iter().any(|action| action == "*")
                || rule.actions.iter().any(|action| action == &request.action);
            let resource_kind_matches = request
                .resource
                .resource_kind
                .as_ref()
                .map(|kind| {
                    rule.resource_selectors
                        .iter()
                        .any(|selector| selector == kind)
                })
                .unwrap_or(false);
            let resource_matches = rule
                .resource_selectors
                .iter()
                .any(|selector| selector == "*")
                || rule
                    .resource_selectors
                    .iter()
                    .any(|selector| selector == &request.resource.resource_id)
                || resource_kind_matches;
            let principal_matches = if rule.principal_selectors.is_empty()
                || rule
                    .principal_selectors
                    .iter()
                    .any(|selector| selector == "*")
            {
                true
            } else if let Some(principal) = &request.principal {
                rule.principal_selectors
                    .iter()
                    .any(|selector| selector == &principal.principal_id)
                    || principal.groups.iter().any(|group| {
                        rule.principal_selectors
                            .iter()
                            .any(|selector| selector == group)
                    })
                    || principal.roles.iter().any(|role| {
                        rule.principal_selectors
                            .iter()
                            .any(|selector| selector == role)
                    })
            } else {
                false
            };

            if !action_matches || !resource_matches || !principal_matches {
                continue;
            }

            match rule.effect {
                RuleEffect::Deny => matching_deny.push(rule.clone()),
                RuleEffect::Allow => matching_allow.push(rule.clone()),
                RuleEffect::Obligate => matching_obligate.push(rule.clone()),
            }
        }

        if !matching_deny.is_empty() {
            matching_deny.sort_by(|left, right| {
                right
                    .priority
                    .cmp(&left.priority)
                    .then_with(|| left.rule_id.cmp(&right.rule_id))
            });
            let policy_refs = matching_deny
                .iter()
                .map(|rule| rule.rule_id.clone())
                .collect::<Vec<_>>();
            return policy_decision(
                PolicyEffect::Deny,
                policy_refs,
                Vec::new(),
                evaluated_at.into(),
                digested_request.input_digest,
            );
        }

        if !matching_allow.is_empty() || !matching_obligate.is_empty() {
            let mut policy_refs = matching_allow
                .iter()
                .map(|rule| rule.rule_id.clone())
                .collect::<Vec<_>>();
            policy_refs.extend(matching_obligate.iter().map(|rule| rule.rule_id.clone()));
            let obligations = matching_obligate
                .into_iter()
                .flat_map(|rule| rule.obligations)
                .collect::<Vec<_>>();
            let effect = if obligations.is_empty() {
                PolicyEffect::Allow
            } else {
                PolicyEffect::AllowWithObligations
            };
            return policy_decision(
                effect,
                policy_refs,
                obligations,
                evaluated_at.into(),
                digested_request.input_digest,
            );
        }

        policy_decision(
            PolicyEffect::Deny,
            vec!["default_deny".to_string()],
            Vec::new(),
            evaluated_at.into(),
            digested_request.input_digest,
        )
    }
}

fn policy_decision(
    effect: PolicyEffect,
    policy_refs: Vec<String>,
    obligations: Vec<PolicyObligation>,
    evaluated_at: String,
    input_digest: String,
) -> PolicyDecision {
    let decision_id = "decision:".to_string()
        + &canonical_hash(&json!({
            "input_digest": input_digest,
            "effect": effect.as_str(),
            "policy_refs": policy_refs,
        }));
    PolicyDecision {
        decision_id,
        effect,
        reason_codes: policy_refs.clone(),
        policy_refs,
        obligations,
        advice: Vec::new(),
        evaluated_at,
        valid_until: None,
        input_digest,
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
