from __future__ import annotations

from dataclasses import replace
from typing import get_args

import pytest

from graphblocks.policy import (
    EnforcementPoint,
    PolicyDecision,
    PolicyEnforcer,
    PolicyEnforcementRecord,
    PolicyObligation,
    PolicyRequest,
    PolicyRule,
    PolicyTestCase,
    PolicyTestExpectation,
    PolicyUnavailableError,
    PrincipalRef,
    ResourceRef,
    StaticPolicyEvaluator,
    run_policy_tests,
    unavailable_policy_decision,
)


def test_policy_request_exposes_output_streaming_enforcement_points() -> None:
    assert {
        "on_generation_chunk",
        "before_client_delivery",
        "before_output_commit",
    }.issubset(set(get_args(EnforcementPoint)))


def test_policy_request_digest_is_stable_for_semantic_input() -> None:
    request = PolicyRequest(
        request_id="req-1",
        enforcement_point="before_provider_call",
        action="model.generate",
        principal=PrincipalRef("user-1", tenant_id="tenant-1"),
        resource=ResourceRef("model:gpt", resource_kind="model"),
        attributes={"model_class": "standard"},
        occurred_at="2026-06-22T00:00:00Z",
    ).with_input_digest()

    same_input = replace(request, request_id="req-2", occurred_at="2026-06-22T00:01:00Z").with_input_digest()
    changed_input = replace(request, action="tool.run").with_input_digest()

    assert request.input_digest.startswith("sha256:")
    assert same_input.input_digest == request.input_digest
    assert changed_input.input_digest != request.input_digest


def test_policy_request_mappings_are_copied_and_read_only() -> None:
    principal_attributes = {"tier": "enterprise"}
    resource_attributes = {"classification": "internal"}
    requested_usage = [{"dimension": "tokens", "amount": 128}]
    attributes = {"output_policy_state": "generating"}

    request = PolicyRequest(
        request_id="req-immutable",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        principal=PrincipalRef("user-1", attributes=principal_attributes),
        resource=ResourceRef("tool:search", resource_kind="tool", attributes=resource_attributes),
        requested_usage=requested_usage,
        attributes=attributes,
        occurred_at="2026-06-23T00:00:00Z",
    )

    principal_attributes["tier"] = "mutated"
    resource_attributes["classification"] = "mutated"
    requested_usage[0]["amount"] = 999
    attributes["output_policy_state"] = "mutated"

    assert request.principal is not None
    assert request.principal.attributes == {"tier": "enterprise"}
    assert request.resource.attributes == {"classification": "internal"}
    assert request.requested_usage == ({"dimension": "tokens", "amount": 128},)
    assert request.attributes == {"output_policy_state": "generating"}
    with pytest.raises(TypeError):
        request.principal.attributes["tier"] = "direct-mutation"
    with pytest.raises(TypeError):
        request.resource.attributes["classification"] = "direct-mutation"
    with pytest.raises(TypeError):
        request.requested_usage[0]["amount"] = 256
    with pytest.raises(TypeError):
        request.attributes["output_policy_state"] = "direct-mutation"
    assert request.with_input_digest().input_digest.startswith("sha256:")

    with pytest.raises(ValueError, match="principal attributes must be a mapping"):
        PrincipalRef("user-1", attributes=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="principal attributes keys must be non-empty strings"):
        PrincipalRef("user-1", attributes={" ": "enterprise"})
    with pytest.raises(ValueError, match="resource attributes must be a mapping"):
        ResourceRef("tool:search", attributes=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="policy obligation parameters must be a mapping"):
        PolicyObligation("obl-1", "capture_audit", parameters=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="policy request attributes must be a mapping"):
        PolicyRequest(
            request_id="req-invalid",
            enforcement_point="before_tool_or_effect",
            action="tool.run",
            resource=ResourceRef("tool:search", resource_kind="tool"),
            attributes=object(),  # type: ignore[arg-type]
            occurred_at="2026-06-23T00:00:00Z",
        )
    with pytest.raises(ValueError, match="policy enforcement metadata must be a mapping"):
        PolicyEnforcementRecord(
            record_id="enforce-invalid",
            decision_id="decision-1",
            enforcement_point="before_provider_call",
            status="enforced",
            metadata=object(),  # type: ignore[arg-type]
        )


