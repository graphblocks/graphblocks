from __future__ import annotations

from dataclasses import replace
from typing import get_args

import pytest

from graphblocks.policy import (
    EnforcementPoint,
    PolicyDecision,
    PolicyEnforcementRecord,
    PolicyObligation,
    PolicyRequest,
    PolicyRule,
    PrincipalRef,
    ResourceRef,
    StaticPolicyEvaluator,
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
    assert decision.reason_codes == ["deny-user"]
    assert decision.policy_refs == ["deny-user"]
    assert decision.obligations == []


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
    assert decision.reason_codes == ["allow-model", "cap-input"]
    assert decision.obligations == [obligation]
    assert decision.input_digest == request.with_input_digest().input_digest


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
