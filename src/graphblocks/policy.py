from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .canonical import canonical_hash


PolicyEffect = Literal["allow", "deny", "allow_with_obligations", "defer"]
RuleEffect = Literal["allow", "deny", "obligate"]
EnforcementPoint = Literal[
    "compile",
    "release",
    "admission",
    "before_node",
    "before_provider_call",
    "on_usage_delta",
    "before_tool_or_effect",
    "before_commit",
    "before_publish",
    "on_resume",
]


@dataclass(frozen=True, slots=True)
class PrincipalRef:
    principal_id: str
    tenant_id: str | None = None
    groups: tuple[str, ...] = field(default_factory=tuple)
    roles: tuple[str, ...] = field(default_factory=tuple)
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    resource_id: str
    resource_kind: str | None = None
    tenant_id: str | None = None
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyObligation:
    obligation_id: str
    obligation_type: str
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyRule:
    rule_id: str
    effect: RuleEffect
    actions: tuple[str, ...]
    resource_selectors: tuple[str, ...]
    principal_selectors: tuple[str, ...] = field(default_factory=tuple)
    obligations: tuple[PolicyObligation, ...] = field(default_factory=tuple)
    priority: int = 0


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    request_id: str
    enforcement_point: EnforcementPoint
    action: str
    resource: ResourceRef
    occurred_at: str
    principal: PrincipalRef | None = None
    tenant: ResourceRef | None = None
    release_id: str | None = None
    deployment_revision_id: str | None = None
    run_id: str | None = None
    atomic_unit: ResourceRef | None = None
    data_labels: tuple[str, ...] = field(default_factory=tuple)
    requested_usage: tuple[dict[str, object], ...] = field(default_factory=tuple)
    attributes: dict[str, object] = field(default_factory=dict)
    policy_snapshot_id: str | None = None
    input_digest: str = ""

    def with_input_digest(self) -> PolicyRequest:
        principal = None
        if self.principal is not None:
            principal = {
                "principal_id": self.principal.principal_id,
                "tenant_id": self.principal.tenant_id,
                "groups": list(self.principal.groups),
                "roles": list(self.principal.roles),
                "attributes": self.principal.attributes,
            }
        tenant = None
        if self.tenant is not None:
            tenant = {
                "resource_id": self.tenant.resource_id,
                "resource_kind": self.tenant.resource_kind,
                "tenant_id": self.tenant.tenant_id,
                "attributes": self.tenant.attributes,
            }
        atomic_unit = None
        if self.atomic_unit is not None:
            atomic_unit = {
                "resource_id": self.atomic_unit.resource_id,
                "resource_kind": self.atomic_unit.resource_kind,
                "tenant_id": self.atomic_unit.tenant_id,
                "attributes": self.atomic_unit.attributes,
            }
        payload = {
            "enforcement_point": self.enforcement_point,
            "action": self.action,
            "principal": principal,
            "tenant": tenant,
            "resource": {
                "resource_id": self.resource.resource_id,
                "resource_kind": self.resource.resource_kind,
                "tenant_id": self.resource.tenant_id,
                "attributes": self.resource.attributes,
            },
            "release_id": self.release_id,
            "deployment_revision_id": self.deployment_revision_id,
            "run_id": self.run_id,
            "atomic_unit": atomic_unit,
            "data_labels": list(self.data_labels),
            "requested_usage": list(self.requested_usage),
            "attributes": self.attributes,
            "policy_snapshot_id": self.policy_snapshot_id,
        }
        return replace(self, input_digest=canonical_hash(payload))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision_id: str
    effect: PolicyEffect
    reason_codes: list[str]
    policy_refs: list[str]
    obligations: list[PolicyObligation] = field(default_factory=list)
    advice: list[dict[str, object]] = field(default_factory=list)
    evaluated_at: str = ""
    valid_until: str | None = None
    input_digest: str = ""


@dataclass(frozen=True, slots=True)
class PolicyEnforcementRecord:
    record_id: str
    decision_id: str
    enforcement_point: EnforcementPoint
    status: Literal["enforced", "blocked", "deferred", "failed"]
    enforced_obligation_ids: tuple[str, ...] = field(default_factory=tuple)
    occurred_at: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class StaticPolicyEvaluator:
    rules: list[PolicyRule] = field(default_factory=list)

    def evaluate(self, request: PolicyRequest, evaluated_at: str) -> PolicyDecision:
        digested_request = request.with_input_digest()
        matching_deny: list[PolicyRule] = []
        matching_allow: list[PolicyRule] = []
        matching_obligate: list[PolicyRule] = []
        for rule in self.rules:
            action_matches = "*" in rule.actions or request.action in rule.actions
            resource_values = {request.resource.resource_id}
            if request.resource.resource_kind is not None:
                resource_values.add(request.resource.resource_kind)
            resource_matches = "*" in rule.resource_selectors or any(
                selector in resource_values for selector in rule.resource_selectors
            )
            principal_matches = not rule.principal_selectors or "*" in rule.principal_selectors
            if request.principal is not None:
                principal_values = {request.principal.principal_id, *request.principal.groups, *request.principal.roles}
                principal_matches = principal_matches or any(
                    selector in principal_values for selector in rule.principal_selectors
                )
            if not action_matches or not resource_matches or not principal_matches:
                continue
            if rule.effect == "deny":
                matching_deny.append(rule)
            elif rule.effect == "allow":
                matching_allow.append(rule)
            else:
                matching_obligate.append(rule)

        if matching_deny:
            matching_deny.sort(key=lambda rule: (-rule.priority, rule.rule_id))
            policy_refs = [rule.rule_id for rule in matching_deny]
            decision_id = "decision:" + canonical_hash(
                {"input_digest": digested_request.input_digest, "effect": "deny", "policy_refs": policy_refs}
            )
            return PolicyDecision(
                decision_id=decision_id,
                effect="deny",
                reason_codes=policy_refs,
                policy_refs=policy_refs,
                evaluated_at=evaluated_at,
                input_digest=digested_request.input_digest,
            )

        if matching_allow or matching_obligate:
            policy_refs = [rule.rule_id for rule in matching_allow] + [rule.rule_id for rule in matching_obligate]
            obligations = [obligation for rule in matching_obligate for obligation in rule.obligations]
            effect: PolicyEffect = "allow_with_obligations" if obligations else "allow"
            decision_id = "decision:" + canonical_hash(
                {"input_digest": digested_request.input_digest, "effect": effect, "policy_refs": policy_refs}
            )
            return PolicyDecision(
                decision_id=decision_id,
                effect=effect,
                reason_codes=policy_refs,
                policy_refs=policy_refs,
                obligations=obligations,
                evaluated_at=evaluated_at,
                input_digest=digested_request.input_digest,
            )

        decision_id = "decision:" + canonical_hash(
            {"input_digest": digested_request.input_digest, "effect": "deny", "policy_refs": ["default_deny"]}
        )
        return PolicyDecision(
            decision_id=decision_id,
            effect="deny",
            reason_codes=["default_deny"],
            policy_refs=[],
            evaluated_at=evaluated_at,
            input_digest=digested_request.input_digest,
        )
