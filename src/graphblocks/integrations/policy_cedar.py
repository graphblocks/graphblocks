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


def _exact_non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CedarPolicyAdapterError(
            f"{field_name} must be a non-empty string"
        )
    if value != value.strip():
        raise CedarPolicyAdapterError(
            f"{field_name} must not contain surrounding whitespace"
        )
    return value


def _alias_value(
    value: Mapping[str, object],
    snake_case: str,
    camel_case: str,
    default: object,
) -> object:
    if snake_case in value and camel_case in value:
        raise CedarPolicyAdapterError(
            f"Cedar result must not contain both {snake_case} and {camel_case}"
        )
    if snake_case in value:
        return value[snake_case]
    if camel_case in value:
        return value[camel_case]
    return default


def _strict_result(
    result: object,
    *,
    contract_name: str,
) -> Mapping[str, object]:
    if not isinstance(result, Mapping):
        raise CedarPolicyAdapterError(f"{contract_name} must be an object")
    materialized = dict(result)
    try:
        canonical_dumps(materialized)
    except (TypeError, ValueError) as error:
        raise CedarPolicyAdapterError(
            f"{contract_name} must be valid strict JSON"
        ) from error
    return materialized


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
        if not isinstance(self.authorization_json, str):
            raise CedarPolicyAdapterError(
                "Cedar authorization input must be valid strict JSON"
            )
        try:
            contract = canonical_loads(self.authorization_json)
        except (TypeError, ValueError) as error:
            raise CedarPolicyAdapterError(
                "Cedar authorization input must be valid strict JSON"
            ) from error
        if not isinstance(contract, Mapping):
            raise CedarPolicyAdapterError(
                "Cedar authorization input must be a JSON object"
            )
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
    if not isinstance(request, PolicyRequest):
        raise CedarPolicyAdapterError("request must be a PolicyRequest")
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
    decision_id = _exact_non_empty("decision_id", decision_id)
    evaluated_at = _exact_non_empty("evaluated_at", evaluated_at)
    if not isinstance(request, PolicyRequest):
        raise CedarPolicyAdapterError("request must be a PolicyRequest")
    result = _strict_result(result, contract_name="Cedar result")

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
    reason_codes = _string_tuple(
        _alias_value(diagnostics, "reason", "reasons", ())
    )
    policy_refs = _string_tuple(
        _alias_value(diagnostics, "policy_refs", "policyRefs", reason_codes)
    )
    digested_request = request if request.input_digest else request.with_input_digest()
    valid_until = _alias_value(result, "valid_until", "validUntil", None)
    if valid_until is not None:
        valid_until = _exact_non_empty(
            "Cedar policy decision valid_until",
            valid_until,
        )
    return PolicyDecision(
        decision_id=decision_id,
        effect=effect,
        reason_codes=reason_codes,
        policy_refs=policy_refs,
        evaluated_at=evaluated_at,
        valid_until=valid_until,  # type: ignore[arg-type]
        input_digest=digested_request.input_digest,
    )


def output_policy_decision_from_cedar_result(
    *,
    decision_id: str,
    request: PolicyRequest,
    result: Mapping[str, object],
    evaluated_at: str,
) -> OutputPolicyDecision:
    decision_id = _exact_non_empty("decision_id", decision_id)
    evaluated_at = _exact_non_empty("evaluated_at", evaluated_at)
    if not isinstance(request, PolicyRequest):
        raise CedarPolicyAdapterError("request must be a PolicyRequest")
    result = _strict_result(
        result,
        contract_name="Cedar output policy result",
    )

    result_body = _alias_value(
        result,
        "output_policy",
        "outputPolicy",
        result,
    )
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
        raw_reason_codes = _alias_value(
            result_body,
            "reason_codes",
            "reasonCodes",
            (),
        )
    else:
        raw_reason_codes = _alias_value(
            diagnostics,
            "reason",
            "reasons",
            (),
        )
    reason_codes = _string_tuple(raw_reason_codes)
    if "policy_refs" in result_body or "policyRefs" in result_body:
        raw_policy_refs = _alias_value(
            result_body,
            "policy_refs",
            "policyRefs",
            reason_codes,
        )
    else:
        raw_policy_refs = _alias_value(
            diagnostics,
            "policy_refs",
            "policyRefs",
            reason_codes,
        )
    policy_refs = _string_tuple(raw_policy_refs)
    digested_request = request if request.input_digest else request.with_input_digest()
    try:
        return OutputPolicyDecision(
            decision_id=decision_id,
            disposition=disposition,
            accepted_through_sequence=_optional_non_negative_int(
                _alias_value(
                    result_body,
                    "accepted_through_sequence",
                    "acceptedThroughSequence",
                    None,
                ),
                "accepted_through_sequence",
            ),
            replacement_parts=_content_parts(result_body),
            redactions=_redactions(result_body.get("redactions", ())),
            reason_codes=reason_codes,
            policy_refs=policy_refs,
            provider_cancellation=_optional_literal(
                _alias_value(
                    result_body,
                    "provider_cancellation",
                    "providerCancellation",
                    None,
                ),
                VALID_PROVIDER_CANCELLATIONS,
                "provider_cancellation",
                "request",
            ),
            draft_disposition=_optional_literal(
                _alias_value(
                    result_body,
                    "draft_disposition",
                    "draftDisposition",
                    None,
                ),
                VALID_DRAFT_DISPOSITIONS,
                "draft_disposition",
                "retract",
            ),
            pending_tool_calls=_optional_literal(
                _alias_value(
                    result_body,
                    "pending_tool_calls",
                    "pendingToolCalls",
                    None,
                ),
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
        if value != value.strip():
            raise CedarPolicyAdapterError(
                "policy result string collection contains surrounding whitespace"
            )
        return (value,)
    if not isinstance(value, list | tuple):
        raise CedarPolicyAdapterError("policy result string collection must be a sequence")
    items = []
    for item in value:
        if not isinstance(item, str):
            raise CedarPolicyAdapterError("policy result string collection contains a non-string item")
        if not item.strip():
            raise CedarPolicyAdapterError("policy result string collection contains a blank string")
        if item != item.strip():
            raise CedarPolicyAdapterError(
                "policy result string collection contains surrounding whitespace"
            )
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
    raw_parts = _alias_value(
        result_body,
        "replacement_parts",
        "replacementParts",
        (),
    )
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
