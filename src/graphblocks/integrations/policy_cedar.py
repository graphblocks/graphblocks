from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from graphblocks import (
    ContentPart,
    OutputPolicyDecision,
    PolicyDecision,
    PolicyRequest,
    canonical_dumps,
    canonical_loads,
)
from graphblocks.output_policy import (
    VALID_DRAFT_DISPOSITIONS,
    VALID_OUTPUT_DISPOSITIONS,
    VALID_PENDING_TOOL_CALLS_DISPOSITIONS as VALID_PENDING_TOOL_CALLS,
    VALID_PROVIDER_CANCELLATIONS,
)


class CedarPolicyAdapterError(RuntimeError):
    pass


def _cedar_decision(value: object) -> Literal["allow", "deny"]:
    if isinstance(value, str):
        decision = value.casefold()
        if decision == "allow":
            return "allow"
        if decision == "deny":
            return "deny"
    raise CedarPolicyAdapterError(f"unknown Cedar decision {value}")


@dataclass(frozen=True, slots=True)
class CedarAuthorizationRequest:
    authorization_json: str
    schema_ref: str | None = None

    def __post_init__(self) -> None:
        if self.schema_ref is not None:
            if not isinstance(self.schema_ref, str):
                raise CedarPolicyAdapterError("schema_ref must be a string")
            schema_ref = self.schema_ref.strip()
            if not schema_ref:
                raise CedarPolicyAdapterError("schema_ref must not be empty")
            object.__setattr__(self, "schema_ref", schema_ref)

    def authorization_contract(self) -> dict[str, object]:
        try:
            contract = canonical_loads(self.authorization_json)
        except (TypeError, ValueError) as error:
            raise CedarPolicyAdapterError("Cedar authorization input must be valid strict JSON") from error
        if not isinstance(contract, Mapping):
            raise CedarPolicyAdapterError("Cedar authorization input must be a JSON object")
        contract = dict(contract)
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

    raw_decision = _cedar_decision(result.get("decision"))
    if raw_decision == "allow":
        effect = "allow"
    else:
        effect = "deny"

    diagnostics = result.get("diagnostics", {})
    if diagnostics is None:
        diagnostics = {}
    if not isinstance(diagnostics, Mapping):
        raise CedarPolicyAdapterError("Cedar diagnostics must be an object")
    reason_codes = _string_tuple(diagnostics.get("reason", diagnostics.get("reasons", ())))
    policy_refs = _string_tuple(diagnostics.get("policy_refs", diagnostics.get("policyRefs", reason_codes)))
    digested_request = request if request.input_digest else request.with_input_digest()
    valid_until = result.get("valid_until", result.get("validUntil"))
    if valid_until is not None and (not isinstance(valid_until, str) or not valid_until.strip()):
        raise CedarPolicyAdapterError("Cedar policy decision valid_until must be a non-empty string")
    return PolicyDecision(
        decision_id=decision_id,
        effect=effect,
        reason_codes=reason_codes,
        policy_refs=policy_refs,
        evaluated_at=evaluated_at,
        valid_until=valid_until.strip() if isinstance(valid_until, str) else None,
        input_digest=digested_request.input_digest,
    )


