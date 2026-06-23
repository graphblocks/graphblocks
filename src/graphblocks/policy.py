from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from types import MappingProxyType
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
    "on_generation_chunk",
    "before_client_delivery",
    "before_output_commit",
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
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "groups", tuple(self.groups))
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class ResourceRef:
    resource_id: str
    resource_kind: str | None = None
    tenant_id: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class PolicyObligation:
    obligation_id: str
    obligation_type: str
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


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
class PolicyBundle:
    bundle_id: str
    version: str
    rule_language: str
    rules: tuple[PolicyRule, ...] = field(default_factory=tuple)
    external_evaluator_ref: str | None = None
    obligation_schema_versions: tuple[str, ...] = field(default_factory=tuple)
    default_fail_modes: dict[str, str] = field(default_factory=dict)
    signature_ref: str | None = None

    def content_digest(self) -> str:
        return canonical_hash(
            _policy_value(
                {
                    "version": self.version,
                    "rule_language": self.rule_language,
                    "rules": self.rules,
                    "external_evaluator_ref": self.external_evaluator_ref,
                    "obligation_schema_versions": self.obligation_schema_versions,
                    "default_fail_modes": self.default_fail_modes,
                }
            )
        )

    @property
    def ref(self) -> str:
        return f"{self.bundle_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class PolicyProfile:
    profile_id: str
    bundle_refs: tuple[str, ...]
    scope_selectors: tuple[str, ...]
    quota_accounts: dict[str, object] = field(default_factory=dict)
    budgets: dict[str, object] = field(default_factory=dict)
    thresholds: tuple[dict[str, object], ...] = field(default_factory=tuple)
    exhaustion: dict[str, object] | None = None
    affinity: Literal["pinned", "boundary_refresh", "live"] = "pinned"
    capture: dict[str, object] = field(default_factory=dict)
    required_reviews: tuple[str, ...] = field(default_factory=tuple)
    required_gates: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class EntitlementSnapshot:
    snapshot_id: str
    subject: PrincipalRef
    scopes: tuple[ResourceRef, ...]
    source_revision: str
    resolved_at: str
    plan_id: str | None = None
    policy_profile_refs: tuple[str, ...] = field(default_factory=tuple)
    grants: tuple[str, ...] = field(default_factory=tuple)
    budget_grants: tuple[str, ...] = field(default_factory=tuple)
    overrides: tuple[str, ...] = field(default_factory=tuple)
    valid_until: str | None = None

    def content_digest(self) -> str:
        return canonical_hash(
            _policy_value(
                {
                    "subject": self.subject,
                    "scopes": self.scopes,
                    "source_revision": self.source_revision,
                    "plan_id": self.plan_id,
                    "policy_profile_refs": self.policy_profile_refs,
                    "grants": self.grants,
                    "budget_grants": self.budget_grants,
                    "overrides": self.overrides,
                    "valid_until": self.valid_until,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    snapshot_id: str
    effective_policy_digest: str
    policy_bundle_refs: tuple[str, ...]
    profile_ref: str
    affinity: Literal["pinned", "boundary_refresh", "live"]
    issued_at: str
    entitlement_snapshot_ref: str | None = None
    pricing_revision: str | None = None
    quota_window_ids: tuple[str, ...] = field(default_factory=tuple)
    valid_until: str | None = None


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
    requested_usage: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    attributes: Mapping[str, object] = field(default_factory=dict)
    policy_snapshot_id: str | None = None
    input_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_labels", tuple(self.data_labels))
        object.__setattr__(
            self,
            "requested_usage",
            tuple(MappingProxyType(dict(usage)) for usage in self.requested_usage),
        )
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

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
        return replace(self, input_digest=canonical_hash(_policy_value(payload)))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision_id: str
    effect: PolicyEffect
    reason_codes: tuple[str, ...]
    policy_refs: tuple[str, ...]
    obligations: tuple[PolicyObligation, ...] = field(default_factory=tuple)
    advice: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    evaluated_at: str = ""
    valid_until: str | None = None
    input_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "policy_refs", tuple(self.policy_refs))
        object.__setattr__(self, "obligations", tuple(self.obligations))
        object.__setattr__(self, "advice", tuple(MappingProxyType(dict(item)) for item in self.advice))


@dataclass(frozen=True, slots=True)
class PolicyEnforcementRecord:
    record_id: str
    decision_id: str
    enforcement_point: EnforcementPoint
    status: Literal["enforced", "blocked", "deferred", "failed"]
    enforced_obligation_ids: tuple[str, ...] = field(default_factory=tuple)
    occurred_at: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_decision(
        cls,
        *,
        record_id: str,
        decision: PolicyDecision,
        enforcement_point: EnforcementPoint,
        status: Literal["enforced", "blocked", "deferred", "failed"],
        enforced_obligation_ids: tuple[str, ...] = (),
        occurred_at: str = "",
        metadata: dict[str, object] | None = None,
    ) -> PolicyEnforcementRecord:
        known_obligation_ids = {obligation.obligation_id for obligation in decision.obligations}
        for obligation_id in enforced_obligation_ids:
            if obligation_id not in known_obligation_ids:
                raise ValueError(
                    f"unknown policy obligation {obligation_id!r} for decision {decision.decision_id!r}"
                )
        return cls(
            record_id=record_id,
            decision_id=decision.decision_id,
            enforcement_point=enforcement_point,
            status=status,
            enforced_obligation_ids=tuple(enforced_obligation_ids),
            occurred_at=occurred_at,
            metadata=dict(metadata or {}),
        )


@dataclass(slots=True)
class StaticPolicyEvaluator:
    rules: list[PolicyRule] = field(default_factory=list)

    @classmethod
    def from_bundles(cls, bundles: list[PolicyBundle]) -> StaticPolicyEvaluator:
        rules: list[PolicyRule] = []
        for bundle in sorted(bundles, key=lambda item: item.ref):
            rules.extend(bundle.rules)
        return cls(rules)

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


def resolve_policy_snapshot(
    *,
    snapshot_id: str,
    profile: PolicyProfile,
    bundles: list[PolicyBundle],
    issued_at: str,
    entitlement: EntitlementSnapshot | None = None,
    pricing_revision: str | None = None,
    quota_window_ids: tuple[str, ...] = (),
    valid_until: str | None = None,
) -> PolicySnapshot:
    ordered_bundles = sorted(bundles, key=lambda item: item.ref)
    effective_policy_digest = canonical_hash(
        _policy_value(
            {
                "profile": profile,
                "bundles": [(bundle.ref, bundle.content_digest()) for bundle in ordered_bundles],
                "entitlement": None if entitlement is None else entitlement.content_digest(),
                "pricing_revision": pricing_revision,
                "quota_window_ids": quota_window_ids,
            }
        )
    )
    return PolicySnapshot(
        snapshot_id=snapshot_id,
        effective_policy_digest=effective_policy_digest,
        policy_bundle_refs=tuple(bundle.ref for bundle in ordered_bundles),
        profile_ref=profile.profile_id,
        entitlement_snapshot_ref=None if entitlement is None else entitlement.snapshot_id,
        pricing_revision=pricing_revision,
        quota_window_ids=quota_window_ids,
        affinity=profile.affinity,
        issued_at=issued_at,
        valid_until=valid_until,
    )


def _policy_value(value: object) -> object:
    if is_dataclass(value):
        return {field.name: _policy_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _policy_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_policy_value(item) for item in value]
    return value
