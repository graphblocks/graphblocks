from __future__ import annotations

from graphblocks.policy import (
    EntitlementSnapshot,
    PolicyBundle,
    PolicyObligation,
    PolicyProfile,
    PolicyRequest,
    PolicyRule,
    PrincipalRef,
    ResourceRef,
    StaticPolicyEvaluator,
    resolve_policy_snapshot,
)


def test_policy_bundle_digest_is_stable_for_rule_content() -> None:
    rule = PolicyRule("allow-model", "allow", actions=("model.generate",), resource_selectors=("model",))
    bundle = PolicyBundle("bundle-1", "1.0.0", rule_language="graphblocks.declarative@1", rules=(rule,))
    same_rules = PolicyBundle("bundle-copy", "1.0.0", rule_language="graphblocks.declarative@1", rules=(rule,))

    assert bundle.content_digest().startswith("sha256:")
    assert same_rules.content_digest() == bundle.content_digest()


def test_resolve_policy_snapshot_pins_effective_policy_identity() -> None:
    bundle = PolicyBundle(
        "bundle-1",
        "1.0.0",
        rule_language="graphblocks.declarative@1",
        rules=(PolicyRule("allow-model", "allow", actions=("model.generate",), resource_selectors=("model",)),),
    )
    profile = PolicyProfile(
        profile_id="profile-1",
        bundle_refs=("bundle-1",),
        scope_selectors=("tenant:acme",),
        affinity="pinned",
    )
    entitlement = EntitlementSnapshot(
        snapshot_id="ent-1",
        subject=PrincipalRef("user-1", tenant_id="tenant-1"),
        scopes=(ResourceRef("tenant:acme"),),
        source_revision="rev-1",
        resolved_at="2026-06-22T00:00:00Z",
    )

    snapshot = resolve_policy_snapshot(
        snapshot_id="policy-snapshot-1",
        profile=profile,
        bundles=[bundle],
        entitlement=entitlement,
        issued_at="2026-06-22T00:01:00Z",
    )
    same_snapshot = resolve_policy_snapshot(
        snapshot_id="policy-snapshot-2",
        profile=profile,
        bundles=[bundle],
        entitlement=entitlement,
        issued_at="2026-06-22T00:02:00Z",
    )

    assert snapshot.profile_ref == "profile-1"
    assert snapshot.policy_bundle_refs == ("bundle-1@1.0.0",)
    assert snapshot.entitlement_snapshot_ref == "ent-1"
    assert snapshot.affinity == "pinned"
    assert snapshot.effective_policy_digest == same_snapshot.effective_policy_digest


def test_static_policy_evaluator_can_be_built_from_policy_bundle() -> None:
    obligation = PolicyObligation("obl-1", "force_sandbox", {"level": "strict"})
    bundle = PolicyBundle(
        "bundle-1",
        "1.0.0",
        rule_language="graphblocks.declarative@1",
        rules=(
            PolicyRule("allow-tools", "allow", actions=("tool.run",), resource_selectors=("*",)),
            PolicyRule("sandbox-tools", "obligate", actions=("tool.run",), resource_selectors=("*",), obligations=(obligation,)),
        ),
    )
    request = PolicyRequest(
        request_id="req-1",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        resource=ResourceRef("tool:exec", resource_kind="tool"),
        occurred_at="2026-06-22T00:00:00Z",
    )

    decision = StaticPolicyEvaluator.from_bundles([bundle]).evaluate(request, evaluated_at="2026-06-22T00:00:01Z")

    assert decision.effect == "allow_with_obligations"
    assert decision.policy_refs == ("allow-tools", "sandbox-tools")
    assert decision.obligations == (obligation,)