def output_policy_decision_from_cedar_result(
    *,
    decision_id: str,
    request: PolicyRequest,
    result: Mapping[str, object],
    evaluated_at: str,
) -> OutputPolicyDecision:
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise CedarPolicyAdapterError("decision_id must be a non-empty string")
    if not isinstance(evaluated_at, str) or not evaluated_at.strip():
        raise CedarPolicyAdapterError("evaluated_at must be a non-empty string")

    result_body = result.get("output_policy", result.get("outputPolicy", result))
    if not isinstance(result_body, Mapping):
        raise CedarPolicyAdapterError("Cedar output policy result must be an object")

    disposition = result_body.get("disposition")
    if disposition is None:
        raw_decision = _cedar_decision(result.get("decision"))
        if raw_decision == "allow":
            disposition = "allow"
        else:
            disposition = "abort_response"
    if not isinstance(disposition, str) or disposition not in VALID_OUTPUT_DISPOSITIONS:
        raise CedarPolicyAdapterError(f"unknown output policy disposition {disposition}")

    diagnostics = result.get("diagnostics", {})
    if diagnostics is None:
        diagnostics = {}
    if not isinstance(diagnostics, Mapping):
        raise CedarPolicyAdapterError("Cedar diagnostics must be an object")
    if "reason_codes" in result_body or "reasonCodes" in result_body:
        raw_reason_codes = result_body.get("reason_codes", result_body.get("reasonCodes"))
    else:
        raw_reason_codes = diagnostics.get("reason", diagnostics.get("reasons", ()))
    reason_codes = _string_tuple(raw_reason_codes)
    if "policy_refs" in result_body or "policyRefs" in result_body:
        raw_policy_refs = result_body.get("policy_refs", result_body.get("policyRefs"))
    else:
        raw_policy_refs = diagnostics.get("policy_refs", diagnostics.get("policyRefs", reason_codes))
    policy_refs = _string_tuple(raw_policy_refs)
    digested_request = request if request.input_digest else request.with_input_digest()
    try:
        return OutputPolicyDecision(
            decision_id=decision_id,
            disposition=disposition,
            accepted_through_sequence=_optional_non_negative_int(
                result_body.get("accepted_through_sequence", result_body.get("acceptedThroughSequence")),
                "accepted_through_sequence",
            ),
            replacement_parts=_content_parts(result_body),
            redactions=_redactions(result_body.get("redactions", ())),
            reason_codes=reason_codes,
            policy_refs=policy_refs,
            provider_cancellation=_optional_literal(
                result_body.get("provider_cancellation", result_body.get("providerCancellation")),
                VALID_PROVIDER_CANCELLATIONS,
                "provider_cancellation",
                "request",
            ),
            draft_disposition=_optional_literal(
                result_body.get("draft_disposition", result_body.get("draftDisposition")),
                VALID_DRAFT_DISPOSITIONS,
                "draft_disposition",
                "retract",
            ),
            pending_tool_calls=_optional_literal(
                result_body.get("pending_tool_calls", result_body.get("pendingToolCalls")),
                VALID_PENDING_TOOL_CALLS,
                "pending_tool_calls",
                "deny",
            ),
            evaluated_at=evaluated_at,
            input_digest=digested_request.input_digest,
        )
    except ValueError as error:
        raise CedarPolicyAdapterError(str(error)) from error


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


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CedarPolicyAdapterError(f"{field_name} must be a non-negative integer")
    return value


def _optional_literal(value: object, valid_values: frozenset[str], field_name: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in valid_values:
        raise CedarPolicyAdapterError(f"invalid {field_name} {value!r}")
    return value


def _content_parts(result_body: Mapping[str, object]) -> tuple[ContentPart, ...]:
    raw_parts = result_body.get("replacement_parts", result_body.get("replacementParts", ()))
    if not isinstance(raw_parts, list | tuple):
        raise CedarPolicyAdapterError("output policy replacement parts must be a sequence")
    parts = [_content_part(raw_part) for raw_part in raw_parts]
    replacement = result_body.get("replacement")
    if not parts and replacement is not None:
        if not isinstance(replacement, str):
            raise CedarPolicyAdapterError("output policy replacement must be a string")
        parts.append(ContentPart(kind="text", text=replacement))
    return tuple(parts)


def _content_part(raw_part: object) -> ContentPart:
    if isinstance(raw_part, str):
        return ContentPart(kind="text", text=raw_part)
    if not isinstance(raw_part, Mapping):
        raise CedarPolicyAdapterError("output policy replacement part must be an object")
    kind = raw_part.get("kind")
    if kind == "text":
        text = raw_part.get("text")
        if not isinstance(text, str):
            raise CedarPolicyAdapterError("text output policy replacement part requires string text")
        return ContentPart(kind="text", text=text)
    if kind in {"json", "artifact_ref"}:
        data = raw_part.get("data")
        if not isinstance(data, Mapping):
            raise CedarPolicyAdapterError(f"{kind} output policy replacement part requires object data")
        return ContentPart(kind=kind, data=dict(data))  # type: ignore[arg-type]
    raise CedarPolicyAdapterError(f"unknown output policy replacement part kind {kind}")


def _redactions(raw_redactions: object) -> tuple[dict[str, object], ...]:
    if not isinstance(raw_redactions, list | tuple):
        raise CedarPolicyAdapterError("output policy redactions must be a sequence")
    redactions = []
    for raw_redaction in raw_redactions:
        if not isinstance(raw_redaction, Mapping):
            raise CedarPolicyAdapterError("output policy redaction must be an object")
        redactions.append(dict(raw_redaction))
    return tuple(redactions)


__all__ = [
    "CedarAuthorizationRequest",
    "CedarPolicyAdapterError",
    "VALID_DRAFT_DISPOSITIONS",
    "VALID_OUTPUT_DISPOSITIONS",
    "VALID_PENDING_TOOL_CALLS",
    "VALID_PROVIDER_CANCELLATIONS",
    "output_policy_decision_from_cedar_result",
    "policy_decision_from_cedar_result",
    "prepare_cedar_authorization_request",
]
