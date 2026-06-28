from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks import PolicyRequest, PrincipalRef, ResourceRef


ROOT = Path(__file__).parents[1]


def _policy_request() -> PolicyRequest:
    return PolicyRequest(
        request_id="policy-req-1",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        principal=PrincipalRef(
            "user-1",
            tenant_id="tenant-1",
            groups=("support",),
            roles=("agent",),
            attributes={"tier": "enterprise"},
        ),
        resource=ResourceRef(
            "tool:knowledge.search",
            resource_kind="tool",
            tenant_id="tenant-1",
            attributes={"classification": "internal"},
        ),
        data_labels=("internal",),
        requested_usage=({"kind": "tool_call", "amount": 1, "unit": "count"},),
        attributes={"tool_call_id": "call-1"},
        policy_snapshot_id="policy-snapshot-1",
        release_id="release-1",
        deployment_revision_id="deployment-1",
        run_id="run-1",
        atomic_unit=ResourceRef("turn:1", resource_kind="conversation_turn", tenant_id="tenant-1"),
        occurred_at="2026-06-23T00:00:00Z",
    ).with_input_digest()


def test_opa_adapter_prepares_canonical_policy_input(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")
    request = _policy_request()

    opa_input = graphblocks_policy_opa.prepare_opa_policy_input(
        request,
        package_ref="policies/support.rego@sha256:abc",
    )

    assert opa_input.input_contract() == {
        "package_ref": "policies/support.rego@sha256:abc",
        "input": {
            "request_id": "policy-req-1",
            "enforcement_point": "before_tool_or_effect",
            "action": "tool.run",
            "principal": {
                "principal_id": "user-1",
                "tenant_id": "tenant-1",
                "groups": ["support"],
                "roles": ["agent"],
                "attributes": {"tier": "enterprise"},
            },
            "resource": {
                "resource_id": "tool:knowledge.search",
                "resource_kind": "tool",
                "tenant_id": "tenant-1",
                "attributes": {"classification": "internal"},
            },
            "tenant": None,
            "occurred_at": "2026-06-23T00:00:00Z",
            "release_id": "release-1",
            "deployment_revision_id": "deployment-1",
            "run_id": "run-1",
            "atomic_unit": {
                "resource_id": "turn:1",
                "resource_kind": "conversation_turn",
                "tenant_id": "tenant-1",
                "attributes": {},
            },
            "data_labels": ["internal"],
            "requested_usage": [{"kind": "tool_call", "amount": 1, "unit": "count"}],
            "attributes": {"tool_call_id": "call-1"},
            "policy_snapshot_id": "policy-snapshot-1",
            "input_digest": request.input_digest,
        },
    }


def test_opa_adapter_maps_result_to_policy_decision(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")
    request = _policy_request()

    decision = graphblocks_policy_opa.policy_decision_from_opa_result(
        decision_id="decision-opa-1",
        request=request,
        result={
            "result": {
                "effect": "allow_with_obligations",
                "reason_codes": ["allow-support"],
                "policy_refs": ["policies/support.rego#allow-support"],
                "obligations": [
                    {
                        "obligation_id": "obl-audit",
                        "obligation_type": "capture_audit",
                        "parameters": {"mode": "strict"},
                    }
                ],
                "advice": [{"message": "log policy match"}],
            }
        },
        evaluated_at="2026-06-23T00:00:01Z",
    )

    assert decision.decision_id == "decision-opa-1"
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_codes == ("allow-support",)
    assert decision.policy_refs == ("policies/support.rego#allow-support",)
    assert decision.obligations[0].obligation_id == "obl-audit"
    assert decision.advice == ({"message": "log policy match"},)
    assert decision.input_digest == request.input_digest


def test_opa_adapter_rejects_unknown_effect(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="unknown policy effect"):
        graphblocks_policy_opa.policy_decision_from_opa_result(
            decision_id="decision-opa-1",
            request=_policy_request(),
            result={"result": {"effect": "maybe"}},
            evaluated_at="2026-06-23T00:00:01Z",
        )


def test_opa_adapter_rejects_blank_decision_metadata(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="decision_id"):
        graphblocks_policy_opa.policy_decision_from_opa_result(
            decision_id=" ",
            request=_policy_request(),
            result={"result": {"effect": "allow"}},
            evaluated_at="2026-06-23T00:00:01Z",
        )

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="evaluated_at"):
        graphblocks_policy_opa.policy_decision_from_opa_result(
            decision_id="decision-opa-1",
            request=_policy_request(),
            result={"result": {"effect": "allow"}},
            evaluated_at=" ",
        )


def test_opa_adapter_rejects_blank_policy_result_strings(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="blank string"):
        graphblocks_policy_opa.policy_decision_from_opa_result(
            decision_id="decision-opa-1",
            request=_policy_request(),
            result={"result": {"effect": "allow", "reason_codes": ["allow-support", " "]}},
            evaluated_at="2026-06-23T00:00:01Z",
        )


def test_cedar_adapter_prepares_authorization_request(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")
    request = _policy_request()

    authorization = graphblocks_policy_cedar.prepare_cedar_authorization_request(
        request,
        schema_ref="cedar/support-schema@1",
    )

    assert authorization.authorization_contract() == {
        "schema_ref": "cedar/support-schema@1",
        "principal": {
            "entity_type": "Principal",
            "entity_id": "user-1",
            "tenant_id": "tenant-1",
            "groups": ["support"],
            "roles": ["agent"],
            "attributes": {"tier": "enterprise"},
        },
        "action": "tool.run",
        "resource": {
            "entity_type": "Resource",
            "entity_id": "tool:knowledge.search",
            "resource_kind": "tool",
            "tenant_id": "tenant-1",
            "attributes": {"classification": "internal"},
        },
        "context": {
            "request_id": "policy-req-1",
            "enforcement_point": "before_tool_or_effect",
            "occurred_at": "2026-06-23T00:00:00Z",
            "release_id": "release-1",
            "deployment_revision_id": "deployment-1",
            "run_id": "run-1",
            "atomic_unit": {
                "entity_type": "Resource",
                "entity_id": "turn:1",
                "resource_kind": "conversation_turn",
                "tenant_id": "tenant-1",
                "attributes": {},
            },
            "data_labels": ["internal"],
            "requested_usage": [{"kind": "tool_call", "amount": 1, "unit": "count"}],
            "attributes": {"tool_call_id": "call-1"},
            "policy_snapshot_id": "policy-snapshot-1",
            "input_digest": request.input_digest,
        },
    }


def test_cedar_adapter_maps_result_to_policy_decision(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")
    request = _policy_request()

    decision = graphblocks_policy_cedar.policy_decision_from_cedar_result(
        decision_id="decision-cedar-1",
        request=request,
        result={
            "decision": "deny",
            "diagnostics": {
                "reason": ["policy::support::deny_write"],
            },
        },
        evaluated_at="2026-06-23T00:00:01Z",
    )

    assert decision.decision_id == "decision-cedar-1"
    assert decision.effect == "deny"
    assert decision.reason_codes == ("policy::support::deny_write",)
    assert decision.policy_refs == ("policy::support::deny_write",)
    assert decision.input_digest == request.input_digest


def test_cedar_adapter_rejects_blank_decision_metadata(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")

    with pytest.raises(graphblocks_policy_cedar.CedarPolicyAdapterError, match="decision_id"):
        graphblocks_policy_cedar.policy_decision_from_cedar_result(
            decision_id=" ",
            request=_policy_request(),
            result={"decision": "allow"},
            evaluated_at="2026-06-23T00:00:01Z",
        )

    with pytest.raises(graphblocks_policy_cedar.CedarPolicyAdapterError, match="evaluated_at"):
        graphblocks_policy_cedar.policy_decision_from_cedar_result(
            decision_id="decision-cedar-1",
            request=_policy_request(),
            result={"decision": "allow"},
            evaluated_at=" ",
        )


def test_cedar_adapter_rejects_blank_policy_result_strings(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")

    with pytest.raises(graphblocks_policy_cedar.CedarPolicyAdapterError, match="blank string"):
        graphblocks_policy_cedar.policy_decision_from_cedar_result(
            decision_id="decision-cedar-1",
            request=_policy_request(),
            result={"decision": "allow", "diagnostics": {"reason": ["policy::support::allow", " "]}},
            evaluated_at="2026-06-23T00:00:01Z",
        )


def test_cedar_adapter_requires_principal(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")
    request = PolicyRequest(
        request_id="policy-req-1",
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        resource=ResourceRef("tool:knowledge.search", resource_kind="tool"),
        occurred_at="2026-06-23T00:00:00Z",
    ).with_input_digest()

    with pytest.raises(graphblocks_policy_cedar.CedarPolicyAdapterError, match="requires a principal"):
        graphblocks_policy_cedar.prepare_cedar_authorization_request(request)
