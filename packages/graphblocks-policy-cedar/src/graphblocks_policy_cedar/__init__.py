from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json

from graphblocks import PolicyDecision, PolicyRequest, canonical_dumps


class CedarPolicyAdapterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CedarAuthorizationRequest:
    authorization_json: str
    schema_ref: str | None = None

    def authorization_contract(self) -> dict[str, object]:
        contract = json.loads(self.authorization_json)
        contract["schema_ref"] = self.schema_ref
        return contract


def prepare_cedar_authorization_request(
    request: PolicyRequest,
    *,
    schema_ref: str | None = None,
) -> CedarAuthorizationRequest:
    if request.principal is None:
        raise CedarPolicyAdapterError("Cedar authorization requires a principal")

    digested_request = request if request.input_digest else request.with_input_digest()
    atomic_unit = None
    if digested_request.atomic_unit is not None:
        atomic_unit = {
            "entity_type": "Resource",
            "entity_id": digested_request.atomic_unit.resource_id,
            "resource_kind": digested_request.atomic_unit.resource_kind,
            "tenant_id": digested_request.atomic_unit.tenant_id,
            "attributes": dict(digested_request.atomic_unit.attributes),
        }
    authorization = {
        "principal": {
            "entity_type": "Principal",
            "entity_id": request.principal.principal_id,
            "tenant_id": request.principal.tenant_id,
            "groups": list(request.principal.groups),
            "roles": list(request.principal.roles),
            "attributes": dict(request.principal.attributes),
        },
        "action": request.action,
        "resource": {
            "entity_type": "Resource",
            "entity_id": request.resource.resource_id,
            "resource_kind": request.resource.resource_kind,
            "tenant_id": request.resource.tenant_id,
            "attributes": dict(request.resource.attributes),
        },
        "context": {
            "request_id": request.request_id,
            "enforcement_point": request.enforcement_point,
            "occurred_at": digested_request.occurred_at,
            "release_id": digested_request.release_id,
            "deployment_revision_id": digested_request.deployment_revision_id,
            "run_id": digested_request.run_id,
            "atomic_unit": atomic_unit,
            "data_labels": list(request.data_labels),
            "requested_usage": [dict(usage) for usage in digested_request.requested_usage],
            "attributes": dict(request.attributes),
            "policy_snapshot_id": request.policy_snapshot_id,
            "input_digest": digested_request.input_digest,
        },
    }
    try:
        authorization_json = canonical_dumps(authorization)
    except (TypeError, ValueError) as error:
        raise CedarPolicyAdapterError("Cedar authorization input must be canonical JSON") from error
    return CedarAuthorizationRequest(authorization_json=authorization_json, schema_ref=schema_ref)


def policy_decision_from_cedar_result(
    *,
    decision_id: str,
    request: PolicyRequest,
    result: Mapping[str, object],
    evaluated_at: str,
) -> PolicyDecision:
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise CedarPolicyAdapterError("decision_id must be a non-empty string")
    if not isinstance(evaluated_at, str) or not evaluated_at.strip():
        raise CedarPolicyAdapterError("evaluated_at must be a non-empty string")

    raw_decision = result.get("decision")
    if raw_decision == "allow":
        effect = "allow"
    elif raw_decision == "deny":
        effect = "deny"
    else:
        raise CedarPolicyAdapterError(f"unknown Cedar decision {raw_decision}")

    diagnostics = result.get("diagnostics", {})
    if diagnostics is None:
        diagnostics = {}
    if not isinstance(diagnostics, Mapping):
        raise CedarPolicyAdapterError("Cedar diagnostics must be an object")
    reason_codes = _string_tuple(diagnostics.get("reason", diagnostics.get("reasons", ())))
    policy_refs = _string_tuple(diagnostics.get("policy_refs", diagnostics.get("policyRefs", reason_codes)))
    digested_request = request if request.input_digest else request.with_input_digest()
    return PolicyDecision(
        decision_id=decision_id,
        effect=effect,
        reason_codes=reason_codes,
        policy_refs=policy_refs,
        evaluated_at=evaluated_at,
        input_digest=digested_request.input_digest,
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        if not value.strip():
            raise CedarPolicyAdapterError("policy result string collection contains a blank string")
        return (value,)
    if not isinstance(value, list | tuple):
        raise CedarPolicyAdapterError("policy result string collection must be a sequence")
    items = []
    for item in value:
        if not isinstance(item, str):
            raise CedarPolicyAdapterError("policy result string collection contains a non-string item")
        if not item.strip():
            raise CedarPolicyAdapterError("policy result string collection contains a blank string")
        items.append(item)
    return tuple(items)


__all__ = [
    "CedarAuthorizationRequest",
    "CedarPolicyAdapterError",
    "policy_decision_from_cedar_result",
    "prepare_cedar_authorization_request",
]
