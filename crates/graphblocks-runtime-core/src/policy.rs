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
pub enum PolicyFailMode {
    FailClosed,
    FailOpenWithAudit,
    UseCachedDecision,
    Defer,
}

impl PolicyFailMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::FailClosed => "fail_closed",
            Self::FailOpenWithAudit => "fail_open_with_audit",
            Self::UseCachedDecision => "use_cached_decision",
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

impl RuleEffect {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Deny => "deny",
            Self::Obligate => "obligate",
        }
    }
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

    fn digest_value(&self) -> Value {
        json!({
            "obligation_id": self.obligation_id,
            "obligation_type": self.obligation_type,
            "parameters": self.parameters,
        })
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

    fn digest_value(&self) -> Value {
        json!({
            "rule_id": self.rule_id,
            "effect": self.effect.as_str(),
            "actions": self.actions,
            "resource_selectors": self.resource_selectors,
            "principal_selectors": self.principal_selectors,
            "obligations": self
                .obligations
                .iter()
                .map(PolicyObligation::digest_value)
                .collect::<Vec<_>>(),
            "priority": self.priority,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolicyBundle {
    pub bundle_id: String,
    pub version: String,
    pub rule_language: String,
    pub rules: Vec<PolicyRule>,
    pub external_evaluator_ref: Option<String>,
    pub obligation_schema_versions: Vec<String>,
    pub default_fail_modes: BTreeMap<String, String>,
    pub signature_ref: Option<String>,
}

impl PolicyBundle {
    pub fn new<I>(
        bundle_id: impl Into<String>,
        version: impl Into<String>,
        rule_language: impl Into<String>,
        rules: I,
    ) -> Self
    where
        I: IntoIterator<Item = PolicyRule>,
    {
        Self {
            bundle_id: bundle_id.into(),
            version: version.into(),
            rule_language: rule_language.into(),
            rules: rules.into_iter().collect(),
            external_evaluator_ref: None,
            obligation_schema_versions: Vec::new(),
            default_fail_modes: BTreeMap::new(),
            signature_ref: None,
        }
    }

    pub fn reference(&self) -> String {
        format!("{}@{}", self.bundle_id, self.version)
    }

    pub fn with_external_evaluator_ref(
        mut self,
        external_evaluator_ref: impl Into<String>,
    ) -> Self {
        self.external_evaluator_ref = Some(external_evaluator_ref.into());
        self
    }

    pub fn with_obligation_schema_version(mut self, version: impl Into<String>) -> Self {
        self.obligation_schema_versions.push(version.into());
        self
    }

    pub fn with_default_fail_mode(
        mut self,
        point: impl Into<String>,
        mode: impl Into<String>,
    ) -> Self {
        self.default_fail_modes.insert(point.into(), mode.into());
        self
    }

    pub fn with_signature_ref(mut self, signature_ref: impl Into<String>) -> Self {
        self.signature_ref = Some(signature_ref.into());
        self
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "version": self.version,
            "rule_language": self.rule_language,
            "rules": self.rules.iter().map(PolicyRule::digest_value).collect::<Vec<_>>(),
            "external_evaluator_ref": self.external_evaluator_ref,
            "obligation_schema_versions": self.obligation_schema_versions,
            "default_fail_modes": self.default_fail_modes,
        }))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolicyProfile {
    pub profile_id: String,
    pub bundle_refs: Vec<String>,
    pub scope_selectors: Vec<String>,
    pub quota_accounts: BTreeMap<String, Value>,
    pub budgets: BTreeMap<String, Value>,
    pub thresholds: Vec<Value>,
    pub exhaustion: Option<Value>,
    pub affinity: String,
    pub capture: BTreeMap<String, Value>,
    pub required_reviews: Vec<String>,
    pub required_gates: Vec<String>,
}

impl PolicyProfile {
    pub fn new<B, S>(profile_id: impl Into<String>, bundle_refs: B, scope_selectors: S) -> Self
    where
        B: IntoIterator,
        B::Item: Into<String>,
        S: IntoIterator,
        S::Item: Into<String>,
    {
        Self {
            profile_id: profile_id.into(),
            bundle_refs: bundle_refs.into_iter().map(Into::into).collect(),
            scope_selectors: scope_selectors.into_iter().map(Into::into).collect(),
            quota_accounts: BTreeMap::new(),
            budgets: BTreeMap::new(),
            thresholds: Vec::new(),
            exhaustion: None,
            affinity: "pinned".to_string(),
            capture: BTreeMap::new(),
            required_reviews: Vec::new(),
            required_gates: Vec::new(),
        }
    }

    pub fn with_affinity(mut self, affinity: impl Into<String>) -> Self {
        self.affinity = affinity.into();
        self
    }

    fn digest_value(&self) -> Value {
        json!({
            "profile_id": self.profile_id,
            "bundle_refs": self.bundle_refs,
            "scope_selectors": self.scope_selectors,
            "quota_accounts": self.quota_accounts,
            "budgets": self.budgets,
            "thresholds": self.thresholds,
            "exhaustion": self.exhaustion,
            "affinity": self.affinity,
            "capture": self.capture,
            "required_reviews": self.required_reviews,
            "required_gates": self.required_gates,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct EntitlementSnapshot {
    pub snapshot_id: String,
    pub subject: PrincipalRef,
    pub scopes: Vec<ResourceRef>,
    pub source_revision: String,
    pub resolved_at: String,
    pub plan_id: Option<String>,
    pub policy_profile_refs: Vec<String>,
    pub grants: Vec<String>,
    pub budget_grants: Vec<String>,
    pub overrides: Vec<String>,
    pub valid_until: Option<String>,
}

impl EntitlementSnapshot {
    pub fn new<I>(
        snapshot_id: impl Into<String>,
        subject: PrincipalRef,
        scopes: I,
        source_revision: impl Into<String>,
        resolved_at: impl Into<String>,
    ) -> Self
    where
        I: IntoIterator<Item = ResourceRef>,
    {
        Self {
            snapshot_id: snapshot_id.into(),
            subject,
            scopes: scopes.into_iter().collect(),
            source_revision: source_revision.into(),
            resolved_at: resolved_at.into(),
            plan_id: None,
            policy_profile_refs: Vec::new(),
            grants: Vec::new(),
            budget_grants: Vec::new(),
            overrides: Vec::new(),
            valid_until: None,
        }
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "subject": self.subject.digest_value(),
            "scopes": self.scopes.iter().map(ResourceRef::digest_value).collect::<Vec<_>>(),
            "source_revision": self.source_revision,
            "plan_id": self.plan_id,
            "policy_profile_refs": self.policy_profile_refs,
            "grants": self.grants,
            "budget_grants": self.budget_grants,
            "overrides": self.overrides,
            "valid_until": self.valid_until,
        }))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolicySnapshot {
    pub snapshot_id: String,
    pub effective_policy_digest: String,
    pub policy_bundle_refs: Vec<String>,
    pub profile_ref: String,
    pub affinity: String,
    pub issued_at: String,
    pub entitlement_snapshot_ref: Option<String>,
    pub pricing_revision: Option<String>,
    pub quota_window_ids: Vec<String>,
    pub valid_until: Option<String>,
}

pub fn resolve_policy_snapshot(
    snapshot_id: impl Into<String>,
    profile: &PolicyProfile,
    bundles: &[PolicyBundle],
    entitlement: Option<&EntitlementSnapshot>,
    issued_at: impl Into<String>,
) -> PolicySnapshot {
    let mut ordered_bundles = bundles.iter().collect::<Vec<_>>();
    ordered_bundles.sort_by_key(|bundle| bundle.reference());
    let policy_bundle_refs = ordered_bundles
        .iter()
        .map(|bundle| bundle.reference())
        .collect::<Vec<_>>();
    let bundle_digests = ordered_bundles
        .iter()
        .map(|bundle| json!([bundle.reference(), bundle.content_digest()]))
        .collect::<Vec<_>>();
    let entitlement_digest = entitlement.map(EntitlementSnapshot::content_digest);
    let effective_policy_digest = canonical_hash(&json!({
        "profile": profile.digest_value(),
        "bundles": bundle_digests,
        "entitlement": entitlement_digest,
        "pricing_revision": Value::Null,
        "quota_window_ids": Vec::<String>::new(),
    }));

    PolicySnapshot {
        snapshot_id: snapshot_id.into(),
        effective_policy_digest,
        policy_bundle_refs,
        profile_ref: profile.profile_id.clone(),
        affinity: profile.affinity.clone(),
        issued_at: issued_at.into(),
        entitlement_snapshot_ref: entitlement.map(|snapshot| snapshot.snapshot_id.clone()),
        pricing_revision: None,
        quota_window_ids: Vec::new(),
        valid_until: None,
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

    pub fn from_bundles<I>(bundles: I) -> Self
    where
        I: IntoIterator<Item = PolicyBundle>,
    {
        let mut bundles = bundles.into_iter().collect::<Vec<_>>();
        bundles.sort_by_key(PolicyBundle::reference);
        Self {
            rules: bundles
                .into_iter()
                .flat_map(|bundle| bundle.rules)
                .collect(),
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PolicyUnavailableError {
    CachedDecisionRequired,
    CachedDecisionInputDigestMismatch,
    CachedDecisionExpired,
}

pub fn unavailable_policy_decision(
    request: &PolicyRequest,
    fail_mode: PolicyFailMode,
    evaluated_at: impl Into<String>,
    cached_decision: Option<&PolicyDecision>,
) -> Result<PolicyDecision, PolicyUnavailableError> {
    let evaluated_at = evaluated_at.into();
    let digested_request = request.clone().with_input_digest();

    if fail_mode == PolicyFailMode::UseCachedDecision {
        let Some(cached_decision) = cached_decision else {
            return Err(PolicyUnavailableError::CachedDecisionRequired);
        };
        if cached_decision.input_digest != digested_request.input_digest {
            return Err(PolicyUnavailableError::CachedDecisionInputDigestMismatch);
        }
        if cached_decision_is_expired(cached_decision.valid_until.as_deref(), &evaluated_at) {
            return Err(PolicyUnavailableError::CachedDecisionExpired);
        }
        return Ok(cached_decision.clone());
    }

    let (effect, obligations) = match fail_mode {
        PolicyFailMode::FailClosed => (PolicyEffect::Deny, Vec::new()),
        PolicyFailMode::FailOpenWithAudit => (
            PolicyEffect::AllowWithObligations,
            vec![
                PolicyObligation::new("policy_unavailable_audit", "capture_audit")
                    .with_parameter("fail_mode", json!(fail_mode.as_str()))
                    .with_parameter(
                        "enforcement_point",
                        json!(request.enforcement_point.as_str()),
                    ),
            ],
        ),
        PolicyFailMode::Defer => (PolicyEffect::Defer, Vec::new()),
        PolicyFailMode::UseCachedDecision => {
            return Err(PolicyUnavailableError::CachedDecisionRequired);
        }
    };

    Ok(policy_decision(
        effect,
        vec![
            "policy_unavailable".to_string(),
            fail_mode.as_str().to_string(),
        ],
        obligations,
        evaluated_at,
        digested_request.input_digest,
    ))
}

fn cached_decision_is_expired(valid_until: Option<&str>, evaluated_at: &str) -> bool {
    let Some(valid_until) = valid_until else {
        return true;
    };
    match (
        parse_policy_datetime_millis(valid_until),
        parse_policy_datetime_millis(evaluated_at),
    ) {
        (Some(valid_until), Some(evaluated_at)) => valid_until <= evaluated_at,
        _ => true,
    }
}

pub(crate) fn parse_policy_datetime_millis(value: &str) -> Option<i128> {
    let value = value.trim();
    if value.is_empty() {
        return None;
    }
    let separator = value.find('T').or_else(|| value.find('t'))?;
    let (date, time_with_offset) = value.split_at(separator);
    let time_with_offset = &time_with_offset[1..];
    let (year, month, day) = parse_policy_date(date)?;
    let (time, offset_seconds) = split_policy_time_offset(time_with_offset)?;
    let (hour, minute, second, fractional_millis) = parse_policy_time(time)?;
    if month == 0
        || month > 12
        || day == 0
        || day > days_in_month(year, month)?
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    let days = days_from_civil(year, month, day);
    Some(
        (((((days * 24) + i128::from(hour)) * 60 + i128::from(minute)) * 60 + i128::from(second)
            - i128::from(offset_seconds))
            * 1_000)
            + i128::from(fractional_millis),
    )
}

fn parse_policy_date(value: &str) -> Option<(i128, u32, u32)> {
    if value.len() != 10 || &value[4..5] != "-" || &value[7..8] != "-" {
        return None;
    }
    Some((
        value[0..4].parse().ok()?,
        value[5..7].parse().ok()?,
        value[8..10].parse().ok()?,
    ))
}

fn split_policy_time_offset(value: &str) -> Option<(&str, i32)> {
    if let Some(time) = value.strip_suffix('Z').or_else(|| value.strip_suffix('z')) {
        return Some((time, 0));
    }
    for (index, character) in value.char_indices().rev() {
        if character == '+' || character == '-' {
            let offset = &value[index..];
            let sign = if character == '+' { 1 } else { -1 };
            if offset.len() != 6 || &offset[3..4] != ":" {
                return None;
            }
            let hours: i32 = offset[1..3].parse().ok()?;
            let minutes: i32 = offset[4..6].parse().ok()?;
            if hours > 23 || minutes > 59 {
                return None;
            }
            return Some((&value[..index], sign * ((hours * 60 + minutes) * 60)));
        }
    }
    Some((value, 0))
}

fn parse_policy_time(value: &str) -> Option<(u32, u32, u32, u32)> {
    let (base, fraction) = value
        .split_once('.')
        .map_or((value, ""), |(base, fraction)| (base, fraction));
    if base.len() != 8 || &base[2..3] != ":" || &base[5..6] != ":" {
        return None;
    }
    let fractional_millis = if fraction.is_empty() {
        0
    } else {
        if !fraction.chars().all(|character| character.is_ascii_digit()) {
            return None;
        }
        let mut millis = 0;
        for (index, character) in fraction.chars().take(3).enumerate() {
            millis += character.to_digit(10)? * 10_u32.pow(2 - index as u32);
        }
        millis
    };
    Some((
        base[0..2].parse().ok()?,
        base[3..5].parse().ok()?,
        base[6..8].parse().ok()?,
        fractional_millis,
    ))
}

fn days_in_month(year: i128, month: u32) -> Option<u32> {
    Some(match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        _ => return None,
    })
}

fn is_leap_year(year: i128) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn days_from_civil(year: i128, month: u32, day: u32) -> i128 {
    let year = year - i128::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let month = i128::from(month);
    let day = i128::from(day);
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PolicyEnforcementRecordError {
    UnknownObligation { obligation_id: String },
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

    pub fn from_decision<I>(
        record_id: impl Into<String>,
        decision: &PolicyDecision,
        enforcement_point: EnforcementPoint,
        status: impl Into<String>,
        enforced_obligation_ids: I,
        occurred_at: impl Into<String>,
    ) -> Result<Self, PolicyEnforcementRecordError>
    where
        I: IntoIterator,
        I::Item: Into<String>,
    {
        let enforced_obligation_ids = enforced_obligation_ids
            .into_iter()
            .map(Into::into)
            .collect::<Vec<_>>();
        for obligation_id in &enforced_obligation_ids {
            if !decision
                .obligations
                .iter()
                .any(|obligation| obligation.obligation_id == *obligation_id)
            {
                return Err(PolicyEnforcementRecordError::UnknownObligation {
                    obligation_id: obligation_id.clone(),
                });
            }
        }

        let mut record = Self::new(
            record_id,
            decision.decision_id.clone(),
            enforcement_point,
            status,
        );
        record.enforced_obligation_ids = enforced_obligation_ids;
        record.occurred_at = occurred_at.into();
        Ok(record)
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
