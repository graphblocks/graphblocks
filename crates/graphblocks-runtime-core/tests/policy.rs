use graphblocks_runtime_core::policy::{
    EnforcementPoint, PolicyEnforcementRecord, PolicyObligation, PolicyRequest, PolicyRule,
    PrincipalRef, ResourceRef, RuleEffect, StaticPolicyEvaluator,
};
use serde_json::json;

#[test]
fn policy_request_exposes_output_streaming_enforcement_points() {
    let enforcement_points = [
        EnforcementPoint::OnGenerationChunk,
        EnforcementPoint::BeforeClientDelivery,
        EnforcementPoint::BeforeOutputCommit,
        EnforcementPoint::BeforeToolOrEffect,
    ]
    .map(EnforcementPoint::as_str);

    assert_eq!(
        enforcement_points,
        [
            "on_generation_chunk",
            "before_client_delivery",
            "before_output_commit",
            "before_tool_or_effect",
        ]
    );
}

#[test]
fn policy_request_digest_is_stable_for_semantic_input() {
    let request = PolicyRequest::new(
        "req-1",
        EnforcementPoint::BeforeProviderCall,
        "model.generate",
        ResourceRef::new("model:gpt").with_resource_kind("model"),
        "2026-06-22T00:00:00Z",
    )
    .with_principal(PrincipalRef::new("user-1").with_tenant_id("tenant-1"))
    .with_attribute("model_class", json!("standard"))
    .with_input_digest();

    let same_input = request
        .clone()
        .with_request_id("req-2")
        .with_occurred_at("2026-06-22T00:01:00Z")
        .with_input_digest();
    let changed_input = request.clone().with_action("tool.run").with_input_digest();

    assert!(request.input_digest.starts_with("sha256:"));
    assert_eq!(same_input.input_digest, request.input_digest);
    assert_ne!(changed_input.input_digest, request.input_digest);
}

#[test]
fn policy_request_digest_includes_atomic_unit_and_policy_snapshot() {
    let request = PolicyRequest::new(
        "req-1",
        EnforcementPoint::BeforeToolOrEffect,
        "tool.run",
        ResourceRef::new("tool:ticket.create").with_resource_kind("tool"),
        "2026-06-22T00:00:00Z",
    )
    .with_atomic_unit(ResourceRef::new("turn:1").with_resource_kind("turn"))
    .with_policy_snapshot_id("snapshot-1")
    .with_input_digest();

    let changed_snapshot = request
        .clone()
        .with_policy_snapshot_id("snapshot-2")
        .with_input_digest();
    let changed_unit = request
        .clone()
        .with_atomic_unit(ResourceRef::new("turn:2").with_resource_kind("turn"))
        .with_input_digest();

    assert_ne!(changed_snapshot.input_digest, request.input_digest);
    assert_ne!(changed_unit.input_digest, request.input_digest);
}

#[test]
fn policy_enforcement_record_is_separate_from_decision() {
    let record = PolicyEnforcementRecord::new(
        "enforce-1",
        "decision-1",
        EnforcementPoint::BeforeProviderCall,
        "enforced",
    )
    .with_enforced_obligation_id("obl-1")
    .with_occurred_at("2026-06-22T00:00:02Z");

    assert_eq!(record.decision_id, "decision-1");
    assert_eq!(record.status, "enforced");
    assert_eq!(record.enforced_obligation_ids, vec!["obl-1"]);
}

#[test]
fn static_policy_evaluator_gives_explicit_deny_precedence() {
    let evaluator = StaticPolicyEvaluator::new([
        PolicyRule::new("allow-model", RuleEffect::Allow, ["model.generate"], ["*"]),
        PolicyRule::new("deny-user", RuleEffect::Deny, ["*"], ["*"])
            .with_principal_selector("user-1")
            .with_priority(10),
    ]);
    let request = PolicyRequest::new(
        "req-1",
        EnforcementPoint::BeforeProviderCall,
        "model.generate",
        ResourceRef::new("model:gpt"),
        "2026-06-22T00:00:00Z",
    )
    .with_principal(PrincipalRef::new("user-1"));

    let decision = evaluator.evaluate(&request, "2026-06-22T00:00:01Z");

    assert_eq!(decision.effect.as_str(), "deny");
    assert_eq!(decision.reason_codes, vec!["deny-user"]);
    assert_eq!(decision.policy_refs, vec!["deny-user"]);
    assert!(decision.obligations.is_empty());
    assert_eq!(
        decision.input_digest,
        request.with_input_digest().input_digest
    );
}

#[test]
fn static_policy_evaluator_returns_allow_with_obligations() {
    let obligation = PolicyObligation::new("obl-1", "cap_model_input")
        .with_parameter("max_tokens", json!(4_000));
    let evaluator = StaticPolicyEvaluator::new([
        PolicyRule::new(
            "allow-model",
            RuleEffect::Allow,
            ["model.generate"],
            ["model"],
        ),
        PolicyRule::new(
            "cap-input",
            RuleEffect::Obligate,
            ["model.generate"],
            ["model"],
        )
        .with_obligation(obligation.clone())
        .with_priority(5),
    ]);
    let request = PolicyRequest::new(
        "req-1",
        EnforcementPoint::BeforeProviderCall,
        "model.generate",
        ResourceRef::new("model:gpt").with_resource_kind("model"),
        "2026-06-22T00:00:00Z",
    );

    let decision = evaluator.evaluate(&request, "2026-06-22T00:00:01Z");

    assert_eq!(decision.effect.as_str(), "allow_with_obligations");
    assert_eq!(decision.reason_codes, vec!["allow-model", "cap-input"]);
    assert_eq!(decision.obligations, vec![obligation]);
    assert!(decision.decision_id.starts_with("decision:sha256:"));
}

#[test]
fn static_policy_evaluator_defaults_to_deny_without_matching_rule() {
    let evaluator = StaticPolicyEvaluator::new([PolicyRule::new(
        "allow-other",
        RuleEffect::Allow,
        ["conversation.read"],
        ["conversation"],
    )]);
    let request = PolicyRequest::new(
        "req-1",
        EnforcementPoint::BeforeToolOrEffect,
        "ticket.create",
        ResourceRef::new("ticket-system").with_resource_kind("ticket"),
        "2026-06-22T00:00:00Z",
    );

    let decision = evaluator.evaluate(&request, "2026-06-22T00:00:01Z");

    assert_eq!(decision.effect.as_str(), "deny");
    assert_eq!(decision.reason_codes, vec!["default_deny"]);
}
