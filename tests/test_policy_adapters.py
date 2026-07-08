from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks import PolicyRequest, PrincipalRef, ResourceRef
from graphblocks.output_policy import (
    VALID_DRAFT_DISPOSITIONS,
    VALID_OUTPUT_DISPOSITIONS,
    VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
    VALID_PROVIDER_CANCELLATIONS,
)
from graphblocks.policy import VALID_POLICY_EFFECTS


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


def _output_policy_request() -> PolicyRequest:
    return PolicyRequest(
        request_id="output-policy-req-1",
        enforcement_point="before_client_delivery",
        action="output.deliver",
        principal=PrincipalRef("user-1", tenant_id="tenant-1"),
        resource=ResourceRef(
            "response:response-1",
            resource_kind="assistant_response",
            tenant_id="tenant-1",
            attributes={"stream_id": "stream-1"},
        ),
        attributes={"response_id": "response-1", "sequence": 2},
        policy_snapshot_id="policy-snapshot-1",
        release_id="release-1",
        run_id="run-1",
        occurred_at="2026-06-23T00:00:00Z",
    ).with_input_digest()


def test_policy_adapters_use_canonical_literal_sets(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")
    opa_constants = {
        "VALID_POLICY_EFFECTS": VALID_POLICY_EFFECTS,
        "VALID_OUTPUT_DISPOSITIONS": VALID_OUTPUT_DISPOSITIONS,
        "VALID_PROVIDER_CANCELLATIONS": VALID_PROVIDER_CANCELLATIONS,
        "VALID_DRAFT_DISPOSITIONS": VALID_DRAFT_DISPOSITIONS,
        "VALID_PENDING_TOOL_CALLS": VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
    }
    cedar_constants = {
        "VALID_OUTPUT_DISPOSITIONS": VALID_OUTPUT_DISPOSITIONS,
        "VALID_PROVIDER_CANCELLATIONS": VALID_PROVIDER_CANCELLATIONS,
        "VALID_DRAFT_DISPOSITIONS": VALID_DRAFT_DISPOSITIONS,
        "VALID_PENDING_TOOL_CALLS": VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
    }

    for name, value in opa_constants.items():
        assert getattr(graphblocks_policy_opa, name) is value
        assert name in graphblocks_policy_opa.__all__
    for name, value in cedar_constants.items():
        assert getattr(graphblocks_policy_cedar, name) is value
        assert name in graphblocks_policy_cedar.__all__


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


def test_opa_adapter_rejects_non_standard_input_json_constants(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")

    opa_input = graphblocks_policy_opa.OpaPolicyInput(input_json='{"score": NaN}')

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="strict JSON"):
        opa_input.input_contract()


def test_opa_adapter_rejects_blank_package_ref(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="package_ref"):
        graphblocks_policy_opa.prepare_opa_policy_input(
            _policy_request(),
            package_ref=" ",
        )

    opa_input = graphblocks_policy_opa.prepare_opa_policy_input(
        _policy_request(),
        package_ref=" policies/support.rego@sha256:abc ",
    )

    assert opa_input.package_ref == "policies/support.rego@sha256:abc"


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
                "validUntil": "2026-06-23T00:05:00Z",
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
    assert decision.valid_until == "2026-06-23T00:05:00Z"
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

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="valid_until"):
        graphblocks_policy_opa.policy_decision_from_opa_result(
            decision_id="decision-opa-1",
            request=_policy_request(),
            result={"result": {"effect": "allow", "valid_until": " "}},
            evaluated_at="2026-06-23T00:00:01Z",
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


def test_opa_adapter_maps_output_policy_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")
    request = _output_policy_request()

    decision = graphblocks_policy_opa.output_policy_decision_from_opa_result(
        decision_id="output-decision-opa-1",
        request=request,
        result={
            "result": {
                "disposition": "replace",
                "acceptedThroughSequence": 2,
                "replacementParts": [{"kind": "text", "text": "[policy-approved replacement]"}],
                "reasonCodes": ["blocked-output"],
                "policyRefs": ["policies/output.rego#blocked-output"],
                "providerCancellation": "required_if_supported",
                "draftDisposition": "retract",
                "pendingToolCalls": "cancel_admitted",
            }
        },
        evaluated_at="2026-06-23T00:00:01Z",
    )

    assert decision.disposition == "replace"
    assert decision.accepted_through_sequence == 2
    assert decision.replacement_parts[0].text == "[policy-approved replacement]"
    assert decision.reason_codes == ("blocked-output",)
    assert decision.policy_refs == ("policies/output.rego#blocked-output",)
    assert decision.provider_cancellation == "required_if_supported"
    assert decision.draft_disposition == "retract"
    assert decision.pending_tool_calls == "cancel_admitted"
    assert decision.evaluated_at == "2026-06-23T00:00:01Z"
    assert decision.input_digest == request.input_digest
    assert "output_policy_decision_from_opa_result" in graphblocks_policy_opa.__all__


def test_opa_adapter_rejects_invalid_output_policy_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-opa" / "src"))
    graphblocks_policy_opa = importlib.import_module("graphblocks_policy_opa")

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="unknown output policy disposition"):
        graphblocks_policy_opa.output_policy_decision_from_opa_result(
            decision_id="output-decision-opa-1",
            request=_output_policy_request(),
            result={"result": {"disposition": "stream"}},
            evaluated_at="2026-06-23T00:00:01Z",
        )

    with pytest.raises(graphblocks_policy_opa.OpaPolicyAdapterError, match="requires string text"):
        graphblocks_policy_opa.output_policy_decision_from_opa_result(
            decision_id="output-decision-opa-1",
            request=_output_policy_request(),
            result={"result": {"disposition": "replace", "replacementParts": [{"kind": "text", "text": 42}]}},
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


def test_cedar_adapter_rejects_non_standard_authorization_json_constants(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")

    authorization = graphblocks_policy_cedar.CedarAuthorizationRequest(authorization_json='{"principal": NaN}')

    with pytest.raises(graphblocks_policy_cedar.CedarPolicyAdapterError, match="strict JSON"):
        authorization.authorization_contract()


def test_cedar_adapter_rejects_blank_schema_ref(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")

    with pytest.raises(graphblocks_policy_cedar.CedarPolicyAdapterError, match="schema_ref"):
        graphblocks_policy_cedar.prepare_cedar_authorization_request(
            _policy_request(),
            schema_ref=" ",
        )

    authorization = graphblocks_policy_cedar.prepare_cedar_authorization_request(
        _policy_request(),
        schema_ref=" cedar/support-schema@1 ",
    )

    assert authorization.schema_ref == "cedar/support-schema@1"


def test_cedar_adapter_maps_result_to_policy_decision(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")
    request = _policy_request()

    decision = graphblocks_policy_cedar.policy_decision_from_cedar_result(
        decision_id="decision-cedar-1",
        request=request,
        result={
            "decision": "deny",
            "valid_until": "2026-06-23T00:05:00Z",
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
    assert decision.valid_until == "2026-06-23T00:05:00Z"
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

    with pytest.raises(graphblocks_policy_cedar.CedarPolicyAdapterError, match="valid_until"):
        graphblocks_policy_cedar.policy_decision_from_cedar_result(
            decision_id="decision-cedar-1",
            request=_policy_request(),
            result={"decision": "allow", "validUntil": " "},
            evaluated_at="2026-06-23T00:00:01Z",
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


def test_cedar_adapter_maps_nested_output_policy_result(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")
    request = _output_policy_request()

    decision = graphblocks_policy_cedar.output_policy_decision_from_cedar_result(
        decision_id="output-decision-cedar-1",
        request=request,
        result={
            "decision": "deny",
            "diagnostics": {"reason": ["policy::output::pii"]},
            "outputPolicy": {
                "disposition": "redact",
                "acceptedThroughSequence": 2,
                "redactions": [{"path": "/chunks/2/text", "start": 6, "end": 12, "replacement": "[redacted]"}],
                "providerCancellation": "request",
                "draftDisposition": "mark_incomplete",
                "pendingToolCalls": "deny",
            },
        },
        evaluated_at="2026-06-23T00:00:01Z",
    )

    assert decision.disposition == "redact"
    assert decision.accepted_through_sequence == 2
    assert dict(decision.redactions[0]) == {
        "path": "/chunks/2/text",
        "start": 6,
        "end": 12,
        "replacement": "[redacted]",
    }
    assert decision.reason_codes == ("policy::output::pii",)
    assert decision.policy_refs == ("policy::output::pii",)
    assert decision.draft_disposition == "mark_incomplete"
    assert decision.input_digest == request.input_digest
    assert "output_policy_decision_from_cedar_result" in graphblocks_policy_cedar.__all__


def test_cedar_adapter_maps_native_deny_to_output_abort(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-policy-cedar" / "src"))
    graphblocks_policy_cedar = importlib.import_module("graphblocks_policy_cedar")

    decision = graphblocks_policy_cedar.output_policy_decision_from_cedar_result(
        decision_id="output-decision-cedar-1",
        request=_output_policy_request(),
        result={
            "decision": "deny",
            "diagnostics": {"reason": ["policy::output::deny"]},
        },
        evaluated_at="2026-06-23T00:00:01Z",
    )

    assert decision.disposition == "abort_response"
    assert decision.provider_cancellation == "request"
    assert decision.pending_tool_calls == "deny"
    assert decision.reason_codes == ("policy::output::deny",)