def test_policy_security_mappings_are_recursively_copied_and_read_only() -> None:
    principal_attributes = {
        "claims": {"can_execute": False},
        "scopes": ["read"],
    }
    request_attributes = {"context": {"trusted": False}}
    requested_usage = [{"limit": {"tokens": 128}}]
    obligation_parameters = {"redaction": {"fields": ["secret"]}}
    principal = PrincipalRef("user-1", attributes=principal_attributes)
    obligation = PolicyObligation("obl-1", "redact", obligation_parameters)
    request = PolicyRequest(
        request_id="req-recursive-immutable",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        principal=principal,
        resource=ResourceRef("tool:search", resource_kind="tool"),
        requested_usage=requested_usage,
        attributes=request_attributes,
        occurred_at="2026-06-23T00:00:00Z",
    )
    digest = request.with_input_digest().input_digest

    principal_attributes["claims"]["can_execute"] = True
    principal_attributes["scopes"].append("admin")
    request_attributes["context"]["trusted"] = True
    requested_usage[0]["limit"]["tokens"] = 999
    obligation_parameters["redaction"]["fields"].append("credentials")

    assert principal.attributes == {
        "claims": {"can_execute": False},
        "scopes": ["read"],
    }
    assert request.attributes == {"context": {"trusted": False}}
    assert request.requested_usage == ({"limit": {"tokens": 128}},)
    assert obligation.parameters == {"redaction": {"fields": ["secret"]}}
    assert request.with_input_digest().input_digest == digest
    with pytest.raises(TypeError):
        principal.attributes["claims"]["can_execute"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        principal.attributes["scopes"].append("admin")  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        request.requested_usage[0]["limit"]["tokens"] = 999  # type: ignore[index]
    with pytest.raises(TypeError):
        obligation.parameters["redaction"]["fields"].append("credentials")  # type: ignore[index,union-attr]


def test_policy_models_reject_unknown_typed_values() -> None:
    with pytest.raises(ValueError, match="unknown enforcement point"):
        PolicyRequest(
            request_id="req-invalid",
            enforcement_point="after_delivery",
            action="model.generate",
            resource=ResourceRef("model:support", resource_kind="model"),
            occurred_at="2026-06-23T00:00:00Z",
        )
    with pytest.raises(ValueError, match="unknown policy rule effect"):
        PolicyRule("rule-invalid", "maybe", actions=("*",), resource_selectors=("*",))
    with pytest.raises(ValueError, match="unknown policy effect"):
        PolicyDecision(
            decision_id="decision-invalid",
            effect="maybe",
            reason_codes=(),
            policy_refs=(),
            evaluated_at="2026-06-23T00:00:01Z",
            input_digest="sha256:input",
        )
    with pytest.raises(ValueError, match="unknown policy enforcement status"):
        PolicyEnforcementRecord(
            record_id="enforce-invalid",
            decision_id="decision-1",
            enforcement_point="before_provider_call",
            status="maybe",
            occurred_at="2026-06-23T00:00:02Z",
        )
    with pytest.raises(ValueError, match="policy enforcement occurred_at must be an ISO datetime"):
        PolicyEnforcementRecord(
            record_id="enforce-compact-timezone",
            decision_id="decision-1",
            enforcement_point="before_provider_call",
            status="enforced",
            occurred_at="2026-06-23T00:00:02+0000",
        )
    with pytest.raises(ValueError, match="unknown enforcement point"):
        PolicyEnforcementRecord(
            record_id="enforce-invalid",
            decision_id="decision-1",
            enforcement_point="after_delivery",
            status="enforced",
            occurred_at="2026-06-23T00:00:02Z",
        )


def test_policy_models_reject_empty_identity_fields() -> None:
    with pytest.raises(ValueError, match="policy obligation obligation_id must not be empty"):
        PolicyObligation(" ", "capture_audit")
    with pytest.raises(ValueError, match="policy obligation obligation_type must not be empty"):
        PolicyObligation("obl-1", "")
    with pytest.raises(ValueError, match="policy rule rule_id must not be empty"):
        PolicyRule(" ", "allow", actions=("tool.run",), resource_selectors=("tool",))
    with pytest.raises(ValueError, match="principal principal_id must not be empty"):
        PrincipalRef(" ")
    with pytest.raises(ValueError, match="principal tenant_id must not be empty"):
        PrincipalRef("user-1", tenant_id="")
    with pytest.raises(ValueError, match="principal groups item must not be empty"):
        PrincipalRef("user-1", groups=("support", " "))
    with pytest.raises(ValueError, match="resource resource_id must not be empty"):
        ResourceRef("")
    with pytest.raises(ValueError, match="resource resource_kind must not be empty"):
        ResourceRef("tool:search", resource_kind=" ")
    with pytest.raises(ValueError, match="policy request request_id must not be empty"):
        PolicyRequest(
            request_id=" ",
            enforcement_point="before_tool_or_effect",
            action="tool.run",
            resource=ResourceRef("tool:search"),
            occurred_at="2026-06-23T00:00:00Z",
        )
    with pytest.raises(ValueError, match="policy request action must not be empty"):
        PolicyRequest(
            request_id="req-1",
            enforcement_point="before_tool_or_effect",
            action="",
            resource=ResourceRef("tool:search"),
            occurred_at="2026-06-23T00:00:00Z",
        )
    with pytest.raises(ValueError, match="policy request occurred_at must not be empty"):
        PolicyRequest(
            request_id="req-1",
            enforcement_point="before_tool_or_effect",
            action="tool.run",
            resource=ResourceRef("tool:search"),
            occurred_at=" ",
        )
    with pytest.raises(ValueError, match="policy request occurred_at must be an ISO datetime"):
        PolicyRequest(
            request_id="req-compact-timezone",
            enforcement_point="before_tool_or_effect",
            action="tool.run",
            resource=ResourceRef("tool:search"),
            occurred_at="2026-06-23T00:00:00+0000",
        )
    with pytest.raises(ValueError, match="policy request resource must be a ResourceRef"):
        PolicyRequest(
            request_id="req-1",
            enforcement_point="before_tool_or_effect",
            action="tool.run",
            resource=object(),  # type: ignore[arg-type]
            occurred_at="2026-06-23T00:00:00Z",
        )
    with pytest.raises(ValueError, match="policy request data_labels item must not be empty"):
        PolicyRequest(
            request_id="req-1",
            enforcement_point="before_tool_or_effect",
            action="tool.run",
            resource=ResourceRef("tool:search"),
            occurred_at="2026-06-23T00:00:00Z",
            data_labels=("restricted", ""),
        )
    with pytest.raises(ValueError, match="policy decision decision_id must not be empty"):
        PolicyDecision(
            decision_id=" ",
            effect="allow",
            reason_codes=(),
            policy_refs=(),
            input_digest="sha256:input",
        )
    with pytest.raises(ValueError, match="policy decision evaluated_at must be an ISO datetime"):
        PolicyDecision(
            decision_id="decision-compact-timezone",
            effect="allow",
            reason_codes=(),
            policy_refs=(),
            evaluated_at="2026-06-23T00:00:01+0000",
            input_digest="sha256:input",
        )
    with pytest.raises(ValueError, match="policy decision valid_until must be an ISO datetime"):
        PolicyDecision(
            decision_id="decision-space-time",
            effect="allow",
            reason_codes=(),
            policy_refs=(),
            evaluated_at="2026-06-23T00:00:01Z",
            valid_until="2026-06-23 00:05:00Z",
            input_digest="sha256:input",
        )


def test_policy_rule_rejects_malformed_collections() -> None:
    with pytest.raises(ValueError, match="policy rule actions must be a collection of strings"):
        PolicyRule("rule-1", "allow", actions="tool.run", resource_selectors=("tool",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="policy rule actions must not be empty"):
        PolicyRule("rule-1", "allow", actions=(), resource_selectors=("tool",))
    with pytest.raises(ValueError, match="policy rule actions item must not be empty"):
        PolicyRule("rule-1", "allow", actions=("tool.run", " "), resource_selectors=("tool",))
    with pytest.raises(ValueError, match="policy rule resource_selectors must not be empty"):
        PolicyRule("rule-1", "allow", actions=("tool.run",), resource_selectors=())
    with pytest.raises(ValueError, match="policy rule resource_selectors items must be strings"):
        PolicyRule("rule-1", "allow", actions=("tool.run",), resource_selectors=("tool", 3))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="policy rule principal_selectors item must not be empty"):
        PolicyRule(
            "rule-1",
            "allow",
            actions=("tool.run",),
            resource_selectors=("tool",),
            principal_selectors=("support", " "),
        )
    with pytest.raises(ValueError, match="policy rule obligations must be PolicyObligation"):
        PolicyRule(
            "rule-1",
            "obligate",
            actions=("tool.run",),
            resource_selectors=("tool",),
            obligations=(object(),),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="policy rule priority must be an integer"):
        PolicyRule("rule-1", "allow", actions=("tool.run",), resource_selectors=("tool",), priority=True)  # type: ignore[arg-type]


def test_policy_decision_rejects_malformed_collections() -> None:
    with pytest.raises(ValueError, match="policy decision reason_codes must be a collection of strings"):
        PolicyDecision(
            decision_id="decision-1",
            effect="allow",
            reason_codes="allow-all",  # type: ignore[arg-type]
            policy_refs=(),
            input_digest="sha256:input",
        )
    with pytest.raises(ValueError, match="policy decision reason_codes item must not be empty"):
        PolicyDecision(
            decision_id="decision-1",
            effect="allow",
            reason_codes=("allow-all", " "),
            policy_refs=(),
            input_digest="sha256:input",
        )
    with pytest.raises(ValueError, match="policy decision policy_refs items must be strings"):
        PolicyDecision(
            decision_id="decision-1",
            effect="allow",
            reason_codes=(),
            policy_refs=("policy-1", 2),  # type: ignore[arg-type]
            input_digest="sha256:input",
        )
    with pytest.raises(ValueError, match="policy decision obligations must be PolicyObligation"):
        PolicyDecision(
            decision_id="decision-1",
            effect="allow",
            reason_codes=(),
            policy_refs=(),
            obligations=(object(),),  # type: ignore[arg-type]
            input_digest="sha256:input",
        )
    with pytest.raises(ValueError, match="policy decision advice must contain mappings"):
        PolicyDecision(
            decision_id="decision-1",
            effect="allow",
            reason_codes=(),
            policy_refs=(),
            advice=(object(),),  # type: ignore[arg-type]
            input_digest="sha256:input",
        )


def test_policy_obligation_parameters_are_copied_and_read_only() -> None:
    parameters = {"max_tokens": 4000}

    obligation = PolicyObligation("obl-immutable", "cap_model_input", parameters)

    parameters["max_tokens"] = 8000

    assert obligation.parameters == {"max_tokens": 4000}
    with pytest.raises(TypeError):
        obligation.parameters["max_tokens"] = 2000


def test_static_policy_evaluator_gives_explicit_deny_precedence() -> None:
    evaluator = StaticPolicyEvaluator(
        rules=[
            PolicyRule("allow-model", "allow", actions=("model.generate",), resource_selectors=("*",), priority=0),
            PolicyRule("deny-user", "deny", actions=("*",), resource_selectors=("*",), principal_selectors=("user-1",), priority=10),
        ]
    )
    request = PolicyRequest(
        request_id="req-1",
        enforcement_point="before_provider_call",
        action="model.generate",
        principal=PrincipalRef("user-1"),
        resource=ResourceRef("model:gpt"),
        occurred_at="2026-06-22T00:00:00Z",
    )

    decision = evaluator.evaluate(request, evaluated_at="2026-06-22T00:00:01Z")

    assert decision.effect == "deny"
    assert decision.reason_codes == ("deny-user",)
    assert decision.policy_refs == ("deny-user",)
    assert decision.obligations == ()


def test_static_policy_evaluator_returns_allow_with_obligations() -> None:
    obligation = PolicyObligation(
        obligation_id="obl-1",
        obligation_type="cap_model_input",
        parameters={"max_tokens": 4000},
    )
    evaluator = StaticPolicyEvaluator(
        rules=[
            PolicyRule("allow-model", "allow", actions=("model.generate",), resource_selectors=("model",)),
            PolicyRule(
                "cap-input",
                "obligate",
                actions=("model.generate",),
                resource_selectors=("model",),
                obligations=(obligation,),
                priority=5,
            ),
        ]
    )
    request = PolicyRequest(
        request_id="req-1",
        enforcement_point="before_provider_call",
        action="model.generate",
        resource=ResourceRef("model:gpt", resource_kind="model"),
        occurred_at="2026-06-22T00:00:00Z",
    )

    decision = evaluator.evaluate(request, evaluated_at="2026-06-22T00:00:01Z")

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_codes == ("allow-model", "cap-input")
    assert decision.obligations == (obligation,)
    assert decision.input_digest == request.with_input_digest().input_digest


def test_policy_decision_collections_are_immutable() -> None:
    reason_codes = ["rule-denied"]
    policy_refs = ["policy/tool-safety"]
    obligations = [PolicyObligation("obl-1", "capture_audit", {"mode": "strict"})]
    advice = [{"message": "manual review required"}]

    decision = PolicyDecision(
        decision_id="decision-immutable",
        effect="deny",
        reason_codes=reason_codes,
        policy_refs=policy_refs,
        obligations=obligations,
        advice=advice,
        evaluated_at="2026-06-23T00:00:00Z",
        input_digest="sha256:input",
    )

    reason_codes.append("mutated")
    policy_refs.append("policy/mutated")
    obligations.append(PolicyObligation("obl-2", "mutated", {}))
    advice[0]["message"] = "mutated"

    assert decision.reason_codes == ("rule-denied",)
    assert decision.policy_refs == ("policy/tool-safety",)
    assert decision.obligations == (PolicyObligation("obl-1", "capture_audit", {"mode": "strict"}),)
    assert decision.advice == ({"message": "manual review required"},)
    with pytest.raises(AttributeError):
        decision.reason_codes.append("direct-mutation")
    with pytest.raises(AttributeError):
        decision.policy_refs.append("direct-mutation")
    with pytest.raises(AttributeError):
        decision.obligations.append(PolicyObligation("obl-3", "direct_mutation", {}))
    with pytest.raises(TypeError):
        decision.advice[0]["message"] = "direct mutation"


def test_policy_enforcement_record_is_separate_from_decision() -> None:
    record = PolicyEnforcementRecord(
        record_id="enforce-1",
        decision_id="decision-1",
        enforcement_point="before_provider_call",
        status="enforced",
        enforced_obligation_ids=("obl-1",),
        occurred_at="2026-06-22T00:00:02Z",
    )

    assert record.decision_id == "decision-1"
    assert record.status == "enforced"
    assert record.enforced_obligation_ids == ("obl-1",)


def test_policy_enforcement_record_from_decision_validates_obligations() -> None:
    decision = PolicyDecision(
        decision_id="decision-1",
        effect="allow_with_obligations",
        reason_codes=["cap-input"],
        policy_refs=["cap-input"],
        obligations=[PolicyObligation("obl-1", "cap_model_input", {"max_tokens": 4000})],
        evaluated_at="2026-06-22T00:00:01Z",
        input_digest="sha256:input",
    )

    record = PolicyEnforcementRecord.from_decision(
        record_id="enforce-1",
        decision=decision,
        enforcement_point="before_provider_call",
        status="enforced",
        enforced_obligation_ids=("obl-1",),
        occurred_at="2026-06-22T00:00:02Z",
    )
    with pytest.raises(ValueError, match="unknown policy obligation"):
        PolicyEnforcementRecord.from_decision(
            record_id="enforce-2",
            decision=decision,
            enforcement_point="before_provider_call",
            status="enforced",
            enforced_obligation_ids=("obl-missing",),
            occurred_at="2026-06-22T00:00:03Z",
        )

    assert record.decision_id == decision.decision_id
    assert record.enforced_obligation_ids == ("obl-1",)


def test_unavailable_external_policy_applies_declared_fail_modes() -> None:
    request = PolicyRequest(
        request_id="req-pdp-down",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        principal=PrincipalRef("user-1"),
        resource=ResourceRef("tool:ticket.create", resource_kind="tool"),
        occurred_at="2026-06-23T00:00:00Z",
    )

    closed = unavailable_policy_decision(
        request,
        fail_mode="fail_closed",
        evaluated_at="2026-06-23T00:00:01Z",
    )
    fail_open = unavailable_policy_decision(
        request,
        fail_mode="fail_open_with_audit",
        evaluated_at="2026-06-23T00:00:01Z",
    )
    deferred = unavailable_policy_decision(
        request,
        fail_mode="defer",
        evaluated_at="2026-06-23T00:00:01Z",
    )

    assert closed.effect == "deny"
    assert closed.reason_codes == ("policy_unavailable", "fail_closed")
    assert closed.input_digest == request.with_input_digest().input_digest
    assert fail_open.effect == "allow_with_obligations"
    assert fail_open.obligations == (
        PolicyObligation(
            "policy_unavailable_audit",
            "capture_audit",
            {"fail_mode": "fail_open_with_audit", "enforcement_point": "before_tool_or_effect"},
        ),
    )
    assert deferred.effect == "defer"
    assert deferred.reason_codes == ("policy_unavailable", "defer")


def test_unavailable_policy_cache_reuse_requires_matching_digest_and_ttl() -> None:
    request = PolicyRequest(
        request_id="req-cache",
        enforcement_point="before_provider_call",
        action="model.generate",
        resource=ResourceRef("model:support", resource_kind="model"),
        occurred_at="2026-06-23T00:00:00Z",
    )
    digested_request = request.with_input_digest()
    cached = PolicyDecision(
        decision_id="decision:cached",
        effect="allow",
        reason_codes=("cached_allow",),
        policy_refs=("bundle-1",),
        evaluated_at="2026-06-23T00:00:00Z",
        valid_until="2026-06-23T00:05:00Z",
        input_digest=digested_request.input_digest,
    )

    reused = unavailable_policy_decision(
        request,
        fail_mode="use_cached_decision",
        evaluated_at="2026-06-23T00:01:00Z",
        cached_decision=cached,
    )
    offset_cached = replace(cached, valid_until="2026-06-23T00:00:00-05:00")
    offset_reused = unavailable_policy_decision(
        request,
        fail_mode="use_cached_decision",
        evaluated_at="2026-06-23T04:59:59Z",
        cached_decision=offset_cached,
    )

    assert reused == cached
    assert offset_reused == offset_cached
    with pytest.raises(PolicyUnavailableError, match="expired"):
        unavailable_policy_decision(
            request,
            fail_mode="use_cached_decision",
            evaluated_at="2026-06-23 00:01:00Z",
            cached_decision=cached,
        )
    with pytest.raises(PolicyUnavailableError, match="cached policy decision is required"):
        unavailable_policy_decision(
            request,
            fail_mode="use_cached_decision",
            evaluated_at="2026-06-23T00:01:00Z",
        )
    with pytest.raises(PolicyUnavailableError, match="input digest"):
        unavailable_policy_decision(
            PolicyRequest(
                request_id="req-cache-changed",
                enforcement_point="before_provider_call",
                action="tool.run",
                resource=ResourceRef("tool:search", resource_kind="tool"),
                occurred_at="2026-06-23T00:00:00Z",
            ),
            fail_mode="use_cached_decision",
            evaluated_at="2026-06-23T00:01:00Z",
            cached_decision=cached,
        )
    with pytest.raises(PolicyUnavailableError, match="expired"):
        unavailable_policy_decision(
            request,
            fail_mode="use_cached_decision",
            evaluated_at="2026-06-23T00:05:00Z",
            cached_decision=cached,
        )
    with pytest.raises(PolicyUnavailableError, match="expired"):
        unavailable_policy_decision(
            request,
            fail_mode="use_cached_decision",
            evaluated_at="2026-06-23T05:00:01Z",
            cached_decision=offset_cached,
        )


def test_policy_enforcer_records_decision_and_enforcement_status() -> None:
    obligation = PolicyObligation("obl-1", "capture_audit", {"mode": "strict"})
    evaluator = StaticPolicyEvaluator(
        rules=[
            PolicyRule("allow-tool", "allow", actions=("tool.run",), resource_selectors=("tool",)),
            PolicyRule(
                "audit-tool",
                "obligate",
                actions=("tool.run",),
                resource_selectors=("tool",),
                obligations=(obligation,),
            ),
        ]
    )
    request = PolicyRequest(
        request_id="req-enforce",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        principal=PrincipalRef("user-1"),
        resource=ResourceRef("tool:search", resource_kind="tool"),
        occurred_at="2026-06-23T00:00:00Z",
    )

    result = PolicyEnforcer(evaluator).enforce(request, evaluated_at="2026-06-23T00:00:01Z")
    failed = PolicyEnforcer(evaluator).enforce(
        request,
        evaluated_at="2026-06-23T00:00:02Z",
        enforced_obligation_ids=(),
    )

    assert result.allowed is True
    assert result.decision.effect == "allow_with_obligations"
    assert result.record.status == "enforced"
    assert result.record.enforced_obligation_ids == ("obl-1",)
    assert failed.allowed is False
    assert failed.record.status == "failed"
    assert failed.record.metadata["missing_obligation_ids"] == ("obl-1",)


def test_policy_enforcer_discards_reported_obligations_for_denied_decisions() -> None:
    evaluator = StaticPolicyEvaluator(
        rules=[
            PolicyRule(
                "deny-tool",
                "deny",
                actions=("tool.run",),
                resource_selectors=("tool",),
            )
        ]
    )
    request = PolicyRequest(
        request_id="req-denied",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        resource=ResourceRef("tool:search", resource_kind="tool"),
        occurred_at="2026-06-23T00:00:00Z",
    )

    enforcement = PolicyEnforcer(evaluator).enforce(
        request,
        evaluated_at="2026-06-23T00:00:01Z",
        enforced_obligation_ids=("caller-reported-obligation",),
    )
    report = run_policy_tests(
        evaluator,
        [
            PolicyTestCase(
                "denied-with-reported-obligations",
                request,
                PolicyTestExpectation(effect="deny", enforcement_status="blocked"),
                evaluated_at="2026-06-23T00:00:01Z",
                enforced_obligation_ids=("caller-reported-obligation",),
            )
        ],
    )

    assert enforcement.record.status == "blocked"
    assert enforcement.record.enforced_obligation_ids == ()
    assert report.passed is True
    assert report.results[0].record.enforced_obligation_ids == ()


def test_policy_test_dsl_reports_expectation_failures() -> None:
    evaluator = StaticPolicyEvaluator(
        rules=[PolicyRule("allow-model", "allow", actions=("model.generate",), resource_selectors=("model",))]
    )
    request = PolicyRequest(
        request_id="req-case",
        enforcement_point="before_provider_call",
        action="model.generate",
        resource=ResourceRef("model:support", resource_kind="model"),
        occurred_at="2026-06-23T00:00:00Z",
    )
    passing = PolicyTestCase(
        "allow-case",
        request,
        PolicyTestExpectation(effect="allow", reason_codes=("allow-model",), enforcement_status="enforced"),
        evaluated_at="2026-06-23T00:00:01Z",
    )
    failing = PolicyTestCase(
        "deny-case",
        request,
        PolicyTestExpectation(effect="deny", reason_codes=("deny-model",), enforcement_status="blocked"),
        evaluated_at="2026-06-23T00:00:01Z",
    )

    report = run_policy_tests(evaluator, [passing, failing])

    assert report.passed is False
    assert report.results[0].passed is True
    assert report.results[1].passed is False
    assert report.failures == (
        "deny-case: expected effect deny but got allow",
        "deny-case: expected reason code deny-model",
        "deny-case: expected enforcement status blocked but got enforced",
    )


def test_policy_test_case_rejects_invalid_case_id() -> None:
    request = PolicyRequest(
        request_id="req-case",
        enforcement_point="before_provider_call",
        action="model.generate",
        resource=ResourceRef("model:support", resource_kind="model"),
        occurred_at="2026-06-23T00:00:00Z",
    )

    with pytest.raises(ValueError, match="policy test case_id must be a string"):
        PolicyTestCase(
            object(),  # type: ignore[arg-type]
            request,
            PolicyTestExpectation(effect="allow"),
            evaluated_at="2026-06-23T00:00:01Z",
        )
    with pytest.raises(ValueError, match="policy test case_id must not be empty"):
        PolicyTestCase(
            " ",
            request,
            PolicyTestExpectation(effect="allow"),
            evaluated_at="2026-06-23T00:00:01Z",
        )
    with pytest.raises(ValueError, match="policy test evaluated_at must be an ISO datetime"):
        PolicyTestCase(
            "compact-timezone",
            request,
            PolicyTestExpectation(effect="allow"),
            evaluated_at="2026-06-23T00:00:01+0000",
        )
